"""Two-pass plan compilation: NL plan → condition-driven task graph.

Reads ctrlflow/plans/current.yaml (NL plan written by plan subagent),
compiles to ctrlflow/task-graph.yaml.

Per 04-python-driver.md Section 3: Compilation Pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from pymh.workspace import load_config


class CompilationError(Exception):
    """Fatal compilation error."""


@dataclass
class CompilationWarning:
    message: str


@dataclass
class CompilationResult:
    task_graph: dict[str, Any]
    warnings: list[CompilationWarning] = field(default_factory=list)
    num_tasks: int = 0
    num_parallel_groups: int = 0


# --- Retry strategy keyword matching ---

RETRY_PATTERNS: list[tuple[list[str], str]] = [
    (["replan", "different approach"], "replan"),
    (["proceed", "move on", "what we have"], "goto_next"),
    (["stop", "abort"], "escalate"),
]


def _match_retry_strategy(text: str | None) -> str:
    """Match retry strategy text to an action.

    Returns: "replan", "goto_next", or "escalate" (default).
    """
    if not text:
        return "escalate"

    text_lower = text.lower()
    for patterns, action in RETRY_PATTERNS:
        for pattern in patterns:
            if pattern in text_lower:
                return action
    return "escalate"


# --- Compiler ---


def compile_plan(workspace: Path) -> CompilationResult:
    """Compile NL plan to task graph.

    Reads: ctrlflow/plans/current.yaml
    Writes: ctrlflow/task-graph.yaml, ctrlflow/cursor.json

    Returns CompilationResult with the graph and any warnings.
    Raises CompilationError on fatal issues.
    """
    plan_path = workspace / "ctrlflow" / "plans" / "current.yaml"
    if not plan_path.exists():
        raise CompilationError("Plan file not found: ctrlflow/plans/current.yaml")

    with open(plan_path) as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict) or "plan" not in raw:
        raise CompilationError("Invalid plan format: missing 'plan' key")

    plan = raw["plan"]
    steps = plan.get("steps", [])
    if not steps:
        raise CompilationError("Plan has no steps")

    warnings: list[CompilationWarning] = []

    # Build the step index
    step_index: dict[str, dict[str, Any]] = {}
    for step in steps:
        sid = step.get("id")
        if not sid:
            raise CompilationError(f"Step missing 'id': {step}")
        if sid in step_index:
            raise CompilationError(f"Duplicate step id: {sid}")
        step_index[sid] = step

    step_ids = list(step_index.keys())

    # Detect parallel groups
    parallel_groups = _find_parallel_groups(step_index)

    # Build task nodes
    task_nodes: list[dict[str, Any]] = []
    wait_nodes: list[dict[str, Any]] = []

    for _i, sid in enumerate(step_ids):
        step = step_index[sid]
        next_id = _get_next_id(sid, step_ids, step_index, parallel_groups)

        on_complete = _build_on_complete(step, next_id, step_index)

        node: dict[str, Any] = {
            "id": sid,
            "action": step.get("action", ""),
            "on_complete": on_complete,
        }
        task_nodes.append(node)

    # Generate wait nodes for parallel groups
    for group_id, members in parallel_groups.items():
        wait_node_id = f"{group_id}_wait"
        # Find what comes after the parallel group
        after_id = _find_after_parallel(members, step_ids, step_index, parallel_groups)

        wait_node: dict[str, Any] = {
            "id": wait_node_id,
            "wait_for": sorted(members),
            "on_complete": [
                {"condition": 'completeness == "full"', "goto": after_id or "done"},
                {"condition": "default", "goto": "retry"},
            ],
        }
        wait_nodes.append(wait_node)

    all_nodes = task_nodes + wait_nodes

    # Validation
    all_ids = {n["id"] for n in all_nodes}
    special_targets = {"done", "replan", "escalate", "retry"}

    validate_no_cycles(all_nodes, all_ids)
    validate_goto_targets(all_nodes, all_ids | special_targets, warnings)
    _validate_reachability(all_nodes, all_ids, step_ids[0], warnings)

    # Build final graph
    task_graph: dict[str, Any] = {
        "tasks": [n for n in all_nodes],
    }

    # Write outputs
    graph_path = workspace / "ctrlflow" / "task-graph.yaml"
    with open(graph_path, "w") as f:
        yaml.dump(task_graph, f, default_flow_style=False, sort_keys=False)

    # Reset cursor to first task
    import json

    cursor = {
        "current_task": step_ids[0],
        "task_attempts": 0,
        "completed_tasks": [],
        "pending_parallel": [],
        "forced_transition": None,
    }
    cursor_path = workspace / "ctrlflow" / "cursor.json"
    with open(cursor_path, "w") as f:
        json.dump(cursor, f, indent=2)
        f.write("\n")

    return CompilationResult(
        task_graph=task_graph,
        warnings=warnings,
        num_tasks=len(task_nodes),
        num_parallel_groups=len(parallel_groups),
    )


def _find_parallel_groups(
    step_index: dict[str, dict[str, Any]],
) -> dict[str, set[str]]:
    """Find parallel groups from can_parallel_with fields.

    Returns dict of group_id → set of member step ids.
    Group id is derived from the sorted member ids.
    """
    # Build adjacency: which steps want to be parallel with which
    parallel_adj: dict[str, set[str]] = {}
    for sid, step in step_index.items():
        can_par = step.get("can_parallel_with", [])
        if can_par:
            if sid not in parallel_adj:
                parallel_adj[sid] = set()
            parallel_adj[sid].add(sid)
            for other in can_par:
                if other in step_index:
                    parallel_adj[sid].add(other)
                    if other not in parallel_adj:
                        parallel_adj[other] = set()
                    parallel_adj[other].add(sid)
                    parallel_adj[other].add(other)

    # Merge connected components
    visited: set[str] = set()
    groups: dict[str, set[str]] = {}

    for sid in parallel_adj:
        if sid in visited:
            continue
        # BFS to find connected component
        component: set[str] = set()
        queue = [sid]
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            component.add(current)
            for neighbor in parallel_adj.get(current, set()):
                if neighbor not in visited:
                    queue.append(neighbor)

        if len(component) > 1:
            group_id = "_".join(sorted(component))
            groups[group_id] = component

    return groups


def _get_next_id(
    sid: str,
    step_ids: list[str],
    step_index: dict[str, dict[str, Any]],
    parallel_groups: dict[str, set[str]],
) -> str | list[str] | None:
    """Determine the next node id for a step's success transition."""
    # If this step is part of a parallel group, success goes to the wait node
    for group_id, members in parallel_groups.items():
        if sid in members:
            return f"{group_id}_wait"

    # Otherwise, find the next step in order that is NOT in the same parallel group
    idx = step_ids.index(sid)
    for j in range(idx + 1, len(step_ids)):
        candidate = step_ids[j]
        # Skip steps that are in the same parallel group as the current step
        skip = False
        for members in parallel_groups.values():
            if sid in members and candidate in members:
                skip = True
                break
        if not skip:
            # If the candidate is in a parallel group, goto the first member
            # (the parallel dispatch will be handled by the goto list)
            for _group_id, members in parallel_groups.items():
                if candidate in members:
                    return list(sorted(members))
            return candidate

    return "done"


