# 03 - Data Flow & Workspace Layout

This document specifies the on-disk workspace structure for Monadic Harness tasks, the purpose and schema of every file, and the protocols governing how data moves between the Python driver, subagents, and the main agent.

---

## 1. Workspace Layout

Every task lives under `~/.mh/tasks/{task-id}/`. The top-level `~/.mh/` directory also holds global configuration and history.

```
~/.mh/
├── config.yaml                       # user-level configuration
├── templates/                        # task graph templates
│   ├── general.yaml                  # default template
│   ├── migration.yaml                # codebase migration template
│   └── research.yaml                 # research & analysis template
├── tasks/
│   └── {task-id}/
│       ├── meta.json                 # task identity (id, goal, created, status)
│       ├── state.json                # monadic state (step, fuel, attempts, counters)
│       ├── profile.json              # accumulated environment knowledge
│       ├── ctrlflow/
│       │   ├── phase.json            # meta phase: {phase: "exec", replan_count: 0}
│       │   ├── task-graph.yaml       # condition-driven task graph (compiled)
│       │   ├── cursor.json           # execution cursor + forced_transition
│       │   ├── plans/                # plan versions
│       │   │   ├── current.yaml      # active NL plan (compile-plan reads this)
│       │   │   ├── v1.yaml           # first plan version
│       │   │   ├── v2.yaml           # replan version (if any)
│       │   │   └── failure_summary.json  # auto-generated on replan
│       │   ├── escalation.json       # written by driver on escalation events
│       │   └── resolution.json       # written by main agent to resolve escalation
│       ├── dataflow/
│       │   ├── instructions/         # one file per task node execution
│       │   │   └── {node-id}-{attempt}.md
│       │   ├── observations/         # one file per task node execution
│       │   │   ├── {node-id}-{attempt}.json
│       │   │   └── {group_id}-merged.json  # parallel merge result
│       │   ├── scratchpad/           # subagent working memory (free-form)
│       │   └── artifacts/            # final outputs + task report
│       └── trace/
│           └── trace.jsonl           # append-only audit log
└── history.jsonl                     # index of all tasks (for resume/list)
```

---

## 2. File-by-File Specification

### meta.json

- **Purpose**: Task identity and top-level status.
- **Format**: JSON.
- **Who Writes**: Driver on `init`.
- **Who Reads**: Driver, `status` command, `report` command.
- **Lifecycle**: Created once at task initialization. Updated in-place on status changes (`running` / `done` / `aborted`).

### state.json

- **Purpose**: Monadic state container — the core data that the `bind` cycle reads and writes.
- **Format**: JSON.
- **Who Writes**: Driver on every `observe` step.
- **Who Reads**: Driver on every `decide` step.
- **Lifecycle**: Created on `init`, updated on every cycle iteration.

Schema:

```json
{
  "step": 5,
  "fuel_remaining": 22,
  "total_attempts": 7,
  "consecutive_failures": 0,
  "surprise_accumulator": 0.58
}
```

Note: `surprise_accumulator` is the sum of squared surprise values (Σ(surprise²)), not a linear sum. This weights high-surprise events exponentially more than low ones. Per [[specs]] I3.

### profile.json

- **Purpose**: Accumulated knowledge about the working environment. Discovered facts that persist across task steps.
- **Format**: JSON (flat key-value).
- **Who Writes**: Driver (merges `profile_updates` from observations).
- **Who Reads**: Driver (for assembling context bundles) and subagents (directly, when referenced in instructions).
- **Lifecycle**: Created on `init` (empty object). Grows monotonically — keys are added or updated, never deleted.

### ctrlflow/phase.json

- **Purpose**: Meta phase tracker. Records which high-level phase the task is in.
- **Format**: JSON.
- **Who Writes**: Driver on phase transitions.
- **Who Reads**: Driver on every `decide`.
- **Lifecycle**: Created when the task enters its first phase. Updated on each phase transition.

Schema:

```json
{
  "phase": "plan" | "exec" | "verify",
  "replan_count": 0,
  "phase_entered_at": "2026-04-12T14:20:00Z"
}
```

### ctrlflow/task-graph.yaml

- **Purpose**: The compiled condition-driven task graph. Defines all task nodes, their dependencies, and transition conditions.
- **Format**: YAML.
- **Who Writes**: Driver on `compile-plan`.
- **Who Reads**: Driver on every `decide`.
- **Lifecycle**: Created during the plan phase. May be modified by the driver on graph mutation (task insertion, dependency rewiring) or after escalation resolution.

