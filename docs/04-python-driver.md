# 04 — Python Driver (`pymh`)

The deterministic interpreter that runs the Monadic Harness execution loop.

---

## 1. Design Philosophy

The driver is responsible for all decidable logic. Every condition evaluation,
state transition, fuel decrement, and invariant check happens inside `pymh`
at zero token cost. The LLM is never invoked for work the driver can perform
itself.

The analogy is a Haskell interpreter evaluating monadic expressions. The driver
interprets condition-driven transitions deterministically — reading observations,
matching rules, updating cursors. When it encounters something it cannot
interpret (generating a plan, writing code, reasoning about an unfamiliar
failure), it yields control to a more powerful but more expensive "interpreter":
the LLM. This boundary is sharp and intentional. Everything on the driver side
of the boundary is free. Everything on the LLM side costs fuel.

### External Dependencies

Only one: **PyYAML**, for parsing `task-graph.yaml`.

Everything else is Python standard library — `json`, `pathlib`, `datetime`,
`argparse`, `collections`, `re`, `copy`, `sys`, `os`.

### Installation

```
pip install pymh
```

After installing, run the setup command to initialize user-level directories:

```bash
pymh setup
```

This creates `~/.mh/` (config, templates, history) and copies skill files to `~/.claude/skills/mh/`. The setup command is idempotent — safe to run multiple times. If `pymh setup` has not been run, `pymh init` will auto-create `~/.mh/` as a fallback (but will not install skill files). Per [[specs]] A8.

