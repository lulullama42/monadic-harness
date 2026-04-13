"""State file I/O for meta.json, state.json, phase.json, cursor.json, profile.json, history.jsonl.

All functions take a workspace Path and read/write the appropriate file.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --- Generic JSON helpers ---


def _read_json(path: Path) -> dict[str, Any]:
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}, got {type(data).__name__}")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _append_jsonl(path: Path, entry: dict[str, Any]) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- meta.json ---


def read_meta(workspace: Path) -> dict[str, Any]:
    return _read_json(workspace / "meta.json")


def write_meta(workspace: Path, data: dict[str, Any]) -> None:
    _write_json(workspace / "meta.json", data)


def create_meta(workspace: Path, task_id: str, goal: str, template: str) -> dict[str, Any]:
    meta = {
        "task_id": task_id,
        "goal": goal,
        "template": template,
        "created": now_iso(),
        "status": "running",
    }
    write_meta(workspace, meta)
    return meta


# --- state.json ---


def read_state(workspace: Path) -> dict[str, Any]:
    return _read_json(workspace / "state.json")


def write_state(workspace: Path, data: dict[str, Any]) -> None:
    _write_json(workspace / "state.json", data)


def create_state(workspace: Path, fuel: int) -> dict[str, Any]:
    from pymh.schemas.defaults import INITIAL_STATE

    state = {**INITIAL_STATE, "fuel_remaining": fuel}
    write_state(workspace, state)
    return state


# --- phase.json ---


def read_phase(workspace: Path) -> dict[str, Any]:
    path = workspace / "ctrlflow" / "phase.json"
    if not path.exists():
        return {"phase": "init", "replan_count": 0, "phase_entered_at": ""}
    return _read_json(path)


def write_phase(workspace: Path, data: dict[str, Any]) -> None:
    _write_json(workspace / "ctrlflow" / "phase.json", data)


def create_phase(workspace: Path) -> dict[str, Any]:
    from pymh.schemas.defaults import INITIAL_PHASE

    phase = {**INITIAL_PHASE, "phase_entered_at": now_iso()}
    write_phase(workspace, phase)
    return phase


# --- cursor.json ---


def read_cursor(workspace: Path) -> dict[str, Any]:
    path = workspace / "ctrlflow" / "cursor.json"
    if not path.exists():
        from pymh.schemas.defaults import INITIAL_CURSOR

        return {**INITIAL_CURSOR}
    return _read_json(path)


def write_cursor(workspace: Path, data: dict[str, Any]) -> None:
    _write_json(workspace / "ctrlflow" / "cursor.json", data)


# --- profile.json ---


def read_profile(workspace: Path) -> dict[str, Any]:
    path = workspace / "profile.json"
    if not path.exists():
        return {}
    return _read_json(path)


def write_profile(workspace: Path, data: dict[str, Any]) -> None:
    _write_json(workspace / "profile.json", data)


def create_profile(workspace: Path) -> dict[str, Any]:
    profile: dict[str, Any] = {}
    write_profile(workspace, profile)
    return profile


# --- history.jsonl ---


def append_history(entry: dict[str, Any]) -> None:
    from pymh.workspace import get_mh_root

    _append_jsonl(get_mh_root() / "history.jsonl", entry)


def read_history() -> list[dict[str, Any]]:
    from pymh.workspace import get_mh_root

    path = get_mh_root() / "history.jsonl"
    if not path.exists():
        return []
    entries = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def update_history_status(task_id: str, status: str) -> None:
    """Update the status of a task in history.jsonl.

    Rewrites the file with the updated entry. Not efficient for large histories,
    but history.jsonl is small in practice.
    """
    from pymh.workspace import get_mh_root

    path = get_mh_root() / "history.jsonl"
    if not path.exists():
        return

    entries = read_history()
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            for entry in entries:
                if entry.get("task_id") == task_id:
                    entry["status"] = status
                    if status in ("done", "aborted"):
                        entry["completed"] = now_iso()
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


# --- trace/trace.jsonl ---


def append_trace(workspace: Path, entry: dict[str, Any]) -> None:
    trace_path = workspace / "trace" / "trace.jsonl"
    _append_jsonl(trace_path, entry)


def read_trace(workspace: Path, last_n: int | None = None) -> list[dict[str, Any]]:
    trace_path = workspace / "trace" / "trace.jsonl"
    if not trace_path.exists():
        return []
    entries = []
    with open(trace_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # skip malformed trace lines
    if last_n is not None:
        return entries[-last_n:]
    return entries
