"""Tests for setup command: template and skill file installation.

Verifies: A8, K1, D15, G2, G3
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pymh.cli import main
from pymh.workspace import (
    ensure_mh_root,
    get_mh_root,
    install_skill_files,
    install_templates,
)


class TestInstallTemplates:
    def test_installs_three_templates(self, isolated_mh_root: Path) -> None:
        ensure_mh_root()
        install_templates()
        templates_dir = get_mh_root() / "templates"
        installed = sorted(f.name for f in templates_dir.glob("*.yaml"))
        assert installed == ["general.yaml", "migration.yaml", "research.yaml"]

    def test_no_overwrite_existing(self, isolated_mh_root: Path) -> None:
        ensure_mh_root()
        templates_dir = get_mh_root() / "templates"
        custom = templates_dir / "general.yaml"
        custom.write_text("custom content\n")

        install_templates()

        assert custom.read_text() == "custom content\n"

    def test_idempotent(self, isolated_mh_root: Path) -> None:
        ensure_mh_root()
        install_templates()
        install_templates()  # should not raise
        templates_dir = get_mh_root() / "templates"
        assert len(list(templates_dir.glob("*.yaml"))) == 3

    def test_template_content_is_valid_yaml(self, isolated_mh_root: Path) -> None:
        import yaml

        ensure_mh_root()
        install_templates()
        templates_dir = get_mh_root() / "templates"
        for f in templates_dir.glob("*.yaml"):
            data = yaml.safe_load(f.read_text())
            assert "plan" in data
            assert "steps" in data["plan"]
            assert len(data["plan"]["steps"]) >= 3


class TestInstallSkillFiles:
    def test_installs_four_skill_files(self, isolated_mh_root: Path) -> None:
        ensure_mh_root()
        skill_dir = install_skill_files()
        files = sorted(f.name for f in skill_dir.iterdir() if f.is_file())
        assert "SKILL.md" in files
        assert "principles.md" in files
        assert "observation-schema.md" in files
        assert "plan-format.md" in files

    def test_creates_templates_symlink(self, isolated_mh_root: Path) -> None:
        ensure_mh_root()
        skill_dir = install_skill_files()
        templates_link = skill_dir / "templates"
        assert templates_link.is_symlink() or templates_link.is_dir()

    def test_symlink_points_to_mh_templates(self, isolated_mh_root: Path) -> None:
        ensure_mh_root()
        skill_dir = install_skill_files()
        templates_link = skill_dir / "templates"
        assert templates_link.resolve() == (get_mh_root() / "templates").resolve()

    def test_idempotent(self, isolated_mh_root: Path) -> None:
        ensure_mh_root()
        install_skill_files()
        install_skill_files()  # should not raise
        skill_dir = install_skill_files()
        assert (skill_dir / "SKILL.md").exists()

    def test_returns_skill_dir_path(self, isolated_mh_root: Path) -> None:
        ensure_mh_root()
        skill_dir = install_skill_files()
        assert skill_dir.exists()
        assert skill_dir.name == "mh"
        assert skill_dir.parent.name == "skills"

    def test_no_overwrite_existing(self, isolated_mh_root: Path) -> None:
        ensure_mh_root()
        skill_dir = install_skill_files()
        custom = skill_dir / "SKILL.md"
        custom.write_text("custom skill content\n")

        install_skill_files()  # default: no overwrite

        assert custom.read_text() == "custom skill content\n"

    def test_force_overwrites_existing(self, isolated_mh_root: Path) -> None:
        ensure_mh_root()
        skill_dir = install_skill_files()
        custom = skill_dir / "SKILL.md"
        custom.write_text("custom skill content\n")

        install_skill_files(force=True)

        assert custom.read_text() != "custom skill content\n"

    def test_broken_symlink_does_not_crash(self, isolated_mh_root: Path) -> None:
        import shutil

        ensure_mh_root()
        skill_dir = install_skill_files()

        # Break the symlink by deleting the target
        templates_target = get_mh_root() / "templates"
        shutil.rmtree(templates_target)

        # Verify symlink is broken
        templates_link = skill_dir / "templates"
        assert templates_link.is_symlink()
        assert not templates_link.exists()  # broken: target gone

        # Should not crash
        install_skill_files()


class TestSetupCLI:
    def test_setup_installs_everything(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["setup"])
        out = capsys.readouterr().out

        assert "Initialized" in out
        assert "Templates installed" in out
        assert "Skill files installed" in out

        # Templates exist
        templates_dir = get_mh_root() / "templates"
        assert (templates_dir / "general.yaml").exists()
        assert (templates_dir / "migration.yaml").exists()
        assert (templates_dir / "research.yaml").exists()

        # Skill files exist
        import pymh.workspace as ws

        skill_dir = ws.CLAUDE_HOME / "skills" / "mh"
        assert (skill_dir / "SKILL.md").exists()
        assert (skill_dir / "principles.md").exists()

    def test_setup_idempotent(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["setup"])
        capsys.readouterr()
        main(["setup"])  # should not raise
        out = capsys.readouterr().out
        assert "Initialized" in out

    def test_setup_force_overwrites_skill_files(
        self, isolated_mh_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import pymh.workspace as ws

        main(["setup"])
        capsys.readouterr()

        skill_file = ws.CLAUDE_HOME / "skills" / "mh" / "SKILL.md"
        skill_file.write_text("custom\n")

        main(["setup", "--force"])
        assert skill_file.read_text() != "custom\n"
