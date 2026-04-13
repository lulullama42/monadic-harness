# Control Flow

This document describes how the Monadic Harness controls execution at two
layers: a fixed **meta loop** that orchestrates planning, execution, and
verification, and a dynamic **task layer** that runs a condition-driven state
machine compiled from the agent's natural-language plan.

---

## Two-Layer Control Flow Model

```
                         META LAYER (fixed)
  ┌──────────────────────────────────────────────────────┐
  │                                                      │
  │   ┌──────┐    ┌──────┐    ┌────────┐    ┌──────┐    │
  │   │ PLAN ├───>│ EXEC ├───>│ VERIFY ├───>│ DONE │    │
  │   └──┬───┘    └──────┘    └───┬────┘    └──────┘    │
  │      ^                        │                      │
  │      │       replan           │                      │
  │      └────────────────────────┘                      │
  │                                                      │
  └──────────────────────────────────────────────────────┘

                         TASK LAYER (dynamic)
  ┌──────────────────────────────────────────────────────┐
  │                                                      │
  │   Condition-driven state machine compiled from       │
  │   the agent's NL plan.                               │
  │                                                      │
  │   ┌────┐  score>=80  ┌────┐  complete  ┌──────┐     │
  │   │ t1 ├────────────>│ t2 ├───────────>│ done │     │
  │   └─┬──┘             └─┬──┘            └──────┘     │
  │     │ retry             │ blocker!=null              │
  │     └──┐                v                            │
  │        │             ┌──────┐                        │
  │        └────>self    │t2_fix│                        │
  │                      └──────┘                        │
  │                                                      │
  └──────────────────────────────────────────────────────┘

                     INVARIANT SYSTEM
  ┌──────────────────────────────────────────────────────┐
  │  Runs at EVERY transition in both layers.            │
  │  Can force abort, escalate, or replan.               │
  └──────────────────────────────────────────────────────┘
```

Both layers share a single condition space. The invariant system
(see [[07-invariants-and-escalation]]) fires at every state transition in
either layer and can override normal control flow.

---

## Meta Control Flow

The meta layer is a fixed, three-phase outer loop. It never changes between
runs; all task-specific logic lives in the task layer inside the exec phase.

### Plan Phase

