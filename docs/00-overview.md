# Monadic Harness — Architecture Overview

Monadic Harness (mh) is a Claude Code Skill that adds structured entropy management to long-horizon agent tasks. It wraps existing agent frameworks with an external control loop and deterministic guardrails, using monadic patterns to manage control flow, data flow, and side effects.

## The Problem

Current mainstream agents (Claude Code, Cursor, Codex, Openclaw) face the same failure mode on long-horizon tasks:

**A single ReAct loop + append-only context + hope the LLM does not get lost.**

Specifically:

1. **Control flow is implicit** — the LLM "remembers" what it is doing via context, but this memory degrades as context grows. No external structure guarantees it stays on the right path.
2. **Data flow is chaotic** — goals, intermediate results, errors, and tool outputs all pile into the same context window with no separation. The LLM must extract useful signal from increasing noise.
3. **Side effects are unmanaged** — every tool call result feeds directly back into context without structured processing. Failures trigger retries, but retries carry no new information.
4. **Concurrency is ad-hoc** — some frameworks support parallel tool calls, but this is framework-level optimization, not task-level structural concurrency. Users cannot declare "these subtasks are independent and can run in parallel."

## Core Value: Entropy Management

mh treats every long-horizon agent task as an entropy management problem. As tasks execute, entropy accumulates across five dimensions — state, control, side-effects, context, and goals. mh provides structured mechanisms to reduce entropy at each level.

See [[01-entropy-and-monads]] for the full theoretical framework and monad mappings.

## Architecture

mh operates as a two-layer control system with a deterministic Python driver at its core:

```
┌──────────────────────────────────────────────────────────────┐
│  Meta Control Flow (fixed, designed by mh)                   │
│                                                              │
│  plan ──→ exec ──→ verify ──┬──→ done                       │
│    ↑                        │                                │
│    └────────────────────────┘  (verify fails → replan)       │
│                                                              │
│  + invariant guardrails at every step                        │
│  + fuel management                                           │
│  + surprise-driven re-evaluation                             │
└──────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────┐
│  Task Control Flow (agent-planned, driver-compiled)          │
│                                                              │
│  Plan subagent produces natural language plan:               │
│    "install vite" → "convert config" → "migrate entries" ... │
│                                                              │
│  Python driver compiles to condition-driven state machine:   │
│    t1 ──[quality >= 80]──→ t2 ──[complete]──→ [t3a,t3b,t3c] │
│         [attempts >= 3]──→ t2                  (parallel)    │
│         [default]────────→ retry                             │
│                                                              │
│  Each node = one subagent invocation                         │
└──────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────┐
│  Invariant System (pure Python, zero tokens, every step)     │
│                                                              │
│  Loop detection │ Drift check │ Fuel management              │
│  ─────────────────────────────────────────────               │
│  Runs inside driver, overrides normal transitions if needed  │
└──────────────────────────────────────────────────────────────┘
```

## Key Components

- **Python Driver** (`pymh`) — deterministic control logic. Decides what to execute next, compiles plans into executable graphs, evaluates conditions, runs invariant checks. Zero token cost. See [[04-python-driver]].
- **SKILL.md Protocol** — orchestration instructions for the main Claude Code agent. Defines how to call the driver, dispatch subagents, and handle escalations. See [[05-skill-protocol]].
- **Subagents** — LLM agents spawned for specific roles: `plan` (produces task list), `exec` (executes one task node), `verify` (checks goal alignment when needed). Each receives a focused context bundle.
- **Workspace** (`~/.mh/`) — file-system-based state management. Separated into `ctrlflow/` (driver reads), `dataflow/` (subagents read/write), and `trace/` (audit log). See [[03-data-flow]].
- **Invariant System** — loop detection, drift check, and fuel management. Pure Python, runs on every step, catches problems the LLM misses. See [[07-invariants-and-escalation]].

## Project Identity

| Property       | Value                 |
| -------------- | --------------------- |
| Repository     | `monadic-harness`     |
| PyPI package   | `pymh`                |
| Skill trigger  | `/mh`                 |
| Short name     | `mh`                  |
| Primary target | Claude Code           |
| Compatibility  | OpenClaw skill format |

## Reading Guide

| Document | Covers | Read When |
|----------|--------|-----------|
| [[00-overview]] | This document — architecture, components, identity | Start here |
| [[01-entropy-and-monads]] | Theoretical foundations: entropy sources, monad mappings, design principles | Understanding the "why" |
| [[02-control-flow]] | Meta + task control flow, conditions, two-pass compilation, fuel | Designing or implementing the control system |
| [[03-data-flow]] | Workspace layout, profile, observation schema, context bundles | Designing or implementing data management |
| [[04-python-driver]] | `pymh` commands, compilation pipeline, condition evaluation | Implementing the driver |
| [[05-skill-protocol]] | SKILL.md layering, orchestration phases, subagent management | Implementing the skill |
| [[06-concurrency]] | Limited concurrency: parallel dispatch, merge semantics, conflict awareness | Implementing parallelism |
| [[07-invariants-and-escalation]] | Invariant checks, escalation protocol | Implementing safety mechanisms |
| [[08-user-interface]] | Progress display, task reports, user commands, templates | Designing user experience |
| [[specs]] | Canonical table of all settled decisions + deferred items | Quick reference for any design choice |
**Suggested reading order for newcomers**: 00 → 01 → 02 → 03 → 04 → 05 → 06 → 07 → 08. The `specs` document is a reference, not sequential reading. Deferred features are tracked in `specs.md § Deferred Items`.
