"""Observe command: process subagent observations.

Reads observation files, validates/coerces, detects contradictions,
updates profile/state/cursor, runs invariant checks, appends trace.

Per 04-python-driver.md Section 2 (observe) and Section 6 (invariants).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from pymh.schemas.defaults import (
    COMP_FULL,
    COMP_NONE,
    COMP_PARTIAL,
    DEFAULT_CONDITIONS,
    DEFAULT_SURPRISE,
    PHASE_EXEC,
)
from pymh.state import (
    append_trace,
    now_iso,
    read_cursor,
    read_phase,
    read_profile,
    read_state,
    write_cursor,
    write_profile,
    write_state,
)
from pymh.workspace import load_config

# System condition names that subagents cannot shadow (decision #55)
RESERVED_CONDITION_NAMES = frozenset({
    "fuel_remaining",
    "task_attempts",
    "consecutive_failures",
    "total_attempts",
    "step",
    "surprise_accumulator",
})

# Core condition fields and their expected types (decision #53)
BOOL_FIELDS = ("needs_replan", "escalate")
NULLABLE_FIELDS = ("blocker",)
NUMERIC_FIELDS = ("quality_score",)
STRING_FIELDS = ("completeness", "confidence")


# --- Public API ---


def process_observation(
    workspace: Path,
    node_id: str,
    attempt: int,
) -> dict[str, Any]:
    """Process a single-node observation.

    Returns a result dict with keys: observation, state, cursor, warnings, invariant_fired.
    """
    obs_path = workspace / "dataflow" / "observations" / f"{node_id}-{attempt}.json"
    warnings: list[str] = []

    # Warn if node_id/attempt don't match cursor (defensive check)
    cursor_check = read_cursor(workspace)
    if cursor_check.get("current_task") and cursor_check["current_task"] != node_id:
        msg = (
            f"observe node_id '{node_id}' != cursor current_task "
            f"'{cursor_check['current_task']}'"
        )
        print(f"WARN:{msg}", file=sys.stderr)
        warnings.append(msg)

    if obs_path.exists():
        try:
            with open(obs_path) as f:
                observation = json.load(f)
            if not isinstance(observation, dict):
                observation = _synthesize_failure("observation is not a JSON object")
                warnings.append("observation is not a JSON object, synthesized failure")
        except (json.JSONDecodeError, OSError) as e:
            observation = _synthesize_failure(f"failed to read observation: {e}")
            warnings.append(f"observation parse error, synthesized failure: {e}")
    else:
        observation = _synthesize_failure("observation file missing")
        warnings.append(f"observation file missing: {obs_path.name}")

    # Validate, coerce, strip, detect contradictions
    observation, val_warnings = _validate_observation(observation)
    warnings.extend(val_warnings)

    # Write canonical observation back so decide reads normalized values
    with open(obs_path, "w") as f:
        json.dump(observation, f, indent=2)
        f.write("\n")

    # Update state
    state = _update_state(workspace, observation)

    # Merge profile
    _merge_profile(workspace, observation)

    # Update cursor — only mark completed on success (decision #58d)
    cursor = read_cursor(workspace)
    if observation.get("success") and node_id not in cursor.get("completed_tasks", []):
        cursor["completed_tasks"].append(node_id)
    write_cursor(workspace, cursor)

    # Invariant checks
    invariant_fired = _run_invariants(workspace, cursor, state, node_id, attempt)

    # Trace
    phase = read_phase(workspace)
    trace_entry = {
        "timestamp": now_iso(),
        "step": state["step"],
        "phase": phase.get("phase", PHASE_EXEC),
        "task_id": node_id,
        "attempt": attempt,
        "action": "observe",
        "observation_summary": observation.get("signal", "n/a"),
        "conditions": observation.get("conditions", {}),
        "surprise": observation.get("surprise", DEFAULT_SURPRISE),
        "fuel_remaining": state["fuel_remaining"],
        "validation_warnings": warnings,
    }
    if invariant_fired:
        trace_entry["invariant_fired"] = invariant_fired
    append_trace(workspace, trace_entry)

    return {
        "observation": observation,
        "state": state,
        "cursor": read_cursor(workspace),
        "warnings": warnings,
        "invariant_fired": invariant_fired,
    }


def process_parallel_observations(
    workspace: Path,
    node_ids: list[str],
) -> dict[str, Any]:
    """Process parallel group observations by merging then processing.

    Reads each node's latest-attempt observation, merges, processes as one.
    """
    obs_dir = workspace / "dataflow" / "observations"
    raw_observations: list[tuple[str, dict[str, Any]]] = []

    # Warn if node_ids don't match cursor pending_parallel
    cursor_check = read_cursor(workspace)
    pending = cursor_check.get("pending_parallel", [])
    if pending and set(node_ids) != set(pending):
        msg = (
            f"parallel node_ids {sorted(node_ids)} != "
            f"cursor pending_parallel {sorted(pending)}"
        )
        print(f"WARN:{msg}", file=sys.stderr)

    for nid in sorted(node_ids):
        matching = sorted(obs_dir.glob(f"{nid}-*.json"), key=extract_attempt_num)
        if matching:
            try:
                with open(matching[-1]) as f:
                    obs = json.load(f)
            except (json.JSONDecodeError, OSError):
                obs = _synthesize_failure(f"parse error for {nid}")
        else:
            obs = _synthesize_failure(f"observation missing for {nid}")
        # Validate each observation before merge so merge operates on clean types
        obs, _val_warnings = _validate_observation(obs)
        raw_observations.append((nid, obs))

    merged = _merge_parallel(raw_observations)

    # Write merged observation so the rest of the pipeline can use it
    group_id = "_".join(sorted(node_ids))
    merged_path = obs_dir / f"{group_id}-merged.json"
    with open(merged_path, "w") as f:
        json.dump(merged, f, indent=2)
        f.write("\n")

    # Validate merged result
    merged, warnings = _validate_observation(merged)

    # Write canonical merged observation back
    with open(merged_path, "w") as f:
        json.dump(merged, f, indent=2)
        f.write("\n")

    # Update state
    state = _update_state(workspace, merged)

    # Merge profile
    _merge_profile(workspace, merged)

    # Update cursor — mark successful parallel members as completed (decision #58d)
    cursor = read_cursor(workspace)
    for nid, obs in raw_observations:
        if obs.get("success") and nid not in cursor.get("completed_tasks", []):
            cursor["completed_tasks"].append(nid)
    write_cursor(workspace, cursor)

    # Invariant checks
    invariant_fired = _run_invariants(workspace, cursor, state, group_id, 0)

    # Trace
    phase = read_phase(workspace)
    append_trace(workspace, {
        "timestamp": now_iso(),
        "step": state["step"],
        "phase": phase.get("phase", PHASE_EXEC),
        "task_id": group_id,
        "attempt": 0,
        "action": "observe_parallel",
        "observation_summary": merged.get("signal", "n/a"),
        "conditions": merged.get("conditions", {}),
        "surprise": merged.get("surprise", DEFAULT_SURPRISE),
        "fuel_remaining": state["fuel_remaining"],
        "validation_warnings": warnings,
    })

    return {
        "observation": merged,
        "state": state,
        "cursor": read_cursor(workspace),
        "warnings": warnings,
        "invariant_fired": invariant_fired,
    }


def format_progress_line(
    state: dict[str, Any],
    phase: dict[str, Any],
    node_id: str,
    observation: dict[str, Any],
) -> str:
    """Format the human-readable progress line for stdout."""
    step = state["step"]
    total = step + state["fuel_remaining"]
    p = phase.get("phase", PHASE_EXEC)
    completeness = observation.get("conditions", {}).get("completeness", "unknown")
    surprise = observation.get("surprise", 0.0)
    return (
        f"[Step {step}/{total}] {p}:{node_id}  "
        f"completeness={completeness} | surprise={surprise:.1f}"
    )


# --- Internal helpers ---


def extract_attempt_num(path: Path) -> int:
    """Extract attempt number from observation filename like 'node-3.json'."""
    m = re.search(r"-(\d+)\.json$", path.name)
    return int(m.group(1)) if m else -1


def _synthesize_failure(reason: str) -> dict[str, Any]:
    """Create a synthetic failure observation when the real one is missing/broken."""
    return {
        "success": False,
        "signal": reason,
        "conditions": {
            "quality_score": 0,
            "completeness": COMP_NONE,
            "blocker": "observation_missing",
            "confidence": "low",
            "needs_replan": False,
            "escalate": False,
        },
        "evidence": {},
        "surprise": DEFAULT_SURPRISE,
        "narrative": reason,
    }


def _validate_observation(
    obs: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Validate and default-fill an observation. Returns (obs, warnings)."""
    warnings: list[str] = []

    # success
    if "success" not in obs or not isinstance(obs["success"], bool):
        obs["success"] = False
        warnings.append("success field missing or non-bool, defaulted to false")

    # signal
    if "signal" not in obs or not isinstance(obs["signal"], str):
        obs["signal"] = "no signal"
        warnings.append("signal field missing, defaulted")

    # surprise
    if "surprise" not in obs or not isinstance(obs.get("surprise"), (int, float)):
        obs["surprise"] = DEFAULT_SURPRISE
        warnings.append(f"surprise field invalid, defaulted to {DEFAULT_SURPRISE}")
    else:
        obs["surprise"] = float(obs["surprise"])

    # conditions block
    if "conditions" not in obs or not isinstance(obs.get("conditions"), dict):
        obs["conditions"] = {**DEFAULT_CONDITIONS}
        warnings.append("conditions block missing, filled with defaults")
    else:
        for key, default_val in DEFAULT_CONDITIONS.items():
            if key not in obs["conditions"]:
                obs["conditions"][key] = default_val
                warnings.append(f"conditions.{key} missing, defaulted to {default_val}")

    # Type coercion for core condition fields (decision #53)
    for field in BOOL_FIELDS:
        val = obs["conditions"].get(field)
        if isinstance(val, str):
            lower = val.lower()
            if lower in ("true", "t", "yes", "1"):
                obs["conditions"][field] = True
                warnings.append(f"coerced conditions.{field} from string '{val}' to true")
            elif lower in ("false", "f", "no", "0"):
                obs["conditions"][field] = False
                warnings.append(f"coerced conditions.{field} from string '{val}' to false")

    for field in NULLABLE_FIELDS:
        val = obs["conditions"].get(field)
        if isinstance(val, str) and val.lower() in ("null", "none"):
            obs["conditions"][field] = None
            warnings.append(f"coerced conditions.{field} from string '{val}' to null")

    for field in NUMERIC_FIELDS:
        val = obs["conditions"].get(field)
        if isinstance(val, str):
            try:
                obs["conditions"][field] = int(val)
                warnings.append(f"coerced conditions.{field} from string '{val}' to int")
            except ValueError:
                obs["conditions"][field] = DEFAULT_CONDITIONS[field]
                warnings.append(f"conditions.{field} non-numeric string, defaulted")

    # Success/completeness reconciliation — unify the two truth sources
    if obs["success"] and obs["conditions"].get("completeness") != COMP_FULL:
        obs["conditions"]["completeness"] = COMP_FULL
        warnings.append("reconciled: success=true but completeness!=full, set completeness=full")
    elif not obs["success"] and obs["conditions"].get("completeness") == COMP_FULL:
        obs["conditions"]["completeness"] = COMP_PARTIAL
        if obs["surprise"] < 0.7:
            obs["surprise"] = 0.7
        warnings.append(
            "reconciled: success=false but completeness=full,"
            " demoted completeness to partial"
        )

    # Namespace collision stripping (decision #55)
    for reserved in RESERVED_CONDITION_NAMES:
        if reserved in obs["conditions"]:
            del obs["conditions"][reserved]
            warnings.append(f"stripped reserved name '{reserved}' from conditions")

    # Evidence-conditions contradiction detection (decision #40)
    evidence = obs.get("evidence", {})
    conditions = obs["conditions"]
    contradictions: list[str] = []

    if evidence.get("tests_passing") is False and conditions.get("completeness") == COMP_FULL:
        contradictions.append("completeness=full but tests_passing=false")

    if evidence.get("build_success") is False and (conditions.get("quality_score") or 0) >= 80:
        qs = conditions.get('quality_score')
        contradictions.append(f"quality_score={qs} but build_success=false")

    if evidence.get("artifact_exists") is False and obs.get("success") is True:
        contradictions.append("success=true but artifact_exists=false")

    exit_codes = evidence.get("command_exit_codes", [])
    has_nonzero = isinstance(exit_codes, list) and any(c != 0 for c in exit_codes)
    if has_nonzero and conditions.get("confidence") == "high":
        contradictions.append("confidence=high but command_exit_codes contains non-zero")

    if contradictions:
        if obs["surprise"] < 0.7:
            obs["surprise"] = 0.7
        warnings.append(f"evidence-conditions contradiction: {'; '.join(contradictions)}")

    return obs, warnings