1. The driver spawns a **plan subagent**.
2. The plan subagent produces a **natural-language plan** (see
   [[#Two-Pass Plan Generation]] below).
3. The driver **compiles** the NL plan into a condition-driven task graph
   (see [[04-python-driver]] for compilation implementation).

The plan subagent has read access to the accumulated profile and, on replans,
the trace of prior attempts. It does not execute any tasks itself.

### Exec Phase

1. The driver walks the compiled task graph.
2. It **dispatches task nodes** to subagents — sequentially or in parallel
   depending on the graph structure.
3. Each subagent executes its action and returns an **observation**
   (see [[03-data-flow]] for the observation schema).
4. The driver **evaluates transition rules** on the completed node using the
   current condition space.
5. The first matching rule determines the next node (or `retry`, `replan`,
   `escalate`, `done`).
6. This cycle repeats until the graph reaches a terminal node.

Each iteration of this dispatch-observe-evaluate cycle costs **1 fuel**.

### Verify Phase

When the task graph reaches its terminal `done` node, control returns to the
meta layer for a lightweight **goal-alignment check**:

- "Is the original goal solved?"
- If **yes** — the task is complete. The driver generates a final report.
- If **no** — the driver triggers a **replan**. The new plan phase receives the
  full accumulated profile and trace, so knowledge is preserved across replans.

This is an entropy check, not a deep validation. It catches **goal drift** —
the most insidious entropy source in long-running agentic tasks.

### Verify Implementation

- **Who executes**: A dedicated verify subagent (not the main agent). Per [[specs]] K5.
- **Fuel cost**: 1 fuel (same as any other subagent dispatch).
- **Structured output**: The verify observation must include `goal_met` (bool), `accepted_artifacts` (list), `missing_items` (list), `evidence_summary` (object), and `recommended_action` ("done" | "repair" | "replan" | "abort"). Per [[specs]] K4.
- **Replan limit**: Controlled by `max_replan_count` (default 3). If exceeded → abort with report.

### Replan Counter

Each replan increments a counter. Invariants can inspect this counter and force
an abort if the replan count is too high (e.g., `replan_count >= 3`). This
prevents infinite plan-exec-verify loops.

---

## Task Control Flow: Condition-Driven State Machine

Traditional agent DAGs use static dependency edges: task B runs after task A
finishes, regardless of outcome. This is too rigid for real work.

The Monadic Harness replaces static DAGs with a **condition-driven state
machine**:

- Tasks are **nodes**.
- Transitions between nodes depend on **condition evaluation**, not fixed
  ordering.
- Each node carries an `on_complete` block with ordered transition rules.
- The driver evaluates rules top-to-bottom; **first match wins**.

This supports patterns that static DAGs cannot express:

- **Conditional branches** — `quality_score > 80` sends execution down path A;
  otherwise path B.
- **Retries with limits** — `task_attempts < 4` loops back to the same node;
  on the fourth failure, the transition switches strategy.
- **Dynamic termination** — a condition is met early, so remaining tasks are
  skipped and the graph jumps to `done`.

---

## Condition Space

Every transition rule references conditions drawn from a shared condition
space. There are two categories, maintained by different actors.

### System Conditions

These are maintained by the **driver**. Subagents cannot write to them.

```yaml
system_conditions:
  fuel_remaining: 25          # int, decrements each driver cycle
  total_attempts: 3           # int, total exec steps so far
  task_attempts: 1            # int, attempts on current task node
  consecutive_failures: 0     # int, resets on success
  current_phase: "exec"       # enum: plan | exec | verify
  surprise_level: 0.3         # float, from last observation
```

- `fuel_remaining` is the primary resource bound
  (see [[#Fuel Semantics]] below).
- `task_attempts` resets when the driver moves to a different node.
- `consecutive_failures` resets on any successful observation.
- `surprise_level` is derived from the observation; the driver computes it
  by comparing the observation against expectations.

### Task Conditions

These are reported by the **subagent** in its observation. There are six core
fields:

```yaml
task_conditions:
  quality_score: 85           # float 0-100, self-assessment
  completeness: "partial"     # enum: none | partial | full
  blocker: null               # string | null
  confidence: "high"          # enum: low | medium | high
  needs_replan: false         # bool
  escalate: false             # bool
```

- `quality_score` — the subagent's self-assessed quality of its output.
- `completeness` — how much of the task action was accomplished.
- `blocker` — if non-null, a description of what is blocking progress.
- `confidence` — the subagent's confidence in the result.
- `needs_replan` — the subagent signals that the current plan is insufficient.
- `escalate` — the subagent signals it cannot proceed without help.

### Tags (Extensibility)

In addition to the six core fields, subagents may include open-ended `tags`
for domain-specific conditions:

```yaml
tags:
  tests_passing: true
  coverage: 87
```

Transition rules can reference both core conditions and tags. Tags are
free-form key-value pairs; no schema enforcement is applied. This keeps the
core condition set small while allowing tasks to communicate arbitrary state.

### Evidence Fields

Per [[specs]] D5, observations also carry an `evidence` block containing hard signals (test results, build status, command exit codes). Evidence fields are **not** part of the condition space — they are not referenced in `on_complete` transition rules. Instead, the driver uses them for contradiction detection: if evidence contradicts the `conditions` block, the driver auto-raises `surprise` to ≥ 0.7. See [[03-data-flow]] for the full evidence schema.

---

## Condition Syntax

Transition rules use a simple expression language. Complexity is deliberately
limited to keep compilation reliable and conditions inspectable.

### Comparisons

```
quality_score >= 80
completeness == "full"
blocker != null
task_attempts >= 3
tests_passing == true
```

Supported operators: `==`, `!=`, `>=`, `<=`, `>`, `<`.

### Connectors

Clauses can be joined with `and` or `or`:

```
quality_score >= 80 and completeness == "full"
blocker != null or escalate == true
```

### Constraints

- **No parentheses.** No nesting. `and` binds tighter than `or` (standard precedence). Per [[specs]] C4.
- **1 to 3 clauses maximum** per rule.
- `default` — unconditional match. Must always be the last rule in an
  `on_complete` block.

### Evaluation Order

Rules in an `on_complete` block are evaluated **top to bottom**. The first
rule whose condition matches the current condition space determines the
transition. If no rule matches and there is no `default`, this situation cannot
occur at runtime — the compiler injects a `default` rule into every
`on_complete` block during plan compilation (see [[04-python-driver]]). Per
[[specs]] C8.

---

## Transition Rules Format

Each task node carries an `on_complete` block that defines where control goes
after the subagent returns an observation.

```yaml
tasks:
  - id: t1
    action: "search for migration guides and analyze project structure"
    on_complete:
      - condition: "quality_score >= 80"
        goto: t2
      - condition: "task_attempts >= 3"
        goto: t2  # proceed with what we have
      - default: retry

  - id: t2
    action: "create vite.config.ts from webpack config"
    on_complete:
      - condition: 'completeness == "full"'
        goto: [t3a, t3b, t3c]  # list = parallel dispatch
      - condition: "blocker != null"
        goto: t2_fix
      - default: retry
```

### Goto Targets

- **Single node ID** (`goto: t2`) — sequential transition.
- **List of node IDs** (`goto: [t3a, t3b, t3c]`) — parallel dispatch. All
  listed nodes are dispatched simultaneously. See [[06-concurrency]] for
  parallel dispatch details.

### Special Nodes

These are reserved node names with built-in semantics:

| Node       | Behavior                                                   |
|------------|------------------------------------------------------------|
| `retry`    | Re-dispatch the current task node. `task_attempts` increments. |
| `replan`   | Exit the task graph and re-enter the meta plan phase.      |
| `escalate` | Hand control to a higher-capability LLM or human.          |
| `done`     | Task graph is complete. Control returns to meta verify.    |

### Wait Nodes

When a transition dispatches parallel nodes, the driver automatically inserts
a **synthetic join point**:

```yaml
  - id: t3_join
    wait_for: [t3a, t3b, t3c]
    on_complete:
      - condition: 'completeness == "full"'
        goto: t4
      - default: retry
```

The `wait_for` field tells the driver to block until all listed nodes have
completed. Only then does the driver evaluate the join node's `on_complete`
rules. The condition space at evaluation time reflects the **merged**
observations from all parallel nodes.

---

## Two-Pass Plan Generation

Plan generation is split into two passes to keep the LLM's job simple and the
compilation deterministic.

### Pass 1: Natural-Language Plan (LLM)

The plan subagent produces a structured but natural-language plan. This format
is easy for LLMs to generate — it is essentially writing a plan document with
light structure.

```yaml
plan:
  goal: "migrate monorepo from webpack 5 to vite"
  steps:
    - id: t1
      action: "search for webpack-to-vite migration guides"
      success_criteria: "clear understanding of project's webpack config"
      retry_strategy: "try up to 3 times, then proceed with what we have"

    - id: t2
      action: "create vite.config.ts"
      depends_on_completion_of: [t1]
      success_criteria: "vite.config.ts covers all webpack features"

    - id: t3a
      action: "migrate entry point: src/app/index.tsx"
      depends_on_completion_of: [t2]
      can_parallel_with: [t3b, t3c]

    - id: t3b
      action: "migrate entry point: src/admin/index.tsx"
      depends_on_completion_of: [t2]
      can_parallel_with: [t3a, t3c]

    - id: t3c
      action: "migrate entry point: src/shared/index.tsx"
      depends_on_completion_of: [t2]
      can_parallel_with: [t3a, t3b]
```

The plan subagent does not need to know about condition syntax, transition
rules, or the state machine format. It writes in terms it understands:
actions, criteria, dependencies, and retry intent.

### Pass 2: Compilation to Condition-Driven Graph (Driver)

The Python driver compiles the NL plan into the task graph format described
above. The compilation transforms each NL field into its formal equivalent:

| NL field                     | Compiled form                                        |
|------------------------------|------------------------------------------------------|
| `depends_on_completion_of`   | Sequential ordering (goto chains)                    |
| `can_parallel_with`          | Parallel groups with synthetic wait nodes             |
| `success_criteria`           | Condition rules via pattern matching                  |
| `retry_strategy`             | Retry/escalate/replan rules with attempt thresholds   |

See [[04-python-driver]] for the full compilation implementation.

---

## Compilation Tolerance

v1 uses **lenient compilation** (Option B from [[specs]] C7).

The compiler does not require perfect NL input. Instead it applies best-effort
keyword matching:

- `retry_strategy` text is scanned for patterns like "try N times", "retry",
  "proceed", "escalate", "give up". Matched patterns map to canonical
  transition rules.
- `success_criteria` text is scanned for quality-related keywords ("complete",
  "all", "covers", "passing") and mapped to condition checks on the core
  fields.

### Unrecognized Patterns

When the compiler cannot match an NL phrase to a known pattern:

1. A **sensible default** is applied (retry 3 times, then escalate).
2. The unrecognized phrase is **logged** with the task ID for debugging.
3. Execution continues — wrong compilation is **recoverable**. The task may
   take a suboptimal path, but it will eventually hit a retry limit and
   escalate or trigger a replan.

### Future: Hybrid Compilation

A planned improvement is a hybrid approach where a cheap LLM call classifies
unrecognized NL phrases into canonical categories. This sits between pure
pattern matching (brittle) and using the main LLM (expensive).

---

## Runtime Graph Mutation

The task graph is **mutable** during execution. This is essential for handling
tasks where the full scope is not known at plan time.

### Small Changes: Node Insertion

A subagent can include a `new_tasks` field in its observation:

```yaml
observation:
  quality_score: 90
  completeness: "partial"
  new_tasks:
    - id: t2_hotfix
      action: "patch the deprecated API call found in utils.ts"
      insert_after: t2
```

The driver validates the new node (no duplicate IDs, valid insertion point) and
splices it into the graph. Transition rules on the preceding node are updated
to route through the new node.

### Large Changes: Replan

When mutations are too large to express as node insertions — the plan
structure itself is wrong — the system falls back to a full replan:

- The verify phase determines the goal is not met.
- Or a subagent sets `needs_replan: true` in its observation.
- Or an invariant fires a replan trigger.

In all cases, control returns to the plan phase. The plan subagent receives the
full accumulated profile and trace, so prior knowledge is preserved. The new
plan replaces the old task graph entirely.

---

## Fuel Semantics

Fuel is the primary resource bound on execution. Per [[specs]] C10:

### Definition

**1 fuel = 1 driver cycle.** A driver cycle is one round of:

```
decide next node → dispatch subagent(s) → observe result → evaluate conditions
```

### Parallel Dispatch Cost

Parallel dispatch of N subagents costs **1 fuel**, not N. Fuel measures
**logical progress**, not resource consumption. This design intentionally
**encourages parallelism** — there is no fuel penalty for dispatching work in
parallel.

### Initialization

```bash
pymh init --fuel 30
```

This sets `fuel_remaining: 30` in the system conditions, meaning the driver
can execute at most 30 cycles before the fuel invariant forces a stop.

### Mid-Task Fuel Addition

A user can add fuel to a running task:

```bash
pymh fuel --add N
```

This increments `fuel_remaining` by N. The driver picks up the change on its
next cycle.

### Fuel and Invariants

`fuel_remaining` is a system condition like any other. The default fuel
invariant is:

```yaml
invariants:
  - name: fuel_exhausted
    condition: "fuel_remaining <= 0"
    action: converge
    message: "Fuel exhausted. Attempting to converge."
```

When fuel hits zero, the driver attempts to converge rather than escalating or
hard-aborting. If any useful artifacts have been produced, the driver enters
the verify phase to assess the current state. If nothing useful has been
produced, the driver aborts with a report. The user can add fuel mid-task via
`pymh fuel --add N`. Per [[specs]] C11.

---

## Meta Verify

When the task graph reaches its `done` node, the meta layer runs a final
verification pass.

### Process

1. The driver signals the **meta verify phase** (`current_phase: "verify"`).
2. A lightweight check evaluates: "Does the overall output align with the
   original goal?"
3. This check can be implemented as a simple LLM call with the goal, the
   accumulated observations, and a yes/no prompt.

### Outcomes

- **Aligned** — the task is complete. The driver generates a final report
  summarizing what was done, fuel consumed, and any notable events from
  the trace.
- **Not aligned** — the driver triggers a **replan**. The new plan phase
  receives all accumulated context:
  - The profile (accumulated knowledge).
  - The full trace of prior execution.
  - The previous plan and why it was deemed insufficient.
  - The replan counter (incremented).

### Why Meta Verify Exists

Meta verify catches **goal drift**. In long-running agentic tasks, each
individual step may succeed on its own terms while the overall trajectory
diverges from the original intent. Without a periodic alignment check, the
system can complete its task graph successfully while solving the wrong
problem.

Meta verify is intentionally lightweight. It is not a test suite or a deep
validation — those belong in task-level actions. It is a sanity check on
the trajectory as a whole.

---

## Cross-References

- [[04-python-driver]] — compilation implementation, driver loop details
- [[06-concurrency]] — parallel dispatch mechanics, wait node semantics
- [[07-invariants-and-escalation]] — fuel invariant, escalation paths, replan limits
- [[03-data-flow]] — observation schema, condition reporting format
- [[specs]] — referenced decisions: C7 (compilation tolerance), C10 (fuel semantics), C4 (precedence), C8 (default injection), C11 (fuel convergence), D5 (evidence fields), K4 (verify output), K5 (verify subagent)
