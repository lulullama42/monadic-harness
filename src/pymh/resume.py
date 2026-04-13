"""Resume command: apply resolution after escalation.

Reads resolution.json, validates, applies the resolution decision,
clears escalation.json and resolution.json, returns RESUMED:{action}.

Per 04-python-driver.md Section 2 (resume) and 07-invariants-and-escalation.md.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from pymh.compiler import (
    CompilationError,
    CompilationWarning,
    validate_goto_targets,
    validate_no_cycles,
)
from pymh.decide import (
    decide,
    dispatch_instruction,
    find_node,
    generate_failure_summary,
    load_task_graph,
    write_escalation,
)
from pymh.observe import process_observation
from pymh.report import generate_report
from pymh.schemas.defaults import PHASE_PLAN
from pymh.state import (
    append_trace,
    now_iso,
    read_cursor,
    read_meta,
    read_phase,
    update_history_status,
    write_cursor,
    write_meta,
    write_phase,
)

VALID_DECISIONS = frozenset({"modify_graph", "skip_task", "write_observation", "replan", "abort"})
SPECIAL_TARGETS = {"done", "replan", "escalate", "retry"}


class ResumeError(Exception):
    """Validation error during resume."""


def resume(workspace: Path) -> str:
    """Process resolution.json and resume execution.

    Returns the stdout output string (RESUMED:...).
    On validation failure, re-escalates and returns ESCALATE:driver_validation_error:...
    """
    resolution = _read_resolution(workspace)
    decision = resolution.get("decision")

    if decision not in VALID_DECISIONS:
        return _re_escalate(workspace, f"unknown decision type: {decision}")

    # Validate and apply
    try:
        _validate_resolution(workspace, resolution)
    except ResumeError as e:
        return _re_escalate(workspace, str(e))

    handlers = {
        "replan": _apply_replan,
        "abort": _apply_abort,
        "skip_task": _apply_skip_task,
        "write_observation": _apply_write_observation,
        "modify_graph": _apply_modify_graph,
    }

    try:
        output = handlers[decision](workspace, resolution)
    except ResumeError as e:
        return _re_escalate(workspace, str(e))

    # Trace the resolution
    append_trace(workspace, {
        "timestamp": now_iso(),
        "action": "resolve",
        "decision": decision,
        "reasoning": resolution.get("reasoning", ""),
    })

    # Cleanup escalation/resolution files
    _cleanup_escalation_files(workspace)

    return output


# --- Reading and validation ---


def _read_resolution(workspace: Path) -> dict[str, Any]:
    """Read resolution.json. Raises ResumeError if missing or malformed."""
    path = workspace / "ctrlflow" / "resolution.json"
    if not path.exists():
        raise ResumeError("resolution.json not found")

    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise ResumeError(f"failed to read resolution.json: {e}") from e

    if not isinstance(data, dict):
        raise ResumeError("resolution.json is not a JSON object")

    return data


def _validate_resolution(workspace: Path, resolution: dict[str, Any]) -> None:
    """Dispatch to type-specific validation. Raises ResumeError on failure."""
    decision = resolution["decision"]
    details = resolution.get("details", {})

    if decision == "modify_graph":
        _validate_modify_graph(workspace, details)
    elif decision == "write_observation":
        _validate_write_observation(details)
    elif decision == "skip_task":
        _validate_skip_task(details)
    # replan and abort need no validation


def _validate_modify_graph(workspace: Path, details: dict[str, Any]) -> None:
    """Validate graph modification will produce a valid graph."""
    action = details.get("action")
    if action not in ("insert_task", "remove_task", "update_transitions"):
        raise ResumeError(f"unknown modify_graph action: {action}")

    graph = load_task_graph(workspace)
    tasks = graph.get("tasks", [])

    if action == "insert_task":
        new_task = details.get("new_task")
        if not new_task or not isinstance(new_task, dict):
            raise ResumeError("insert_task requires 'new_task' dict")
        new_id = new_task.get("id")
        if not new_id:
            raise ResumeError("new_task must have an 'id'")
        existing_ids = {t["id"] for t in tasks}
        if new_id in existing_ids:
            raise ResumeError(f"duplicate node id: {new_id}")
        insert_before = details.get("insert_before")
        if insert_before and insert_before not in existing_ids:
            raise ResumeError(f"insert_before target not found: {insert_before}")

    elif action == "remove_task":
        target = details.get("node_id")
        if not target:
            raise ResumeError("remove_task requires 'node_id'")
        if not find_node(graph, target):
            raise ResumeError(f"node to remove not found: {target}")
        # Cannot remove the node the cursor currently points to
        cursor = read_cursor(workspace)
        if cursor.get("current_task") == target:
            raise ResumeError(f"cannot remove current cursor node: {target}")

    elif action == "update_transitions":
        target = details.get("node_id")
        if not target:
            raise ResumeError("update_transitions requires 'node_id'")
        if not find_node(graph, target):
            raise ResumeError(f"node not found: {target}")
        if "new_on_complete" not in details:
            raise ResumeError("update_transitions requires 'new_on_complete'")


def _validate_write_observation(details: dict[str, Any]) -> None:
    """Validate injected observation has required fields."""
    obs = details.get("observation")
    if not obs or not isinstance(obs, dict):
        raise ResumeError("write_observation requires 'observation' dict in details")
    if "success" not in obs:
        raise ResumeError("observation must include 'success'")
    if "signal" not in obs:
        raise ResumeError("observation must include 'signal'")
    if "conditions" not in obs or not isinstance(obs.get("conditions"), dict):
        raise ResumeError("observation must include 'conditions' dict")


def _validate_skip_task(details: dict[str, Any]) -> None:
    """Validate synthetic observation for skip_task."""
    obs = details.get("observation")
    if not obs or not isinstance(obs, dict):
        raise ResumeError("skip_task requires 'observation' dict in details")
    if "success" not in obs:
        raise ResumeError("synthetic observation must include 'success'")
    if "signal" not in obs:
        raise ResumeError("synthetic observation must include 'signal'")


# --- Resolution handlers ---


def _apply_replan(workspace: Path, resolution: dict[str, Any]) -> str:
    """Transition to plan phase."""
    generate_failure_summary(workspace)
    phase = read_phase(workspace)
    phase["phase"] = PHASE_PLAN
    phase["replan_count"] = phase.get("replan_count", 0) + 1
    phase["phase_entered_at"] = now_iso()
    write_phase(workspace, phase)
    return "RESUMED:REPLAN"


def _apply_abort(workspace: Path, resolution: dict[str, Any]) -> str:
    """Mark task as aborted and generate report."""
    meta = read_meta(workspace)
    task_id = meta["task_id"]
    meta["status"] = "aborted"
    write_meta(workspace, meta)
    update_history_status(task_id, "aborted")

    append_trace(workspace, {
        "timestamp": now_iso(),
        "action": "abort",
        "task_id": task_id,
        "source": "resume",
    })

    generate_report(workspace)
    return "RESUMED:ABORT"


def _apply_write_observation(workspace: Path, resolution: dict[str, Any]) -> str:
    """Write injected observation and process it normally."""
    details = resolution.get("details", {})
    observation = details["observation"]

    cursor = read_cursor(workspace)
    node_id = cursor.get("current_task", "unknown")
    attempt = cursor.get("task_attempts", 0)

    # Write the observation file
    obs_path = workspace / "dataflow" / "observations" / f"{node_id}-{attempt}.json"
    obs_path.parent.mkdir(parents=True, exist_ok=True)
    with open(obs_path, "w") as f:
        json.dump(observation, f, indent=2)
        f.write("\n")

    # Process it through the normal pipeline
    process_observation(workspace, node_id, attempt)

    # Get next dispatch
    result = decide(workspace)
    return f"RESUMED:{result.output}"


def _apply_skip_task(workspace: Path, resolution: dict[str, Any]) -> str:
    """Write synthetic observation for skipped task and advance."""
    details = resolution.get("details", {})
    observation = details["observation"]

    # Ensure success=True so cursor advances
    if not observation.get("success"):
        observation["success"] = True

    cursor = read_cursor(workspace)
    node_id = cursor.get("current_task", "unknown")
    attempt = cursor.get("task_attempts", 0)

    # Write the observation file
    obs_path = workspace / "dataflow" / "observations" / f"{node_id}-{attempt}.json"
    obs_path.parent.mkdir(parents=True, exist_ok=True)
    with open(obs_path, "w") as f:
        json.dump(observation, f, indent=2)
        f.write("\n")

    # Process it through the normal pipeline
    process_observation(workspace, node_id, attempt)

    # Get next dispatch
    result = decide(workspace)
    return f"RESUMED:{result.output}"


def _apply_modify_graph(workspace: Path, resolution: dict[str, Any]) -> str:
    """Patch task-graph.yaml and dispatch from the new/updated node."""
    details = resolution.get("details", {})
    action = details["action"]

    graph = load_task_graph(workspace)

    if action == "insert_task":
        graph = _patch_graph_insert(graph, details)
    elif action == "remove_task":
        graph = _patch_graph_remove(graph, details)
    elif action == "update_transitions":
        graph = _patch_graph_update_transitions(graph, details)

    # Validate the modified graph
    tasks = graph.get("tasks", [])
    all_ids = {t["id"] for t in tasks}
    valid_targets = all_ids | SPECIAL_TARGETS

    try:
        validate_no_cycles(tasks, all_ids)
        warnings: list[CompilationWarning] = []
        validate_goto_targets(tasks, valid_targets, warnings)
    except CompilationError as e:
        raise ResumeError(f"modified graph is invalid: {e}") from e

    # Validate cursor reachability (D59b)
    cursor = read_cursor(workspace)
    cursor_target = cursor.get("current_task")
    if action == "insert_task":
        cursor_target = details["new_task"]["id"]
    elif action == "remove_task":
        successor = details.get("successor")
        if successor:
            cursor_target = successor
    if cursor_target and cursor_target in all_ids and tasks:
        start_id = tasks[0]["id"]
        reachable = _reachable_from(tasks, all_ids, start_id)
        if cursor_target not in reachable:
            raise ResumeError(
                f"cursor node {cursor_target} is not reachable from graph start {start_id}"
            )

    # Write the patched graph
    graph_path = workspace / "ctrlflow" / "task-graph.yaml"
    with open(graph_path, "w") as f:
        yaml.dump(graph, f, default_flow_style=False, sort_keys=False)

    # Update cursor to the target node
    cursor = read_cursor(workspace)
    if action == "insert_task":
        new_id = details["new_task"]["id"]
        cursor["current_task"] = new_id
        cursor["task_attempts"] = 0
    elif action == "remove_task":
        # Advance to the removed node's successor
        successor = details.get("successor")
        if successor:
            cursor["current_task"] = successor
            cursor["task_attempts"] = 0
    # update_transitions: keep current cursor position
    write_cursor(workspace, cursor)

    # Dispatch instruction for the current task
    current = cursor["current_task"]
    attempt = cursor.get("task_attempts", 0)
    node = find_node(graph, current)
    if node is None:
        raise ResumeError(f"cursor node {current} not found in modified graph")

    result = dispatch_instruction(workspace, node, current, attempt)
    return f"RESUMED:{result.output}"


# --- Graph patching helpers ---


def _patch_graph_insert(graph: dict[str, Any], details: dict[str, Any]) -> dict[str, Any]:
    """Insert a new task node before the specified target."""
    new_task = details["new_task"]
    new_id = new_task["id"]
    insert_before = details.get("insert_before")

    tasks = graph.get("tasks", [])

    # Insert the new task at the right position
    if insert_before:
        idx = next((i for i, t in enumerate(tasks) if t["id"] == insert_before), len(tasks))
        tasks.insert(idx, new_task)

        # Redirect existing goto references from insert_before to new_id
        _rewrite_goto_targets(tasks, insert_before, new_id, exclude_id=new_id)
    else:
        tasks.append(new_task)

    graph["tasks"] = tasks
    return graph


def _patch_graph_remove(graph: dict[str, Any], details: dict[str, Any]) -> dict[str, Any]:
    """Remove a task node and redirect references to its successor."""
    node_id = details["node_id"]
    successor = details.get("successor")

    tasks = graph.get("tasks", [])

    # If no successor provided, find the first goto target of the removed node
    if not successor:
        node = find_node(graph, node_id)
        if node:
            for rule in node.get("on_complete", []):
                target = rule.get("goto")
                if target and isinstance(target, str) and target not in SPECIAL_TARGETS:
                    successor = target
                    break

    # Remove the node
    tasks = [t for t in tasks if t["id"] != node_id]

    # Redirect references to the removed node
    redirect_to = successor or "done"
    _rewrite_goto_targets(tasks, node_id, redirect_to)
    # Also update wait_for references
    for task in tasks:
        if "wait_for" in task:
            if successor:
                task["wait_for"] = [
                    successor if wf == node_id else wf
                    for wf in task["wait_for"]
                ]
            else:
                task["wait_for"] = [wf for wf in task["wait_for"] if wf != node_id]

    graph["tasks"] = tasks
    return graph


def _patch_graph_update_transitions(
    graph: dict[str, Any], details: dict[str, Any]
) -> dict[str, Any]:
    """Replace on_complete rules for a specified node."""
    node_id = details["node_id"]
    new_rules = details["new_on_complete"]

    for task in graph.get("tasks", []):
        if task["id"] == node_id:
            task["on_complete"] = new_rules
            break

    return graph


def _rewrite_goto_targets(
    tasks: list[dict[str, Any]],
    old_target: str,
    new_target: str,
    exclude_id: str | None = None,
) -> None:
    """Rewrite all goto references from old_target to new_target across all nodes."""
    for task in tasks:
        if exclude_id and task["id"] == exclude_id:
            continue
        for rule in task.get("on_complete", []):
            target = rule.get("goto")
            if target == old_target:
                rule["goto"] = new_target
            elif isinstance(target, list):
                rule["goto"] = [new_target if t == old_target else t for t in target]


# --- Helpers ---


def _reachable_from(
    tasks: list[dict[str, Any]], all_ids: set[str], start_id: str
) -> set[str]:
    """Return the set of node IDs reachable from start_id via goto traversal."""
    adj: dict[str, set[str]] = {}
    for node in tasks:
        nid = node["id"]
        targets: set[str] = set()
        for rule in node.get("on_complete", []):
            target = rule.get("goto")
            if target is None:
                continue
            ts = target if isinstance(target, list) else [target]
            for t in ts:
                if t in all_ids:
                    targets.add(t)
        adj[nid] = targets

    reachable: set[str] = set()
    queue = [start_id]
    while queue:
        current = queue.pop(0)
        if current in reachable:
            continue
        reachable.add(current)
        for neighbor in adj.get(current, set()):
            if neighbor not in reachable:
                queue.append(neighbor)
    return reachable


def _re_escalate(workspace: Path, reason: str) -> str:
    """Re-escalate when resolution validation fails."""
    cursor = read_cursor(workspace)
    write_escalation(workspace, cursor, f"driver_validation_error: {reason}")
    return f"ESCALATE:driver_validation_error:{reason}"


def _cleanup_escalation_files(workspace: Path) -> None:
    """Remove escalation.json and resolution.json."""
    (workspace / "ctrlflow" / "escalation.json").unlink(missing_ok=True)
    (workspace / "ctrlflow" / "resolution.json").unlink(missing_ok=True)