### ctrlflow/cursor.json

- **Purpose**: Execution pointer into the task graph.
- **Format**: JSON.
- **Who Writes**: Driver.
- **Who Reads**: Driver.
- **Lifecycle**: Created when execution begins. Updated after each task node completes or on parallel dispatch.

Schema:

```json
{
  "current_task": "t3a",
  "task_attempts": 1,
  "completed_tasks": ["t1", "t2"],
  "pending_parallel": ["t3a", "t3b", "t3c"],
  "forced_transition": null
}
```

**Field semantics**:

- **`current_task`**: The node currently being executed. Set by `decide` when transitioning to a new node. Reset to the next node's id on transition; `null` when no more tasks.
- **`task_attempts`**: Per-node retry counter. Reset to 0 when transitioning to a new node. Incremented on retry.
- **`completed_tasks`**: List of node IDs that completed **successfully** (`observation.success == true`). Only `observe` appends to this list. Failed observations do NOT add to completed_tasks. Per [[specs]] P8. This is the authoritative record of what work is done — wait nodes check this to determine if dependencies are met.
- **`pending_parallel`**: Node IDs in the current parallel dispatch group. Set by `decide` when dispatching parallel tasks. Cleared by `decide` when the corresponding wait node proceeds. Per [[specs]] P7.
- **`forced_transition`**: Set by invariant checks during `observe` and consumed (set back to `null`) by `decide`. See [[07-invariants-and-escalation]] and [[specs]] I1, I2.

### ctrlflow/plans/

- **Purpose**: Plan version history. Contains the active NL plan and all previous versions for traceability.
- **Format**: YAML (NL plan format per [[05-skill-protocol]]).
- **Who Writes**: Plan subagent writes `current.yaml`. Driver copies `current.yaml` to `v{n}.yaml` before each replan.
- **Who Reads**: `compile-plan` reads `current.yaml`. Replan subagent receives previous plan versions in its context bundle.
- **Lifecycle**: `current.yaml` created during the plan phase. Versioned copies (`v1.yaml`, `v2.yaml`, ...) created on each replan. Never deleted.

### ctrlflow/plans/failure_summary.json

- **Purpose**: Auto-generated context for replan subagent. Summarizes what went wrong in the previous execution so the replan can avoid repeating mistakes.
- **Format**: JSON. Schema (per [[specs]] D16, D17):

```json
{
  "failed_nodes": ["t1", "t3a"],
  "failure_signals": {"t1": "webpack config minified", "t3a": "type error"},
  "evidence_contradictions": [
    {"node": "t1", "detail": "completeness=full but tests_passing=false"}
  ],
  "profile_facts": {"vite_version": "6.2", "config_format": "ts"},
  "total_steps_used": 12,
  "fuel_remaining": 18
}
```

- **Who Writes**: Driver, automatically when entering replan phase (from forced transition or `needs_replan` condition).
- **Who Reads**: Plan subagent receives it in the plan instruction under "## Previous Failure".
- **Lifecycle**: Created on replan. Overwritten on subsequent replans.

### ctrlflow/escalation.json

- **Purpose**: Captures the escalation event when the driver determines that autonomous recovery is not possible.
- **Format**: JSON.
- **Who Writes**: Driver when escalation is triggered.
- **Who Reads**: Main agent.
- **Lifecycle**: Created on escalation. Consumed (read and acted upon) by the main agent. See [[07-invariants-and-escalation]] for full schema.

### ctrlflow/resolution.json

- **Purpose**: The main agent's response to an escalation — contains the decision and any graph mutations.
- **Format**: JSON.
- **Who Writes**: Main agent after analyzing the escalation.
- **Who Reads**: Driver on `resume`.
- **Lifecycle**: Created by the main agent during escalation handling. Consumed by the driver to apply the resolution. See [[07-invariants-and-escalation]] for full schema.

### dataflow/instructions/{node-id}-{attempt}.md

- **Purpose**: Assembled context bundle (instruction) for a subagent.
- **Format**: Markdown.
- **Who Writes**: Driver on `decide`.
- **Who Reads**: Subagent (passed as its prompt/instruction).
- **Lifecycle**: One file per task node execution attempt. Never modified after creation.

`node-id` refers to task graph node identifiers (e.g., `t1`, `t3a`), not the workspace task-id.

