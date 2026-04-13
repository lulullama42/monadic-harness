"""Workspace creation and path management.

Manages the ~/.mh/ root directory and per-task workspace directories.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import yaml

from pymh.schemas.defaults import DEFAULT_CONFIG

MH_ROOT = Path.home() / ".mh"
CLAUDE_HOME = Path.home() / ".claude"


def get_mh_root() -> Path:
    return MH_ROOT


def ensure_mh_root() -> Path:
    """Create ~/.mh/ with default structure if it doesn't exist. Idempotent."""
    root = get_mh_root()
    root.mkdir(exist_ok=True)
    (root / "tasks").mkdir(exist_ok=True)
    (root / "templates").mkdir(exist_ok=True)

    config_path = root / "config.yaml"
    if not config_path.exists():
        with open(config_path, "w") as f:
            yaml.dump(DEFAULT_CONFIG, f, default_flow_style=False, sort_keys=False)

    history_path = root / "history.jsonl"
    if not history_path.exists():
        history_path.touch()

    return root


def load_config() -> dict[str, Any]:
    """Load config.yaml, falling back to defaults for missing keys."""
    root = get_mh_root()
    config_path = root / "config.yaml"

    config: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path) as f:
            loaded = yaml.safe_load(f)
            if isinstance(loaded, dict):
                config = loaded

    # Merge with defaults: config overrides defaults
    merged: dict[str, Any] = {}
    for section, defaults in DEFAULT_CONFIG.items():
        section_config = config.get(section, {})
        if isinstance(defaults, dict) and isinstance(section_config, dict):
            merged[section] = {**defaults, **section_config}
        else:
            merged[section] = section_config if section in config else defaults

    return merged


def create_workspace(task_id: str) -> Path:
    """Create the full workspace directory tree for a task.

    Returns the workspace path.
    """
    root = ensure_mh_root()
    workspace = root / "tasks" / task_id
    workspace.mkdir(parents=True, exist_ok=False)

    # ctrlflow/
    ctrlflow = workspace / "ctrlflow"
    ctrlflow.mkdir()
    (ctrlflow / "plans").mkdir()

    # dataflow/
    dataflow = workspace / "dataflow"
    dataflow.mkdir()
    (dataflow / "instructions").mkdir()
    (dataflow / "observations").mkdir()
    (dataflow / "scratchpad").mkdir()
    (dataflow / "artifacts").mkdir()

    # trace/
    trace = workspace / "trace"
    trace.mkdir()

    return workspace


def _get_data_dir() -> Path:
    """Get path to package data directory (skill/ and templates/)."""
    return Path(__file__).resolve().parent / "data"


def install_templates() -> None:
    """Install default templates to ~/.mh/templates/. Skips existing files."""
    source_dir = _get_data_dir() / "templates"
    target_dir = get_mh_root() / "templates"
    target_dir.mkdir(parents=True, exist_ok=True)

    for src_file in source_dir.glob("*.yaml"):
        target_file = target_dir / src_file.name
        if not target_file.exists():
            shutil.copy2(src_file, target_file)


def install_skill_files(force: bool = False) -> Path:
    """Install skill files to ~/.claude/skills/mh/. Returns skill dir path.

    Args:
        force: If True, overwrite existing files. If False, skip existing.
    """
    source_dir = _get_data_dir() / "skill"
    skill_dir = CLAUDE_HOME / "skills" / "mh"
    skill_dir.mkdir(parents=True, exist_ok=True)

    for src_file in source_dir.iterdir():
        if src_file.is_file():
            target_file = skill_dir / src_file.name
            if force or not target_file.exists():
                shutil.copy2(src_file, target_file)

    # Create templates symlink pointing to ~/.mh/templates/
    templates_link = skill_dir / "templates"
    templates_target = get_mh_root() / "templates"
    if not templates_link.is_symlink() and not templates_link.exists():
        templates_target.mkdir(parents=True, exist_ok=True)
        os.symlink(templates_target, templates_link)

    return skill_dir


def resolve_workspace(workspace_arg: str | None = None) -> Path:
    """Resolve workspace path from CLI arg or find the last active task.

    Args:
        workspace_arg: Explicit workspace path, or None to auto-detect.

    Returns:
        Path to the task workspace directory.

    Raises:
        SystemExit: If no workspace can be found.
    """
    if workspace_arg:
        path = Path(workspace_arg)
        if not path.exists():
            raise SystemExit(f"Workspace not found: {path}")
        return path

    # Find last active task from history.jsonl
    history_path = get_mh_root() / "history.jsonl"
    if not history_path.exists():
        raise SystemExit("No tasks found. Run 'pymh init' first.")

    last_running: dict[str, Any] | None = None
    with open(history_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            if entry.get("status") == "running":
                last_running = entry

    if last_running is None:
        # Fall back to the most recent task regardless of status
        with open(history_path) as f:
            lines = [ln.strip() for ln in f if ln.strip()]
        if not lines:
            raise SystemExit("No tasks found. Run 'pymh init' first.")
        last_running = json.loads(lines[-1])

    task_id = last_running["task_id"]
    path = get_mh_root() / "tasks" / task_id
    if not path.exists():
        raise SystemExit(f"Workspace for task '{task_id}' not found at {path}")
    return path
