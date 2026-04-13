"""Tests for workspace creation and path management.

Verifies: A5, D3, D12, I7
"""

from __future__ import annotations

from pathlib import Path

import yaml

from pymh.schemas.defaults import DEFAULT_CONFIG
from pymh.workspace import create_workspace, ensure_mh_root, load_config

_DEFAULTS = DEFAULT_CONFIG["defaults"]


class TestEnsureMhRoot:
    def test_creates_root_structure(self, isolated_mh_root: Path) -> None:
        root = ensure_mh_root()
        assert root == isolated_mh_root
        assert root.exists()
        assert (root / "tasks").is_dir()
        assert (root / "templates").is_dir()
        assert (root / "config.yaml").is_file()
        assert (root / "history.jsonl").is_file()

    def test_idempotent(self, isolated_mh_root: Path) -> None:
        root1 = ensure_mh_root()
        root2 = ensure_mh_root()
        assert root1 == root2
        # Config should not be overwritten
        with open(root1 / "config.yaml") as f:
            config = yaml.safe_load(f)
        assert config["defaults"]["fuel"] == _DEFAULTS["fuel"]

    def test_preserves_existing_config(self, isolated_mh_root: Path) -> None:
        ensure_mh_root()
        # Modify config
        config_path = isolated_mh_root / "config.yaml"
        with open(config_path, "w") as f:
            yaml.dump({"defaults": {"fuel": 50}}, f)
        # Re-ensure should not overwrite
        ensure_mh_root()
        with open(config_path) as f:
            config = yaml.safe_load(f)
        assert config["defaults"]["fuel"] == 50


class TestCreateWorkspace:
    def test_creates_full_directory_tree(self, isolated_mh_root: Path) -> None:
        ensure_mh_root()
        ws = create_workspace("test-task-123")
        assert ws.exists()
        assert ws.name == "test-task-123"

        # ctrlflow/
        assert (ws / "ctrlflow").is_dir()
        assert (ws / "ctrlflow" / "plans").is_dir()

        # dataflow/
        assert (ws / "dataflow").is_dir()
        assert (ws / "dataflow" / "instructions").is_dir()
        assert (ws / "dataflow" / "observations").is_dir()
        assert (ws / "dataflow" / "scratchpad").is_dir()
        assert (ws / "dataflow" / "artifacts").is_dir()

        # trace/
        assert (ws / "trace").is_dir()

    def test_rejects_duplicate_task_id(self, isolated_mh_root: Path) -> None:
        ensure_mh_root()
        create_workspace("dup-task")
        import pytest

        with pytest.raises(FileExistsError):
            create_workspace("dup-task")


class TestLoadConfig:
    def test_returns_defaults_when_no_config(self, isolated_mh_root: Path) -> None:
        config = load_config()
        assert config["defaults"]["fuel"] == _DEFAULTS["fuel"]
        assert config["defaults"]["max_task_attempts"] == _DEFAULTS["max_task_attempts"]
        assert config["preferences"]["progress_verbosity"] == "normal"

    def test_merges_partial_config(self, isolated_mh_root: Path) -> None:
        ensure_mh_root()
        config_path = isolated_mh_root / "config.yaml"
        with open(config_path, "w") as f:
            yaml.dump({"defaults": {"fuel": 50}}, f)
        config = load_config()
        assert config["defaults"]["fuel"] == 50
        # Other defaults should still be present
        assert config["defaults"]["max_task_attempts"] == _DEFAULTS["max_task_attempts"]