### dataflow/observations/{node-id}-{attempt}.json

- **Purpose**: Structured output from subagent execution.
- **Format**: JSON (see Observation Protocol below).
- **Who Writes**: Subagent (guided by instruction template). After validation, the driver writes back the canonical (normalized) observation to the same file.
- **Who Reads**: Driver on `observe`; `decide` reads canonical (normalized) observations for condition evaluation.
- **Lifecycle**: One file per execution attempt. Overwritten once by the driver after validation with the normalized form.

`node-id` refers to task graph node identifiers (e.g., `t1`, `t3a`), not the workspace task-id.

### dataflow/scratchpad/

- **Purpose**: Free-form working memory for subagents. Temporary notes, intermediate results, draft files.
- **Format**: Any. Subagents can create and read files here freely.
- **Who Writes**: Subagents.
- **Who Reads**: Subagents (and selectively included in context bundles by the driver).
- **Lifecycle**: Grows during execution. Driver does not process these files — they are raw working space.

### dataflow/artifacts/

- **Purpose**: Final task outputs. The deliverables of the task.
- **Format**: Any. Includes `task-report.md` generated by the driver on completion.
- **Who Writes**: Subagents (deliverables), driver (`task-report.md`).
- **Who Reads**: User, main agent, `report` command.
- **Lifecycle**: Populated during execution. Persists after task completion.

### trace/trace.jsonl

- **Purpose**: Append-only audit log of every driver step.
- **Format**: JSONL (one JSON object per line).
- **Who Writes**: Driver on every step.
- **Who Reads**: `report` command, `status` command, plan/replan subagent (recent entries).
- **Lifecycle**: Created on first step. Only appended to, never modified. See Trace Format below.

### history.jsonl

- **Purpose**: Global index of all tasks across the workspace.
- **Format**: JSONL.
- **Who Writes**: Driver — appends on `init` and updates on task completion.
- **Who Reads**: `list` command, `resume` command.
- **Lifecycle**: Persists across tasks. One line per task, appended on creation and updated on completion.

Schema per line:

```json
{
  "task_id": "abc123",
  "goal": "Migrate webpack to vite",
  "created": "2026-04-12T14:00:00Z",
  "status": "done",
  "completed": "2026-04-12T14:45:00Z",
  "fuel_used": 18,
  "steps": 12
}
```

---

## 3. Separation Logic

The task directory is split into three zones with distinct ownership semantics.

### ctrlflow/ — Control Plane

Files the Python driver reads to decide "what to do next." The driver is the primary reader and writer. Subagents should **not** modify these files. Contains the task graph, execution cursor, phase tracker, and escalation protocol files.

### dataflow/ — Data Plane

Files that subagents read and write — the content of the work itself. The driver writes instructions and reads observations, but the working content (scratchpad, artifacts) belongs to subagents.

### trace/ — Observability Plane

Append-only audit data. Neither control nor data — purely for auditing, reporting, and debugging. Never modified after writing.

### Root-Level Bridge Files

`state.json` and `profile.json` live at the task root (outside both `ctrlflow/` and `dataflow/`) because they bridge both worlds: the driver reads and writes them for control decisions, but `profile.json` is also directly readable by subagents for environment context.

---

## 3.5 File Ownership Contract

Strict ownership rules prevent conflicting writes. Per [[specs]] C9.

| Owner | Can Write | Cannot Write |
|-------|-----------|-------------|
| **Driver** | `state.json`, `profile.json`, `ctrlflow/*` (all files), `trace/*` | `dataflow/*` (except assembled instructions) |
| **Main Agent** | `ctrlflow/resolution.json` only | Everything else in `ctrlflow/` |
| **Plan Subagent** | `ctrlflow/plans/current.yaml` | Everything else |
| **Exec Subagent** | `dataflow/observations/{node-id}-{attempt}.json`, `dataflow/scratchpad/*`, `dataflow/artifacts/*` | `ctrlflow/*`, `state.json`, `profile.json` |
| **Verify Subagent** | `dataflow/observations/verify-{attempt}.json` | Same restrictions as exec |

The driver also writes `dataflow/instructions/{node-id}-{attempt}.md` — these are assembled context bundles, not subagent output.

Violations of ownership (e.g., a subagent modifying `ctrlflow/` files) are not enforced at the filesystem level in v1. They are enforced by instruction — the subagent's context bundle explicitly states which files it may write.

