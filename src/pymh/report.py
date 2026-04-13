"""Report generation: trace + profile + meta -> task-report.md.

Reads state files and formats a markdown report summarizing task execution.
Per 04-python-driver.md Section 2 (report) and 08-user-interface.md Section 2.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pymh.schemas.defaults import COMP_FULL
from pymh.state import read_meta, read_phase, read_profile, read_state, read_trace
from pymh.workspace import load_config


def generate_report(workspace: Path) -> str:
    """Generate task-report.md and return its absolute path.

    Reads: meta.json, state.json, phase.json, profile.json, trace/trace.jsonl,
           dataflow/artifacts/ directory.
    Writes: dataflow/artifacts/task-report.md
    """
    meta = read_meta(workspace)
    state = read_state(workspace)
    phase = read_phase(workspace)
    profile = read_profile(workspace)
    traces = read_trace(workspace)

    sections = [
        _build_header(meta),
        _build_goal(meta),
        _build_result(meta, state, phase, traces),
        _build_timeline(traces),
        _build_profile(profile),
        _build_artifacts(workspace),
        _build_key_decisions(traces),
    ]

    report = "\n\n".join(s for s in sections if s) + "\n"

    report_path = workspace / "dataflow" / "artifacts" / "task-report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report)

    return str(report_path)


def _build_header(meta: dict[str, Any]) -> str:
    task_id = meta.get("task_id", "unknown")
    return f"# Task Report: {task_id}"


def _build_goal(meta: dict[str, Any]) -> str:
    return f"## Goal\n{meta.get('goal', 'unknown')}"


def _build_result(
    meta: dict[str, Any],
    state: dict[str, Any],
    phase: dict[str, Any],
    traces: list[dict[str, Any]],
) -> str:
    status = meta.get("status", "unknown").capitalize()
    step = state.get("step", 0)
    total = step + state.get("fuel_remaining", 0)
    replan_count = phase.get("replan_count", 0)

    # Duration from meta.created to last trace entry (or now)
    duration_str = _compute_duration(meta, traces)

    lines = [
        "## Result",
        f"**Status**: {status}",
        f"**Steps**: {step}/{total} used",
        f"**Duration**: {duration_str}",
        f"**Replan count**: {replan_count}",
    ]
    return "\n".join(lines)


def _compute_duration(meta: dict[str, Any], traces: list[dict[str, Any]]) -> str:
    """Compute duration from task creation to last trace entry."""
    created = meta.get("created", "")
    if not created:
        return "unknown"

    try:
        start = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return "unknown"

    # Find end time: last trace entry timestamp, or now
    end = datetime.now(timezone.utc)
    if traces:
        last_ts = traces[-1].get("timestamp", "")
        if last_ts:
            with contextlib.suppress(ValueError, AttributeError):
                end = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))

    delta = end - start
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        total_seconds = 0
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    return f"{minutes}m {seconds}s"


def _build_timeline(traces: list[dict[str, Any]]) -> str:
    lines = [
        "## Execution Timeline",
        "| Step | Task | Result | Surprise |",
        "|------|------|--------|----------|",
    ]

    for entry in traces:
        action = entry.get("action", "")
        step = entry.get("step", "-")

        if action in ("observe", "observe_parallel"):
            task_id = entry.get("task_id", "?")
            conditions = entry.get("conditions", {})
            completeness = conditions.get("completeness", "?")
            result_icon = "\u2713" if completeness == COMP_FULL else "\u2717"
            surprise = entry.get("surprise", 0)
            lines.append(f"| {step} | {task_id} | {result_icon} {completeness} | {surprise} |")

        elif action == "compile_plan":
            lines.append("| - | compile plan | - | - |")

        elif action == "escalate":
            reason = entry.get("type", "unknown")
            lines.append(f"| {step} | escalate: {reason} | - | - |")

        elif action == "resolve":
            decision = entry.get("decision", "unknown")
            lines.append(f"| {step} | resolve: {decision} | - | - |")

        elif action == "fuel_add":
            added = entry.get("fuel_added", 0)
            lines.append(f"| - | fuel +{added} | - | - |")

    if len(lines) == 3:
        # No data rows
        lines.append("| - | (no steps recorded) | - | - |")

    return "\n".join(lines)


def _build_profile(profile: dict[str, Any]) -> str:
    lines = ["## Profile (Accumulated Knowledge)"]
    if profile:
        for k, v in profile.items():
            lines.append(f"- {k}: {v}")
    else:
        lines.append("(empty)")
    return "\n".join(lines)


def _build_artifacts(workspace: Path) -> str:
    artifacts_dir = workspace / "dataflow" / "artifacts"
    lines = ["## Artifacts"]

    if artifacts_dir.exists():
        files = sorted(
            f.name
            for f in artifacts_dir.iterdir()
            if f.is_file() and f.name != "task-report.md"
        )
        if files:
            for name in files:
                lines.append(f"- `{name}`")
        else:
            lines.append("(none)")
    else:
        lines.append("(none)")

    return "\n".join(lines)


def _build_key_decisions(traces: list[dict[str, Any]]) -> str:
    lines = ["## Key Decisions"]

    high_surprise = load_config()["defaults"]["high_surprise_threshold"]
    entries = []
    for entry in traces:
        action = entry.get("action", "")
        surprise = entry.get("surprise", 0)

        if surprise > high_surprise and action in ("observe", "observe_parallel"):
            step = entry.get("step", "?")
            summary = entry.get("observation_summary", "n/a")
            entries.append(f"- Step {step}: {summary} (surprise={surprise})")

        elif action in ("escalate", "resolve"):
            step = entry.get("step", "?")
            if action == "escalate":
                reason = entry.get("type", entry.get("reason", "unknown"))
                entries.append(f"- Step {step}: escalation — {reason}")
            else:
                decision = entry.get("decision", "unknown")
                reasoning = entry.get("reasoning", "")
                text = f"- Step {step}: resolved — {decision}"
                if reasoning:
                    text += f" ({reasoning})"
                entries.append(text)

    if entries:
        lines.extend(entries)
    else:
        lines.append("(none)")

    return "\n".join(lines)
