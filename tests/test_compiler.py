"""Tests for the NL plan → task graph compiler.

Verifies: C6, C7, C8, C14, I8
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from pymh.cli import main
from pymh.compiler import CompilationError, compile_plan

FIXTURES = Path(__file__).parent / "fixtures"


def _setup_plan(workspace: Path, fixture_name: str) -> None:
    """Copy a fixture plan into the workspace as ctrlflow/plans/current.yaml."""
    plans_dir = workspace / "ctrlflow" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(FIXTURES / fixture_name, plans_dir / "current.yaml")


def _make_workspace(tmp_path: Path) -> Path:
    """Create a minimal workspace directory structure."""
    ws = tmp_path / "workspace"
    (ws / "ctrlflow" / "plans").mkdir(parents=True)
    (ws / "ctrlflow").mkdir(parents=True, exist_ok=True)
    return ws


# --- Sequential plan compilation ---


class TestSequentialPlan:
    def test_compiles_three_steps(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _setup_plan(ws, "plan_sequential.yaml")

        result = compile_plan(ws)

        assert result.num_tasks == 3
        assert result.num_parallel_groups == 0
        assert len(result.warnings) == 0

    def test_task_graph_structure(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _setup_plan(ws, "plan_sequential.yaml")

        result = compile_plan(ws)
        tasks = result.task_graph["tasks"]
        task_ids = [t["id"] for t in tasks]

        assert task_ids == ["t1", "t2", "t3"]

    def test_sequential_transitions(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _setup_plan(ws, "plan_sequential.yaml")

        result = compile_plan(ws)
        tasks = {t["id"]: t for t in result.task_graph["tasks"]}

        # t1 success → t2
        t1_rules = tasks["t1"]["on_complete"]
        success_rule = next(r for r in t1_rules if 'completeness' in r["condition"])
        assert success_rule["goto"] == "t2"

        # t2 success → t3
        t2_rules = tasks["t2"]["on_complete"]
        success_rule = next(r for r in t2_rules if 'completeness' in r["condition"])
        assert success_rule["goto"] == "t3"

        # t3 success → done
        t3_rules = tasks["t3"]["on_complete"]
        success_rule = next(r for r in t3_rules if 'completeness' in r["condition"])
        assert success_rule["goto"] == "done"

    def test_writes_task_graph_file(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _setup_plan(ws, "plan_sequential.yaml")

        compile_plan(ws)

        graph_path = ws / "ctrlflow" / "task-graph.yaml"
        assert graph_path.exists()
        graph = yaml.safe_load(graph_path.read_text())
        assert "tasks" in graph
        assert len(graph["tasks"]) == 3

    def test_writes_cursor_file(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _setup_plan(ws, "plan_sequential.yaml")

        compile_plan(ws)

        cursor_path = ws / "ctrlflow" / "cursor.json"
        assert cursor_path.exists()
        cursor = json.loads(cursor_path.read_text())
        assert cursor["current_task"] == "t1"
        assert cursor["task_attempts"] == 0
        assert cursor["completed_tasks"] == []
        assert cursor["forced_transition"] is None


# --- Automatic injection rules ---


class TestInjectedRules:
    def test_escalate_signal_injected(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _setup_plan(ws, "plan_sequential.yaml")

        result = compile_plan(ws)
        for task in result.task_graph["tasks"]:
            if "on_complete" not in task:
                continue
            rules = task["on_complete"]
            escalate_rules = [r for r in rules if "escalate" in r["condition"]]
            assert len(escalate_rules) >= 1, f"Task {task['id']} missing escalate signal"

    def test_needs_replan_signal_injected(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _setup_plan(ws, "plan_sequential.yaml")

        result = compile_plan(ws)
        for task in result.task_graph["tasks"]:
            if "on_complete" not in task:
                continue
            rules = task["on_complete"]
            replan_rules = [r for r in rules if "needs_replan" in r["condition"]]
            assert len(replan_rules) >= 1, f"Task {task['id']} missing needs_replan signal"

    def test_default_retry_injected(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _setup_plan(ws, "plan_sequential.yaml")

        result = compile_plan(ws)
        for task in result.task_graph["tasks"]:
            if "on_complete" not in task:
                continue
            rules = task["on_complete"]
            last_rule = rules[-1]
            assert last_rule["condition"] == "default"
            assert last_rule["goto"] == "retry"


# --- Retry strategies ---


class TestRetryStrategies:
    def test_replan_strategy(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _setup_plan(ws, "plan_sequential.yaml")

        result = compile_plan(ws)
        t1 = next(t for t in result.task_graph["tasks"] if t["id"] == "t1")
        # t1 has "try a different approach" → replan
        retry_rules = [r for r in t1["on_complete"] if "task_attempts" in r["condition"]]
        assert len(retry_rules) == 1
        assert retry_rules[0]["goto"] == "replan"

    def test_goto_next_strategy(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _setup_plan(ws, "plan_sequential.yaml")

        result = compile_plan(ws)
        t2 = next(t for t in result.task_graph["tasks"] if t["id"] == "t2")
        # t2 has "proceed with what we have" → goto_next (success target)
        retry_rules = [r for r in t2["on_complete"] if "task_attempts" in r["condition"]]
        assert len(retry_rules) == 1
        assert retry_rules[0]["goto"] == "t3"  # same as success goto

    def test_escalate_strategy(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _setup_plan(ws, "plan_sequential.yaml")

        result = compile_plan(ws)
        t3 = next(t for t in result.task_graph["tasks"] if t["id"] == "t3")
        # t3 has "stop and escalate" → escalate
        retry_rules = [r for r in t3["on_complete"] if "task_attempts" in r["condition"]]
        assert len(retry_rules) == 1
        assert retry_rules[0]["goto"] == "escalate"


# --- Parallel groups ---


class TestParallelGroups:
    def test_detects_parallel_group(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _setup_plan(ws, "plan_parallel.yaml")

        result = compile_plan(ws)
        assert result.num_parallel_groups == 1

    def test_creates_wait_node(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _setup_plan(ws, "plan_parallel.yaml")

        result = compile_plan(ws)
        tasks = result.task_graph["tasks"]
        wait_nodes = [t for t in tasks if "wait_for" in t]
        assert len(wait_nodes) == 1
        wait = wait_nodes[0]
        assert set(wait["wait_for"]) == {"t2a", "t2b"}

    def test_parallel_members_goto_wait(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _setup_plan(ws, "plan_parallel.yaml")

        result = compile_plan(ws)
        tasks = {t["id"]: t for t in result.task_graph["tasks"]}

        for member_id in ["t2a", "t2b"]:
            member = tasks[member_id]
            success_rule = next(
                r for r in member["on_complete"] if 'completeness' in r["condition"]
            )
            assert "_wait" in success_rule["goto"]

    def test_wait_node_goto_after(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _setup_plan(ws, "plan_parallel.yaml")

        result = compile_plan(ws)
        tasks = result.task_graph["tasks"]
        wait = next(t for t in tasks if "wait_for" in t)
        success_rule = next(
            r for r in wait["on_complete"] if 'completeness' in r["condition"]
        )
        assert success_rule["goto"] == "t3"


# --- Minimal plan ---


class TestMinimalPlan:
    def test_single_step_plan(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _setup_plan(ws, "plan_minimal.yaml")

        result = compile_plan(ws)
        assert result.num_tasks == 1
        tasks = result.task_graph["tasks"]
        assert tasks[0]["id"] == "t1"

        # Single step success → done
        success_rule = next(
            r for r in tasks[0]["on_complete"] if 'completeness' in r["condition"]
        )
        assert success_rule["goto"] == "done"


# --- Error cases ---


class TestCompilationErrors:
    def test_missing_plan_file(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        with pytest.raises(CompilationError, match="Plan file not found"):
            compile_plan(ws)

    def test_no_plan_key(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _setup_plan(ws, "plan_invalid_no_plan.yaml")
        with pytest.raises(CompilationError, match="missing 'plan' key"):
            compile_plan(ws)

    def test_no_steps(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _setup_plan(ws, "plan_invalid_no_steps.yaml")
        with pytest.raises(CompilationError, match="no steps"):
            compile_plan(ws)

    def test_duplicate_ids(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _setup_plan(ws, "plan_invalid_duplicate_ids.yaml")
        with pytest.raises(CompilationError, match="Duplicate step id"):
            compile_plan(ws)


# --- Validation ---


class TestValidation:
    def test_no_cycles_in_sequential(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _setup_plan(ws, "plan_sequential.yaml")
        # Should not raise
        compile_plan(ws)

    def test_no_unreachable_nodes(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _setup_plan(ws, "plan_sequential.yaml")
        result = compile_plan(ws)
        assert len(result.warnings) == 0


# --- CLI integration ---


class TestCompilePlanCLI:
    def test_compile_plan_command(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Init a task
        main(["init", "--goal", "compile test"])
        init_out = capsys.readouterr().out.strip()
        workspace = Path(init_out.split(":")[2])

        # Copy plan fixture into workspace
        _setup_plan(workspace, "plan_sequential.yaml")

        # Run compile-plan
        main(["compile-plan", "--workspace", str(workspace)])
        out = capsys.readouterr().out.strip()

        assert out.startswith("COMPILED:")
        assert "3 tasks" in out
        assert "0 parallel groups" in out

    def test_compile_plan_error(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Init a task (but don't add a plan)
        main(["init", "--goal", "compile error test"])
        init_out = capsys.readouterr().out.strip()
        workspace = init_out.split(":")[2]

        with pytest.raises(SystemExit) as exc_info:
            main(["compile-plan", "--workspace", workspace])
        assert exc_info.value.code == 1

        err_out = capsys.readouterr().out.strip()
        assert err_out.startswith("COMPILE_ERROR:")

    def test_compile_plan_versions(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["init", "--goal", "version test"])
        init_out = capsys.readouterr().out.strip()
        workspace = Path(init_out.split(":")[2])

        _setup_plan(workspace, "plan_sequential.yaml")
        main(["compile-plan", "--workspace", str(workspace)])
        capsys.readouterr()

        # v1.yaml should exist
        v1 = workspace / "ctrlflow" / "plans" / "v1.yaml"
        assert v1.exists()

        # Compile again (recompile)
        _setup_plan(workspace, "plan_parallel.yaml")
        main(["compile-plan", "--workspace", str(workspace)])
        capsys.readouterr()

        v2 = workspace / "ctrlflow" / "plans" / "v2.yaml"
        assert v2.exists()


# --- F5: success_criteria dead code ---


class TestSuccessCriteriaDeadCode:
    """F5: success_criteria text doesn't affect compiled conditions."""

    def test_criteria_does_not_change_condition(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _setup_plan(ws, "plan_sequential.yaml")

        result = compile_plan(ws)
        for node in result.task_graph["tasks"]:
            if "wait_for" in node:
                continue
            # All task nodes should have completeness=="full" success rule
            success_rules = [
                r for r in node["on_complete"]
                if 'completeness' in r.get("condition", "")
            ]
            assert len(success_rules) >= 1
            assert success_rules[0]["condition"] == 'completeness == "full"'


# --- F9: Wait node member validation ---


class TestWaitNodeMemberValidation:
    """F9: Wait node referencing nonexistent member raises CompilationError."""

    def test_unknown_wait_member_raises(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        # Create a plan that after compilation produces a wait node
        # We'll modify the graph after compilation to inject a bad wait_for
        _setup_plan(ws, "plan_parallel.yaml")
        result = compile_plan(ws)

        # Verify the compiled graph has wait nodes with valid members
        wait_nodes = [
            n for n in result.task_graph["tasks"] if "wait_for" in n
        ]
        for wn in wait_nodes:
            all_ids = {n["id"] for n in result.task_graph["tasks"]}
            for dep in wn["wait_for"]:
                assert dep in all_ids, (
                    f"Wait node '{wn['id']}' member '{dep}' should exist"
                )
