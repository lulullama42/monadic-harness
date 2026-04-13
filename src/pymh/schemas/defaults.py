"""Default values for observations, conditions, config, and state."""

from __future__ import annotations

# --- Enum-like string constants ---

# Completeness values (observation conditions)
COMP_FULL = "full"
COMP_PARTIAL = "partial"
COMP_NONE = "none"

# Phase values
PHASE_PLAN = "plan"
PHASE_EXEC = "exec"
PHASE_VERIFY = "verify"

# --- Config defaults ---

DEFAULT_CONFIG = {
    "defaults": {
        "fuel": 30,
        "template": "general",
        "max_task_attempts": 3,
        "max_consecutive_failures": 3,
        "drift_threshold": 2.0,
        "fuel_warning_threshold": 5,
        "max_replan_count": 3,
        "high_surprise_threshold": 0.5,
    },
    "preferences": {
        "progress_verbosity": "normal",
        "report_format": "markdown",
    },
}

# --- Initial state ---

INITIAL_STATE = {
    "step": 0,
    "fuel_remaining": 30,  # overridden by config/CLI
    "total_attempts": 0,
    "consecutive_failures": 0,
    "surprise_accumulator": 0.0,
}

INITIAL_PHASE = {
    "phase": PHASE_PLAN,
    "replan_count": 0,
    "phase_entered_at": "",  # filled at creation time
}

INITIAL_CURSOR = {
    "current_task": None,
    "task_attempts": 0,
    "completed_tasks": [],
    "pending_parallel": [],
    "forced_transition": None,
}

# --- Observation defaults (for default-filling) ---

DEFAULT_CONDITIONS = {
    "quality_score": 50,
    "completeness": COMP_PARTIAL,
    "blocker": None,
    "confidence": "low",
    "needs_replan": False,
    "escalate": False,
}

DEFAULT_SURPRISE = 0.5