def _update_state(workspace: Path, observation: dict[str, Any]) -> dict[str, Any]:
    """Update state.json with observation results."""
    state = read_state(workspace)

    state["step"] += 1
    state["fuel_remaining"] -= 1
    state["total_attempts"] += 1

    if observation.get("success"):
        state["consecutive_failures"] = 0
        state["surprise_accumulator"] = 0.0
    else:
        state["consecutive_failures"] += 1

    # Σ(surprise²) — decision #38
    surprise = observation.get("surprise", DEFAULT_SURPRISE)
    state["surprise_accumulator"] += surprise ** 2

    write_state(workspace, state)
    return state


def _merge_profile(workspace: Path, observation: dict[str, Any]) -> None:
    """Shallow-merge profile_updates into profile.json."""
    updates = observation.get("profile_updates")
    if not updates or not isinstance(updates, dict):
        return

    profile = read_profile(workspace)
    for key, value in updates.items():
        profile[key] = value
    write_profile(workspace, profile)


def _run_invariants(
    workspace: Path,
    cursor: dict[str, Any],
    state: dict[str, Any],
    node_id: str,
    attempt: int,
) -> str | None:
    """Run invariant checks. Returns description if an invariant fired, else None."""
    defaults = load_config()["defaults"]

    max_task_attempts = defaults["max_task_attempts"]
    max_consecutive_failures = defaults["max_consecutive_failures"]
    drift_threshold = defaults["drift_threshold"]

    # Re-read cursor in case it was updated
    cursor = read_cursor(workspace)

    # 1. Loop detection
    if cursor.get("task_attempts", 0) >= max_task_attempts:
        reason = f"loop detected: {node_id} attempted {cursor['task_attempts']} times"
        cursor["forced_transition"] = {"type": "escalate", "reason": reason}
        write_cursor(workspace, cursor)
        return reason

    # 2. Drift check — consecutive failures
    if state["consecutive_failures"] >= max_consecutive_failures:
        reason = f"drift detected: {state['consecutive_failures']} consecutive failures"
        cursor["forced_transition"] = {"type": "replan", "reason": reason}
        write_cursor(workspace, cursor)
        return reason

    # 2b. Drift check — surprise accumulator
    if state["surprise_accumulator"] > drift_threshold:
        reason = (
            f"drift detected: surprise accumulator {state['surprise_accumulator']:.2f} "
            f"exceeds threshold {drift_threshold}"
        )
        cursor["forced_transition"] = {"type": "replan", "reason": reason}
        write_cursor(workspace, cursor)
        return reason

    # 3. Fuel management
    if state["fuel_remaining"] <= 0:
        reason = "fuel_exhausted"
        cursor["forced_transition"] = {"type": "verify_or_abort", "reason": reason}
        write_cursor(workspace, cursor)
        return reason

    return None


