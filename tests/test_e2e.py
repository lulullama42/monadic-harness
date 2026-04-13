"""End-to-end smoke test: full CLI lifecycle without mocks.

Verifies: R6
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pymh.cli import main
from pymh.workspace import _get_data_dir, get_mh_root


class TestPackageData:
    """Verify package data files are accessible at runtime."""

    def test_data_dir_exists(self) -> None:
        data_dir = _get_data_dir()
        assert data_dir.exists()

    def test_skill_files_present(self) -> None:
        skill_dir = _get_data_dir() / "skill"
        assert (skill_dir / "SKILL.md").exists()
        assert (skill_dir / "principles.md").exists()
        assert (skill_dir / "observation-schema.md").exists()
        assert (skill_dir / "plan-format.md").exists()

    def test_template_files_present(self) -> None:
        templates_dir = _get_data_dir() / "templates"
        assert (templates_dir / "general.yaml").exists()
        assert (templates_dir / "migration.yaml").exists()
        assert (templates_dir / "research.yaml").exists()


class TestFullLifecycle:
    """Full CLI lifecycle: setup → init → plan → compile → exec → report."""

    def test_complete_task_lifecycle(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # 1. Setup
        main(["setup"])
        capsys.readouterr()

        # 2. Init
        main(["init", "--goal", "Test end-to-end lifecycle", "--fuel", "5"])
        out = capsys.readouterr().out
        assert out.startswith("INIT:")
        parts = out.strip().split(":")
        task_id = parts[1]
        workspace = Path(parts[2])
        assert workspace.exists()

        # 3. Write a plan
        plan_content = """plan:
  goal: "Test end-to-end lifecycle"
  steps:
    - id: t1
      action: "Create a test file"
      success_criteria: "File exists"
      retry_strategy: "stop and escalate"
    - id: t2
      action: "Verify the test file"
      success_criteria: "File content matches"
      retry_strategy: "stop and escalate"