def _find_after_parallel(
    members: set[str],
    step_ids: list[str],
    step_index: dict[str, dict[str, Any]],
    parallel_groups: dict[str, set[str]],
) -> str | None:
    """Find the first step after a parallel group."""
    max_idx = max(step_ids.index(m) for m in members if m in step_ids)
    for j in range(max_idx + 1, len(step_ids)):
        candidate = step_ids[j]
        if candidate not in members:
            return candidate
    return "done"


def _build_on_complete(
    step: dict[str, Any],
    next_id: Any,  # str, List[str], or None
    step_index: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build the on_complete rules for a task node."""
    rules: list[dict[str, Any]] = []

    # 1. Injected signals (per spec: always present)
    rules.append({"condition": "escalate == true", "goto": "escalate"})
    rules.append({"condition": "needs_replan == true", "goto": "replan"})

    # 2. Success condition — V1: always uses completeness rule.
    # success_criteria text is informational for subagents only, not compiled to conditions.
    success_goto = next_id if next_id else "done"
    rules.append({"condition": 'completeness == "full"', "goto": success_goto})

    # 3. Retry strategy rules — threshold from config (single source of truth)
    max_attempts = load_config()["defaults"]["max_task_attempts"]
    retry_strategy = step.get("retry_strategy", "")
    retry_action = _match_retry_strategy(retry_strategy)

    if retry_action == "replan":
        rules.append({"condition": f"task_attempts >= {max_attempts}", "goto": "replan"})
    elif retry_action == "goto_next":
        rules.append({"condition": f"task_attempts >= {max_attempts}", "goto": success_goto})
    else:  # escalate (default)
        rules.append({"condition": f"task_attempts >= {max_attempts}", "goto": "escalate"})

    # 4. Injected default rule (per decisions #33: compiler always injects default)
    rules.append({"condition": "default", "goto": "retry"})

    return rules


# --- Validation ---


def validate_no_cycles(
    nodes: list[dict[str, Any]], all_ids: set[str]
) -> None:
    """Verify no cycles exist in the task graph. Raises CompilationError if found."""
    # Build adjacency list
    adj: dict[str, set[str]] = {n["id"]: set() for n in nodes}
    for node in nodes:
        nid = node["id"]
        # Collect goto targets
        for rule in node.get("on_complete", []):
            target = rule.get("goto")
            if target is None:
                continue
            targets = target if isinstance(target, list) else [target]
            for t in targets:
                if t in all_ids and t != "retry":
                    adj[nid].add(t)
        # Wait nodes: validate that waited members exist.
        # Forward edges (member on_complete → wait node) are already in adjacency above.
        if "wait_for" in node:
            for dep in node["wait_for"]:
                if dep not in all_ids:
                    raise CompilationError(
                        f"Wait node '{nid}' references unknown member '{dep}'"
                    )

    # Topological sort via DFS
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {nid: WHITE for nid in adj}

    def dfs(nid: str) -> None:
        color[nid] = GRAY
        for neighbor in adj.get(nid, set()):
            if neighbor not in color:
                continue
            if color[neighbor] == GRAY:
                raise CompilationError(f"Cycle detected involving node '{nid}' -> '{neighbor}'")
            if color[neighbor] == WHITE:
                dfs(neighbor)
        color[nid] = BLACK

    for nid in adj:
        if color[nid] == WHITE:
            dfs(nid)


def validate_goto_targets(
    nodes: list[dict[str, Any]],
    valid_targets: set[str],
    warnings: list[CompilationWarning],
) -> None:
    """Verify all goto targets reference valid nodes or special targets."""
    for node in nodes:
        for rule in node.get("on_complete", []):
            target = rule.get("goto")
            if target is None:
                continue
            targets = target if isinstance(target, list) else [target]
            for t in targets:
                if t not in valid_targets:
                    raise CompilationError(
                        f"Node '{node['id']}' references unknown target '{t}'"
                    )


def _validate_reachability(
    nodes: list[dict[str, Any]],
    all_ids: set[str],
    start_id: str,
    warnings: list[CompilationWarning],
) -> None:
    """Warn about unreachable nodes."""
    # Build reachability from start node
    adj: dict[str, set[str]] = {}
    for node in nodes:
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

    # BFS
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

    unreachable = all_ids - reachable
    for uid in sorted(unreachable):
        warnings.append(CompilationWarning(f"Unreachable node: '{uid}'"))