---

## 3.6 Observation Discovery Protocol

Per [[specs]] D13, the driver never uses implicit "latest file" discovery. All observation reads are explicit.

**Single task**: The main agent calls `pymh observe --node <node-id> --attempt <n>`. The driver reads exactly `dataflow/observations/{node-id}-{n}.json`.

**Parallel group**: The main agent calls `pymh observe --parallel <id1>,<id2>,...`. The driver reads each node's latest-attempt observation (by glob sorting), merges them (see [[06-concurrency]]), writes the merged result to `dataflow/observations/{group_id}-merged.json` (where `group_id` is the sorted, underscore-joined node IDs), and processes the merged result.

**Missing observation**: If the specified file does not exist, the driver synthesizes a failure observation with `success: false`, `escalate: true`, `completeness: "none"`, `surprise: 0.8`, and logs a `validation_warning: "observation file missing"` in the trace.

This eliminates the risk of reading stale data from a previous attempt or a different node's observation.

---

## 4. Profile Management

Per [[specs]] D3, D4.

### Design Rationale

Profile is a **separate file** (not embedded in `state.json`) so that subagents can read it directly without needing to parse driver-internal state.

### Merge Strategy

Mechanical merge — the driver reads `profile_updates` from each observation and:

1. Updates existing keys with new values.
2. Adds new keys.
3. Never deletes keys.

Profile grows monotonically. This is intentional: discovered facts about the environment do not un-discover themselves.

### Size Considerations

No size cap for v1. Profile is unlikely to exceed a few KB in MVP tasks. If profile growth becomes a problem in future versions, a summarization or eviction strategy can be added.

### Parallel Merge Semantics

When parallel subagents produce observations simultaneously, the driver merges them in **sorted task-id order** (alphabetically). This means:

- If two parallel subagents write **different keys**, both are preserved (no conflict).
- If two parallel subagents write **the same key** with different values, the higher task id wins (last-write-wins in sorted order).

This is a known v1 limitation. The higher task id wins regardless of semantic correctness. Documented and accepted for MVP scope.

---

## 5. Observation Protocol

Every subagent execution must produce a structured observation. This is the contract between subagents and the driver.

### Full Schema

```json
{
  "success": true,
  "signal": "one-line summary of information value",

  "conditions": {
    "quality_score": 85,
    "completeness": "full",
    "blocker": null,
    "confidence": "high",
    "needs_replan": false,
    "escalate": false
  },

  "evidence": {
    "tests_passing": true,
    "build_success": true,
    "command_exit_codes": [0, 0],
    "artifact_exists": true
  },

  "tags": {
    "coverage": 87
  },

  "surprise": 0.3,

  "profile_updates": {
    "vite_version": "6.2",
    "config_format": "ts"
  },

  "files_changed": ["vite.config.ts", "package.json"],
  "new_tasks": [],

  "narrative": "Installed vite successfully. The existing webpack config uses a custom resolve alias setup that will need special handling."
}
```

### Field Specification

| Field             | Type      | Required | Who Reads            | Purpose                                                                                                                 |
| ----------------- | --------- | -------- | -------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `success`         | bool      | yes      | driver               | Overall success/failure of this execution                                                                               |
| `signal`          | string    | yes      | driver, report       | One-line summary for progress display and trace                                                                         |
| `conditions`      | object    | yes      | driver               | 6 core self-assessment values for transition evaluation. See [[specs]] D5 for separation from evidence              |
| `evidence`        | object    | no       | driver               | Hard signals from tool execution: test results, build status, command exit codes, file existence. Driver trusts evidence over conditions when they contradict. Per [[specs]] D5 |
| `tags`            | object    | no       | driver               | Open-ended key-value pairs for custom conditions                                                                        |
| `surprise`        | float 0-1 | yes      | driver               | Quantified unexpectedness; high values trigger re-evaluation                                                            |
| `profile_updates` | object    | no       | driver               | Key-value pairs to merge into profile.json                                                                              |
| `files_changed`   | string[]  | no       | driver, report       | Paths of modified files                                                                                                 |
| `new_tasks`       | object[]  | no       | driver               | Suggested new task nodes to insert into graph                                                                           |
| `narrative`       | string    | no       | plan/replan subagent | Free-form context. Driver passes through but does NOT parse. Carries rich context that structured fields cannot capture |

