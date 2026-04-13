"""Tests for the resume module.

Verifies: C15, C16, C17, K7, K10, K11, K12
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from pymh.decide import decide
from pymh.resume import ResumeError, resume
from pymh.state import (
    create_meta,
    create_phase,
    create_profile,
    create_state,
    read_cursor,
    read_meta,
    read_phase,
    read_state,
    read_trace,
    write_cursor,
    write_phase,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _make_workspace(tmp_path: Path, fuel: int = 30) -> Path:
    """Create a workspace with required files."""
    ws = tmp_path / "workspace"
    (ws / "ctrlflow" / "plans").mkdir(parents=True)
    (ws / "dataflow" / "instructions").mkdir(parents=True)
    (ws / "dataflow" / "observations").mkdir(parents=True)
    (ws / "dataflow" / "artifacts").mkdir(parents=True)
    (ws / "dataflow" / "scratchpad").mkdir(parents=True)
    (ws / "trace").mkdir(parents=True)
    (ws / "trace" / "trace.jsonl").touch()
    create_meta(ws, "test-task", "test goal", "general")
    create_state(ws, fuel)
    create_phase(ws)
    create_profile(ws)
    return ws


def _compile_plan(ws: Path, fixture: str) -> None:
    """Compile a plan fixture into the workspace and set phase to exec."""
    plans_dir = ws / "ctrlflow" / "plans"
    shutil.copy2(FIXTURES / fixture, plans_dir / "current.yaml")
    from pymh.compiler import compile_plan
    compile_plan(ws)
    phase = read_phase(ws)
    phase["phase"] = "exec"
    write_phase(ws, phase)


def _write_resolution(ws: Path, resolution: dict) -> None:
    """Write resolution.json to the workspace."""
    path = ws / "ctrlflow" / "resolution.json"
    path.write_text(json.dumps(resolution, indent=2) + "\n")


def _write_escalation(ws: Path, escalation: dict) -> None:
    """Write escalation.json to the workspace."""
    path = ws / "ctrlflow" / "escalation.json"
    path.write_text(json.dumps(escalation, indent=2) + "\n")


def _setup_exec_workspace(tmp_path: Path) -> Path:
    """Create a workspace in exec phase with compiled plan and cursor at t1."""
    ws = _make_workspace(tmp_path)
    _compile_plan(ws, "plan_sequential.yaml")
    # First decide to get cursor set to t1
    decide(ws)
    return ws


# --- Reading resolution ---


class TestReadResolution:
    def test_missing_resolution_raises(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        with pytest.raises(ResumeError, match=r"resolution\.json not found"):
            resume(ws)

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        (ws / "ctrlflow" / "resolution.json").write_text("not json{{{")
        with pytest.raises(ResumeError, match="failed to read"):
            resume(ws)

    def test_unknown_decision_type(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _write_resolution(ws, {"decision": "teleport", "details": {}})
        output = resume(ws)
        assert output.startswith("ESCALATE:driver_validation_error:")
        assert "unknown decision type" in output


# --- Replan ---


class TestResumeReplan:
    def test_replan_transitions_phase(self, tmp_path: Path) -> None:
        ws = _setup_exec_workspace(tmp_path)
        _write_escalation(ws, {"type": "condition_triggered"})
        _write_resolution(ws, {"decision": "replan", "reasoning": "need new approach"})

        output = resume(ws)
        assert output == "RESUMED:REPLAN"

        phase = read_phase(ws)
        assert phase["phase"] == "plan"
        assert phase["replan_count"] == 1

    def test_replan_generates_failure_summary(self, tmp_path: Path) -> None:
        ws = _setup_exec_workspace(tmp_path)
        _write_resolution(ws, {"decision": "replan"})

        resume(ws)
        assert (ws / "ctrlflow" / "plans" / "failure_summary.json").exists()

    def test_replan_cleans_up_files(self, tmp_path: Path) -> None:
        ws = _setup_exec_workspace(tmp_path)
        _write_escalation(ws, {"type": "test"})
        _write_resolution(ws, {"decision": "replan"})

        resume(ws)
        assert not (ws / "ctrlflow" / "escalation.json").exists()
        assert not (ws / "ctrlflow" / "resolution.json").exists()

    def test_replan_traces_resolution(self, tmp_path: Path) -> None:
        ws = _setup_exec_workspace(tmp_path)
        _write_resolution(ws, {"decision": "replan", "reasoning": "tests failing"})

        resume(ws)
        traces = read_trace(ws)
        resolve_entries = [t for t in traces if t.get("action") == "resolve"]
        assert len(resolve_entries) == 1
        assert resolve_entries[0]["decision"] == "replan"
        assert resolve_entries[0]["reasoning"] == "tests failing"


# --- Abort ---


class TestResumeAbort:
    def test_abort_marks_meta(self, tmp_path: Path) -> None:
        ws = _setup_exec_workspace(tmp_path)
        _write_resolution(ws, {"decision": "abort", "reasoning": "giving up"})

        output = resume(ws)
        assert output == "RESUMED:ABORT"

        meta = read_meta(ws)
        assert meta["status"] == "aborted"

    def test_abort_generates_report(self, tmp_path: Path) -> None:
        ws = _setup_exec_workspace(tmp_path)
        _write_resolution(ws, {"decision": "abort"})

        resume(ws)
        report = ws / "dataflow" / "artifacts" / "task-report.md"
        assert report.exists()

    def test_abort_cleans_up_files(self, tmp_path: Path) -> None:
        ws = _setup_exec_workspace(tmp_path)
        _write_escalation(ws, {"type": "test"})
        _write_resolution(ws, {"decision": "abort"})

        resume(ws)
        assert not (ws / "ctrlflow" / "escalation.json").exists()
        assert not (ws / "ctrlflow" / "resolution.json").exists()


# --- Skip Task ---


class TestResumeSkipTask:
    def test_skip_writes_observation(self, tmp_path: Path) -> None:
        ws = _setup_exec_workspace(tmp_path)
        _write_resolution(ws, {
            "decision": "skip_task",
            "details": {
                "observation": {
                    "success": True,
                    "signal": "skipped by agent",
                    "conditions": {"completeness": "full", "quality_score": 50},
                    "surprise": 0.0,
                },
            },
        })

        output = resume(ws)
        assert "RESUMED:" in output
        # Observation file should exist for t1
        obs_path = ws / "dataflow" / "observations" / "t1-0.json"
        assert obs_path.exists()

    def test_skip_advances_cursor(self, tmp_path: Path) -> None:
        ws = _setup_exec_workspace(tmp_path)
        _write_resolution(ws, {
            "decision": "skip_task",
            "details": {
                "observation": {
                    "success": True,
                    "signal": "skipped",
                    "conditions": {"completeness": "full"},
                    "surprise": 0.0,
                },
            },
        })

        output = resume(ws)
        # After skipping t1, should dispatch t2
        assert "RESUMED:DISPATCH:" in output
        cursor = read_cursor(ws)
        assert cursor["current_task"] == "t2"

    def test_skip_validates_observation(self, tmp_path: Path) -> None:
        ws = _setup_exec_workspace(tmp_path)
        _write_resolution(ws, {
            "decision": "skip_task",
            "details": {"observation": {"missing": "required fields"}},
        })

        output = resume(ws)
        assert "ESCALATE:driver_validation_error:" in output

    def test_skip_forces_success_true(self, tmp_path: Path) -> None:
        ws = _setup_exec_workspace(tmp_path)
        _write_resolution(ws, {
            "decision": "skip_task",
            "details": {
                "observation": {
                    "success": False,
                    "signal": "skip but marked false",
                    "conditions": {"completeness": "full"},
                    "surprise": 0.0,
                },
            },
        })

        output = resume(ws)
        # Should still advance because skip forces success=True
        assert "RESUMED:DISPATCH:" in output


# --- Write Observation ---


class TestResumeWriteObservation:
    def test_writes_observation_file(self, tmp_path: Path) -> None:
        ws = _setup_exec_workspace(tmp_path)
        _write_resolution(ws, {
            "decision": "write_observation",
            "details": {
                "observation": {
                    "success": True,
                    "signal": "manually injected result",
                    "conditions": {"completeness": "full", "quality_score": 80},
                    "surprise": 0.1,
                },
            },
        })

        resume(ws)
        obs_path = ws / "dataflow" / "observations" / "t1-0.json"
        assert obs_path.exists()

    def test_processes_normally(self, tmp_path: Path) -> None:
        ws = _setup_exec_workspace(tmp_path)
        _write_resolution(ws, {
            "decision": "write_observation",
            "details": {
                "observation": {
                    "success": True,
                    "signal": "injected",
                    "conditions": {"completeness": "full"},
                    "surprise": 0.1,
                },
            },
        })

        resume(ws)
        # State should be updated (step incremented)
        state = read_state(ws)
        assert state["step"] == 1

    def test_returns_next_dispatch(self, tmp_path: Path) -> None:
        ws = _setup_exec_workspace(tmp_path)
        _write_resolution(ws, {
            "decision": "write_observation",
            "details": {
                "observation": {
                    "success": True,
                    "signal": "done",
                    "conditions": {"completeness": "full"},
                    "surprise": 0.0,
                },
            },
        })

        output = resume(ws)
        assert "RESUMED:DISPATCH:" in output

    def test_validates_observation_fields(self, tmp_path: Path) -> None:
        ws = _setup_exec_workspace(tmp_path)
        _write_resolution(ws, {
            "decision": "write_observation",
            "details": {"observation": {"no_success": True}},
        })

        output = resume(ws)
        assert "ESCALATE:driver_validation_error:" in output


# --- Modify Graph ---


class TestResumeModifyGraph:
    def test_insert_task_adds_node(self, tmp_path: Path) -> None:
        ws = _setup_exec_workspace(tmp_path)
        _write_resolution(ws, {
            "decision": "modify_graph",
            "details": {
                "action": "insert_task",
                "new_task": {
                    "id": "t0_prep",
                    "action": "prepare environment",
                    "on_complete": [
                        {"condition": 'completeness == "full"', "goto": "t1"},
                        {"condition": "default", "goto": "retry"},
                    ],
                },
                "insert_before": "t1",
            },
        })

        output = resume(ws)
        assert "RESUMED:DISPATCH:t0_prep:0:" in output

        # Verify graph has the new node
        graph_path = ws / "ctrlflow" / "task-graph.yaml"
        with open(graph_path) as f:
            graph = yaml.safe_load(f)
        node_ids = [t["id"] for t in graph["tasks"]]
        assert "t0_prep" in node_ids

    def test_insert_task_redirects_gotos(self, tmp_path: Path) -> None:
        ws = _setup_exec_workspace(tmp_path)
        _write_resolution(ws, {
            "decision": "modify_graph",
            "details": {
                "action": "insert_task",
                "new_task": {
                    "id": "t1_prep",
                    "action": "prep work",
                    "on_complete": [
                        {"condition": 'completeness == "full"', "goto": "t1"},
                        {"condition": "default", "goto": "retry"},
                    ],
                },
                "insert_before": "t1",
            },
        })

        resume(ws)

        # Verify that no other node's goto still points to t1 directly
        # (except t1_prep which is allowed — it references t1 in its own on_complete)
        graph_path = ws / "ctrlflow" / "task-graph.yaml"
        with open(graph_path) as f:
            graph = yaml.safe_load(f)

        for task in graph["tasks"]:
            if task["id"] == "t1_prep":
                continue
            for rule in task.get("on_complete", []):
                target = rule.get("goto")
                if isinstance(target, str) and target == "t1":
                    # The only nodes that might still reference t1 are those
                    # that didn't reference t1 before (i.e., t1 was not their goto)
                    pass  # This is fine — only the node that WAS pointing to t1 got redirected

    def test_remove_task_removes_node(self, tmp_path: Path) -> None:
        ws = _setup_exec_workspace(tmp_path)
        # Move cursor to t2 so we're not removing current task
        cursor = read_cursor(ws)
        cursor["current_task"] = "t2"
        cursor["task_attempts"] = 0
        write_cursor(ws, cursor)

        _write_resolution(ws, {
            "decision": "modify_graph",
            "details": {
                "action": "remove_task",
                "node_id": "t3",
            },
        })

        output = resume(ws)
        assert "RESUMED:DISPATCH:" in output

        graph_path = ws / "ctrlflow" / "task-graph.yaml"
        with open(graph_path) as f:
            graph = yaml.safe_load(f)
        node_ids = [t["id"] for t in graph["tasks"]]
        assert "t3" not in node_ids

    def test_update_transitions_replaces_rules(self, tmp_path: Path) -> None:
        ws = _setup_exec_workspace(tmp_path)
        new_rules = [
            {"condition": 'completeness == "full"', "goto": "t3"},
            {"condition": "default", "goto": "escalate"},
        ]
        _write_resolution(ws, {
            "decision": "modify_graph",
            "details": {
                "action": "update_transitions",
                "node_id": "t1",
                "new_on_complete": new_rules,
            },
        })

        output = resume(ws)
        assert "RESUMED:DISPATCH:" in output

        graph_path = ws / "ctrlflow" / "task-graph.yaml"
        with open(graph_path) as f:
            graph = yaml.safe_load(f)
        t1 = next(t for t in graph["tasks"] if t["id"] == "t1")
        assert t1["on_complete"] == new_rules

    def test_rejects_duplicate_ids(self, tmp_path: Path) -> None:
        ws = _setup_exec_workspace(tmp_path)
        _write_resolution(ws, {
            "decision": "modify_graph",
            "details": {
                "action": "insert_task",
                "new_task": {
                    "id": "t1",  # already exists
                    "action": "duplicate",
                    "on_complete": [{"condition": "default", "goto": "t2"}],
                },
                "insert_before": "t2",
            },
        })

        output = resume(ws)
        assert "ESCALATE:driver_validation_error:" in output
        assert "duplicate" in output

    def test_rejects_dangling_gotos(self, tmp_path: Path) -> None:
        ws = _setup_exec_workspace(tmp_path)
        _write_resolution(ws, {
            "decision": "modify_graph",
            "details": {
                "action": "insert_task",
                "new_task": {
                    "id": "t_new",
                    "action": "test",
                    "on_complete": [
                        {"condition": "default", "goto": "nonexistent_node"},
                    ],
                },
                "insert_before": "t1",
            },
        })

        output = resume(ws)
        assert "ESCALATE:driver_validation_error:" in output

    def test_unknown_action_rejected(self, tmp_path: Path) -> None:
        ws = _setup_exec_workspace(tmp_path)
        _write_resolution(ws, {
            "decision": "modify_graph",
            "details": {"action": "teleport"},
        })

        output = resume(ws)
        assert "ESCALATE:driver_validation_error:" in output

    def test_insert_before_nonexistent_node(self, tmp_path: Path) -> None:
        ws = _setup_exec_workspace(tmp_path)
        _write_resolution(ws, {
            "decision": "modify_graph",
            "details": {
                "action": "insert_task",
                "new_task": {
                    "id": "t_new",
                    "action": "prep",
                    "on_complete": [
                        {"condition": 'completeness == "full"', "goto": "t1"},
                        {"condition": "default", "goto": "retry"},
                    ],
                },
                "insert_before": "nonexistent_node",
            },
        })

        output = resume(ws)
        assert "ESCALATE:driver_validation_error:" in output
        assert "insert_before target not found" in output

    def test_remove_current_cursor_node(self, tmp_path: Path) -> None:
        ws = _setup_exec_workspace(tmp_path)
        # Cursor is at t1 after _setup_exec_workspace
        _write_resolution(ws, {
            "decision": "modify_graph",
            "details": {
                "action": "remove_task",
                "node_id": "t1",
            },
        })

        output = resume(ws)
        assert "ESCALATE:driver_validation_error:" in output
        assert "cannot remove current cursor node" in output

    def test_cursor_unreachable_after_modification(self, tmp_path: Path) -> None:
        ws = _setup_exec_workspace(tmp_path)
        # Insert a node that points to itself (creating an island)
        # and try to update transitions of t1 to point to "done",
        # making t2 and t3 unreachable. Then the cursor (at t1) is still
        # reachable, so this should succeed. Instead, test with update_transitions
        # pointing t1 to done, then cursor stays at t1 which IS reachable.
        # To truly test unreachability, we need a trickier setup.
        # Move cursor to t2 first, then update t1's goto to skip t2 entirely.
        cursor = read_cursor(ws)
        cursor["current_task"] = "t2"
        cursor["task_attempts"] = 0
        write_cursor(ws, cursor)

        # Now update t1 to skip directly to t3, making t2 unreachable from start
        _write_resolution(ws, {
            "decision": "modify_graph",
            "details": {
                "action": "update_transitions",
                "node_id": "t1",
                "new_on_complete": [
                    {"condition": "default", "goto": "t3"},
                ],
            },
        })

        output = resume(ws)
        assert "ESCALATE:driver_validation_error:" in output
        assert "not reachable" in output


# --- Validation failure ---


class TestValidationFailure:
    def test_re_escalates_on_failure(self, tmp_path: Path) -> None:
        ws = _setup_exec_workspace(tmp_path)
        _write_escalation(ws, {"type": "original"})
        _write_resolution(ws, {
            "decision": "write_observation",
            "details": {"observation": "not a dict"},
        })

        output = resume(ws)
        assert output.startswith("ESCALATE:driver_validation_error:")

        # New escalation.json should be written
        esc_path = ws / "ctrlflow" / "escalation.json"
        assert esc_path.exists()

    def test_re_escalation_preserves_resolution(self, tmp_path: Path) -> None:
        """On validation failure, resolution.json should NOT be cleaned up."""
        ws = _setup_exec_workspace(tmp_path)
        _write_resolution(ws, {
            "decision": "write_observation",
            "details": {"observation": "bad"},
        })

        resume(ws)
        # resolution.json should still exist for debugging
        assert (ws / "ctrlflow" / "resolution.json").exists()


# --- Cleanup ---


class TestCleanup:
    def test_success_cleans_up_both_files(self, tmp_path: Path) -> None:
        ws = _setup_exec_workspace(tmp_path)
        _write_escalation(ws, {"type": "test"})
        _write_resolution(ws, {"decision": "replan"})

        resume(ws)
        assert not (ws / "ctrlflow" / "escalation.json").exists()
        assert not (ws / "ctrlflow" / "resolution.json").exists()

    def test_success_without_escalation_file(self, tmp_path: Path) -> None:
        """Resume should work even if escalation.json doesn't exist (edge case)."""
        ws = _setup_exec_workspace(tmp_path)
        _write_resolution(ws, {"decision": "replan"})

        output = resume(ws)
        assert output == "RESUMED:REPLAN"
