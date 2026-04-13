"""Tests for the decide module.

Verifies: C1, C11, C18, D10, D16, K10, K11, K14, P1, P4, P7, P9, I9
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from pymh.cli import main
from pymh.decide import decide
from pymh.schemas.defaults import DEFAULT_CONFIG
from pymh.state import (
    create_meta,
    create_phase,
    create_profile,
    create_state,
    read_cursor,
    read_phase,
    write_cursor,
    write_phase,
)

FIXTURES = Path(__file__).parent / "fixtures"
_DEFAULTS = DEFAULT_CONFIG["defaults"]


def _make_workspace(tmp_path: Path, fuel: int = _DEFAULTS["fuel"]) -> Path:
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
    # Match CLI behavior: set phase to exec after compilation
    phase = read_phase(ws)
    phase["phase"] = "exec"
    write_phase(ws, phase)


def _write_observation(ws: Path, node_id: str, attempt: int, obs: dict) -> None:
    path = ws / "dataflow" / "observations" / f"{node_id}-{attempt}.json"
    path.write_text(json.dumps(obs))


# --- Plan phase ---


class TestDecidePlan:
    def test_plan_dispatch(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        result = decide(ws, phase_override="plan")
        assert result.output.startswith("DISPATCH:plan:0:")
        assert len(result.instruction_paths) == 1

    def test_plan_instruction_contains_goal(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        result = decide(ws, phase_override="plan")
        inst_path = Path(result.instruction_paths[0])
        content = inst_path.read_text()
        assert "test goal" in content
        assert "## Goal" in content
        assert "## Output" in content

    def test_plan_includes_failure_summary(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        # Create a failure summary
        summary = {"failed_nodes": ["t1"], "fuel_remaining": 20}
        (ws / "ctrlflow" / "plans" / "failure_summary.json").write_text(json.dumps(summary))

        result = decide(ws, phase_override="plan")
        inst_path = Path(result.instruction_paths[0])
        content = inst_path.read_text()
        assert "Previous Failure" in content
        assert "t1" in content


# --- Verify phase ---


class TestDecideVerify:
    def test_verify_dispatch(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        result = decide(ws, phase_override="verify")
        assert result.output.startswith("DISPATCH:verify:0:")

    def test_verify_instruction_contains_goal(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        result = decide(ws, phase_override="verify")
        inst_path = Path(result.instruction_paths[0])
        content = inst_path.read_text()
        assert "test goal" in content
        assert "## Artifacts" in content


# --- Exec phase: first call ---


class TestDecideExecFirstCall:
    def test_first_call_dispatches(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_sequential.yaml")
        # Phase should be exec after compile
        phase = read_phase(ws)
        assert phase["phase"] == "exec"

        result = decide(ws)
        assert result.output.startswith("DISPATCH:t1:0:")

    def test_first_call_writes_instruction(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_sequential.yaml")
        result = decide(ws)
        inst_path = Path(result.instruction_paths[0])
        assert inst_path.exists()
        content = inst_path.read_text()
        assert "## Action" in content
        assert "webpack" in content.lower()  # From fixture action


# --- Exec phase: condition evaluation ---


class TestDecideExecEvaluation:
    def test_success_transitions_to_next(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_sequential.yaml")

        # Write observation for t1 with completeness=full
        _write_observation(ws, "t1", 0, {
            "success": True,
            "signal": "done",
            "surprise": 0.1,
            "conditions": {
                "completeness": "full",
                "quality_score": 90,
                "escalate": False,
                "needs_replan": False,
            },
        })

        result = decide(ws)
        # Should transition to t2
        assert "DISPATCH:t2:0:" in result.output

    def test_retry_on_partial(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_sequential.yaml")

        _write_observation(ws, "t1", 0, {
            "success": False,
            "signal": "partial",
            "surprise": 0.3,
            "conditions": {
                "completeness": "partial",
                "quality_score": 50,
                "escalate": False,
                "needs_replan": False,
            },
        })

        result = decide(ws)
        # Default rule: retry → DISPATCH:t1:1:
        assert "DISPATCH:t1:1:" in result.output

    def test_escalate_signal(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_sequential.yaml")

        _write_observation(ws, "t1", 0, {
            "success": False,
            "signal": "stuck",
            "surprise": 0.5,
            "conditions": {
                "completeness": "none",
                "escalate": True,
                "needs_replan": False,
            },
        })

        result = decide(ws)
        assert result.output.startswith("ESCALATE:")

    def test_needs_replan_signal(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_sequential.yaml")

        _write_observation(ws, "t1", 0, {
            "success": False,
            "signal": "wrong approach",
            "surprise": 0.8,
            "conditions": {
                "completeness": "partial",
                "escalate": False,
                "needs_replan": True,
            },
        })

        result = decide(ws)
        assert result.output == "REPLAN"
        phase = read_phase(ws)
        assert phase["phase"] == "plan"

    def test_done_on_last_task(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_minimal.yaml")

        _write_observation(ws, "t1", 0, {
            "success": True,
            "signal": "done",
            "surprise": 0.0,
            "conditions": {
                "completeness": "full",
                "quality_score": 100,
                "escalate": False,
                "needs_replan": False,
            },
        })

        result = decide(ws)
        assert result.output == "DONE"


# --- Forced transitions ---


class TestForcedTransitions:
    def test_escalate_forced(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_sequential.yaml")

        cursor = read_cursor(ws)
        cursor["forced_transition"] = {
            "type": "escalate",
            "reason": "loop detected: t1 attempted 3 times",
        }
        write_cursor(ws, cursor)

        result = decide(ws)
        assert result.output.startswith("ESCALATE:")
        assert "loop" in result.output

        # Forced transition should be consumed
        cursor = read_cursor(ws)
        assert cursor["forced_transition"] is None

    def test_replan_forced(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_sequential.yaml")

        cursor = read_cursor(ws)
        cursor["forced_transition"] = {"type": "replan", "reason": "drift detected"}
        write_cursor(ws, cursor)

        result = decide(ws)
        assert result.output == "REPLAN"
        phase = read_phase(ws)
        assert phase["phase"] == "plan"

    def test_verify_or_abort_with_completed(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_sequential.yaml")

        cursor = read_cursor(ws)
        cursor["forced_transition"] = {"type": "verify_or_abort", "reason": "fuel_exhausted"}
        cursor["completed_tasks"] = ["t1"]
        write_cursor(ws, cursor)

        result = decide(ws)
        # Should enter verify phase
        assert "DISPATCH:verify:" in result.output

    def test_verify_or_abort_without_completed(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_sequential.yaml")

        cursor = read_cursor(ws)
        cursor["forced_transition"] = {"type": "verify_or_abort", "reason": "fuel_exhausted"}
        cursor["completed_tasks"] = []
        write_cursor(ws, cursor)

        result = decide(ws)
        assert result.output == "ABORT"


# --- Wait nodes (parallel) ---


class TestWaitNodes:
    def test_blocked_when_not_all_complete(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_parallel.yaml")

        # Navigate to the wait node
        cursor = read_cursor(ws)
        # Find the wait node id
        graph_path = ws / "ctrlflow" / "task-graph.yaml"
        with open(graph_path) as f:
            graph = yaml.safe_load(f)
        wait_nodes = [t for t in graph["tasks"] if "wait_for" in t]
        assert len(wait_nodes) == 1
        wait_id = wait_nodes[0]["id"]

        cursor["current_task"] = wait_id
        cursor["completed_tasks"] = ["t1", "t2a"]  # Missing t2b
        write_cursor(ws, cursor)

        result = decide(ws)
        assert result.output.startswith("BLOCKED:")

    def test_proceeds_when_all_complete(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_parallel.yaml")

        cursor = read_cursor(ws)
        graph_path = ws / "ctrlflow" / "task-graph.yaml"
        with open(graph_path) as f:
            graph = yaml.safe_load(f)
        wait_nodes = [t for t in graph["tasks"] if "wait_for" in t]
        wait_id = wait_nodes[0]["id"]

        cursor["current_task"] = wait_id
        cursor["completed_tasks"] = ["t1", "t2a", "t2b"]
        write_cursor(ws, cursor)

        result = decide(ws)
        # Should proceed to t3
        assert "t3" in result.output


# --- Instruction content ---


class TestInstructionContent:
    def test_exec_instruction_has_action(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_sequential.yaml")
        result = decide(ws)
        inst_path = Path(result.instruction_paths[0])
        content = inst_path.read_text()
        assert "## Action" in content
        assert "## Output" in content
        assert "observations/t1-0.json" in content

    def test_exec_instruction_has_observation_schema(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_sequential.yaml")
        result = decide(ws)
        inst_path = Path(result.instruction_paths[0])
        content = inst_path.read_text()
        assert "## Observation Schema" in content
        assert "quality_score" in content
        assert "completeness" in content

    def test_retry_instruction_has_previous_attempt(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_sequential.yaml")

        # Write first observation with narrative
        _write_observation(ws, "t1", 0, {
            "success": False,
            "signal": "partial",
            "surprise": 0.3,
            "conditions": {"completeness": "partial", "escalate": False, "needs_replan": False},
            "narrative": "Tried webpack analysis but config was minified",
        })

        # Decide should retry
        result = decide(ws)
        assert "DISPATCH:t1:1:" in result.output
        inst_path = Path(result.instruction_paths[0])
        content = inst_path.read_text()
        assert "Previous Attempt" in content
        assert "minified" in content


# --- CLI integration ---


class TestDecideCLI:
    def test_decide_plan_phase(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["init", "--goal", "decide plan test"])
        init_out = capsys.readouterr().out.strip()
        workspace = init_out.split(":")[2]

        main(["decide", "--phase", "plan", "--workspace", workspace])
        out = capsys.readouterr().out.strip()
        assert out.startswith("DISPATCH:plan:")

    def test_decide_after_compile(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["init", "--goal", "decide exec test"])
        init_out = capsys.readouterr().out.strip()
        workspace = Path(init_out.split(":")[2])

        # Copy plan and compile
        plans_dir = workspace / "ctrlflow" / "plans"
        shutil.copy2(FIXTURES / "plan_sequential.yaml", plans_dir / "current.yaml")
        main(["compile-plan", "--workspace", str(workspace)])
        capsys.readouterr()

        # Decide should dispatch first task
        main(["decide", "--workspace", str(workspace)])
        out = capsys.readouterr().out.strip()
        assert out.startswith("DISPATCH:t1:0:")


class TestObserveCLI:
    def test_observe_single(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["init", "--goal", "observe test"])
        init_out = capsys.readouterr().out.strip()
        workspace = Path(init_out.split(":")[2])

        # Write an observation
        obs = {
            "success": True,
            "signal": "done",
            "surprise": 0.1,
            "conditions": {"completeness": "full", "quality_score": 95},
        }
        obs_path = workspace / "dataflow" / "observations" / "t1-0.json"
        obs_path.write_text(json.dumps(obs))

        main(["observe", "--node", "t1", "--attempt", "0", "--workspace", str(workspace)])
        out = capsys.readouterr().out.strip()
        assert "[Step" in out
        assert "completeness=full" in out


# --- Failure summary ---


class TestFailureSummary:
    def test_generates_on_replan(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_sequential.yaml")

        cursor = read_cursor(ws)
        cursor["forced_transition"] = {"type": "replan", "reason": "drift"}
        write_cursor(ws, cursor)

        decide(ws)

        summary_path = ws / "ctrlflow" / "plans" / "failure_summary.json"
        assert summary_path.exists()
        summary = json.loads(summary_path.read_text())
        assert "failed_nodes" in summary
        assert "fuel_remaining" in summary


class TestParallelCursorAdvancement:
    def test_parallel_dispatch_advances_to_wait_node(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_parallel.yaml")

        # Write observation for t1 (success → triggers parallel dispatch [t2a, t2b])
        _write_observation(ws, "t1", 0, {
            "success": True,
            "signal": "done",
            "surprise": 0.0,
            "conditions": {
                "completeness": "full",
                "quality_score": 90,
                "escalate": False,
                "needs_replan": False,
            },
        })

        result = decide(ws)
        assert result.output.startswith("PARALLEL:")

        # Cursor should now point to the wait node, not t1
        cursor = read_cursor(ws)
        assert cursor["current_task"] != "t1"
        assert cursor["pending_parallel"] == ["t2a", "t2b"]

        # Load graph and verify cursor is at the wait node
        graph_path = ws / "ctrlflow" / "task-graph.yaml"
        with open(graph_path) as f:
            graph = yaml.safe_load(f)
        wait_nodes = [t for t in graph["tasks"] if "wait_for" in t]
        assert len(wait_nodes) == 1
        assert cursor["current_task"] == wait_nodes[0]["id"]


class TestFailureSummaryParallel:
    def test_includes_observe_parallel_entries(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_sequential.yaml")

        # Manually write trace entries with observe_parallel action
        from pymh.state import append_trace, now_iso
        append_trace(ws, {
            "timestamp": now_iso(),
            "action": "observe_parallel",
            "task_id": "t2a_t2b",
            "conditions": {"completeness": "partial"},
            "observation_summary": "parallel partial failure",
        })

        # Trigger replan which generates failure_summary
        cursor = read_cursor(ws)
        cursor["forced_transition"] = {"type": "replan", "reason": "drift"}
        write_cursor(ws, cursor)

        decide(ws)

        summary_path = ws / "ctrlflow" / "plans" / "failure_summary.json"
        assert summary_path.exists()
        summary = json.loads(summary_path.read_text())
        assert "t2a_t2b" in summary["failed_nodes"]


class TestDecidePlanTemplate:
    def test_plan_instruction_includes_template(
        self, isolated_mh_root: Path, tmp_path: Path
    ) -> None:
        from pymh.workspace import ensure_mh_root, install_templates

        ensure_mh_root()
        install_templates()

        ws = _make_workspace(tmp_path)
        result = decide(ws, phase_override="plan")
        inst_path = Path(result.output.split(":")[-1])
        content = inst_path.read_text()
        assert "## Template" in content
        assert "general" in content.lower() or "plan:" in content

    def test_plan_instruction_skips_missing_template(
        self, isolated_mh_root: Path, tmp_path: Path
    ) -> None:
        from pymh.workspace import ensure_mh_root

        ensure_mh_root()
        # Don't install templates — template should be gracefully skipped

        ws = _make_workspace(tmp_path)
        result = decide(ws, phase_override="plan")
        inst_path = Path(result.output.split(":")[-1])
        content = inst_path.read_text()
        assert "## Template" not in content

    def test_plan_instruction_uses_specified_template(
        self, isolated_mh_root: Path, tmp_path: Path
    ) -> None:
        from pymh.state import read_meta, write_meta
        from pymh.workspace import ensure_mh_root, install_templates

        ensure_mh_root()
        install_templates()

        ws = _make_workspace(tmp_path)
        meta = read_meta(ws)
        meta["template"] = "migration"
        write_meta(ws, meta)

        result = decide(ws, phase_override="plan")
        inst_path = Path(result.output.split(":")[-1])
        content = inst_path.read_text()
        assert "## Template" in content
        assert "migration" in content.lower()


class TestFinalizeDone:
    def test_done_clears_cursor(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_minimal.yaml")

        _write_observation(ws, "t1", 0, {
            "success": True,
            "signal": "done",
            "surprise": 0.0,
            "conditions": {
                "completeness": "full",
                "quality_score": 100,
                "escalate": False,
                "needs_replan": False,
            },
        })

        result = decide(ws)
        assert result.output == "DONE"

        cursor = read_cursor(ws)
        assert cursor["current_task"] is None
        assert cursor["task_attempts"] == 0

    def test_done_sets_meta_status(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_minimal.yaml")

        _write_observation(ws, "t1", 0, {
            "success": True,
            "signal": "done",
            "surprise": 0.0,
            "conditions": {
                "completeness": "full",
                "quality_score": 100,
                "escalate": False,
                "needs_replan": False,
            },
        })

        from pymh.state import read_meta
        meta = read_meta(ws)
        assert meta["status"] == "running"

        decide(ws)

        meta = read_meta(ws)
        assert meta["status"] == "done"

    def test_done_appends_trace(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_minimal.yaml")

        _write_observation(ws, "t1", 0, {
            "success": True,
            "signal": "done",
            "surprise": 0.0,
            "conditions": {
                "completeness": "full",
                "quality_score": 100,
                "escalate": False,
                "needs_replan": False,
            },
        })

        decide(ws)

        from pymh.state import read_trace
        traces = read_trace(ws)
        done_entries = [t for t in traces if t.get("action") == "done"]
        assert len(done_entries) == 1
        assert done_entries[0]["task_id"] == "test-task"

    def test_done_updates_history(self, isolated_mh_root: Path, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_minimal.yaml")

        # Ensure history.jsonl exists and add a history entry
        from pymh.workspace import ensure_mh_root, get_mh_root
        ensure_mh_root()
        (get_mh_root() / "history.jsonl").touch()
        from pymh.state import append_history
        append_history({"task_id": "test-task", "status": "running", "goal": "test"})

        _write_observation(ws, "t1", 0, {
            "success": True,
            "signal": "done",
            "surprise": 0.0,
            "conditions": {
                "completeness": "full",
                "quality_score": 100,
                "escalate": False,
                "needs_replan": False,
            },
        })

        decide(ws)

        from pymh.state import read_history
        history = read_history()
        entry = next(h for h in history if h["task_id"] == "test-task")
        assert entry["status"] == "done"
        assert "completed" in entry

    def test_no_current_task_finalizes(self, tmp_path: Path) -> None:
        """When cursor has no current_task, decide returns DONE and finalizes."""
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_minimal.yaml")

        # Manually clear current_task
        cursor = read_cursor(ws)
        cursor["current_task"] = None
        write_cursor(ws, cursor)

        result = decide(ws)
        assert result.output == "DONE"

        from pymh.state import read_meta
        meta = read_meta(ws)
        assert meta["status"] == "done"


class TestDoubleDecideIdempotency:
    """MT1: decide called twice without observe should be idempotent."""

    def test_double_decide_same_dispatch(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_sequential.yaml")

        r1 = decide(ws)
        r2 = decide(ws)
        assert r1.output == r2.output
        assert r1.output.startswith("DISPATCH:t1:0:")

        # State should not change between the two calls
        from pymh.state import read_state
        state = read_state(ws)
        assert state["step"] == 0
        assert state["fuel_remaining"] == _DEFAULTS["fuel"]


class TestVerifyOrAbortFinalization:
    """MT2: verify_or_abort with no completed_tasks calls _finalize_abort."""

    def test_abort_finalizes_state(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_sequential.yaml")

        cursor = read_cursor(ws)
        cursor["forced_transition"] = {
            "type": "verify_or_abort",
            "reason": "fuel_exhausted",
        }
        cursor["completed_tasks"] = []
        write_cursor(ws, cursor)

        result = decide(ws)
        assert result.output == "ABORT"

        from pymh.state import read_meta

        meta = read_meta(ws)
        assert meta["status"] == "aborted"

        cursor = read_cursor(ws)
        assert cursor["current_task"] is None


class TestReplanResetsState:
    """MT3: replan resets consecutive_failures and surprise_accumulator."""

    def test_forced_replan_resets_counters(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_sequential.yaml")

        # Simulate some failures in state
        from pymh.state import read_state, write_state
        state = read_state(ws)
        state["consecutive_failures"] = 2
        state["surprise_accumulator"] = 1.5
        write_state(ws, state)

        cursor = read_cursor(ws)
        cursor["forced_transition"] = {"type": "replan", "reason": "drift"}
        write_cursor(ws, cursor)

        decide(ws)

        state = read_state(ws)
        assert state["consecutive_failures"] == 0
        assert state["surprise_accumulator"] == 0.0

    def test_condition_replan_resets_counters(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_sequential.yaml")

        # Simulate failures in state
        from pymh.state import read_state, write_state
        state = read_state(ws)
        state["consecutive_failures"] = 2
        state["surprise_accumulator"] = 1.8
        write_state(ws, state)

        # Write observation that triggers needs_replan
        _write_observation(ws, "t1", 0, {
            "success": False,
            "signal": "wrong approach",
            "surprise": 0.8,
            "conditions": {
                "completeness": "partial",
                "escalate": False,
                "needs_replan": True,
            },
        })

        result = decide(ws)
        assert result.output == "REPLAN"

        state = read_state(ws)
        assert state["consecutive_failures"] == 0
        assert state["surprise_accumulator"] == 0.0


class TestFullInvariantCycle:
    """MT6: Full path from observe invariant → forced_transition → decide consumes."""

    def test_loop_detection_full_cycle(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_sequential.yaml")

        # Set task_attempts to max so observe's invariant check fires
        max_attempts = _DEFAULTS["max_task_attempts"]
        cursor = read_cursor(ws)
        cursor["task_attempts"] = max_attempts
        write_cursor(ws, cursor)

        # Write and observe a failed observation
        _write_observation(ws, "t1", max_attempts, {
            "success": False,
            "signal": "still failing",
            "surprise": 0.3,
            "conditions": {
                "completeness": "partial",
                "escalate": False,
                "needs_replan": False,
            },
        })

        from pymh.observe import process_observation
        result = process_observation(ws, "t1", max_attempts)
        assert result["invariant_fired"] is not None
        assert "loop" in result["invariant_fired"]

        # Cursor should now have forced_transition set
        cursor = read_cursor(ws)
        assert cursor["forced_transition"] is not None
        assert cursor["forced_transition"]["type"] == "escalate"

        # Next decide should consume it
        decide_result = decide(ws)
        assert decide_result.output.startswith("ESCALATE:")
        assert "loop" in decide_result.output

        # forced_transition should be cleared
        cursor = read_cursor(ws)
        assert cursor["forced_transition"] is None

        # escalation.json should exist
        esc_path = ws / "ctrlflow" / "escalation.json"
        assert esc_path.exists()


# --- F1: Verify post-observation logic ---


class TestVerifyPostObservation:
    """F1: After verify observation, decide branches correctly."""

    def _enter_verify(self, ws: Path) -> None:
        """Put workspace in verify phase."""
        phase = read_phase(ws)
        phase["phase"] = "verify"
        write_phase(ws, phase)

    def test_verify_success_finalizes_done(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        self._enter_verify(ws)

        obs_dir = ws / "dataflow" / "observations"
        obs_dir.mkdir(parents=True, exist_ok=True)
        (obs_dir / "verify-0.json").write_text(json.dumps({
            "success": True,
            "signal": "all goals met",
            "surprise": 0.0,
            "conditions": {"completeness": "full"},
        }))

        result = decide(ws)
        assert result.output == "DONE"

        from pymh.state import read_meta
        meta = read_meta(ws)
        assert meta["status"] == "done"

    def test_verify_needs_replan(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        self._enter_verify(ws)

        obs_dir = ws / "dataflow" / "observations"
        obs_dir.mkdir(parents=True, exist_ok=True)
        (obs_dir / "verify-0.json").write_text(json.dumps({
            "success": False,
            "signal": "incomplete",
            "surprise": 0.5,
            "conditions": {"completeness": "partial", "needs_replan": True},
        }))

        result = decide(ws)
        assert result.output == "REPLAN"

    def test_verify_failure_aborts(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        self._enter_verify(ws)

        obs_dir = ws / "dataflow" / "observations"
        obs_dir.mkdir(parents=True, exist_ok=True)
        (obs_dir / "verify-0.json").write_text(json.dumps({
            "success": False,
            "signal": "goal not met",
            "surprise": 0.5,
            "conditions": {"completeness": "none"},
        }))

        result = decide(ws)
        assert result.output == "ABORT"

        from pymh.state import read_meta
        meta = read_meta(ws)
        assert meta["status"] == "aborted"

    def test_verify_no_observation_dispatches(self, tmp_path: Path) -> None:
        """When no verify observation exists, dispatch verify instruction."""
        ws = _make_workspace(tmp_path)
        self._enter_verify(ws)

        result = decide(ws)
        assert result.output.startswith("DISPATCH:verify:")


# --- F2: Parallel failure retry at wait node ---


class TestParallelFailureRetry:
    """F2: Failed parallel member gets retried instead of deadlocking."""

    def test_failed_member_retried(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_parallel.yaml")

        # Advance to wait node with one member completed, one failed
        cursor = read_cursor(ws)
        cursor["current_task"] = "t2a_t2b_wait"
        cursor["completed_tasks"] = ["t1", "t2a"]
        cursor["pending_parallel"] = []
        write_cursor(ws, cursor)

        # Write a failed observation for t2b
        _write_observation(ws, "t2b", 0, {
            "success": False,
            "signal": "failed",
            "surprise": 0.5,
            "conditions": {"completeness": "none"},
        })

        result = decide(ws)
        # Should retry t2b, not return BLOCKED
        assert result.output.startswith("DISPATCH:t2b:")

    def test_exhausted_member_escalates(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_parallel.yaml")

        max_attempts = _DEFAULTS["max_task_attempts"]
        cursor = read_cursor(ws)
        cursor["current_task"] = "t2a_t2b_wait"
        cursor["completed_tasks"] = ["t1", "t2a"]
        cursor["pending_parallel"] = []
        write_cursor(ws, cursor)

        # Write max_attempts failed observations for t2b
        for i in range(max_attempts):
            _write_observation(ws, "t2b", i, {
                "success": False,
                "signal": f"fail {i}",
                "surprise": 0.5,
                "conditions": {"completeness": "none"},
            })

        result = decide(ws)
        assert result.output.startswith("ESCALATE:")
        assert "t2b" in result.output

    def test_unattempted_members_blocked(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_parallel.yaml")

        cursor = read_cursor(ws)
        cursor["current_task"] = "t2a_t2b_wait"
        cursor["completed_tasks"] = ["t1"]
        cursor["pending_parallel"] = []
        write_cursor(ws, cursor)

        # No observations at all for t2a or t2b
        result = decide(ws)
        assert result.output.startswith("BLOCKED:")


# --- F3: Off-by-one retry test ---


class TestRetryOffByOne:
    """F3: max_task_attempts=3 → exactly 3 dispatches then ESCALATE."""

    def test_exactly_max_dispatches(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_sequential.yaml")

        max_attempts = _DEFAULTS["max_task_attempts"]
        dispatches = 0

        for i in range(max_attempts + 2):  # extra iterations to catch off-by-one
            result = decide(ws)
            if result.output.startswith("DISPATCH:"):
                dispatches += 1
                # Write a failed observation
                _write_observation(ws, "t1", i, {
                    "success": False,
                    "signal": f"fail {i}",
                    "surprise": 0.3,
                    "conditions": {"completeness": "none"},
                })
            elif result.output.startswith("ESCALATE:"):
                break

        assert dispatches == max_attempts
        assert result.output.startswith("ESCALATE:")


# --- F4: Fuel exhaustion abort test ---


class TestFuelExhaustionAbort:
    """F4: Fuel exhaustion + no completed tasks → ABORT + status=aborted."""

    def test_abort_generates_report(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_sequential.yaml")

        cursor = read_cursor(ws)
        cursor["forced_transition"] = {
            "type": "verify_or_abort",
            "reason": "fuel_exhausted",
        }
        cursor["completed_tasks"] = []
        write_cursor(ws, cursor)

        result = decide(ws)
        assert result.output == "ABORT"

        # Report should be generated
        report_path = ws / "dataflow" / "artifacts" / "task-report.md"
        assert report_path.exists()

        # Trace should have abort entry
        from pymh.state import read_trace
        traces = read_trace(ws)
        abort_entries = [t for t in traces if t.get("action") == "abort"]
        assert len(abort_entries) >= 1
        assert abort_entries[-1]["source"] == "fuel_exhausted"


# --- F6: Escalation trace test ---


class TestEscalationTrace:
    """F6: Escalation writes trace entry with action='escalate'."""

    def test_escalation_writes_trace(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_sequential.yaml")

        cursor = read_cursor(ws)
        cursor["forced_transition"] = {
            "type": "escalate",
            "reason": "test escalation",
        }
        write_cursor(ws, cursor)

        result = decide(ws)
        assert result.output.startswith("ESCALATE:")

        from pymh.state import read_trace
        traces = read_trace(ws)
        esc_entries = [t for t in traces if t.get("action") == "escalate"]
        assert len(esc_entries) >= 1
        assert esc_entries[-1]["reason"] == "test escalation"
        assert esc_entries[-1]["type"] == "condition_triggered"

    def test_escalation_in_report_timeline(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _compile_plan(ws, "plan_sequential.yaml")

        cursor = read_cursor(ws)
        cursor["forced_transition"] = {
            "type": "escalate",
            "reason": "test escalation",
        }
        write_cursor(ws, cursor)

        decide(ws)

        from pymh.report import generate_report
        report_path = generate_report(ws)
        content = Path(report_path).read_text()
        assert "escalate" in content