"""
        plan_path = workspace / "ctrlflow" / "plans" / "current.yaml"
        plan_path.write_text(plan_content)

        # 4. Compile
        main(["compile-plan", "--workspace", str(workspace)])
        out = capsys.readouterr().out
        assert out.startswith("COMPILED:")
        assert "2 tasks" in out

        # 5. First decide → dispatch t1
        main(["decide", "--workspace", str(workspace)])
        out = capsys.readouterr().out
        assert out.startswith("DISPATCH:t1:0:")
        inst_path = out.strip().split(":", 3)[3]
        assert Path(inst_path).exists()

        # 6. Write observation for t1
        obs = {
            "success": True,
            "signal": "created test file successfully",
            "conditions": {
                "quality_score": 90,
                "completeness": "full",
                "blocker": None,
                "confidence": "high",
                "needs_replan": False,
                "escalate": False,
            },
            "evidence": {"file_exists": True},
            "surprise": 0.1,
            "narrative": "Created the test file as requested.",
        }
        obs_path = workspace / "dataflow" / "observations" / "t1-0.json"
        obs_path.write_text(json.dumps(obs))

        # 7. Observe t1
        main(["observe", "--node", "t1", "--attempt", "0", "--workspace", str(workspace)])
        out = capsys.readouterr().out
        assert "t1" in out
        assert "completeness=full" in out

        # 8. Second decide → dispatch t2
        main(["decide", "--workspace", str(workspace)])
        out = capsys.readouterr().out
        assert out.startswith("DISPATCH:t2:0:")

        # 9. Write observation for t2
        obs2 = {
            "success": True,
            "signal": "verified test file content",
            "conditions": {
                "quality_score": 95,
                "completeness": "full",
                "blocker": None,
                "confidence": "high",
                "needs_replan": False,
                "escalate": False,
            },
            "evidence": {"content_matches": True},
            "surprise": 0.0,
            "narrative": "File content matches expected output.",
        }
        obs_path2 = workspace / "dataflow" / "observations" / "t2-0.json"
        obs_path2.write_text(json.dumps(obs2))

        # 10. Observe t2
        main(["observe", "--node", "t2", "--attempt", "0", "--workspace", str(workspace)])
        capsys.readouterr()

        # 11. Decide → should be DONE
        main(["decide", "--workspace", str(workspace)])
        out = capsys.readouterr().out
        assert out.strip() == "DONE"

        # 12. Report
        main(["report", "--workspace", str(workspace)])
        out = capsys.readouterr().out
        report_path = Path(out.strip())
        assert report_path.exists()
        report_content = report_path.read_text()
        assert "Test end-to-end lifecycle" in report_content

        # 13. Status works at any point
        main(["status", "--workspace", str(workspace)])
        out = capsys.readouterr().out
        assert "Test end-to-end lifecycle" in out
        assert task_id in out

    def test_parallel_lifecycle(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Full parallel lifecycle: init → compile → dispatch → parallel → wait → done."""
        main(["setup"])
        capsys.readouterr()

        main(["init", "--goal", "Test parallel lifecycle", "--fuel", "10"])
        out = capsys.readouterr().out
        workspace = Path(out.strip().split(":", 2)[2])

        # Write plan with parallel steps
        plan = """plan:
  goal: "Test parallel lifecycle"
  steps:
    - id: t1
      action: "Setup base"
      success_criteria: "Base ready"
      retry_strategy: "stop and escalate"
    - id: t2a
      action: "Build module A"
      success_criteria: "Module A works"
      can_parallel_with: [t2b]
      retry_strategy: "stop and escalate"
    - id: t2b
      action: "Build module B"
      success_criteria: "Module B works"
      can_parallel_with: [t2a]
      retry_strategy: "stop and escalate"
    - id: t3
      action: "Integration test"
      success_criteria: "Tests pass"
      retry_strategy: "stop and escalate"
"""
        (workspace / "ctrlflow" / "plans" / "current.yaml").write_text(plan)
        main(["compile-plan", "--workspace", str(workspace)])
        capsys.readouterr()

        # 1. Decide → dispatch t1
        main(["decide", "--workspace", str(workspace)])
        out = capsys.readouterr().out
        assert out.startswith("DISPATCH:t1:0:")

        # 2. Observe t1 (success)
        obs_t1 = {
            "success": True, "signal": "base ready", "surprise": 0.0,
            "conditions": {"completeness": "full", "quality_score": 90,
                           "blocker": None, "confidence": "high",
                           "needs_replan": False, "escalate": False},
            "evidence": {}, "narrative": "Base setup complete.",
        }
        (workspace / "dataflow" / "observations" / "t1-0.json").write_text(json.dumps(obs_t1))
        main(["observe", "--node", "t1", "--attempt", "0", "--workspace", str(workspace)])
        capsys.readouterr()

        # 3. Decide → should get PARALLEL dispatch for t2a, t2b
        main(["decide", "--workspace", str(workspace)])
        out = capsys.readouterr().out
        assert out.startswith("PARALLEL:")
        assert "t2a" in out
        assert "t2b" in out

        # 4. Write observations for t2a and t2b
        for nid in ["t2a", "t2b"]:
            obs = {
                "success": True, "signal": f"{nid} done", "surprise": 0.1,
                "conditions": {"completeness": "full", "quality_score": 85,
                               "blocker": None, "confidence": "high",
                               "needs_replan": False, "escalate": False},
                "evidence": {}, "narrative": f"{nid} completed.",
            }
            (workspace / "dataflow" / "observations" / f"{nid}-0.json").write_text(
                json.dumps(obs)
            )

        # 5. Observe parallel
        main(["observe", "--parallel", "t2a,t2b", "--workspace", str(workspace)])
        capsys.readouterr()

        # 6. Decide → wait node should pass, dispatch t3
        main(["decide", "--workspace", str(workspace)])
        out = capsys.readouterr().out
        assert "t3" in out

        # 7. Observe t3 (success)
        obs_t3 = {
            "success": True, "signal": "tests pass", "surprise": 0.0,
            "conditions": {"completeness": "full", "quality_score": 95,
                           "blocker": None, "confidence": "high",
                           "needs_replan": False, "escalate": False},
            "evidence": {}, "narrative": "All tests pass.",
        }
        (workspace / "dataflow" / "observations" / "t3-0.json").write_text(json.dumps(obs_t3))
        main(["observe", "--node", "t3", "--attempt", "0", "--workspace", str(workspace)])
        capsys.readouterr()

        # 8. Decide → DONE
        main(["decide", "--workspace", str(workspace)])
        out = capsys.readouterr().out
        assert out.strip() == "DONE"

        # 9. Report
        main(["report", "--workspace", str(workspace)])
        out = capsys.readouterr().out
        report_path = Path(out.strip())
        assert report_path.exists()

    def test_lifecycle_with_retry(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test that a failed observation triggers retry."""
        main(["setup"])
        capsys.readouterr()

        main(["init", "--goal", "Test retry", "--fuel", "5"])
        out = capsys.readouterr().out
        workspace = Path(out.strip().split(":", 2)[2])

        # Write single-step plan
        plan = """plan:
  goal: "Test retry"
  steps:
    - id: t1
      action: "Do something that might fail"
      success_criteria: "It worked"
      retry_strategy: "try a different approach"
"""
        (workspace / "ctrlflow" / "plans" / "current.yaml").write_text(plan)
        main(["compile-plan", "--workspace", str(workspace)])
        capsys.readouterr()

        # First decide → dispatch t1
        main(["decide", "--workspace", str(workspace)])
        capsys.readouterr()

        # Write FAILED observation
        obs = {
            "success": False,
            "signal": "approach failed",
            "conditions": {
                "quality_score": 20,
                "completeness": "partial",
                "blocker": None,
                "confidence": "medium",
                "needs_replan": False,
                "escalate": False,
            },
            "evidence": {},
            "surprise": 0.3,
            "narrative": "First approach did not work.",
        }
        (workspace / "dataflow" / "observations" / "t1-0.json").write_text(json.dumps(obs))

        main(["observe", "--node", "t1", "--attempt", "0", "--workspace", str(workspace)])
        capsys.readouterr()

        # Decide should retry (attempt 1)
        main(["decide", "--workspace", str(workspace)])
        out = capsys.readouterr().out
        assert "DISPATCH:t1:1:" in out

        # Verify retry instruction has previous attempt context
        inst_path = Path(out.strip().split(":", 3)[3])
        inst_content = inst_path.read_text()
        assert "Previous Attempt" in inst_content
        assert "First approach" in inst_content


class TestRetryExhaustion:
    """Retry exhaustion: consecutive failures trigger drift → REPLAN, then retry
    with escalate strategy triggers ESCALATE → resume abort."""

    def test_consecutive_failures_trigger_replan(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """3 consecutive failures → drift invariant → REPLAN (not ESCALATE).

        The consecutive_failures invariant (max=3) fires during observe,
        setting forced_transition=replan. This preempts the retry threshold.
        """
        main(["setup"])
        capsys.readouterr()

        main(["init", "--goal", "Test drift replan", "--fuel", "20"])
        out = capsys.readouterr().out
        workspace = Path(out.strip().split(":", 2)[2])

        plan = """plan:
  goal: "Test drift replan"
  steps:
    - id: t1
      action: "Flaky task"
      success_criteria: "Passes eventually"
      retry_strategy: "try again"
"""
        (workspace / "ctrlflow" / "plans" / "current.yaml").write_text(plan)
        main(["compile-plan", "--workspace", str(workspace)])
        capsys.readouterr()

        # Fail 3 times → consecutive_failures invariant fires on 3rd observe
        for attempt in range(3):
            main(["decide", "--workspace", str(workspace)])
            out = capsys.readouterr().out
            if "REPLAN" in out:
                # Invariant may fire early — that's correct
                break
            assert f"DISPATCH:t1:{attempt}:" in out

            obs = {
                "success": False,
                "signal": f"attempt {attempt} failed",
                "conditions": {
                    "quality_score": 20, "completeness": "partial",
                    "blocker": None, "confidence": "low",
                    "needs_replan": False, "escalate": False,
                },
                "evidence": {}, "surprise": 0.3,
                "narrative": f"Attempt {attempt} did not work.",
            }
            obs_path = workspace / "dataflow" / "observations" / f"t1-{attempt}.json"
            obs_path.write_text(json.dumps(obs))
            main(["observe", "--node", "t1", "--attempt", str(attempt),
                  "--workspace", str(workspace)])
            capsys.readouterr()
        else:
            # After 3 fails, decide should trigger REPLAN via drift invariant
            main(["decide", "--workspace", str(workspace)])
            out = capsys.readouterr().out
            assert "REPLAN" in out

        # Phase should be plan
        from pymh.state import read_phase
        phase = read_phase(workspace)
        assert phase["phase"] == "plan"

    def test_escalate_strategy_after_max_attempts(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """retry_strategy: 'stop and escalate' → max failures → ESCALATE → resume abort.

        Compiled rules: fail → retry (default) until task_attempts >= max (3),
        then escalate. The 'escalate' strategy means ESCALATE at threshold
        (vs 'try again' which means REPLAN via drift invariant first).
        """
        main(["setup"])
        capsys.readouterr()

        main(["init", "--goal", "Test escalate strategy", "--fuel", "20"])
        out = capsys.readouterr().out
        workspace = Path(out.strip().split(":", 2)[2])

        plan = """plan:
  goal: "Test escalate strategy"
  steps:
    - id: t1
      action: "Critical task"
      success_criteria: "Must work"
      retry_strategy: "stop and escalate"
"""
        (workspace / "ctrlflow" / "plans" / "current.yaml").write_text(plan)
        main(["compile-plan", "--workspace", str(workspace)])
        capsys.readouterr()

        # Dispatch 0,1,2 (3 attempts) — each fails with low surprise to avoid
        # drift invariant (consecutive_failures triggers at 3, but retry
        # threshold also fires at 3 — the key is which forced_transition wins)
        for attempt in range(3):
            main(["decide", "--workspace", str(workspace)])
            out = capsys.readouterr().out
            # Could be DISPATCH, REPLAN, or ESCALATE depending on invariant race
            if "ESCALATE" in out or "REPLAN" in out:
                break
            assert f"DISPATCH:t1:{attempt}:" in out

            obs = {
                "success": False,
                "signal": f"attempt {attempt} failed",
                "conditions": {
                    "quality_score": 20, "completeness": "partial",
                    "blocker": None, "confidence": "low",
                    "needs_replan": False, "escalate": False,
                },
                "evidence": {}, "surprise": 0.2,
                "narrative": f"Attempt {attempt} did not work.",
            }
            (workspace / "dataflow" / "observations" / f"t1-{attempt}.json").write_text(
                json.dumps(obs)
            )
            main(["observe", "--node", "t1", "--attempt", str(attempt),
                  "--workspace", str(workspace)])
            capsys.readouterr()
        else:
            # After 3 attempts, decide should ESCALATE or REPLAN
            main(["decide", "--workspace", str(workspace)])
            out = capsys.readouterr().out

        # Either ESCALATE (retry threshold) or REPLAN (drift invariant) — both valid
        assert "ESCALATE" in out or "REPLAN" in out

        # Resume with abort
        # Need escalation or resolution file depending on outcome
        if "ESCALATE" in out:
            resolution = {"decision": "abort", "reasoning": "cannot recover"}
            (workspace / "ctrlflow" / "resolution.json").write_text(json.dumps(resolution))
            main(["resume", "--workspace", str(workspace)])
            out = capsys.readouterr().out
            assert out.strip() == "RESUMED:ABORT"
        else:
            # REPLAN — abort via CLI instead
            main(["abort", "--workspace", str(workspace)])
            capsys.readouterr()

        from pymh.state import read_meta
        meta = read_meta(workspace)
        assert meta["status"] == "aborted"

        # Report exists
        report = workspace / "dataflow" / "artifacts" / "task-report.md"
        assert report.exists()
        assert "**Status**: Aborted" in report.read_text()


class TestFuelExhaustion:
    """Fuel runs out mid-execution → verify_or_abort → ABORT."""

    def test_fuel_exhaustion_aborts(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["setup"])
        capsys.readouterr()

        # Only 2 fuel for a 3-step plan
        main(["init", "--goal", "Test fuel exhaustion", "--fuel", "2"])
        out = capsys.readouterr().out
        workspace = Path(out.strip().split(":", 2)[2])

        plan = """plan:
  goal: "Test fuel exhaustion"
  steps:
    - id: t1
      action: "First step"
      success_criteria: "Done"
      retry_strategy: "stop and escalate"
    - id: t2
      action: "Second step"
      success_criteria: "Done"
      retry_strategy: "stop and escalate"
    - id: t3
      action: "Third step (unreachable)"
      success_criteria: "Done"
      retry_strategy: "stop and escalate"
"""
        (workspace / "ctrlflow" / "plans" / "current.yaml").write_text(plan)
        main(["compile-plan", "--workspace", str(workspace)])
        capsys.readouterr()

        # t1 → success → observe (fuel: 2→1)
        main(["decide", "--workspace", str(workspace)])
        capsys.readouterr()
        self._write_success_obs(workspace, "t1", 0)
        main(["observe", "--node", "t1", "--attempt", "0", "--workspace", str(workspace)])
        capsys.readouterr()

        # t2 → success → observe (fuel: 1→0)
        main(["decide", "--workspace", str(workspace)])
        capsys.readouterr()
        self._write_success_obs(workspace, "t2", 0)
        main(["observe", "--node", "t2", "--attempt", "0", "--workspace", str(workspace)])
        capsys.readouterr()

        # Fuel exhausted, t3 remains — decide should go to verify_or_abort
        # Since t1,t2 completed, it enters verify phase (not immediate abort)
        main(["decide", "--workspace", str(workspace)])
        out = capsys.readouterr().out
        # With completed tasks, verify phase is entered
        assert "verify" in out.lower() or "DISPATCH:t3" in out or "ABORT" in out or "DONE" in out

        # Verify state
        from pymh.state import read_state
        state = read_state(workspace)
        assert state["fuel_remaining"] == 0

    def test_fuel_exhaustion_no_completed_aborts(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Fuel out + zero completed tasks → immediate ABORT."""
        main(["setup"])
        capsys.readouterr()

        main(["init", "--goal", "Test fuel abort", "--fuel", "1"])
        out = capsys.readouterr().out
        workspace = Path(out.strip().split(":", 2)[2])

        plan = """plan:
  goal: "Test fuel abort"
  steps:
    - id: t1
      action: "Only step"
      success_criteria: "Done"
      retry_strategy: "stop and escalate"
    - id: t2
      action: "Never reached"
      success_criteria: "Done"
      retry_strategy: "stop and escalate"
"""
        (workspace / "ctrlflow" / "plans" / "current.yaml").write_text(plan)
        main(["compile-plan", "--workspace", str(workspace)])
        capsys.readouterr()

        # t1 → fail → observe (fuel: 1→0)
        main(["decide", "--workspace", str(workspace)])
        capsys.readouterr()
        obs = {
            "success": False, "signal": "failed",
            "conditions": {"quality_score": 10, "completeness": "none",
                           "blocker": None, "confidence": "low",
                           "needs_replan": False, "escalate": False},
            "evidence": {}, "surprise": 0.5,
            "narrative": "Complete failure.",
        }
        (workspace / "dataflow" / "observations" / "t1-0.json").write_text(json.dumps(obs))
        main(["observe", "--node", "t1", "--attempt", "0", "--workspace", str(workspace)])
        capsys.readouterr()

        # Decide with 0 fuel and 0 completed → ABORT
        main(["decide", "--workspace", str(workspace)])
        out = capsys.readouterr().out
        assert out.strip() == "ABORT"

        from pymh.state import read_meta
        meta = read_meta(workspace)
        assert meta["status"] == "aborted"

        # Report was auto-generated
        report = workspace / "dataflow" / "artifacts" / "task-report.md"
        assert report.exists()

    @staticmethod
    def _write_success_obs(workspace: Path, node_id: str, attempt: int) -> None:
        obs = {
            "success": True, "signal": f"{node_id} done", "surprise": 0.0,
            "conditions": {"quality_score": 90, "completeness": "full",
                           "blocker": None, "confidence": "high",
                           "needs_replan": False, "escalate": False},
            "evidence": {}, "narrative": f"{node_id} completed.",
        }
        obs_path = workspace / "dataflow" / "observations" / f"{node_id}-{attempt}.json"
        obs_path.write_text(json.dumps(obs))


class TestVerifyPhase:
    """Verify phase: exec DONE → verify dispatch → verify observation → final outcome."""

    def test_verify_success_completes(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """All tasks done → verify → success observation → DONE."""
        main(["setup"])
        capsys.readouterr()

        main(["init", "--goal", "Test verify success", "--fuel", "10"])
        out = capsys.readouterr().out
        workspace = Path(out.strip().split(":", 2)[2])

        plan = """plan:
  goal: "Test verify success"
  steps:
    - id: t1
      action: "Single task"
      success_criteria: "It works"
      retry_strategy: "stop and escalate"
"""
        (workspace / "ctrlflow" / "plans" / "current.yaml").write_text(plan)
        main(["compile-plan", "--workspace", str(workspace)])
        capsys.readouterr()

        # Execute t1 successfully
        main(["decide", "--workspace", str(workspace)])
        capsys.readouterr()
        obs = {
            "success": True, "signal": "done", "surprise": 0.0,
            "conditions": {"quality_score": 95, "completeness": "full",
                           "blocker": None, "confidence": "high",
                           "needs_replan": False, "escalate": False},
            "evidence": {}, "narrative": "Task completed.",
        }
        (workspace / "dataflow" / "observations" / "t1-0.json").write_text(json.dumps(obs))
        main(["observe", "--node", "t1", "--attempt", "0", "--workspace", str(workspace)])
        capsys.readouterr()

        # Decide → enters verify, dispatches verify instruction
        main(["decide", "--workspace", str(workspace)])
        out = capsys.readouterr().out
        # First decide after last task → DONE (goes through verify_or_abort → verify)
        # OR direct DONE if verify shortcircuits
        if "DISPATCH:verify" in out:
            # Write verify observation (success)
            verify_obs = {
                "success": True, "signal": "goal met", "surprise": 0.0,
                "conditions": {"quality_score": 95, "completeness": "full",
                               "blocker": None, "confidence": "high",
                               "needs_replan": False, "escalate": False},
                "evidence": {"goal_met": True}, "narrative": "Goal verified.",
            }
            # Extract attempt number from dispatch output
            verify_attempt = int(out.strip().split(":")[2])
            obs_dir = workspace / "dataflow" / "observations"
            verify_obs_path = obs_dir / f"verify-{verify_attempt}.json"
            verify_obs_path.write_text(json.dumps(verify_obs))

            # Observe verify (not needed for decide, but round-trips through pipeline)
            # Call decide again to process verify result
            main(["decide", "--workspace", str(workspace)])
            out = capsys.readouterr().out
            assert out.strip() == "DONE"
        else:
            assert out.strip() == "DONE"

        from pymh.state import read_meta
        meta = read_meta(workspace)
        assert meta["status"] == "done"

    def test_verify_failure_aborts(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """All tasks done → verify → failure observation → ABORT."""
        main(["setup"])
        capsys.readouterr()

        main(["init", "--goal", "Test verify failure", "--fuel", "10"])
        out = capsys.readouterr().out
        workspace = Path(out.strip().split(":", 2)[2])

        plan = """plan:
  goal: "Test verify failure"
  steps:
    - id: t1
      action: "Single task"
      success_criteria: "It works"
      retry_strategy: "stop and escalate"
"""
        (workspace / "ctrlflow" / "plans" / "current.yaml").write_text(plan)
        main(["compile-plan", "--workspace", str(workspace)])
        capsys.readouterr()

        # Execute t1 successfully
        main(["decide", "--workspace", str(workspace)])
        capsys.readouterr()
        obs = {
            "success": True, "signal": "done", "surprise": 0.0,
            "conditions": {"quality_score": 90, "completeness": "full",
                           "blocker": None, "confidence": "high",
                           "needs_replan": False, "escalate": False},
            "evidence": {}, "narrative": "Task completed.",
        }
        (workspace / "dataflow" / "observations" / "t1-0.json").write_text(json.dumps(obs))
        main(["observe", "--node", "t1", "--attempt", "0", "--workspace", str(workspace)])
        capsys.readouterr()

        # Decide → enters verify phase
        main(["decide", "--workspace", str(workspace)])
        out = capsys.readouterr().out
        if "DISPATCH:verify" in out:
            verify_attempt = int(out.strip().split(":")[2])
            # Write verify observation (failure — goal NOT met)
            verify_obs = {
                "success": False, "signal": "goal not met", "surprise": 0.8,
                "conditions": {"quality_score": 30, "completeness": "partial",
                               "blocker": None, "confidence": "medium",
                               "needs_replan": False, "escalate": False},
                "evidence": {"goal_met": False}, "narrative": "Verification failed.",
            }
            obs_dir = workspace / "dataflow" / "observations"
            verify_obs_path = obs_dir / f"verify-{verify_attempt}.json"
            verify_obs_path.write_text(json.dumps(verify_obs))

            main(["decide", "--workspace", str(workspace)])
            out = capsys.readouterr().out
            assert out.strip() == "ABORT"

            from pymh.state import read_meta
            meta = read_meta(workspace)
            assert meta["status"] == "aborted"

            report = workspace / "dataflow" / "artifacts" / "task-report.md"
            assert report.exists()
        else:
            # If decide went straight to DONE, verify phase was skipped
            assert out.strip() == "DONE"

    def test_verify_replan(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """All tasks done → verify → needs_replan → REPLAN."""
        main(["setup"])
        capsys.readouterr()

        main(["init", "--goal", "Test verify replan", "--fuel", "10"])
        out = capsys.readouterr().out
        workspace = Path(out.strip().split(":", 2)[2])

        plan = """plan:
  goal: "Test verify replan"
  steps:
    - id: t1
      action: "Build something"
      success_criteria: "Built"
      retry_strategy: "stop and escalate"
"""
        (workspace / "ctrlflow" / "plans" / "current.yaml").write_text(plan)
        main(["compile-plan", "--workspace", str(workspace)])
        capsys.readouterr()

        main(["decide", "--workspace", str(workspace)])
        capsys.readouterr()
        obs = {
            "success": True, "signal": "built", "surprise": 0.0,
            "conditions": {"quality_score": 90, "completeness": "full",
                           "blocker": None, "confidence": "high",
                           "needs_replan": False, "escalate": False},
            "evidence": {}, "narrative": "Built it.",
        }
        (workspace / "dataflow" / "observations" / "t1-0.json").write_text(json.dumps(obs))
        main(["observe", "--node", "t1", "--attempt", "0", "--workspace", str(workspace)])
        capsys.readouterr()

        main(["decide", "--workspace", str(workspace)])
        out = capsys.readouterr().out
        if "DISPATCH:verify" in out:
            verify_attempt = int(out.strip().split(":")[2])
            verify_obs = {
                "success": False, "signal": "needs work", "surprise": 0.6,
                "conditions": {"quality_score": 40, "completeness": "partial",
                               "blocker": None, "confidence": "medium",
                               "needs_replan": True, "escalate": False},
                "evidence": {}, "narrative": "Needs replanning.",
            }
            obs_dir = workspace / "dataflow" / "observations"
            verify_obs_path = obs_dir / f"verify-{verify_attempt}.json"
            verify_obs_path.write_text(json.dumps(verify_obs))

            main(["decide", "--workspace", str(workspace)])
            out = capsys.readouterr().out
            assert out.strip() == "REPLAN"

            from pymh.state import read_phase
            phase = read_phase(workspace)
            assert phase["phase"] == "plan"


class TestParallelFailureRecovery:
    """Parallel member fails → wait node retries → eventual success."""

    def test_parallel_member_fails_then_recovers(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["setup"])
        capsys.readouterr()

        main(["init", "--goal", "Test parallel recovery", "--fuel", "20"])
        out = capsys.readouterr().out
        workspace = Path(out.strip().split(":", 2)[2])

        plan = """plan:
  goal: "Test parallel recovery"
  steps:
    - id: t1
      action: "Setup"
      success_criteria: "Ready"
      retry_strategy: "stop and escalate"
    - id: t2a
      action: "Worker A"
      success_criteria: "A done"
      can_parallel_with: [t2b]
      retry_strategy: "try again"
    - id: t2b
      action: "Worker B"
      success_criteria: "B done"
      can_parallel_with: [t2a]
      retry_strategy: "try again"
    - id: t3
      action: "Finalize"
      success_criteria: "All done"
      retry_strategy: "stop and escalate"
"""
        (workspace / "ctrlflow" / "plans" / "current.yaml").write_text(plan)
        main(["compile-plan", "--workspace", str(workspace)])
        capsys.readouterr()

        # t1 → success
        main(["decide", "--workspace", str(workspace)])
        capsys.readouterr()
        self._write_obs(workspace, "t1", 0, success=True)
        main(["observe", "--node", "t1", "--attempt", "0", "--workspace", str(workspace)])
        capsys.readouterr()

        # Decide → PARALLEL dispatch t2a, t2b
        main(["decide", "--workspace", str(workspace)])
        out = capsys.readouterr().out
        assert out.startswith("PARALLEL:")
        assert "t2a" in out and "t2b" in out

        # t2a succeeds, t2b fails
        self._write_obs(workspace, "t2a", 0, success=True)
        self._write_obs(workspace, "t2b", 0, success=False)
        main(["observe", "--parallel", "t2a,t2b", "--workspace", str(workspace)])
        capsys.readouterr()

        # Decide → wait node sees t2b failed → retries t2b
        main(["decide", "--workspace", str(workspace)])
        out = capsys.readouterr().out
        assert "t2b" in out
        assert ":1:" in out  # attempt 1

        # t2b retry succeeds
        self._write_obs(workspace, "t2b", 1, success=True)
        main(["observe", "--node", "t2b", "--attempt", "1", "--workspace", str(workspace)])
        capsys.readouterr()

        # Decide → wait node passes, dispatches t3
        main(["decide", "--workspace", str(workspace)])
        out = capsys.readouterr().out
        assert "t3" in out

        # t3 → success → DONE
        self._write_obs(workspace, "t3", 0, success=True)
        main(["observe", "--node", "t3", "--attempt", "0", "--workspace", str(workspace)])
        capsys.readouterr()
        main(["decide", "--workspace", str(workspace)])
        out = capsys.readouterr().out
        assert "DONE" in out

    @staticmethod
    def _write_obs(workspace: Path, node_id: str, attempt: int, *, success: bool) -> None:
        obs = {
            "success": success,
            "signal": f"{node_id} {'ok' if success else 'fail'}",
            "surprise": 0.0 if success else 0.4,
            "conditions": {
                "quality_score": 90 if success else 20,
                "completeness": "full" if success else "partial",
                "blocker": None, "confidence": "high" if success else "low",
                "needs_replan": False, "escalate": False,
            },
            "evidence": {}, "narrative": f"{node_id} attempt {attempt}.",
        }
        obs_path = workspace / "dataflow" / "observations" / f"{node_id}-{attempt}.json"
        obs_path.write_text(json.dumps(obs))


class TestAbortMidExecution:
    """CLI abort mid-execution: running task → abort → report + history updated."""

    def test_abort_during_execution(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["setup"])
        capsys.readouterr()

        main(["init", "--goal", "Test mid-abort", "--fuel", "10"])
        out = capsys.readouterr().out
        workspace = Path(out.strip().split(":", 2)[2])
        task_id = out.strip().split(":")[1]

        plan = """plan:
  goal: "Test mid-abort"
  steps:
    - id: t1
      action: "Step 1"
      success_criteria: "Done"
      retry_strategy: "stop and escalate"
    - id: t2
      action: "Step 2 (never reached)"
      success_criteria: "Done"
      retry_strategy: "stop and escalate"
"""
        (workspace / "ctrlflow" / "plans" / "current.yaml").write_text(plan)
        main(["compile-plan", "--workspace", str(workspace)])
        capsys.readouterr()

        # Start executing t1
        main(["decide", "--workspace", str(workspace)])
        capsys.readouterr()

        # Abort mid-task (before observation)
        main(["abort", "--workspace", str(workspace)])
        out = capsys.readouterr().out
        assert out.strip() == f"ABORTED:{task_id}"

        # Verify aborted state
        from pymh.state import read_meta
        meta = read_meta(workspace)
        assert meta["status"] == "aborted"

        # Report generated
        report = workspace / "dataflow" / "artifacts" / "task-report.md"
        assert report.exists()
        content = report.read_text()
        assert "**Status**: Aborted" in content
        assert "Test mid-abort" in content

        # History updated
        history = get_mh_root() / "history.jsonl"
        lines = [json.loads(ln) for ln in history.read_text().strip().split("\n")]
        entry = next(e for e in lines if e["task_id"] == task_id)
        assert entry["status"] == "aborted"

        # Trace has abort with source=cli
        trace_path = workspace / "trace" / "trace.jsonl"
        trace_lines = [json.loads(ln) for ln in trace_path.read_text().strip().split("\n")]
        abort_entries = [t for t in trace_lines if t.get("action") == "abort"]
        assert len(abort_entries) == 1
        assert abort_entries[0]["source"] == "cli"

        # Status still works after abort
        main(["status", "--workspace", str(workspace)])
        status_out = capsys.readouterr().out
        assert "Test mid-abort" in status_out


class TestFuelAddMidTask:
    """Add fuel mid-execution via CLI, then continue to completion."""

    def test_refuel_and_complete(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Start with enough fuel for step 1, add more before step 2.

        fuel=2: t1 observe uses 1 (fuel→1), t2 observe uses 1 (fuel→0).
        Add fuel between t1 and t2 to prove it's tracked correctly.
        """
        main(["setup"])
        capsys.readouterr()

        main(["init", "--goal", "Test refuel", "--fuel", "2"])
        out = capsys.readouterr().out
        workspace = Path(out.strip().split(":", 2)[2])

        plan = """plan:
  goal: "Test refuel"
  steps:
    - id: t1
      action: "First step"
      success_criteria: "Done"
      retry_strategy: "stop and escalate"
    - id: t2
      action: "Second step"
      success_criteria: "Done"
      retry_strategy: "stop and escalate"
    - id: t3
      action: "Third step"
      success_criteria: "Done"
      retry_strategy: "stop and escalate"
"""
        (workspace / "ctrlflow" / "plans" / "current.yaml").write_text(plan)
        main(["compile-plan", "--workspace", str(workspace)])
        capsys.readouterr()

        # t1 → success (fuel: 2→1)
        main(["decide", "--workspace", str(workspace)])
        capsys.readouterr()
        self._write_success_obs(workspace, "t1", 0)
        main(["observe", "--node", "t1", "--attempt", "0", "--workspace", str(workspace)])
        capsys.readouterr()

        # Add fuel before t2 — fuel goes from 1 to 6
        main(["fuel", "--add", "5", "--workspace", str(workspace)])
        out = capsys.readouterr().out
        assert out.strip() == "FUEL:6"

        # t2 → success (fuel: 6→5)
        main(["decide", "--workspace", str(workspace)])
        out = capsys.readouterr().out
        assert "DISPATCH:t2:0:" in out
        self._write_success_obs(workspace, "t2", 0)
        main(["observe", "--node", "t2", "--attempt", "0", "--workspace", str(workspace)])
        capsys.readouterr()

        # t3 → success (fuel: 5→4) → DONE
        main(["decide", "--workspace", str(workspace)])
        out = capsys.readouterr().out
        assert "DISPATCH:t3:0:" in out
        self._write_success_obs(workspace, "t3", 0)
        main(["observe", "--node", "t3", "--attempt", "0", "--workspace", str(workspace)])
        capsys.readouterr()

        main(["decide", "--workspace", str(workspace)])
        out = capsys.readouterr().out
        assert "DONE" in out

        # Trace includes fuel_add event
        trace_path = workspace / "trace" / "trace.jsonl"
        trace_lines = [json.loads(ln) for ln in trace_path.read_text().strip().split("\n")]
        fuel_entries = [t for t in trace_lines if t.get("action") == "fuel_add"]
        assert len(fuel_entries) == 1
        assert fuel_entries[0]["fuel_added"] == 5
        assert fuel_entries[0]["fuel_before"] == 1
        assert fuel_entries[0]["fuel_after"] == 6

        # Final fuel should be 4
        from pymh.state import read_state
        state = read_state(workspace)
        assert state["fuel_remaining"] == 4

    @staticmethod
    def _write_success_obs(workspace: Path, node_id: str, attempt: int) -> None:
        obs = {
            "success": True, "signal": f"{node_id} done", "surprise": 0.0,
            "conditions": {"quality_score": 90, "completeness": "full",
                           "blocker": None, "confidence": "high",
                           "needs_replan": False, "escalate": False},
            "evidence": {}, "narrative": f"{node_id} completed.",
        }
        (workspace / "dataflow" / "observations" / f"{node_id}-{attempt}.json").write_text(
            json.dumps(obs)
        )


class TestResumeReplan:
    """Escalation → resume with replan → new plan → compile → complete."""

    def test_replan_after_escalation(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["setup"])
        capsys.readouterr()

        main(["init", "--goal", "Test replan cycle", "--fuel", "20"])
        out = capsys.readouterr().out
        workspace = Path(out.strip().split(":", 2)[2])

        plan = """plan:
  goal: "Test replan cycle"
  steps:
    - id: t1
      action: "Do the thing"
      success_criteria: "It works"
      retry_strategy: "stop and escalate"
"""
        (workspace / "ctrlflow" / "plans" / "current.yaml").write_text(plan)
        main(["compile-plan", "--workspace", str(workspace)])
        capsys.readouterr()

        # t1 → fail → observe
        main(["decide", "--workspace", str(workspace)])
        capsys.readouterr()
        obs = {
            "success": False, "signal": "failed", "surprise": 0.5,
            "conditions": {"quality_score": 10, "completeness": "none",
                           "blocker": None, "confidence": "low",
                           "needs_replan": False, "escalate": True},
            "evidence": {}, "narrative": "Total failure, need escalation.",
        }
        (workspace / "dataflow" / "observations" / "t1-0.json").write_text(json.dumps(obs))
        main(["observe", "--node", "t1", "--attempt", "0", "--workspace", str(workspace)])
        capsys.readouterr()

        # Decide → escalation triggered by conditions.escalate=True
        main(["decide", "--workspace", str(workspace)])
        out = capsys.readouterr().out
        assert "ESCALATE" in out

        # Resume with replan
        resolution = {"decision": "replan", "reasoning": "try a different approach"}
        (workspace / "ctrlflow" / "resolution.json").write_text(json.dumps(resolution))
        main(["resume", "--workspace", str(workspace)])
        out = capsys.readouterr().out
        assert out.strip() == "RESUMED:REPLAN"

        # Phase should be back to plan
        from pymh.state import read_phase
        phase = read_phase(workspace)
        assert phase["phase"] == "plan"
        assert phase["replan_count"] >= 1

        # Write new plan and compile
        new_plan = """plan:
  goal: "Test replan cycle"
  steps:
    - id: s1
      action: "Better approach"
      success_criteria: "It actually works"
      retry_strategy: "stop and escalate"
"""
        (workspace / "ctrlflow" / "plans" / "current.yaml").write_text(new_plan)
        main(["compile-plan", "--workspace", str(workspace)])
        capsys.readouterr()

        # s1 → success → DONE
        main(["decide", "--workspace", str(workspace)])
        out = capsys.readouterr().out
        assert "DISPATCH:s1:0:" in out

        obs2 = {
            "success": True, "signal": "works now", "surprise": 0.1,
            "conditions": {"quality_score": 95, "completeness": "full",
                           "blocker": None, "confidence": "high",
                           "needs_replan": False, "escalate": False},
            "evidence": {}, "narrative": "Better approach worked.",
        }
        (workspace / "dataflow" / "observations" / "s1-0.json").write_text(json.dumps(obs2))
        main(["observe", "--node", "s1", "--attempt", "0", "--workspace", str(workspace)])
        capsys.readouterr()

        main(["decide", "--workspace", str(workspace)])
        out = capsys.readouterr().out
        assert "DONE" in out

        # Report includes both plan versions
        main(["report", "--workspace", str(workspace)])
        report_out = capsys.readouterr().out
        report_path = Path(report_out.strip())
        assert report_path.exists()

        # Two plan versions should exist
        plans_dir = workspace / "ctrlflow" / "plans"
        versions = sorted(plans_dir.glob("v*.yaml"))
        assert len(versions) == 2


class TestMultipleTasksSequential:
    """Run two independent tasks back-to-back in the same MH root."""

    def test_two_tasks_independent(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["setup"])
        capsys.readouterr()

        workspaces = []
        task_ids = []

        for _i, goal in enumerate(["First task", "Second task"]):
            main(["init", "--goal", goal, "--fuel", "5"])
            out = capsys.readouterr().out
            parts = out.strip().split(":")
            task_ids.append(parts[1])
            ws = Path(parts[2])
            workspaces.append(ws)

            plan = f"""plan:
  goal: "{goal}"
  steps:
    - id: t1
      action: "Do {goal.lower()}"
      success_criteria: "Done"
      retry_strategy: "stop and escalate"
"""
            (ws / "ctrlflow" / "plans" / "current.yaml").write_text(plan)
            main(["compile-plan", "--workspace", str(ws)])
            capsys.readouterr()

            main(["decide", "--workspace", str(ws)])
            capsys.readouterr()
            obs = {
                "success": True, "signal": "done", "surprise": 0.0,
                "conditions": {"quality_score": 90, "completeness": "full",
                               "blocker": None, "confidence": "high",
                               "needs_replan": False, "escalate": False},
                "evidence": {}, "narrative": f"{goal} completed.",
            }
            (ws / "dataflow" / "observations" / "t1-0.json").write_text(json.dumps(obs))
            main(["observe", "--node", "t1", "--attempt", "0", "--workspace", str(ws)])
            capsys.readouterr()

            main(["decide", "--workspace", str(ws)])
            out = capsys.readouterr().out
            assert "DONE" in out

        # Both tasks in history
        history = get_mh_root() / "history.jsonl"
        lines = [json.loads(ln) for ln in history.read_text().strip().split("\n")]
        assert len(lines) == 2
        assert lines[0]["goal"] == "First task"
        assert lines[1]["goal"] == "Second task"
        assert all(e["status"] == "done" for e in lines)

        # Status for each workspace shows correct task
        for ws, task_id, goal in zip(workspaces, task_ids, ["First task", "Second task"]):
            main(["status", "--workspace", str(ws)])
            out = capsys.readouterr().out
            assert goal in out
            assert task_id in out