def _merge_parallel(
    observations: list[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    """Merge parallel group observations per 06-concurrency rules.

    Decisions #56 (quality_score=MAX), #57 (evidence keyed by node-id).
    """
    if not observations:
        return _synthesize_failure("no observations to merge")

    merged: dict[str, Any] = {
        "success": all(obs.get("success", False) for _, obs in observations),
        "signal": "; ".join(obs.get("signal", "") for _, obs in observations),
        "conditions": {},
        "evidence": {},
        "tags": {},
        "surprise": 0.0,
        "profile_updates": {},
        "files_changed": [],
        "narrative": "\n\n".join(
            obs.get("narrative", "") for _, obs in observations if obs.get("narrative")
        ),
    }

    # quality_score: MAX
    scores = [obs.get("conditions", {}).get("quality_score", 50) for _, obs in observations]
    merged["conditions"]["quality_score"] = max(scores)

    # completeness: ALL-or-nothing
    all_full = all(
        obs.get("conditions", {}).get("completeness") == COMP_FULL for _, obs in observations
    )
    merged["conditions"]["completeness"] = COMP_FULL if all_full else COMP_PARTIAL

    # blocker: join non-null with ";"
    blockers = [
        obs.get("conditions", {}).get("blocker")
        for _, obs in observations
        if obs.get("conditions", {}).get("blocker") is not None
    ]
    merged["conditions"]["blocker"] = "; ".join(str(b) for b in blockers) if blockers else None

    # confidence: worst case
    conf_order = {"low": 0, "medium": 1, "high": 2}
    confs = [obs.get("conditions", {}).get("confidence", "low") for _, obs in observations]
    merged["conditions"]["confidence"] = min(confs, key=lambda c: conf_order.get(c, 0))

    # needs_replan / escalate: any
    merged["conditions"]["needs_replan"] = any(
        obs.get("conditions", {}).get("needs_replan", False) for _, obs in observations
    )
    merged["conditions"]["escalate"] = any(
        obs.get("conditions", {}).get("escalate", False) for _, obs in observations
    )

    # evidence: keyed by node-id (decision #57)
    for nid, obs in observations:
        if obs.get("evidence"):
            merged["evidence"][nid] = obs["evidence"]

    # tags: union
    for _, obs in observations:
        merged["tags"].update(obs.get("tags", {}))

    # surprise: MAX
    merged["surprise"] = max(obs.get("surprise", DEFAULT_SURPRISE) for _, obs in observations)

    # profile_updates: last-write-wins in sorted order (already sorted)
    for _, obs in observations:
        merged["profile_updates"].update(obs.get("profile_updates", {}))

    # files_changed: deduplicated union (preserving order)
    seen_files: dict[str, None] = {}
    for _, obs in observations:
        for f in obs.get("files_changed", []):
            seen_files[f] = None
    merged["files_changed"] = list(seen_files)

    return merged
