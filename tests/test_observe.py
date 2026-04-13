"""Tests for the observe module.

Verifies: D1, D2, D5, D7, D8, D9, D13, D18, D19, I1, I2, I3, I4, I5, I6, I7,
I10, I11, C5, C9, C10, C12, P8
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pymh.observe import (
    _merge_parallel,
    _validate_observation,
    format_progress_line,
    process_observation,
    process_parallel_observations,
)
from pymh.schemas.defaults import DEFAULT_CONFIG
from pymh.state import (
    create_meta,
    create_phase,
    create_profile,
    create_state,
    read_cursor,
    write_cursor,
)

_DEFAULTS = DEFAULT_CONFIG["defaults"]


def _make_workspace(tmp_path: Path, fuel: int = _DEFAULTS["fuel"]) -> Path:
    """Create a minimal workspace with required state files."""
    ws = tmp_path / "workspace"
    (ws / "ctrlflow" / "plans").mkdir(parents=True)
    (ws / "dataflow" / "instructions").mkdir(parents=True)
    (ws / "dataflow" / "observations").mkdir(parents=True)
    (ws / "dataflow" / "artifacts").mkdir(parents=True)
    (ws / "trace").mkdir(parents=True)
    (ws / "trace" / "trace.jsonl").touch()
    create_meta(ws, "test-task", "test goal", "general")
    create_state(ws, fuel)
    create_phase(ws)
    create_profile(ws)
    # Initialize cursor
    cursor = {
        "current_task": "t1",
        "task_attempts": 0,
        "completed_tasks": [],
        "pending_parallel": [],
        "forced_transition": None,
    }
    write_cursor(ws, cursor)
    return ws


def _write_observation(ws: Path, node_id: str, attempt: int, obs: dict) -> None:
    """Write an observation file."""
    path = ws / "dataflow" / "observations" / f"{node_id}-{attempt}.json"
    path.write_text(json.dumps(obs))


class TestValidation:
    def test_fills_missing_success(self) -> None:
        obs, warnings = _validate_observation({"signal": "test"})
        assert obs["success"] is False
        assert any("success" in w for w in warnings)

    def test_fills_missing_conditions(self) -> None:
        obs, _warnings = _validate_observation({"success": True, "signal": "test", "surprise": 0.1})
        # Default completeness is "partial", but reconciliation
        # coerces to "full" because success=True
        assert obs["conditions"]["completeness"] == "full"
        assert obs["conditions"]["quality_score"] == 50

    def test_fills_missing_individual_conditions(self) -> None:
        obs, _warnings = _validate_observation({
            "success": True,
            "signal": "test",
            "surprise": 0.1,
            "conditions": {"completeness": "full"},
        })
        assert obs["conditions"]["completeness"] == "full"  # Preserved
        assert obs["conditions"]["quality_score"] == 50  # Filled
        assert obs["conditions"]["needs_replan"] is False  # Filled

    def test_fills_missing_surprise(self) -> None:
        obs, _warnings = _validate_observation({"success": True, "signal": "test"})
        assert obs["surprise"] == 0.5

    def test_coerces_string_booleans(self) -> None:
        obs, warnings = _validate_observation({
            "success": True,
            "signal": "test",
            "surprise": 0.1,
            "conditions": {
                "needs_replan": "true",
                "escalate": "False",
                "completeness": "full",
                "quality_score": 80,
                "blocker": None,
                "confidence": "high",
            },
        })
        assert obs["conditions"]["needs_replan"] is True
        assert obs["conditions"]["escalate"] is False
        assert any("coerced" in w for w in warnings)

    def test_coerces_string_quality_score(self) -> None:
        obs, _warnings = _validate_observation({
            "success": True,
            "signal": "test",
            "surprise": 0.1,
            "conditions": {"quality_score": "85"},
        })
        assert obs["conditions"]["quality_score"] == 85

    def test_coerces_string_null_blocker(self) -> None:
        obs, _warnings = _validate_observation({
            "success": True,
            "signal": "test",
            "surprise": 0.1,
            "conditions": {"blocker": "null"},
        })
        assert obs["conditions"]["blocker"] is None

    def test_strips_reserved_names(self) -> None:
        obs, warnings = _validate_observation({
            "success": True,
            "signal": "test",
            "surprise": 0.1,
            "conditions": {
                "fuel_remaining": 100,
                "completeness": "full",
            },
        })
        assert "fuel_remaining" not in obs["conditions"]
        assert any("stripped" in w for w in warnings)

    def test_contradiction_raises_surprise(self) -> None:
        obs, warnings = _validate_observation({
            "success": True,
            "signal": "test",
            "surprise": 0.1,
            "conditions": {"completeness": "full", "quality_score": 80},
            "evidence": {"tests_passing": False},
        })
        assert obs["surprise"] >= 0.7
        assert any("contradiction" in w for w in warnings)

    def test_contradiction_build_success(self) -> None:
        obs, _ = _validate_observation({
            "success": True,
            "signal": "test",
            "surprise": 0.1,
            "conditions": {"quality_score": 90},
            "evidence": {"build_success": False},
        })
        assert obs["surprise"] >= 0.7

    def test_contradiction_exit_codes(self) -> None:
        obs, _ = _validate_observation({
            "success": True,
            "signal": "test",
            "surprise": 0.1,
            "conditions": {"confidence": "high"},
            "evidence": {"command_exit_codes": [0, 1]},
        })
        assert obs["surprise"] >= 0.7

    def test_no_contradiction_when_consistent(self) -> None:
        obs, warnings = _validate_observation({
            "success": True,
            "signal": "test",
            "surprise": 0.1,
            "conditions": {"completeness": "full", "quality_score": 90},
            "evidence": {"tests_passing": True, "build_success": True},
        })
        assert obs["surprise"] == 0.1
        assert not any("contradiction" in w for w in warnings)


class TestProcessObservation:
    def test_updates_state(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path, fuel=30)
        _write_observation(ws, "t1", 0, {
            "success": True,
            "signal": "done",
            "surprise": 0.2,
            "conditions": {"completeness": "full", "quality_score": 90},
        })

        result = process_observation(ws, "t1", 0)
        assert result["state"]["step"] == 1
        assert result["state"]["fuel_remaining"] == 29
        assert result["state"]["consecutive_failures"] == 0

    def test_increments_failures_on_failure(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _write_observation(ws, "t1", 0, {
            "success": False,
            "signal": "failed",
            "surprise": 0.5,
            "conditions": {"completeness": "partial"},
        })

        result = process_observation(ws, "t1", 0)
        assert result["state"]["consecutive_failures"] == 1

    def test_resets_failures_on_success(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        from pymh.state import read_state, write_state
        state = read_state(ws)
        state["consecutive_failures"] = 2
        write_state(ws, state)

        _write_observation(ws, "t1", 0, {
            "success": True,
            "signal": "done",
            "surprise": 0.1,
            "conditions": {"completeness": "full"},
        })

        result = process_observation(ws, "t1", 0)
        assert result["state"]["consecutive_failures"] == 0

    def test_surprise_accumulator_squared(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _write_observation(ws, "t1", 0, {
            "success": False,
            "signal": "surprise",
            "surprise": 0.8,
            "conditions": {"completeness": "partial"},
        })

        result = process_observation(ws, "t1", 0)
        assert abs(result["state"]["surprise_accumulator"] - 0.64) < 0.01  # 0.8²

    def test_surprise_accumulator_resets_on_success(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        from pymh.state import read_state, write_state
        state = read_state(ws)
        state["surprise_accumulator"] = 1.5
        write_state(ws, state)

        _write_observation(ws, "t1", 0, {
            "success": True,
            "signal": "done",
            "surprise": 0.1,
            "conditions": {"completeness": "full"},
        })

        result = process_observation(ws, "t1", 0)
        # Resets to 0 then adds 0.1² = 0.01
        assert abs(result["state"]["surprise_accumulator"] - 0.01) < 0.001

    def test_merges_profile(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _write_observation(ws, "t1", 0, {
            "success": True,
            "signal": "done",
            "surprise": 0.1,
            "conditions": {"completeness": "full"},
            "profile_updates": {"vite_version": "6.2"},
        })

        process_observation(ws, "t1", 0)
        from pymh.state import read_profile
        profile = read_profile(ws)
        assert profile["vite_version"] == "6.2"

    def test_missing_observation_synthesizes_failure(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        result = process_observation(ws, "t1", 0)
        assert result["observation"]["success"] is False
        assert result["observation"]["conditions"]["escalate"] is False
        assert any("missing" in w for w in result["warnings"])

    def test_appends_trace(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _write_observation(ws, "t1", 0, {
            "success": True,
            "signal": "done",
            "surprise": 0.1,
            "conditions": {"completeness": "full"},
        })

        process_observation(ws, "t1", 0)
        from pymh.state import read_trace
        traces = read_trace(ws)
        assert len(traces) == 1
        assert traces[0]["action"] == "observe"
        assert traces[0]["task_id"] == "t1"


class TestInvariants:
    def test_loop_detection(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        max_attempts = _DEFAULTS["max_task_attempts"]
        cursor = read_cursor(ws)
        cursor["task_attempts"] = max_attempts
        write_cursor(ws, cursor)

        _write_observation(ws, "t1", max_attempts, {
            "success": False,
            "signal": "failed again",
            "surprise": 0.5,
            "conditions": {"completeness": "partial"},
        })

        result = process_observation(ws, "t1", max_attempts)
        assert result["invariant_fired"] is not None
        assert "loop" in result["invariant_fired"]
        cursor = result["cursor"]
        assert cursor["forced_transition"]["type"] == "escalate"

    def test_drift_consecutive_failures(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        from pymh.state import read_state, write_state
        max_failures = _DEFAULTS["max_consecutive_failures"]
        state = read_state(ws)
        state["consecutive_failures"] = max_failures - 1  # Will hit threshold after observe
        write_state(ws, state)

        _write_observation(ws, "t1", 0, {
            "success": False,
            "signal": "failed",
            "surprise": 0.3,
            "conditions": {"completeness": "partial"},
        })

        result = process_observation(ws, "t1", 0)
        assert result["invariant_fired"] is not None
        assert "drift" in result["invariant_fired"]
        assert result["cursor"]["forced_transition"]["type"] == "replan"

    def test_drift_surprise_accumulator(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        from pymh.state import read_state, write_state
        threshold = _DEFAULTS["drift_threshold"]
        state = read_state(ws)
        # Set just below threshold; 0.9² = 0.81 will push it over
        state["surprise_accumulator"] = threshold - 0.5
        write_state(ws, state)

        _write_observation(ws, "t1", 0, {
            "success": False,
            "signal": "very surprising",
            "surprise": 0.9,
            "conditions": {"completeness": "partial"},
        })

        result = process_observation(ws, "t1", 0)
        assert result["invariant_fired"] is not None
        assert "surprise" in result["invariant_fired"]

    def test_fuel_exhaustion(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path, fuel=1)
        _write_observation(ws, "t1", 0, {
            "success": True,
            "signal": "done",
            "surprise": 0.1,
            "conditions": {"completeness": "full"},
        })

        result = process_observation(ws, "t1", 0)
        assert result["state"]["fuel_remaining"] == 0
        assert result["invariant_fired"] is not None
        assert "fuel" in result["invariant_fired"]
        assert result["cursor"]["forced_transition"]["type"] == "verify_or_abort"

    def test_no_invariant_on_normal(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path, fuel=30)
        _write_observation(ws, "t1", 0, {
            "success": True,
            "signal": "done",
            "surprise": 0.1,
            "conditions": {"completeness": "full"},
        })

        result = process_observation(ws, "t1", 0)
        assert result["invariant_fired"] is None


class TestParallelMerge:
    def test_quality_score_max(self) -> None:
        obs = [
            ("t2a", {
                "success": True, "signal": "a",
                "conditions": {"quality_score": 70}, "surprise": 0.1,
            }),
            ("t2b", {
                "success": True, "signal": "b",
                "conditions": {"quality_score": 90}, "surprise": 0.2,
            }),
        ]
        merged = _merge_parallel(obs)
        assert merged["conditions"]["quality_score"] == 90

    def test_completeness_all_or_nothing(self) -> None:
        obs = [
            ("t2a", {
                "success": True, "signal": "a",
                "conditions": {"completeness": "full"}, "surprise": 0.1,
            }),
            ("t2b", {
                "success": True, "signal": "b",
                "conditions": {"completeness": "partial"}, "surprise": 0.1,
            }),
        ]
        merged = _merge_parallel(obs)
        assert merged["conditions"]["completeness"] == "partial"

    def test_completeness_all_full(self) -> None:
        obs = [
            ("t2a", {
                "success": True, "signal": "a",
                "conditions": {"completeness": "full"}, "surprise": 0.1,
            }),
            ("t2b", {
                "success": True, "signal": "b",
                "conditions": {"completeness": "full"}, "surprise": 0.1,
            }),
        ]
        merged = _merge_parallel(obs)
        assert merged["conditions"]["completeness"] == "full"

    def test_evidence_keyed_by_node(self) -> None:
        obs = [
            ("t2a", {"success": True, "signal": "a", "conditions": {}, "surprise": 0.1,
                     "evidence": {"tests_passing": True}}),
            ("t2b", {"success": True, "signal": "b", "conditions": {}, "surprise": 0.1,
                     "evidence": {"tests_passing": False}}),
        ]
        merged = _merge_parallel(obs)
        assert "t2a" in merged["evidence"]
        assert "t2b" in merged["evidence"]
        assert merged["evidence"]["t2a"]["tests_passing"] is True
        assert merged["evidence"]["t2b"]["tests_passing"] is False

    def test_surprise_max(self) -> None:
        obs = [
            ("t2a", {"success": True, "signal": "a", "conditions": {}, "surprise": 0.1}),
            ("t2b", {"success": True, "signal": "b", "conditions": {}, "surprise": 0.7}),
        ]
        merged = _merge_parallel(obs)
        assert merged["surprise"] == 0.7

    def test_escalate_any(self) -> None:
        obs = [
            ("t2a", {
                "success": True, "signal": "a",
                "conditions": {"escalate": False}, "surprise": 0.1,
            }),
            ("t2b", {
                "success": False, "signal": "b",
                "conditions": {"escalate": True}, "surprise": 0.5,
            }),
        ]
        merged = _merge_parallel(obs)
        assert merged["conditions"]["escalate"] is True


class TestReconciliation:
    def test_success_true_completeness_partial(self) -> None:
        obs, warnings = _validate_observation({
            "success": True,
            "signal": "test",
            "surprise": 0.1,
            "conditions": {"completeness": "partial"},
        })
        assert obs["conditions"]["completeness"] == "full"
        assert obs["success"] is True
        assert any("reconciled" in w for w in warnings)

    def test_success_false_completeness_full(self) -> None:
        """F7: success=false + completeness=full → demote completeness, not promote success."""
        obs, warnings = _validate_observation({
            "success": False,
            "signal": "test",
            "surprise": 0.1,
            "conditions": {"completeness": "full"},
        })
        assert obs["success"] is False
        assert obs["conditions"]["completeness"] == "partial"
        assert obs["surprise"] >= 0.7
        assert any("demoted completeness" in w for w in warnings)

    def test_consistent_no_reconciliation(self) -> None:
        _obs, warnings = _validate_observation({
            "success": True,
            "signal": "test",
            "surprise": 0.1,
            "conditions": {"completeness": "full"},
        })
        assert not any("reconciled" in w for w in warnings)


class TestCanonicalWriteBack:
    def test_observation_file_has_normalized_values(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        # Write observation with string boolean and reserved field
        _write_observation(ws, "t1", 0, {
            "success": True,
            "signal": "test",
            "surprise": 0.1,
            "conditions": {
                "completeness": "full",
                "needs_replan": "false",
                "fuel_remaining": 999,
            },
        })

        process_observation(ws, "t1", 0)

        # Read the file back — should be normalized
        obs_path = ws / "dataflow" / "observations" / "t1-0.json"
        on_disk = json.loads(obs_path.read_text())
        assert on_disk["conditions"]["needs_replan"] is False  # coerced from string
        assert "fuel_remaining" not in on_disk["conditions"]  # stripped


class TestProcessParallelObservations:
    def test_full_pipeline(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        _write_observation(ws, "t2a", 0, {
            "success": True, "signal": "a done", "surprise": 0.1,
            "conditions": {"completeness": "full", "quality_score": 80},
        })
        _write_observation(ws, "t2b", 0, {
            "success": True, "signal": "b done", "surprise": 0.2,
            "conditions": {"completeness": "full", "quality_score": 90},
        })

        result = process_parallel_observations(ws, ["t2a", "t2b"])
        assert result["observation"]["success"] is True
        assert result["state"]["step"] == 1
        assert "t2a" in result["cursor"]["completed_tasks"]
        assert "t2b" in result["cursor"]["completed_tasks"]

        # Merged file should exist
        merged_path = ws / "dataflow" / "observations" / "t2a_t2b-merged.json"
        assert merged_path.exists()

    def test_pre_validates_dirty_inputs(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        # Write observations with string booleans that could cause merge issues
        _write_observation(ws, "t2a", 0, {
            "success": True, "signal": "a", "surprise": 0.1,
            "conditions": {
                "completeness": "full",
                "needs_replan": "false",  # string — truthy without validation
                "escalate": "false",
                "quality_score": "85",
            },
        })
        _write_observation(ws, "t2b", 0, {
            "success": True, "signal": "b", "surprise": 0.1,
            "conditions": {
                "completeness": "full",
                "needs_replan": "false",
                "escalate": "false",
                "quality_score": "70",
            },
        })

        result = process_parallel_observations(ws, ["t2a", "t2b"])
        # Pre-validation should have coerced "false" → False before merge
        assert result["observation"]["conditions"]["needs_replan"] is False
        assert result["observation"]["conditions"]["escalate"] is False


class TestMalformedObservations:
    """MT4/MT7: Malformed observation files — invalid JSON, wrong types, etc."""

    def test_invalid_json_synthesizes_failure(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        obs_path = ws / "dataflow" / "observations" / "t1-0.json"
        obs_path.write_text("{ not valid json !!!")

        result = process_observation(ws, "t1", 0)
        assert result["observation"]["success"] is False
        assert any("parse error" in w for w in result["warnings"])

    def test_empty_file_synthesizes_failure(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        obs_path = ws / "dataflow" / "observations" / "t1-0.json"
        obs_path.write_text("")

        result = process_observation(ws, "t1", 0)
        assert result["observation"]["success"] is False

    def test_json_array_synthesizes_failure(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        obs_path = ws / "dataflow" / "observations" / "t1-0.json"
        obs_path.write_text("[1, 2, 3]")

        result = process_observation(ws, "t1", 0)
        assert result["observation"]["success"] is False
        assert any("not a JSON object" in w for w in result["warnings"])

    def test_string_booleans_coerced_in_e2e(self, tmp_path: Path) -> None:
        """Full observe path with string booleans — verify canonical write-back."""
        ws = _make_workspace(tmp_path)
        _write_observation(ws, "t1", 0, {
            "success": True,
            "signal": "done",
            "surprise": 0.1,
            "conditions": {
                "completeness": "full",
                "needs_replan": "false",
                "escalate": "FALSE",
                "quality_score": "85",
                "confidence": "high",
                "blocker": "null",
            },
        })

        result = process_observation(ws, "t1", 0)
        obs = result["observation"]
        assert obs["conditions"]["needs_replan"] is False
        assert obs["conditions"]["escalate"] is False
        assert obs["conditions"]["quality_score"] == 85
        assert obs["conditions"]["blocker"] is None

        # Verify file on disk is canonical
        obs_path = ws / "dataflow" / "observations" / "t1-0.json"
        on_disk = json.loads(obs_path.read_text())
        assert on_disk["conditions"]["needs_replan"] is False
        assert on_disk["conditions"]["quality_score"] == 85

    def test_missing_fields_filled(self, tmp_path: Path) -> None:
        """Observation with only success and signal — all defaults filled."""
        ws = _make_workspace(tmp_path)
        _write_observation(ws, "t1", 0, {
            "success": True,
            "signal": "minimal obs",
        })

        result = process_observation(ws, "t1", 0)
        obs = result["observation"]
        assert "conditions" in obs
        assert obs["conditions"]["completeness"] == "full"  # reconciled
        assert obs["conditions"]["quality_score"] == 50  # default
        assert isinstance(obs["surprise"], float)


class TestObserveNodeIdMismatch:
    """MT5: observe warns when node_id doesn't match cursor."""

    def test_warns_on_mismatch(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        ws = _make_workspace(tmp_path)
        # Set cursor to t2
        cursor = read_cursor(ws)
        cursor["current_task"] = "t2"
        write_cursor(ws, cursor)

        # Observe t1 (mismatch)
        _write_observation(ws, "t1", 0, {
            "success": True,
            "signal": "done",
            "surprise": 0.0,
            "conditions": {"completeness": "full"},
        })

        result = process_observation(ws, "t1", 0)
        assert any("node_id" in w and "t2" in w for w in result["warnings"])
        err = capsys.readouterr().err
        assert "WARN" in err

    def test_no_warning_when_matching(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ws = _make_workspace(tmp_path)
        cursor = read_cursor(ws)
        cursor["current_task"] = "t1"
        write_cursor(ws, cursor)

        _write_observation(ws, "t1", 0, {
            "success": True,
            "signal": "done",
            "surprise": 0.0,
            "conditions": {"completeness": "full"},
        })

        result = process_observation(ws, "t1", 0)
        assert not any("node_id" in w for w in result["warnings"])


class TestExtractAttemptNum:
    """F10: Numeric sort for observation files with 10+ attempts."""

    def test_single_digit(self, tmp_path: Path) -> None:
        from pymh.observe import extract_attempt_num

        p = tmp_path / "t1-3.json"
        assert extract_attempt_num(p) == 3

    def test_double_digit(self, tmp_path: Path) -> None:
        from pymh.observe import extract_attempt_num

        p = tmp_path / "t1-12.json"
        assert extract_attempt_num(p) == 12

    def test_sort_order_with_many_attempts(self, tmp_path: Path) -> None:
        """Verify numeric sort: t1-10.json > t1-9.json (not lexicographic)."""
        from pymh.observe import extract_attempt_num

        obs_dir = tmp_path / "obs"
        obs_dir.mkdir()
        for i in range(12):
            (obs_dir / f"t1-{i}.json").write_text("{}")
        files = sorted(obs_dir.glob("t1-*.json"), key=extract_attempt_num)
        assert files[-1].name == "t1-11.json"
        assert files[0].name == "t1-0.json"

    def test_parallel_picks_highest_attempt(self, tmp_path: Path) -> None:
        """process_parallel_observations picks numerically highest attempt."""
        ws = _make_workspace(tmp_path)
        # Write observations for attempts 0..10 for t2a
        for i in range(11):
            _write_observation(ws, "t2a", i, {
                "success": i == 10,
                "signal": f"attempt {i}",
                "surprise": 0.1,
                "conditions": {"completeness": "full" if i == 10 else "none"},
            })
        _write_observation(ws, "t2b", 0, {
            "success": True,
            "signal": "b done",
            "surprise": 0.1,
            "conditions": {"completeness": "full"},
        })

        result = process_parallel_observations(ws, ["t2a", "t2b"])
        # Should pick t2a-10.json (highest), not t2a-9.json (lexicographic last)
        assert result["observation"]["success"] is True


class TestProgressLine:
    def test_format(self) -> None:
        state = {"step": 5, "fuel_remaining": 25}
        phase = {"phase": "exec"}
        obs = {"conditions": {"completeness": "full"}, "surprise": 0.1}
        line = format_progress_line(state, phase, "t3a", obs)
        assert "[Step 5/30]" in line
        assert "exec:t3a" in line
        assert "completeness=full" in line