**Notes on `conditions`**: The six core condition fields (`quality_score`, `completeness`, `blocker`, `confidence`, `needs_replan`, `escalate`) are evaluated by the task graph's transition conditions. See [[02-control-flow]] for the condition space definition. The `completeness` field uses a three-value enum: `"full"` | `"partial"` | `"none"`. "none" means zero progress (used in synthesized failure observations and catastrophic failures). Per [[specs]] D14.

**Notes on `tags`**: Open-ended key-value pairs that can be referenced by custom conditions in the task graph. For example, a task graph could define a transition condition `tags.tests_passing == true`. Tags extend the condition space without modifying the core schema.

**Notes on `narrative`**: This is the "information-rich but unstructured" complement to the structured fields. The driver does not parse it — it is passed through to plan/replan subagents who benefit from the qualitative context. This is where subagents communicate nuances, surprises, and observations that do not fit into the structured fields.

**Evidence-Conditions Contradiction Detection**: Per [[specs]] D7, the driver compares `evidence` fields against `conditions` after reading an observation. If they contradict (e.g., `conditions.completeness == "full"` but `evidence.tests_passing == false`), the driver auto-raises `surprise` to at least `0.7`. This is a zero-token safety check — no LLM call needed. Contradiction events are logged in the trace with `validation_warnings`.

---

## 6. Observation Validation

Per [[specs]] D2.

The driver uses **default-filling with logging** (Option B) — a lenient strategy that keeps the loop running while recording anomalies.

### Validation Rules

**Missing `conditions` block**: Fill all 6 fields with conservative defaults:

```json
{
  "quality_score": 50,
  "completeness": "partial",
  "blocker": null,
  "confidence": "low",
  "needs_replan": false,
  "escalate": false
}
```

**Missing or non-numeric `surprise`**: Default to `0.5`.

**Missing `success`**: Default to `false` (conservative — assume failure when uncertain).

**Entire observation is not valid JSON**: Treat as failure with `escalate: true`. Log the raw output in the `narrative` field of the synthesized observation.

**Type coercion for core conditions**: The 6 core condition fields are coerced to their expected types. String booleans (`"true"`, `"True"`, `"TRUE"`) are converted to `true`; `"false"` variants to `false`; `"null"`/`"None"` to `null`; numeric strings for `quality_score` to `int`. This prevents silent evaluation failures when subagents produce string representations of typed values. Applied only to the 6 core fields, not to tags or custom fields. Per [[specs]] D8.

**Namespace collision stripping**: Any keys in `observation.conditions` that match system condition names are stripped with a warning. Reserved names: `fuel_remaining`, `task_attempts`, `consecutive_failures`, `total_attempts`, `step`, `surprise_accumulator`. This prevents subagents from accidentally shadowing system state in the condition engine. Per [[specs]] D9.

**Success/completeness reconciliation**: After default-filling and type coercion, the driver reconciles `success` and `completeness` to ensure consistency:
- `success=true` + `completeness!="full"` -> `completeness` set to `"full"` (success implies full completion)
- `success=false` + `completeness="full"` -> `success` set to `true` (full completion implies success)

**Canonical write-back**: After all validation and reconciliation steps, the driver writes the normalized observation back to the same file on disk. This ensures that `decide` (and any subsequent readers) see the canonical form, not raw subagent output. The trust boundary is at observation normalization.

### Logging

All default-filling events are logged in `trace.jsonl` with a `validation_warnings` array on the trace entry. Each warning is a string describing what was filled (e.g., `"conditions block missing, filled with defaults"`, `"surprise field non-numeric, defaulted to 0.5"`).

### Rationale

No strict rejection for v1. Asking a subagent to rewrite its observation is expensive (burns fuel) and may fail again, producing the same malformed output. Default-filling is cheaper and keeps the loop progressing, while the trace captures the anomaly for debugging.

---

## 7. Context Bundles

Each subagent receives a tailored instruction file — the Reader Monad in practice. The driver assembles the instruction based on the subagent's role, including only the information relevant to that role.

### Visibility Matrix

| Subagent Role | What It Sees | What It Does NOT See |
|---------------|-------------|---------------------|
| **Plan** | Goal, profile.json, previous plan failures (from trace), template (if applicable) | Raw scratchpad files, old observations, detailed trace |
| **Exec** (task node) | Task action description, all profile entries, previous attempt narrative (if retry) | Other tasks' details, full trace, scratchpad files |
| **Verify** | Goal, artifacts list, acceptance criteria | Profile, detailed attempt history, scratchpad |

