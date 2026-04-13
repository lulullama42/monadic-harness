"""CLI entry point for pymh.

All 10 subcommands: init, setup, decide, observe, compile-plan, status, report, resume, fuel, abort.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import time

from pymh import __version__
from pymh.compiler import CompilationError, compile_plan
from pymh.decide import decide
from pymh.observe import format_progress_line, process_observation, process_parallel_observations
from pymh.report import generate_report
from pymh.resume import ResumeError
from pymh.resume import resume as resume_fn
from pymh.schemas.defaults import PHASE_EXEC, PHASE_PLAN, PHASE_VERIFY
from pymh.state import (
    append_history,
    append_trace,
    create_meta,
    create_phase,
    create_profile,
    create_state,
    now_iso,
    read_cursor,
    read_meta,
    read_phase,
    read_profile,
    read_state,
    read_trace,
    update_history_status,
    write_meta,
    write_phase,
    write_state,
)
from pymh.workspace import (
    create_workspace,
    ensure_mh_root,
    install_skill_files,
    install_templates,
    load_config,
    resolve_workspace,
)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="pymh",
        description="Monadic Harness — structured entropy management for agent tasks",
    )
    parser.add_argument("--version", action="version", version=f"pymh {__version__}")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- init ---
    p_init = subparsers.add_parser("init", help="Initialize a new task")
    p_init.add_argument("--goal", required=True, help="Natural-language goal string")
    p_init.add_argument("--fuel", type=int, default=None, help="Initial fuel budget")
    p_init.add_argument("--template", default=None, help="Template name")

    # --- decide ---
    p_decide = subparsers.add_parser("decide", help="Get next dispatch instruction")
    p_decide.add_argument("--phase", choices=[PHASE_PLAN, PHASE_VERIFY], default=None)
    p_decide.add_argument("--workspace", default=None)

    # --- observe ---
    p_observe = subparsers.add_parser("observe", help="Process subagent observation")
    p_observe.add_argument("--node", default=None, help="Node ID of completed task")
    p_observe.add_argument("--attempt", type=int, default=None, help="Attempt number")
    p_observe.add_argument("--parallel", default=None, help="Comma-separated node IDs")
    p_observe.add_argument("--workspace", default=None)

    # --- compile-plan ---
    p_compile = subparsers.add_parser("compile-plan", help="Compile NL plan to task graph")
    p_compile.add_argument("--workspace", default=None)

    # --- status ---
    p_status = subparsers.add_parser("status", help="Show current task state")
    p_status.add_argument("--workspace", default=None)

    # --- report ---
    p_report = subparsers.add_parser("report", help="Generate task report")
    p_report.add_argument("--workspace", default=None)

    # --- resume ---
    p_resume = subparsers.add_parser("resume", help="Resume after escalation resolution")
    p_resume.add_argument("--workspace", default=None)

    # --- fuel ---
    p_fuel = subparsers.add_parser("fuel", help="Add fuel to running task")
    p_fuel.add_argument("--add", type=int, required=True, help="Fuel units to add")
    p_fuel.add_argument("--workspace", default=None)

    # --- abort ---
    p_abort = subparsers.add_parser("abort", help="Abort task and generate report")
    p_abort.add_argument("--workspace", default=None)

    # --- setup ---
    p_setup = subparsers.add_parser("setup", help="Initialize ~/.mh/ and install skill files")
    p_setup.add_argument("--force", action="store_true", help="Overwrite existing skill files")

    args = parser.parse_args(argv)

    commands = {
        "init": cmd_init,
        "decide": cmd_decide,
        "observe": cmd_observe,
        "compile-plan": cmd_compile_plan,
        "status": cmd_status,
        "report": cmd_report,
        "resume": cmd_resume,
        "fuel": cmd_fuel,
        "abort": cmd_abort,
        "setup": cmd_setup,
    }

    handler = commands.get(args.command, cmd_stub)
    handler(args)


# --- Implemented commands ---


def cmd_init(args: argparse.Namespace) -> None:
    defaults = load_config()["defaults"]

    fuel = args.fuel if args.fuel is not None else defaults["fuel"]
    template = args.template if args.template is not None else defaults["template"]

    # Generate task ID: timestamp + short hash of goal
    ts = str(int(time.time()))
    goal_hash = hashlib.sha256(args.goal.encode()).hexdigest()[:8]
    task_id = f"{ts}-{goal_hash}"

    workspace = create_workspace(task_id)
    create_meta(workspace, task_id, args.goal, template)
    create_state(workspace, fuel)
    create_phase(workspace)
    create_profile(workspace)

    # Create empty trace file
    (workspace / "trace" / "trace.jsonl").touch()

    # Append to history
    append_history({
        "task_id": task_id,
        "goal": args.goal,
        "created": now_iso(),
        "status": "running",
        "fuel_used": 0,
        "steps": 0,
    })

    print(f"INIT:{task_id}:{workspace}")


def cmd_status(args: argparse.Namespace) -> None:
    workspace = resolve_workspace(args.workspace)

    meta = read_meta(workspace)
    state = read_state(workspace)
    phase = read_phase(workspace)
    cursor = read_cursor(workspace)
    profile = read_profile(workspace)

    goal = meta.get("goal", "unknown")
    current_phase = phase.get("phase", "unknown")
    step = state.get("step", 0)
    fuel_total = step + state.get("fuel_remaining", 0)
    fuel_remaining = state.get("fuel_remaining", 0)

    current_task = cursor.get("current_task") or None
    task_attempts = cursor.get("task_attempts", 0)

    # Compact profile summary
    profile_items = list(profile.items())[:5]
    profile_str = ", ".join(f"{k}: {v}" for k, v in profile_items)
    if len(profile) > 5:
        profile_str += f", ... ({len(profile)} total)"
    if not profile_str:
        profile_str = "(empty)"

    # Last trace entry
    traces = read_trace(workspace, last_n=1)
    if traces:
        last = traces[-1]
        last_signal = last.get("observation_summary", "n/a")
        last_surprise = last.get("surprise", 0)
        last_str = f"{last_signal} (surprise={last_surprise})"
    else:
        last_str = "no steps yet"

    print(f"Task: {meta.get('task_id', 'unknown')}")
    print(f"Goal: {goal}")
    print(f"Phase: {current_phase} | Step: {step}/{fuel_total} | Fuel: {fuel_remaining}")
    if current_task:
        print(f"Current: {current_task} (attempt {task_attempts})")
    print(f"Profile: {{{profile_str}}}")
    print(f"Last: {last_str}")


def cmd_fuel(args: argparse.Namespace) -> None:
    workspace = resolve_workspace(args.workspace)

    if args.add <= 0:
        raise SystemExit("--add must be a positive integer")

    state = read_state(workspace)
    old_fuel = state["fuel_remaining"]
    state["fuel_remaining"] = old_fuel + args.add
    write_state(workspace, state)

    # Trace the fuel event
    append_trace(workspace, {
        "timestamp": now_iso(),
        "action": "fuel_add",
        "fuel_added": args.add,
        "fuel_before": old_fuel,
        "fuel_after": state["fuel_remaining"],
    })

    print(f"FUEL:{state['fuel_remaining']}")


def cmd_report(args: argparse.Namespace) -> None:
    workspace = resolve_workspace(args.workspace)
    report_path = generate_report(workspace)
    print(report_path)


def cmd_resume(args: argparse.Namespace) -> None:
    workspace = resolve_workspace(args.workspace)
    try:
        output = resume_fn(workspace)
    except ResumeError as e:
        print(f"RESUME_ERROR:{e}")
        sys.exit(1)
    print(output)


def cmd_abort(args: argparse.Namespace) -> None:
    workspace = resolve_workspace(args.workspace)

    meta = read_meta(workspace)
    task_id = meta["task_id"]

    # Update meta status
    meta["status"] = "aborted"
    write_meta(workspace, meta)

    # Update history
    update_history_status(task_id, "aborted")

    # Trace the abort event
    append_trace(workspace, {
        "timestamp": now_iso(),
        "action": "abort",
        "task_id": task_id,
        "source": "cli",
    })

    # Generate report (per spec: abort writes task-report.md)
    generate_report(workspace)

    print(f"ABORTED:{task_id}")


def cmd_decide(args: argparse.Namespace) -> None:
    workspace = resolve_workspace(args.workspace)
    result = decide(workspace, phase_override=args.phase)
    print(result.output)


def cmd_observe(args: argparse.Namespace) -> None:
    workspace = resolve_workspace(args.workspace)

    if args.parallel:
        node_ids = [nid.strip() for nid in args.parallel.split(",")]
        result = process_parallel_observations(workspace, node_ids)
        group_id = "_".join(sorted(node_ids))
        progress = format_progress_line(
            result["state"], read_phase(workspace), group_id, result["observation"]
        )
    elif args.node is not None and args.attempt is not None:
        result = process_observation(workspace, args.node, args.attempt)
        progress = format_progress_line(
            result["state"], read_phase(workspace), args.node, result["observation"]
        )
    else:
        print("ERROR: observe requires --node/--attempt or --parallel")
        sys.exit(1)

    print(progress)

    # Print fuel warning if low
    fuel_warning = load_config()["defaults"]["fuel_warning_threshold"]
    if result["state"]["fuel_remaining"] <= fuel_warning and result["state"]["fuel_remaining"] > 0:
        print(f"WARNING: Low fuel: {result['state']['fuel_remaining']} steps remaining",
              file=sys.stderr)

    # Print invariant warning if fired
    if result.get("invariant_fired"):
        print(f"INVARIANT:{result['invariant_fired']}", file=sys.stderr)


def cmd_compile_plan(args: argparse.Namespace) -> None:
    workspace = resolve_workspace(args.workspace)
    try:
        result = compile_plan(workspace)
    except CompilationError as e:
        print(f"COMPILE_ERROR:{e}")
        sys.exit(1)

    # Print warnings
    for w in result.warnings:
        print(f"WARNING:{w.message}", file=sys.stderr)

    # Version the plan: copy current.yaml to v{n}.yaml
    plans_dir = workspace / "ctrlflow" / "plans"
    existing = sorted(plans_dir.glob("v*.yaml"))
    version = len(existing) + 1
    shutil.copy2(plans_dir / "current.yaml", plans_dir / f"v{version}.yaml")

    # Update phase to exec after successful compilation
    phase = read_phase(workspace)
    phase["phase"] = PHASE_EXEC
    write_phase(workspace, phase)

    # Trace the compilation event
    append_trace(workspace, {
        "timestamp": now_iso(),
        "action": "compile_plan",
        "num_tasks": result.num_tasks,
        "num_parallel_groups": result.num_parallel_groups,
        "warnings": len(result.warnings),
        "plan_version": version,
    })

    print(f"COMPILED:{result.num_tasks} tasks, {result.num_parallel_groups} parallel groups")


def cmd_setup(args: argparse.Namespace) -> None:
    root = ensure_mh_root()
    install_templates()
    skill_dir = install_skill_files(force=getattr(args, "force", False))
    print(f"Initialized {root}")
    print(f"Templates installed to {root / 'templates'}")
    print(f"Skill files installed to {skill_dir}")


# --- Stubs for unimplemented commands ---


def cmd_stub(args: argparse.Namespace) -> None:
    print(f"NOT_IMPLEMENTED:{args.command}")
    sys.exit(1)


if __name__ == "__main__":
    main()
