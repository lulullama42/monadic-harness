"""Decide command: determine next dispatch instruction.

Implements the state machine runtime per 04-python-driver.md Section 5.
Handles plan/verify/exec phases, forced transitions, wait nodes,
condition evaluation, and instruction file generation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml

from pymh.conditions import ConditionParseError, evaluate
from pymh.schemas.defaults import (
    COMP_FULL,
    PHASE_EXEC,
    PHASE_PLAN,
    PHASE_VERIFY,
)
from pymh.state import (
    append_trace,
    now_iso,
    read_cursor,
    read_meta,
    read_phase,
    read_profile,
    read_state,
    read_trace,
    update_history_status,
    write_cursor,
    write_meta,
    write_phase,
    write_state,
)

# --- Public API ---


class DecideResult:
    """Result of a decide call."""

    def __init__(self, output: str, instruction_paths: list[str] | None = None) -> None:
        self.output = output
        self.instruction_paths = instruction_paths or []


def decide(workspace: Path, phase_override: str | None = None) -> DecideResult:
    """Determine next dispatch instruction.

    Args:
        workspace: Path to task workspace.
        phase_override: If set, forces plan or verify phase.

    Returns:
        DecideResult with the stdout output line and any instruction paths.
    """
    phase = read_phase(workspace)
    current_phase = phase_override if phase_override else phase.get("phase", PHASE_EXEC)

    # Phase-specific dispatch
    if current_phase == PHASE_PLAN:
        return _decide_plan(workspace, phase)
    elif current_phase == PHASE_VERIFY:
        return _decide_verify(workspace, phase)
    else:
        return _decide_exec(workspace, phase)


# --- Phase handlers ---


def _decide_plan(workspace: Path, phase: dict[str, Any]) -> DecideResult:
    """Generate plan subagent instruction."""
    meta = read_meta(workspace)
    profile = read_profile(workspace)
    replan_count = phase.get("replan_count", 0)

    # Build instruction
    lines = ["# Plan Task", "", "## Goal", meta.get("goal", "unknown"), ""]

    lines.extend(["## Profile"])
    if profile:
        for k, v in profile.items():
            lines.append(f"- {k}: {v}")
    else:
        lines.append("(empty)")
    lines.append("")

    # Template
    from pymh.workspace import get_mh_root

    template_name = meta.get("template", "general")
    template_path = get_mh_root() / "templates" / f"{template_name}.yaml"
    if template_path.exists():
        lines.extend(["## Template", "```yaml"])
        lines.append(template_path.read_text().strip())
        lines.extend(["```", ""])

    # Previous failure context
    failure_path = workspace / "ctrlflow" / "plans" / "failure_summary.json"
    if failure_path.exists():
        lines.extend(["## Previous Failure"])
        with open(failure_path) as f:
            failure = json.load(f)
        lines.append("```json")
        lines.append(json.dumps(failure, indent=2))
        lines.append("```")
        lines.append("")

    lines.extend([
        "## Output",
        "Write your plan to: ctrlflow/plans/current.yaml",
        "Follow the plan format: each step needs id, action, success_criteria, retry_strategy.",
    ])

    instruction = "\n".join(lines) + "\n"
    inst_path = workspace / "dataflow" / "instructions" / f"plan-{replan_count}.md"
    inst_path.parent.mkdir(parents=True, exist_ok=True)
    inst_path.write_text(instruction)

    output = f"DISPATCH:plan:{replan_count}:{inst_path}"
    return DecideResult(output, [str(inst_path)])


def _decide_verify(workspace: Path, phase: dict[str, Any]) -> DecideResult:
    """Generate verify subagent instruction, or process verify observation."""
    obs_dir = workspace / "dataflow" / "observations"
    existing_verify = sorted(obs_dir.glob("verify-*.json")) if obs_dir.exists() else []

    # Check if a verify observation already exists (re-entry after observe)
    if existing_verify:
        return _process_verify_result(workspace, existing_verify[-1])

    # No observation yet — generate verify instruction
    meta = read_meta(workspace)
    cursor = read_cursor(workspace)

    artifacts_dir = workspace / "dataflow" / "artifacts"
    artifacts = []
    if artifacts_dir.exists():
        artifacts = [f.name for f in artifacts_dir.iterdir() if f.is_file()]

    completed = cursor.get("completed_tasks", [])
    attempt = 0

    lines = [
        "# Verify Task",
        "",
        "## Goal",
        meta.get("goal", "unknown"),
        "",
        "## Artifacts",
    ]
    if artifacts:
        for a in artifacts:
            lines.append(f"- {a}")
    else:
        lines.append("(none)")
    lines.append("")

    lines.append("## Completed Tasks")
    if completed:
        for t in completed:
            lines.append(f"- {t}")
    else:
        lines.append("(none)")
    lines.append("")

    lines.extend([
        "## Output",
        f"Write your observation to: dataflow/observations/verify-{attempt}.json",
        "Use standard observation schema: success (bool), signal, conditions, "
        "evidence, surprise, narrative.",
        "success=true means the goal is met.",
    ])

    instruction = "\n".join(lines) + "\n"
    inst_path = workspace / "dataflow" / "instructions" / f"verify-{attempt}.md"
    inst_path.parent.mkdir(parents=True, exist_ok=True)
    inst_path.write_text(instruction)

    output = f"DISPATCH:verify:{attempt}:{inst_path}"
    return DecideResult(output, [str(inst_path)])


def _process_verify_result(workspace: Path, obs_path: Path) -> DecideResult:
    """Process verify observation and branch: done / replan / abort."""
    obs = _read_observation(obs_path)
    conditions = obs.get("conditions", {})

    if obs.get("success"):
        _finalize_done(workspace)
        return DecideResult("DONE")

    if conditions.get("needs_replan"):
        _enter_replan(workspace)
        return DecideResult("REPLAN")

    if conditions.get("escalate"):
        cursor = read_cursor(workspace)
        write_escalation(workspace, cursor, "verify escalation")
        return DecideResult("ESCALATE:verify escalation")

    # Verification failed, no replan requested → abort
    _finalize_abort(workspace, source="verify_failed")
    return DecideResult("ABORT")


def _decide_exec(workspace: Path, phase: dict[str, Any]) -> DecideResult:
    """Main exec phase state machine runtime."""
    cursor = read_cursor(workspace)

    # 1. Check forced transition
    forced = cursor.get("forced_transition")
    if forced and isinstance(forced, dict):
        return _apply_forced_transition(workspace, cursor, forced)

    current_task = cursor.get("current_task")
    if not current_task:
        _finalize_done(workspace)
        return DecideResult("DONE")

    # 2. Load task graph
    graph = load_task_graph(workspace)
    node = find_node(graph, current_task)
    if node is None:
        return DecideResult(f"ESCALATE:unknown node {current_task}")

    # 3. Check wait node
    if "wait_for" in node:
        return _handle_wait_node(workspace, cursor, node, graph)

    # 4. Check for existing observation (first call = no observation → dispatch)
    attempt = cursor.get("task_attempts", 0)
    obs_path = workspace / "dataflow" / "observations" / f"{current_task}-{attempt}.json"
    if not obs_path.exists():
        return dispatch_instruction(workspace, node, current_task, attempt)

    # 5. Build condition space
    observation = _read_observation(obs_path)
    state = read_state(workspace)
    condition_space = _build_condition_space(observation, state, cursor)

    # 6. Evaluate on_complete rules (first match wins)
    on_complete = node.get("on_complete", [])
    for rule in on_complete:
        condition_str = rule.get("condition", "default")
        try:
            if evaluate(condition_str, condition_space):
                goto_target = rule.get("goto")
                return _apply_transition(workspace, cursor, goto_target, graph)
        except ConditionParseError:
            # Malformed condition — log and skip this rule
            print(f"WARN:condition_parse_error:{condition_str}", file=sys.stderr)
            append_trace(workspace, {
                "timestamp": now_iso(),
                "action": "condition_error",
                "step": state.get("step", 0),
                "task_id": current_task,
                "condition": condition_str,
                "error_type": "parse",
            })
            continue

    # No rule matched (should not happen due to compiler-injected default)
    return DecideResult("ESCALATE:no matching condition rule")


# --- Transition handlers ---


def _apply_forced_transition(
    workspace: Path, cursor: dict[str, Any], forced: dict[str, Any]
) -> DecideResult:
    """Apply a forced transition from invariant checks."""
    transition_type = forced.get("type", "escalate")
    reason = forced.get("reason", "unknown")

    # Clear forced_transition
    cursor["forced_transition"] = None
    write_cursor(workspace, cursor)

    if transition_type == "escalate":
        write_escalation(workspace, cursor, reason)
        return DecideResult(f"ESCALATE:{reason}")

    elif transition_type == "replan":
        generate_failure_summary(workspace)
        _enter_replan(workspace)
        return DecideResult("REPLAN")

    elif transition_type == "verify_or_abort":
        completed = cursor.get("completed_tasks", [])
        if completed:
            # Has artifacts → verify
            phase = read_phase(workspace)
            phase["phase"] = PHASE_VERIFY
            phase["phase_entered_at"] = now_iso()
            write_phase(workspace, phase)
            return _decide_verify(workspace, phase)
        else:
            # No artifacts → abort (not done — nothing was accomplished)
            _finalize_abort(workspace)
            return DecideResult("ABORT")

    return DecideResult(f"ESCALATE:{reason}")


def _handle_wait_node(
    workspace: Path, cursor: dict[str, Any], node: dict[str, Any], graph: dict[str, Any]
) -> DecideResult:
    """Handle a wait node — check if all dependencies completed."""
    waited_tasks = node.get("wait_for", [])
    completed = set(cursor.get("completed_tasks", []))

    if all(t in completed for t in waited_tasks):
        # All done — clear pending_parallel (decision #58e)
        cursor["pending_parallel"] = []
        write_cursor(workspace, cursor)

        # Evaluate on_complete to find goto target
        on_complete = node.get("on_complete", [])
        for rule in on_complete:
            condition_str = rule.get("condition", "default")
            # Wait nodes typically use completeness=="full" or default
            state = read_state(workspace)
            space = _build_condition_space({}, state, cursor)
            # For wait nodes, set completeness to full since all waited tasks completed
            if "conditions" not in space:
                space["conditions"] = {}
            space["conditions"]["completeness"] = COMP_FULL

            try:
                if evaluate(condition_str, space):
                    goto = rule.get("goto")
                    return _apply_transition(workspace, cursor, goto, graph)
            except Exception:
                continue

        return DecideResult("ESCALATE:wait node has no matching rule")
    else:
        from pymh.observe import extract_attempt_num
        from pymh.workspace import load_config

        missing = [t for t in waited_tasks if t not in completed]
        obs_dir = workspace / "dataflow" / "observations"
        max_attempts = load_config()["defaults"]["max_task_attempts"]

        # Check if any missing tasks have been attempted but failed
        unattempted = []
        for mid in missing:
            obs_files = sorted(
                obs_dir.glob(f"{mid}-*.json"), key=extract_attempt_num
            )
            if not obs_files:
                unattempted.append(mid)
                continue
            # Has observation = was attempted and failed → retry
            attempt_num = extract_attempt_num(obs_files[-1]) + 1
            if attempt_num >= max_attempts:
                write_escalation(
                    workspace, cursor,
                    f"parallel member {mid} exhausted {max_attempts} attempts",
                )
                return DecideResult(
                    f"ESCALATE:parallel member {mid} max attempts"
                )
            member_node = find_node(graph, mid)
            return dispatch_instruction(
                workspace, member_node, mid, attempt_num
            )

        return DecideResult(f"BLOCKED:waiting on {','.join(unattempted or missing)}")


def _apply_transition(
    workspace: Path,
    cursor: dict[str, Any],
    goto_target: Any,
    graph: dict[str, Any],
) -> DecideResult:
    """Apply a transition goto target."""
    if goto_target == "retry":
        from pymh.workspace import load_config

        cursor["task_attempts"] = cursor.get("task_attempts", 0) + 1
        max_attempts = load_config()["defaults"]["max_task_attempts"]
        if cursor["task_attempts"] >= max_attempts:
            write_cursor(workspace, cursor)
            write_escalation(
                workspace, cursor, f"max attempts ({max_attempts}) reached"
            )
            return DecideResult(f"ESCALATE:max attempts ({max_attempts}) reached")
        write_cursor(workspace, cursor)
        current = cursor["current_task"]
        node = find_node(graph, current)
        return dispatch_instruction(workspace, node, current, cursor["task_attempts"])

    elif goto_target == "replan":
        generate_failure_summary(workspace)
        cursor["forced_transition"] = None
        write_cursor(workspace, cursor)
        _enter_replan(workspace)
        return DecideResult("REPLAN")

    elif goto_target == "escalate":
        write_escalation(workspace, cursor, "condition_triggered")
        return DecideResult("ESCALATE:condition_triggered")

    elif goto_target == "done":
        _finalize_done(workspace)
        return DecideResult("DONE")

    elif isinstance(goto_target, list):
        # Parallel dispatch — advance cursor to wait node
        cursor["pending_parallel"] = goto_target
        wait_id = _find_wait_node(graph, goto_target)
        if wait_id:
            cursor["current_task"] = wait_id
            cursor["task_attempts"] = 0
        write_cursor(workspace, cursor)

        dispatches = []
        instruction_paths = []
        for nid in goto_target:
            node = find_node(graph, nid)
            result = dispatch_instruction(workspace, node, nid, 0)
            dispatches.append(f"{nid}:0:{result.instruction_paths[0]}")
            instruction_paths.extend(result.instruction_paths)

        output = f"PARALLEL:{','.join(dispatches)}"
        return DecideResult(output, instruction_paths)

    else:
        # Goto a specific node
        cursor["current_task"] = goto_target
        cursor["task_attempts"] = 0
        write_cursor(workspace, cursor)
        node = find_node(graph, goto_target)
        if node is None:
            return DecideResult(f"ESCALATE:unknown target node {goto_target}")
        return dispatch_instruction(workspace, node, goto_target, 0)


# --- Instruction generation ---


def dispatch_instruction(
    workspace: Path,
    node: dict[str, Any] | None,
    node_id: str,
    attempt: int,
) -> DecideResult:
    """Generate an exec instruction file and return DISPATCH output."""
    profile = read_profile(workspace)
    action = node.get("action", "unknown action") if node else "unknown action"

    lines = [
        f"# Task: {node_id} (attempt {attempt})",
        "",
        "## Action",
        action,
        "",
        "## Context",
    ]

    if profile:
        for k, v in profile.items():
            lines.append(f"- {k}: {v}")
    else:
        lines.append("(empty)")
    lines.append("")

    # Previous attempt narrative (if retry)
    if attempt > 0:
        prev_obs_path = workspace / "dataflow" / "observations" / f"{node_id}-{attempt - 1}.json"
        if prev_obs_path.exists():
            try:
                with open(prev_obs_path) as f:
                    prev_obs = json.load(f)
                narrative = prev_obs.get("narrative", "")
                if narrative:
                    lines.extend(["## Previous Attempt", narrative, ""])
            except (json.JSONDecodeError, OSError):
                pass

    lines.extend([
        "## Output",
        f"Write your observation to: dataflow/observations/{node_id}-{attempt}.json",
        "",
        "## Observation Schema",
        "Required fields:",
        "- `success` (bool): did the task succeed?",
        "- `signal` (string): one-line summary for progress display and trace",
        "- `conditions` (object): `quality_score` (0-100), "
        "`completeness` (\"full\"|\"partial\"|\"none\"), `blocker` (string|null), "
        "`confidence` (\"high\"|\"medium\"|\"low\"), `needs_replan` (bool), `escalate` (bool)",
        "- `evidence` (object): supporting data (test results, file paths, exit codes)",
        "- `surprise` (float 0-1): how unexpected was the outcome?",
        "- `narrative` (string): human-readable summary of what happened",
        "Optional: `profile_updates` (object), `files_changed` (string[]).",
    ])

    instruction = "\n".join(lines) + "\n"
    inst_path = workspace / "dataflow" / "instructions" / f"{node_id}-{attempt}.md"
    inst_path.parent.mkdir(parents=True, exist_ok=True)
    inst_path.write_text(instruction)

    output = f"DISPATCH:{node_id}:{attempt}:{inst_path}"
    return DecideResult(output, [str(inst_path)])


# --- Helpers ---


def load_task_graph(workspace: Path) -> dict[str, Any]:
    """Load task-graph.yaml."""
    path = workspace / "ctrlflow" / "task-graph.yaml"
    if not path.exists():
        return {"tasks": []}
    with open(path) as f:
        return yaml.safe_load(f) or {"tasks": []}


def find_node(graph: dict[str, Any], node_id: str) -> dict[str, Any] | None:
    """Find a task node by id."""
    for task in graph.get("tasks", []):
        if task.get("id") == node_id:
            return task
    return None


def _find_wait_node(graph: dict[str, Any], members: list[str]) -> str | None:
    """Find the wait node whose wait_for list contains the parallel members."""
    member_set = set(members)
    for task in graph.get("tasks", []):
        wait_for = task.get("wait_for", [])
        if wait_for and set(wait_for) == member_set:
            return task["id"]
    return None


def _read_observation(path: Path) -> dict[str, Any]:
    """Read an observation file, returning empty dict on failure."""
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        print(f"WARNING: failed to read observation {path.name}: {e}", file=sys.stderr)
        return {}


def _build_condition_space(
    observation: dict[str, Any],
    state: dict[str, Any],
    cursor: dict[str, Any],
) -> dict[str, Any]:
    """Build the merged condition space for rule evaluation."""
    return {
        "conditions": observation.get("conditions", {}),
        "tags": observation.get("tags", {}),
        "system_conditions": {
            "fuel_remaining": state.get("fuel_remaining", 0),
            "task_attempts": cursor.get("task_attempts", 0),
            "consecutive_failures": state.get("consecutive_failures", 0),
            "total_attempts": state.get("total_attempts", 0),
            "step": state.get("step", 0),
            "surprise_accumulator": state.get("surprise_accumulator", 0.0),
        },
    }


def write_escalation(
    workspace: Path, cursor: dict[str, Any], reason: str
) -> None:
    """Write escalation.json."""
    state = read_state(workspace)
    escalation = {
        "type": "condition_triggered",
        "source_task": cursor.get("current_task", "unknown"),
        "reason": reason,
        "state_snapshot": {
            "step": state.get("step", 0),
            "fuel_remaining": state.get("fuel_remaining", 0),
            "completed_tasks": cursor.get("completed_tasks", []),
            "current_task": cursor.get("current_task"),
            "task_attempts": cursor.get("task_attempts", 0),
        },
        "options": ["modify_graph", "skip_task", "write_observation", "replan", "abort"],
    }
    path = workspace / "ctrlflow" / "escalation.json"
    with open(path, "w") as f:
        json.dump(escalation, f, indent=2)
        f.write("\n")

    append_trace(workspace, {
        "timestamp": now_iso(),
        "action": "escalate",
        "type": "condition_triggered",
        "reason": reason,
        "source_task": cursor.get("current_task", "unknown"),
        "step": state.get("step", 0),
    })


def _finalize_done(workspace: Path) -> None:
    """Finalize DONE state: clear cursor, update meta and history."""
    meta = read_meta(workspace)
    cursor = read_cursor(workspace)

    # Clear cursor
    cursor["current_task"] = None
    cursor["task_attempts"] = 0
    write_cursor(workspace, cursor)

    # Update meta status
    meta["status"] = "done"
    write_meta(workspace, meta)

    # Update history
    task_id = meta.get("task_id", "")
    if task_id:
        update_history_status(task_id, "done")

    # Trace
    append_trace(workspace, {
        "timestamp": now_iso(),
        "action": "done",
        "task_id": task_id,
    })


def _finalize_abort(workspace: Path, source: str = "fuel_exhausted") -> None:
    """Finalize ABORT state: clear cursor, update meta/history, generate report."""
    meta = read_meta(workspace)
    cursor = read_cursor(workspace)

    cursor["current_task"] = None
    cursor["task_attempts"] = 0
    write_cursor(workspace, cursor)

    meta["status"] = "aborted"
    write_meta(workspace, meta)

    task_id = meta.get("task_id", "")
    if task_id:
        update_history_status(task_id, "aborted")

    append_trace(workspace, {
        "timestamp": now_iso(),
        "action": "abort",
        "task_id": task_id,
        "source": source,
    })

    from pymh.report import generate_report

    generate_report(workspace)


def _enter_replan(workspace: Path) -> None:
    """Transition to plan phase and reset state counters for a clean slate."""
    from pymh.workspace import load_config

    phase = read_phase(workspace)
    new_count = phase.get("replan_count", 0) + 1
    max_replans = load_config()["defaults"]["max_replan_count"]

    if new_count > max_replans:
        # Exceeded replan limit — escalate instead of looping
        cursor = read_cursor(workspace)
        write_escalation(
            workspace, cursor,
            f"max replan count ({max_replans}) exceeded",
        )
        return

    phase["phase"] = PHASE_PLAN
    phase["replan_count"] = new_count
    phase["phase_entered_at"] = now_iso()
    write_phase(workspace, phase)

    # Reset failure counters so the new plan isn't penalized by old failures
    state = read_state(workspace)
    state["consecutive_failures"] = 0
    state["surprise_accumulator"] = 0.0
    write_state(workspace, state)


def generate_failure_summary(workspace: Path) -> None:
    """Auto-generate failure_summary.json from trace data. Per decision #42."""
    traces = read_trace(workspace)
    state = read_state(workspace)
    profile = read_profile(workspace)

    failed_nodes: list[str] = []
    failure_signals: dict[str, str] = {}
    contradictions: list[dict[str, str]] = []

    for entry in traces:
        if entry.get("action") in ("observe", "observe_parallel"):
            conditions = entry.get("conditions", {})
            if conditions.get("completeness") != COMP_FULL:
                tid = entry.get("task_id", "unknown")
                if tid not in failed_nodes:
                    failed_nodes.append(tid)
                failure_signals[tid] = entry.get("observation_summary", "unknown failure")

            # Check for contradiction warnings
            for w in entry.get("validation_warnings", []):
                if "contradiction" in w:
                    contradictions.append({
                        "node": entry.get("task_id", "unknown"),
                        "detail": w,
                    })

    summary = {
        "failed_nodes": failed_nodes,
        "failure_signals": failure_signals,
        "evidence_contradictions": contradictions,
        "profile_facts": dict(profile),
        "total_steps_used": state.get("step", 0),
        "fuel_remaining": state.get("fuel_remaining", 0),
    }

    path = workspace / "ctrlflow" / "plans" / "failure_summary.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(summary, f, indent=2)
        f.write("\n")