This installs `pymh`, default templates, and PyYAML. See
[[#7. Dependencies and Installation]] for full details.

---

## 2. Command Reference

Each command documents: synopsis, arguments, what it reads, what it writes,
stdout output, and side effects.

---

### `init`

**Synopsis**

```
pymh init --goal "<goal>" --fuel N [--template <name>]
```

**Arguments**

| Argument     | Required | Default | Description                          |
|-------------|----------|---------|--------------------------------------|
| `--goal`     | yes      | —       | Natural-language goal string         |
| `--fuel`     | no       | 30      | Initial fuel budget (configurable in config.yaml) |
| `--template` | no       | `general` | Template name from `~/.mh/templates/` |

**What It Reads**

- `~/.mh/config.yaml` (for defaults)
- `~/.mh/templates/{name}.yaml` (if template specified)

**What It Writes**

Creates workspace under `~/.mh/tasks/{generated-task-id}/`:

```
{task-id}/
  meta.json            # goal, template, created_at, status
  state.json           # fuel=N, step=0, counters zeroed
  profile.json         # {} (empty, populated during execution)
  ctrlflow/
    phase.json         # phase=plan
    plans/
  dataflow/
    instructions/
    observations/
    scratchpad/
    artifacts/
  trace/
    trace.jsonl
```

Appends entry to `~/.mh/history.jsonl`.

**Stdout**

```
INIT:{task-id}:{workspace-path}
```

**Side Effects**

- Generates a unique task ID (timestamp + short hash of goal).
- If `~/.mh/` does not exist, creates it with default structure.

---

### `decide`

**Synopsis**

```
pymh decide [--phase plan|verify] [--workspace <path>]
```

**Arguments**

| Argument      | Required | Default           | Description                        |
|--------------|----------|-------------------|------------------------------------|
| `--phase`     | no       | —                 | Force plan or verify phase         |
| `--workspace` | no       | last active task  | Path to task workspace             |

**What It Reads**

- `state.json` — current fuel, step count
- `ctrlflow/phase.json` — current phase
- `ctrlflow/cursor.json` — current task, attempts counter
- `ctrlflow/task-graph.yaml` — the compiled task graph
- `dataflow/observations/{node-id}-{attempt}.json` — canonical (normalized) observations written back by `observe` after validation

**What It Writes**

- `dataflow/instructions/{node-id}-{attempt}.md` — instruction file(s) for the next dispatch
- `ctrlflow/cursor.json` — updated cursor position
- `ctrlflow/escalation.json` — if escalating

**Stdout**

One of the following lines:

| Output                              | Meaning                                     |
|-------------------------------------|---------------------------------------------|
| `DISPATCH:{node-id}:{attempt}:{instruction-path}` | Execute this single task |
| `PARALLEL:{node-id-1}:{attempt-1}:{path-1},{node-id-2}:{attempt-2}:{path-2},...` | Execute these tasks concurrently |
| `DONE`                              | Task graph complete                         |
| `REPLAN`                            | Conditions triggered a replan               |
| `ESCALATE:{reason}`                 | Needs agent or human intervention           |
| `BLOCKED:{reason}`                  | Waiting on dependency or user input         |

**Phase-specific behavior:**

- `--phase plan`: writes a plan subagent instruction that includes the goal,
  current profile, selected template, and any previous failure context.
- `--phase verify`: writes a verify instruction that includes the goal and a
  summary of all produced artifacts.

**Side Effects**

- Evaluates the condition engine (see [[#4. Condition Evaluation Engine]]).
- Checks for forced transitions injected by invariant checks.

---

### `observe`

**Synopsis**

```
pymh observe --node <node-id> --attempt <n> [--workspace <path>]
pymh observe --parallel <node-id-1>,<node-id-2>,... [--workspace <path>]
```

**Arguments**

| Argument      | Required         | Default          | Description            |
|--------------|------------------|------------------|------------------------|
| `--node`      | yes (single)     | -                | Node ID of the completed task graph node |
| `--attempt`   | yes (single)     | -                | Attempt number for this node |
| `--parallel`  | yes (parallel)   | -                | Comma-separated node IDs of completed parallel group |
| `--workspace` | no               | last active task | Path to task workspace |

**What It Reads**

- Single mode: `dataflow/observations/{node-id}-{attempt}.json`
- Parallel mode: `dataflow/observations/{node-id}-{latest-attempt}.json` for each node in the group, then merges (see [[06-concurrency]])
- `state.json`
- `profile.json`
- `ctrlflow/cursor.json`

**What It Writes**

- `profile.json` — merged with `profile_updates` from observation
- `state.json` — step incremented, fuel decremented, counters updated
- `ctrlflow/cursor.json` — updated with observation outcome
- `trace/trace.jsonl` — appended observation record

**Stdout**

Human-readable progress line:

```
[Step 5/30] exec:t3a  completeness=full | surprise=0.1
```

**Side Effects (in execution order)**

1. **Read observation** — reads the observation file. If missing, unparseable, or not a dict, synthesizes a failure observation with `success: false`, `escalate: true`, `surprise: 0.8`, `completeness: "none"`.
2. **Validation** — default-fills missing or malformed fields. Logs warnings for any fields that required correction. Never rejects an observation outright — always repairs and continues.
   - **Type coercion for core conditions**: normalizes string booleans (`"true"` → `true`, `"false"` → `false`), string nulls (`"null"` → `null`), and numeric strings for `quality_score` to their proper types. Applied only to the 6 core condition fields. Per [[specs]] D8.
   - **Namespace validation**: strips any keys from `observation.conditions` that collide with system condition names (`fuel_remaining`, `task_attempts`, `consecutive_failures`, `total_attempts`, `step`, `surprise_accumulator`). Logs a warning. Per [[specs]] D9.
   - **Evidence-conditions contradiction detection**: see [[#6. Invariant Checks]] §4.
3. **State update** — `step++`, `fuel--`, `total_attempts++`. On success: `consecutive_failures = 0`, `surprise_accumulator = 0.0`, then `surprise_accumulator += surprise²`. On failure: `consecutive_failures++`, then `surprise_accumulator += surprise²`.
4. **Profile merge** — shallow-merges `profile_updates` into `profile.json`. New keys are added, existing keys are overwritten.
5. **Cursor update** — if the observation was successful, appends the node ID to `completed_tasks`. Failed observations do NOT modify completed_tasks. Per [[specs]] P8.
6. **Invariant checks** — runs loop detection, drift check, and fuel management (see [[#6. Invariant Checks]]). If an invariant fires, it writes a `forced_transition` dict to `cursor.json` that the next `decide` call will consume.
7. **Canonical write-back** — writes the normalized observation back to the same file on disk, so that `decide` and any subsequent readers see the canonical form rather than raw subagent output. The trust boundary is at observation normalization.
8. **Trace append** — writes a complete record to `trace.jsonl` including timestamp, task ID, observation summary, conditions, surprise, state snapshot, validation warnings, and any invariant triggers.

For parallel mode, `observe --parallel` reads all member observations, validates each individual observation before merge, merges them (per [[06-concurrency]] §3), writes the merged result to `{group_id}-merged.json`, then runs steps 2-8 on the merged observation. Parallel dispatch always uses `attempt=0` in the trace entry.

---

### `compile-plan`

**Synopsis**

```
pymh compile-plan [--workspace <path>]
```

**Arguments**

| Argument      | Required | Default          | Description            |
|--------------|----------|------------------|------------------------|
| `--workspace` | no       | last active task | Path to task workspace |

**What It Reads**

- NL plan from `ctrlflow/plans/current.yaml` (written by plan subagent)

**What It Writes**

- `ctrlflow/task-graph.yaml` — compiled condition-driven task graph
- `ctrlflow/cursor.json` — reset to the first task in the graph

**Stdout**

```
COMPILED:{num-tasks} tasks, {num-parallel-groups} parallel groups
```

Or on failure:

```
COMPILE_ERROR:{description}
```

**Side Effects**

- Runs the full compilation pipeline (see [[#3. Compilation Pipeline]]).
- Validates the graph: no cycles, all references valid, all nodes reachable.
- Injects signal rules (escalate, needs_replan) and default retry on every task node.

---

### `status`

**Synopsis**

```
pymh status [--workspace <path>]
```

**Arguments**

| Argument      | Required | Default          | Description            |
|--------------|----------|------------------|------------------------|
| `--workspace` | no       | last active task | Path to task workspace |

**What It Reads**

- `meta.json`, `state.json`, `ctrlflow/phase.json`, `ctrlflow/cursor.json`, `profile.json`
- Latest entries from `trace/trace.jsonl`

**What It Writes**

Nothing.

**Stdout**

Compact status block:

```
Task: migrate-webpack-to-vite
Phase: exec | Step: 5/30 | Fuel: 25
Current: t3c (attempt 2) -- migrate worker entry point
Profile: {build: webpack5->vite6.2, entries: 3, done: 2/3}
Last: x dynamic require not supported (surprise=0.6)
```

**Side Effects**

None. Read-only command.

---

### `report`

**Synopsis**

```
pymh report [--workspace <path>]
```

**Arguments**

| Argument      | Required | Default          | Description            |
|--------------|----------|------------------|------------------------|
| `--workspace` | no       | last active task | Path to task workspace |

**What It Reads**

- `meta.json` — task ID, goal, status, created timestamp
- `state.json` — step count, fuel remaining
- `ctrlflow/phase.json` — current phase, replan count
- `profile.json` — accumulated environment knowledge
- `trace/trace.jsonl` — execution history
- `dataflow/artifacts/` — directory scan for artifact listing

**What It Writes**

- `dataflow/artifacts/task-report.md`

**Stdout**

```
{absolute-path-to-generated-report}
```

**Side Effects**

- Generates a markdown report with 7 sections: Header, Goal, Result, Execution Timeline, Profile, Artifacts, Key Decisions.
- Duration is computed from `meta.created` to the last trace entry timestamp (or current time if no traces exist).
- The report excludes `task-report.md` itself from the Artifacts listing.
- See [[08-user-interface]] §2 for the full report format specification.

---

### `resume`

**Synopsis**

```
pymh resume [--workspace <path>]
```

**Arguments**

| Argument      | Required | Default          | Description            |
|--------------|----------|------------------|------------------------|
| `--workspace` | no       | last active task | Path to task workspace |

**What It Reads**

- `ctrlflow/resolution.json` — written by the main agent after reviewing an
  escalation

**What It Writes**

- `ctrlflow/cursor.json` — updated based on resolution decision
- `state.json` — updated if resolution modifies fuel or counters
- Clears `ctrlflow/escalation.json`
- Clears `ctrlflow/resolution.json`

**Stdout**

The `RESUMED:` prefix indicates the resolution was applied successfully. The suffix is the next driver action — parsed with the same logic as `decide` output. Per [[specs]] K11.

```
RESUMED:{next-action}
```

Full output grammar:

| Output | When |
|--------|------|
| `RESUMED:DISPATCH:{node-id}:{attempt}:{instruction-path}` | `modify_graph`, `skip_task`, or `write_observation` resolved and next task ready |
| `RESUMED:ESCALATE:{reason}` | Resolution applied but next `decide` triggers a new escalation |
| `RESUMED:REPLAN` | `replan` resolution, or next `decide` triggers replan |
| `RESUMED:DONE` | Resolution applied and task graph is now complete |
| `RESUMED:ABORT` | `abort` resolution |

On validation failure (resolution.json is malformed or invalid):

```
ESCALATE:driver_validation_error:{reason}
```

The driver re-escalates with a new `escalation.json` so the main agent can fix and resubmit. The original `resolution.json` is preserved for debugging.

**Side Effects**

- Applies the resolution decision to the task graph and cursor.
- For `modify_graph`: patches `task-graph.yaml` with the changes specified in
  the resolution. Validates the modified graph (no cycles, no dangling gotos, cursor reachable). Per [[specs]] C15.
- For `skip_task`: forces `success=True` on the synthetic observation to ensure cursor advancement, then processes normally. Per [[specs]] C17.
- For `write_observation`: writes a synthetic observation into
  `dataflow/observations/` and triggers the normal observe flow.
- On success: clears both `escalation.json` and `resolution.json`.
- On validation failure: preserves `resolution.json` for debugging, writes new `escalation.json`.
- Appends a `resolve` trace entry with the decision and reasoning.

---

### `fuel`

**Synopsis**

```
pymh fuel --add N [--workspace <path>]
```

**Arguments**

| Argument      | Required | Default          | Description            |
|--------------|----------|------------------|------------------------|
| `--add`       | yes      | —                | Number of fuel units to add |
| `--workspace` | no       | last active task | Path to task workspace |

**What It Reads**

- `state.json`

**What It Writes**

- `state.json` — `fuel_remaining += N`

**Stdout**

```
FUEL:{new-total}
```

**Side Effects**

- Appends a fuel-add event to `trace/trace.jsonl`.

---

### `abort`

**Synopsis**

```
pymh abort [--workspace <path>]
```

**Arguments**

| Argument      | Required | Default          | Description            |
|--------------|----------|------------------|------------------------|
| `--workspace` | no       | last active task | Path to task workspace |

**What It Reads**

- `meta.json`, `state.json`, `profile.json`, `trace/trace.jsonl`

**What It Writes**

- `meta.json` — status set to `"aborted"`
- `dataflow/artifacts/task-report.md` — auto-generated final report
- `trace/trace.jsonl` — abort event with `"source": "cli"`

**Stdout**

```
ABORTED:{task-id}
```

**Side Effects**

- Sets `status: "aborted"` in `meta.json`.
- Auto-generates a final report (same as `report` command) at `dataflow/artifacts/task-report.md`.
- Marks the task as aborted in `~/.mh/history.jsonl`.
- Appends an abort trace entry with `"source": "cli"` (distinguishes from abort via `resume`, which uses `"source": "resume"`). Per [[specs]] K12.

---

## 3. Compilation Pipeline

Pass 2 of the planning phase: transforms a natural-language plan (written by
the plan subagent) into a condition-driven `task-graph.yaml` that the driver
can evaluate deterministically.

### Field Mapping

| NL Plan Field                | Compiled To                                                                 |
|-----------------------------|-----------------------------------------------------------------------------|
| `action`                     | Task node `action` field (passed through verbatim)                         |
| `depends_on_completion_of`   | Sequential ordering: task becomes successor of listed dependencies          |
| `can_parallel_with`          | Parallel group: generates a wait node; tasks dispatch together             |
| `success_criteria`           | Condition rule: `completeness == "full"` triggers goto next. V1 uses a generic completeness rule. Future versions will parse criteria into specific conditions. |
| `retry_strategy`             | Retry, escalate, or replan rules based on keyword matching                 |

### Keyword Matching for retry_strategy

The compiler uses lenient keyword matching (Option B) to interpret the
natural-language retry strategy:

| Pattern Detected                          | Generated Rule                        |
|------------------------------------------|---------------------------------------|
| Contains "replan" or "different approach" | `task_attempts >= max_task_attempts -> replan`        |
| Contains "proceed" or "move on" or "what we have" | `task_attempts >= max_task_attempts -> goto next` |
| Contains "stop" or "abort"               | `task_attempts >= max_task_attempts -> escalate`      |
| No match (default)                       | `task_attempts >= max_task_attempts -> escalate`      |

The retry threshold (`max_task_attempts`) is read from `config.yaml` `defaults:` section (default: 3). This is the same value used by the loop detection invariant, ensuring a single source of truth for the attempt threshold. The default is conservative: when the compiler does not understand the retry strategy, it escalates rather than silently continuing. All unrecognized patterns are logged with the original text so they can be reviewed.

### Automatic Injections

Every compiled task graph receives the following injections on each task node's `on_complete`:

1. **Escalate signal**: `escalate == true -> escalate`. Any observation that sets `escalate` in its conditions will trigger escalation.

2. **Replan signal**: `needs_replan == true -> replan`. Any observation that sets `needs_replan` in its conditions will trigger a replan regardless of the task's own rules.

3. **Retry strategy rule**: Based on the NL plan's `retry_strategy` field (see keyword matching above). Defaults to `task_attempts >= max_task_attempts -> escalate` if unrecognized. The threshold is read from config.

4. **Default retry**: `default: retry` as the last rule, ensuring that an observation without a matching condition triggers a retry rather than a hang.

**Note**: Fuel convergence is NOT a compiled rule. It is handled as a runtime invariant during `observe` (see [[#6. Invariant Checks]]). Per [[specs]] C12.

### Validation Checks

The compiler runs these checks before writing the graph:

**Fatal (block compilation):**
- **No cycles**: topological sort of the graph must succeed. A cycle means the
  plan has a circular dependency and cannot be executed.
- **All goto targets valid**: every `goto` in every rule must reference an
  existing task ID or a special node (`done`, `replan`, `escalate`).
- **All nodes reachable**: every task node must be reachable from the first task
  via some path through the graph.

**Warnings (non-fatal):**
- Unreferenced task IDs (defined but never targeted by any `goto`).
- Empty parallel groups (a `can_parallel_with` that references no valid tasks).

---

## 4. Condition Evaluation Engine

The core of the driver: deterministic evaluation of `on_complete` rules against
observation data. This is what makes the driver an interpreter rather than a
router.

### Parsing

Each condition string is split by `and` / `or` keywords. Each clause is parsed
as:

```
variable operator value
```

Examples:

```
completeness == "full"
task_attempts >= 3
blocker != null
surprise > 0.5 and completeness != "full"
```

### Supported Operators

| Operator | Meaning                |
|----------|------------------------|
| `==`     | Equal                  |
| `!=`     | Not equal              |
| `>=`     | Greater than or equal  |
| `<=`     | Less than or equal     |
| `>`      | Greater than           |
| `<`      | Less than              |

### Value Types

| Type     | Examples              |
|----------|-----------------------|
| Number   | `3`, `0.5`, `100`     |
| String   | `"full"`, `"partial"` |
| Boolean  | `true`, `false`       |
| Null     | `null`                |

### Variable Resolution

When the driver encounters a variable name, it resolves it by searching in
this order:

1. **`conditions`** block of the current observation
2. **`tags`** block of the current observation
3. **`system_conditions`** maintained by the driver (e.g., `fuel_remaining`,
   `task_attempts`, `consecutive_failures`)

First match wins. If the variable is not found in any scope, it resolves to
`null`.

**Note**: `evidence` fields are NOT part of the condition space. They are used only for the driver's internal contradiction detection (see [[#6. Invariant Checks]], §4). Condition rules cannot reference evidence fields. Per [[specs]] D6.

### Null Handling

- `blocker != null` evaluates to `true` if `blocker` has any non-null value.
- `blocker == null` evaluates to `true` if `blocker` is not set or is
  explicitly null.
- Unknown (unresolved) variables evaluate to `null`.
- Comparisons involving `null` and numeric operators (`>`, `<`, `>=`, `<=`)
  evaluate to `false`.

### Evaluation Order

Rules in `on_complete` are evaluated **top to bottom**. The first rule whose
condition matches wins. The `default` keyword always matches — it acts as a
catch-all and should be the last rule.

This situation cannot occur at runtime. The compiler injects a `default` rule into every `on_complete` block that lacks one (see Compilation Pipeline below). If the compiler's injected default is reached, the default action is `retry` for exec nodes and `escalate` for plan/verify nodes. Per [[specs]] C8.

### Logical Operators

- `and` / `or` use standard short-circuit evaluation.
- `and` binds tighter than `or`.
- In practice, conditions rarely exceed 1-3 clauses and never nest, so
  precedence ambiguity is unlikely. If a condition requires complex logic,
  that is a sign it should be handled by the LLM, not the driver.

### Condition Grammar (EBNF)

```
condition   = or_expr
or_expr     = and_expr ("or" and_expr)*
and_expr    = comparison ("and" comparison)*
comparison  = variable operator value
variable    = identifier ("." identifier)*
operator    = ">=" | "<=" | ">" | "<" | "==" | "!="
value       = number | quoted_string | "null" | "true" | "false"
identifier  = [a-z_][a-z0-9_]*
```

Maximum 3 comparisons per condition (enforced at compilation). No parentheses, no nesting. Per [[specs]] C4.

---

## 5. State Machine Runtime

The internal execution loop inside the `decide` command. This is the driver's
main interpreter cycle.

### Execution Flow

```
1. Read cursor.json
   |
   v
2. Is current_task a wait node?
   |-- yes --> Are all waited tasks in completed_tasks?
   |           |-- yes --> Proceed to wait node's goto target
   |           |-- no  --> Return BLOCKED:waiting
   |
   |-- no  --> Continue
   |
   v
3. Read observation for current_task (from last observe call)
   |
   v
4. Evaluate on_complete rules against merged conditions
   (observation conditions + system conditions)
   |
   v
5. Apply result:
   |
   |-- goto: <node-id>        --> Update cursor, return DISPATCH:<node-id>:<attempt>:<instruction-path>
   |-- goto: [<id1>, <id2>]   --> Update cursor, return PARALLEL:<id1>:<a1>:<path1>,<id2>:<a2>:<path2>,...
   |-- retry                  --> Increment task_attempts, return DISPATCH:<same-task>
   |-- replan                 --> Update phase.json, return REPLAN
   |-- escalate               --> Write escalation.json, return ESCALATE:<reason>
   |-- done                   --> Return DONE
   |
   v
6. Write updated cursor.json
```

### Wait Nodes

Wait nodes are synthetic nodes generated by the compiler when a task has
parallel dependencies. A wait node does not produce instructions or consume
fuel. It simply blocks until all its dependencies are in the `completed_tasks`
set, then transitions to its `goto` target.

### First Call (No Observation)

On the first `decide` call for a task (no observation exists yet), the driver
skips rule evaluation entirely and returns `DISPATCH:{current-task}` with a
fresh instruction file. This is the "first dispatch" for that task.

### Forced Transitions

If `cursor.json` contains a `forced_transition` field (injected by invariant
checks during `observe`), the driver applies it immediately without evaluating
`on_complete` rules. The forced transition is consumed (set back to `null` in
`cursor.json`) after use. Per [[specs]] I1.

The `forced_transition` field is always a dict with `type` and `reason`:

```json
{
  "type": "escalate" | "replan" | "verify_or_abort",
  "reason": "human-readable explanation"
}
```

The `decide` command reads `type` to determine the action, and uses `reason` for the `ESCALATE:{reason}` output and trace logging. Per [[specs]] I2.

### Failure Summary for Replan

When the driver enters a replan phase (either from meta verify failure or from `needs_replan: true`), it auto-generates a `failure_summary` from trace data and writes it to `ctrlflow/plans/failure_summary.json`. This file is included in the replan subagent's context bundle.

Schema:

```json
{
  "failed_nodes": ["t3c"],
  "failure_signals": {
    "t3c": "dynamic require not supported by vite"
  },
  "evidence_contradictions": [
    {"node": "t3c", "detail": "completeness=full but tests_passing=false"}
  ],
  "profile_facts": {
    "worker_entry_uses_dynamic_require": true
  },
  "total_steps_used": 8,
  "fuel_remaining": 22
}
```

This is generated by the driver from `trace.jsonl` and `profile.json` at zero LLM cost. The driver checks for both `"observe"` and `"observe_parallel"` trace entries when collecting failure data, ensuring that failures from parallel groups are included in the summary. Per [[specs]] D16.

---

## 6. Invariant Checks

Four checks run inside the `observe` command after state is updated.
When an invariant fires, it injects a forced transition into `cursor.json` that
overrides normal `on_complete` evaluation on the next `decide` call.

### 1. Loop Detection

**Trigger**: `task_attempts >= max_task_attempts` for the current task.

**Default threshold**: `max_task_attempts = 3`

**Action**: Force escalate. The task has been retried too many times — it is
likely fundamentally broken and retrying will not help. The agent (or user)
needs to examine the failure and decide how to proceed.

**Injected transition**:
```json
{
  "type": "escalate",
  "reason": "loop detected: {node-id} attempted {n} times"
}
```

### 2. Drift Check

**Trigger**: Either condition is met:
- `consecutive_failures >= max_consecutive_failures` (default: 3)
- `surprise_accumulator > drift_threshold` (default: 2.0)

The surprise accumulator tracks `Σ(surprise²)`. On success, it resets to 0.0
before adding the current observation's surprise² (per [[specs]] I4).
A success with low surprise effectively clears the accumulator.

**Action**: Force replan. Multiple consecutive failures or high accumulated
surprise indicates the plan is no longer well-suited to the actual state of the
codebase. A replan will produce a new task graph that accounts for discovered
realities.

**Injected transition**:
```json
{
  "type": "replan",
  "reason": "drift detected: {consecutive_failures} consecutive failures"
}
```
or:
```json
{
  "type": "replan",
  "reason": "drift detected: surprise accumulator {value} exceeds threshold {threshold}"
}
```

### 3. Fuel Management

**Trigger**: `fuel_remaining <= 0`

**Action**: Force convergence.

- If any useful artifacts have been produced (at least one task completed with `success: true`), enter the verify phase to assess the current state.
- If nothing useful has been produced, abort with report.

The user can add fuel mid-task via `pymh fuel --add N`.

**Injected forced_transition** (in `cursor.json`):
```json
{
  "type": "verify_or_abort",
  "reason": "fuel_exhausted"
}
```

The driver checks `completed_tasks` in `cursor.json` to determine the branch. Per [[specs]] C11.

### 4. Evidence-Conditions Contradiction Check

**Trigger**: `evidence` and `conditions` fields in an observation contradict each other.

**Detection rules** (pure Python, no LLM):

| Evidence Field | Conditions Field | Contradiction |
|---------------|-----------------|---------------|
| `tests_passing == false` | `completeness == "full"` | Yes |
| `build_success == false` | `quality_score >= 80` | Yes |
| `artifact_exists == false` | `success == true` | Yes |
| `command_exit_codes` contains non-zero | `confidence == "high"` | Yes |

**Action**: Auto-raise `surprise` to at least `0.7` in the observation before processing. Log the contradiction in `trace.jsonl` with a `validation_warning`.

This is not an invariant in the traditional sense (it does not inject a forced transition). It is a **pre-processing step** during `observe` that adjusts the observation before condition evaluation. Per [[specs]] D7.

### Threshold Configuration

All thresholds are configurable in `~/.mh/config.yaml`:

```yaml
defaults:
  fuel: 30
  template: general
  fuel_warning_threshold: 5
  max_task_attempts: 3
  max_consecutive_failures: 3
  max_replan_count: 3
  drift_threshold: 2.0
```

Invariant thresholds live under `defaults:` alongside other configuration (per [[specs]] I7).

See [[07-invariants-and-escalation]] for the full invariant specification and
escalation protocol, including how the main agent handles escalation.json and
writes resolution.json.

---

## 7. Dependencies and Installation

### Requirements

- Python 3.9+
- PyYAML (sole external dependency)

### What `pip install pymh` Provides

- **`pymh`** — the driver CLI, also runnable as `python3 -m pymh`
- **`pymh setup`** — initializes `~/.mh/` and copies skill files to `~/.claude/skills/mh/`
- **Default templates** installed to `~/.mh/templates/`
- **Config scaffold**: creates `~/.mh/config.yaml` if it does not exist, with
  documented defaults for all thresholds and settings

### Directory Structure After `pymh setup`

```
~/.mh/
  config.yaml          # global configuration
  history.jsonl        # task history log
  templates/           # plan templates
    general.yaml
    migration.yaml
    research.yaml
  tasks/               # per-task workspaces (created by init)
```

---

## Cross-References

- [[02-control-flow]] — condition space, transition rules, fuel semantics
- [[03-data-flow]] — workspace files the driver reads and writes
- [[07-invariants-and-escalation]] — full invariant specification and escalation protocol
- [[specs]] — rationale for specific threshold values and design choices
