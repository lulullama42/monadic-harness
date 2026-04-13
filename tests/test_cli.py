"""Tests for CLI commands: init, status, abort, fuel.

Verifies: A3
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pymh.cli import main
from pymh.schemas.defaults import DEFAULT_CONFIG
from pymh.state import read_meta, read_state
from pymh.workspace import get_mh_root

_DEFAULTS = DEFAULT_CONFIG["defaults"]


class TestInit:
    def test_creates_workspace_and_outputs_init_line(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["init", "--goal", "test goal"])
        out = capsys.readouterr().out.strip()
        assert out.startswith("INIT:")
        parts = out.split(":")
        assert len(parts) == 3
        task_id = parts[1]
        workspace_path = Path(parts[2])
        assert workspace_path.exists()
        assert task_id in str(workspace_path)

    def test_creates_correct_files(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["init", "--goal", "migrate webpack", "--fuel", "50"])
        out = capsys.readouterr().out.strip()
        workspace = Path(out.split(":")[2])

        meta = read_meta(workspace)
        assert meta["goal"] == "migrate webpack"
        assert meta["status"] == "running"

        state = read_state(workspace)
        assert state["fuel_remaining"] == 50
        assert state["step"] == 0

        assert (workspace / "profile.json").exists()
        assert (workspace / "ctrlflow" / "phase.json").exists()
        assert (workspace / "trace" / "trace.jsonl").exists()

    def test_appends_to_history(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["init", "--goal", "task one"])
        capsys.readouterr()
        main(["init", "--goal", "task two"])
        capsys.readouterr()

        history_path = get_mh_root() / "history.jsonl"
        lines = [json.loads(ln) for ln in history_path.read_text().strip().split("\n")]
        assert len(lines) == 2
        assert lines[0]["goal"] == "task one"
        assert lines[1]["goal"] == "task two"
        assert lines[0]["status"] == "running"

    def test_uses_config_defaults(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["init", "--goal", "default fuel test"])
        out = capsys.readouterr().out.strip()
        workspace = Path(out.split(":")[2])
        state = read_state(workspace)
        assert state["fuel_remaining"] == _DEFAULTS["fuel"]


class TestStatus:
    def test_displays_status(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["init", "--goal", "test status"])
        init_out = capsys.readouterr().out.strip()
        workspace = init_out.split(":")[2]

        main(["status", "--workspace", workspace])
        status_out = capsys.readouterr().out.strip()

        assert "test status" in status_out
        assert "Phase: plan" in status_out
        assert "Step: 0/" in status_out
        assert f"Fuel: {_DEFAULTS['fuel']}" in status_out

    def test_auto_detects_workspace(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["init", "--goal", "auto detect test"])
        capsys.readouterr()

        main(["status"])
        status_out = capsys.readouterr().out.strip()
        assert "auto detect test" in status_out


class TestAbort:
    def test_aborts_task(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["init", "--goal", "abort me"])
        init_out = capsys.readouterr().out.strip()
        workspace = Path(init_out.split(":")[2])
        task_id = init_out.split(":")[1]

        main(["abort", "--workspace", str(workspace)])
        abort_out = capsys.readouterr().out.strip()
        assert abort_out == f"ABORTED:{task_id}"

        meta = read_meta(workspace)
        assert meta["status"] == "aborted"

    def test_updates_history(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["init", "--goal", "abort history test"])
        init_out = capsys.readouterr().out.strip()
        workspace = init_out.split(":")[2]
        task_id = init_out.split(":")[1]

        main(["abort", "--workspace", workspace])
        capsys.readouterr()

        history_path = get_mh_root() / "history.jsonl"
        lines = [json.loads(ln) for ln in history_path.read_text().strip().split("\n")]
        entry = next(e for e in lines if e["task_id"] == task_id)
        assert entry["status"] == "aborted"
        assert "completed" in entry

    def test_abort_trace_has_source_cli(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["init", "--goal", "abort source test"])
        init_out = capsys.readouterr().out.strip()
        workspace = Path(init_out.split(":")[2])

        main(["abort", "--workspace", str(workspace)])
        capsys.readouterr()

        trace_path = workspace / "trace" / "trace.jsonl"
        lines = [json.loads(ln) for ln in trace_path.read_text().strip().split("\n")]
        abort_entries = [t for t in lines if t.get("action") == "abort"]
        assert len(abort_entries) == 1
        assert abort_entries[0]["source"] == "cli"


class TestFuel:
    def test_adds_fuel(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["init", "--goal", "fuel test", "--fuel", "20"])
        init_out = capsys.readouterr().out.strip()
        workspace = Path(init_out.split(":")[2])

        main(["fuel", "--add", "10", "--workspace", str(workspace)])
        fuel_out = capsys.readouterr().out.strip()
        assert fuel_out == "FUEL:30"

        state = read_state(workspace)
        assert state["fuel_remaining"] == 30

    def test_appends_trace(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["init", "--goal", "fuel trace test"])
        init_out = capsys.readouterr().out.strip()
        workspace = Path(init_out.split(":")[2])

        main(["fuel", "--add", "5", "--workspace", str(workspace)])
        capsys.readouterr()

        trace_path = workspace / "trace" / "trace.jsonl"
        lines = [json.loads(ln) for ln in trace_path.read_text().strip().split("\n")]
        assert len(lines) == 1
        assert lines[0]["action"] == "fuel_add"
        assert lines[0]["fuel_added"] == 5


class TestReport:
    def test_report_generates_file(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["init", "--goal", "report test"])
        init_out = capsys.readouterr().out.strip()
        workspace = init_out.split(":")[2]

        main(["report", "--workspace", workspace])
        out = capsys.readouterr().out.strip()
        assert out.endswith("task-report.md")
        assert Path(out).exists()

    def test_abort_generates_report(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["init", "--goal", "abort report test"])
        init_out = capsys.readouterr().out.strip()
        workspace = Path(init_out.split(":")[2])

        main(["abort", "--workspace", str(workspace)])
        capsys.readouterr()

        report_path = workspace / "dataflow" / "artifacts" / "task-report.md"
        assert report_path.exists()
        content = report_path.read_text()
        assert "**Status**: Aborted" in content


class TestResume:
    def test_resume_missing_resolution_exits(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["init", "--goal", "resume test"])
        init_out = capsys.readouterr().out.strip()
        workspace = init_out.split(":")[2]

        with pytest.raises(SystemExit) as exc_info:
            main(["resume", "--workspace", workspace])
        assert exc_info.value.code == 1

    def test_resume_replan_via_cli(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["init", "--goal", "resume replan test"])
        init_out = capsys.readouterr().out.strip()
        workspace = Path(init_out.split(":")[2])

        # Write resolution.json
        resolution = {"decision": "replan", "reasoning": "need new plan"}
        (workspace / "ctrlflow" / "resolution.json").write_text(json.dumps(resolution))

        main(["resume", "--workspace", str(workspace)])
        out = capsys.readouterr().out.strip()
        assert out == "RESUMED:REPLAN"


class TestSetup:
    def test_setup_creates_root(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["setup"])
        out = capsys.readouterr().out
        assert "Initialized" in out
        assert isolated_mh_root.exists()

    def test_setup_installs_templates(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["setup"])
        capsys.readouterr()
        templates_dir = isolated_mh_root / "templates"
        assert (templates_dir / "general.yaml").exists()
        assert (templates_dir / "migration.yaml").exists()
        assert (templates_dir / "research.yaml").exists()

    def test_setup_installs_skill_files(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import pymh.workspace as ws

        main(["setup"])
        capsys.readouterr()
        skill_dir = ws.CLAUDE_HOME / "skills" / "mh"
        assert (skill_dir / "SKILL.md").exists()
        assert (skill_dir / "principles.md").exists()
        assert (skill_dir / "observation-schema.md").exists()
        assert (skill_dir / "plan-format.md").exists()


class TestVersionConsistency:
    """F8: __version__ matches pyproject.toml."""

    def test_version_matches_pyproject(self) -> None:
        import sys

        if sys.version_info < (3, 11):
            import re

            from pymh import __version__

            pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
            text = pyproject_path.read_text()
            m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
            assert m is not None, "version not found in pyproject.toml"
            assert __version__ == m.group(1)
        else:
            import tomllib

            from pymh import __version__

            pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
            with open(pyproject_path, "rb") as f:
                pyproject = tomllib.load(f)
            assert __version__ == pyproject["project"]["version"]