### Assembly Process

1. The driver determines the subagent role from the current phase and task node type.
2. The driver selects the relevant data sources per the visibility matrix above.
3. The driver writes the assembled instruction to `dataflow/instructions/{node-id}-{attempt}.md`.
4. The main agent passes this file as the subagent's prompt.

The instruction file is a self-contained document. Subagents do not need to read any other file to understand their assignment (though exec subagents may reference scratchpad files mentioned in the instruction).

### Instruction File Template

Per [[specs]] D10. V1 uses simple markdown with labeled sections.

**Exec subagent instruction** (`dataflow/instructions/{node-id}-{attempt}.md`):

```markdown
# Task: {node-id} (attempt {attempt})

## Action
{action text from task graph node}

## Context
{all profile.json entries as "- key: value" list, or "(empty)"}

## Previous Attempt
{if retry: summary of last observation's narrative, otherwise omitted}

## Output
Write your observation to: dataflow/observations/{node-id}-{attempt}.json
Follow the observation schema: include conditions, evidence, surprise, narrative, profile_updates.
```

**Plan subagent instruction** (via `decide --phase plan`):

```markdown
# Plan Task

## Goal
{goal from meta.json}

## Profile
{all profile.json entries}

## Template
{template content if exists, otherwise omitted}

## Previous Failure
{failure_summary.json content if replanning, otherwise omitted}

## Output
Write your plan to: ctrlflow/plans/current.yaml
Follow the plan format: each step needs id, action, success_criteria, retry_strategy.
```

**Verify subagent instruction** (via `decide --phase verify`):

```markdown
# Verify Task

## Goal
{goal from meta.json}

## Artifacts
{list of files in dataflow/artifacts/}

## Completed Tasks
{summary of completed task nodes and their outcomes}

## Output
Write your observation to: dataflow/observations/verify-{attempt}.json
Include: goal_met, accepted_artifacts, missing_items, evidence_summary, recommended_action.
```

---

## 8. Trace Format

Each line in `trace/trace.jsonl` is a self-contained JSON object recording one driver step.

### Schema

```json
{
  "timestamp": "2026-04-12T14:23:01Z",
  "step": 5,
  "phase": "exec",
  "task_id": "t3a",
  "attempt": 1,
  "action": "dispatch",
  "observation_summary": "migrated src/app/index.tsx to vite entry format",
  "conditions": {
    "quality_score": 90,
    "completeness": "full",
    "confidence": "high"
  },
  "surprise": 0.1,
  "fuel_remaining": 22,
  "validation_warnings": [],
  "invariant_fired": null
}
```

### Consumers

- **`pymh report`**: Reads the full trace to generate the task summary report in `dataflow/artifacts/task-report.md`.
- **`pymh status`**: Reads recent trace entries to display current activity.
- **Plan/replan subagent**: Receives recent trace entries in its context bundle to inform replanning decisions.
- **Post-mortem debugging**: The trace is the primary artifact for understanding what happened during a task execution.

### Properties

- **Append-only**: Lines are only ever appended. No line is modified or deleted after writing.
- **One line per step**: Each driver cycle produces exactly one trace entry.
- **Self-contained**: Each line can be understood independently (no references to other lines required).

---

## Cross-References

- [[02-control-flow]] — Condition space referenced from observations; transition evaluation logic.
- [[04-python-driver]] — Driver commands that read and write workspace files.
- [[07-invariants-and-escalation]] — Full schemas for `escalation.json` and `resolution.json`.
- [[08-user-interface]] — How trace data feeds into user-facing reports and status display.
- [[05-skill-protocol]] — NL plan format referenced from ctrlflow/plans/.
- [[06-concurrency]] — Parallel observation merge referenced from Observation Discovery Protocol.
- [[specs]] — Decision log entries D3, D4 (profile management), D2 (observation validation), I1 (forced_transition), D13 (observation discovery), I3 (surprise²), D5 (evidence-conditions split), D7 (contradiction detection), D16 (failure_summary), C9 (file ownership), D8 (bool coercion), D10 (instruction template), D9 (namespace collision), P8 (completed_tasks success only), P7 (pending_parallel), D17 (failure_summary schema), D14 (completeness "none"), D18 (canonical observation write-back), D19 (success/completeness reconciliation).
