"""Tests for report generation.

Verifies: R1, R2, R3, R4, R5
"""

from __future__ import annotations

from pathlib import Path

from pymh.report import generate_report
from pymh.state import (
    append_trace,
    create_meta,
    create_phase,
    create_profile,
    create_state,
    now_iso,
    write_profile,
)


def _make_workspace(tmp_path: Path) -> Path:
    """Create a minimal workspace with required files."""
    ws = tmp_path / "workspace"
    (ws / "ctrlflow" / "plans").mkdir(parents=True)
    (ws / "dataflow" / "instructions").mkdir(parents=True)
    (ws / "dataflow" / "observations").mkdir(parents=True)
    (ws / "dataflow" / "artifacts").mkdir(parents=True)
    (ws / "trace").mkdir(parents=True)
    (ws / "trace" / "trace.jsonl").touch()
    create_meta(ws, "test-report-task", "migrate webpack to vite", "general")
    create_state(ws, 30)
    create_phase(ws)
    create_profile(ws)
    return ws


class TestReportGeneration:
    def test_generates_report_file(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        path = generate_report(ws)
        assert Path(path).exists()
        assert Path(path).name == "task-report.md"

    def test_returns_absolute_path(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        path = generate_report(ws)
        assert Path(path).is_absolute()

    def test_report_contains_goal(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        path = generate_report(ws)
        content = Path(path).read_text()
        assert "migrate webpack to vite" in content

    def test_report_contains_header(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        path = generate_report(ws)
        content = Path(path).read_text()
        assert "# Task Report: test-report-task" in content

    def test_report_contains_result_section(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        path = generate_report(ws)
        content = Path(path).read_text()
        assert "## Result" in content
        assert "**Status**: Running" in content
        assert "**Steps**: 0/30 used" in content
        assert "**Replan count**: 0" in content

    def test_report_timeline_from_trace(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        append_trace(ws, {
            "timestamp": now_iso(),
            "step": 1,
            "action": "observe",
            "task_id": "t1",
            "conditions": {"completeness": "full"},
            "surprise": 0.1,
            "observation_summary": "created config file",
        })
        append_trace(ws, {
            "timestamp": now_iso(),
            "step": 2,
            "action": "observe",
            "task_id": "t2",
            "conditions": {"completeness": "partial"},
            "surprise": 0.6,
            "observation_summary": "migration incomplete",
        })

        path = generate_report(ws)
        content = Path(path).read_text()
        assert "## Execution Timeline" in content
        assert "t1" in content
        assert "t2" in content
        assert "\u2713 full" in content
        assert "\u2717 partial" in content

    def test_report_timeline_parallel(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        append_trace(ws, {
            "timestamp": now_iso(),
            "step": 1,
            "action": "observe_parallel",
            "task_id": "t2a_t2b",
            "conditions": {"completeness": "full"},
            "surprise": 0.2,
            "observation_summary": "parallel tasks done",
        })

        path = generate_report(ws)
        content = Path(path).read_text()
        assert "t2a_t2b" in content

    def test_report_profile_section(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        write_profile(ws, {"build_system": "webpack5", "framework": "react18"})

        path = generate_report(ws)
        content = Path(path).read_text()
        assert "## Profile" in content
        assert "build_system: webpack5" in content
        assert "framework: react18" in content

    def test_report_artifacts_section(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        artifacts_dir = ws / "dataflow" / "artifacts"
        (artifacts_dir / "vite.config.ts").write_text("export default {}")
        (artifacts_dir / "package.json").write_text("{}")

        path = generate_report(ws)
        content = Path(path).read_text()
        assert "## Artifacts" in content
        assert "`package.json`" in content
        assert "`vite.config.ts`" in content

    def test_report_artifacts_excludes_self(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        # Generate once so task-report.md exists
        generate_report(ws)
        # Generate again — should not list itself
        path = generate_report(ws)
        content = Path(path).read_text()
        artifacts_section = content.split("## Artifacts")[1].split("## Key")[0]
        assert "task-report.md" not in artifacts_section

    def test_report_key_decisions_high_surprise(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        append_trace(ws, {
            "timestamp": now_iso(),
            "step": 3,
            "action": "observe",
            "task_id": "t3",
            "conditions": {"completeness": "partial"},
            "surprise": 0.8,
            "observation_summary": "dynamic require not supported",
        })

        path = generate_report(ws)
        content = Path(path).read_text()
        assert "## Key Decisions" in content
        assert "dynamic require not supported" in content
        assert "surprise=0.8" in content

    def test_report_key_decisions_escalate(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        append_trace(ws, {
            "timestamp": now_iso(),
            "step": 5,
            "action": "escalate",
            "type": "loop_detected",
        })

        path = generate_report(ws)
        content = Path(path).read_text()
        assert "escalation" in content
        assert "loop_detected" in content

    def test_report_empty_trace(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        path = generate_report(ws)
        content = Path(path).read_text()
        assert "no steps recorded" in content

    def test_report_aborted_status(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        from pymh.state import read_meta, write_meta
        meta = read_meta(ws)
        meta["status"] = "aborted"
        write_meta(ws, meta)

        path = generate_report(ws)
        content = Path(path).read_text()
        assert "**Status**: Aborted" in content
