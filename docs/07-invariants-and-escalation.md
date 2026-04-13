# 07 - Invariants and Escalation

## Design Principle

Invariants are the meta-meta safety net. They sit above the meta control
flow (plan -> exec -> verify) and ensure the system does not get stuck,
loop, drift, or exhaust resources.

Key properties:

- **Pure Python** -- no LLM calls, no token cost. Invariant checks are
  ordinary conditionals evaluated against numeric state. They never invoke
  a model and therefore have zero marginal cost per step.
- **Every step** -- invariant checks run inside the `observe` command after
  every subagent execution. There is no way to skip them; the driver
  enforces this as part of its core loop.
- **Override capability** -- when an invariant fires, it injects a *forced
  transition* that overrides normal `on_complete` evaluation. The graph's
  own routing rules are bypassed in favor of the invariant's corrective
  action.
- **Independent of LLM judgment** -- even if the LLM is completely lost,
  invariants catch problems externally. They rely only on counters and
  thresholds maintained by the driver itself.

Together these properties guarantee that the system always terminates and
always surfaces problems, regardless of what the subagent or the plan
decides to do.

---

## Three Core Invariants

### 2.1 Loop Detection

Detects when the same task node is retried too many times.

| Parameter            | Default | Configurable       |
| -------------------- | ------- | ------------------ |
| `max_task_attempts`  | 3       | Yes, in config.yaml |

**Trigger**: `task_attempts >= max_task_attempts` for the current task node.

**Action**: inject forced transition `{"type": "escalate", "reason": "loop detected: {node-id} attempted {n} times"}` on next `decide` call.
The driver skips normal condition evaluation and returns
`ESCALATE:loop_detected:{node-id}`.

**Single source of truth**: `max_task_attempts` from config is used by both the loop detection invariant and the compiler's generated retry rules (e.g., `task_attempts >= max_task_attempts -> escalate`). This ensures consistent behavior -- the invariant and the compiled rules agree on when to stop retrying.

**Rationale**: if a task has failed 3 times with the same approach,
retrying again is unlikely to succeed. Escalation brings in LLM judgment
to try a different approach.

### 2.2 Drift Check

Detects when execution is diverging from the plan -- too many failures,
too much unexpected behavior.

| Parameter                  | Default | Configurable |
| -------------------------- | ------- | ------------ |
| `max_consecutive_failures` | 3       | Yes          |
| `drift_threshold`          | 2.0     | Yes          |

Two sub-checks:

- **Consecutive failures** --
  `consecutive_failures >= max_consecutive_failures` (resets to 0 on any
  success).
- **Surprise accumulation** -- the driver maintains a rolling
  `surprise_accumulator` using squared values: `Σ(surprise²)`. Squaring
  amplifies high-surprise events while dampening low ones (per [[specs]]
  I3). On success, the accumulator resets to 0.0 before the current
  observation's surprise² is added (so a success with low surprise
  effectively clears it; per [[specs]] I4). If `surprise_accumulator >
  drift_threshold`, the plan may be based on wrong assumptions.

**Action**: inject forced transition `{"type": "replan", "reason": "drift detected: ..."}` on next `decide`. The
reasoning: if the task is consistently failing or producing surprises, the
plan itself may be wrong -- retrying individual tasks won't help.

**Rationale**: this catches the scenario where the agent is "boiling a
frog" -- each individual step seems like a minor failure, but cumulatively
the execution has gone off track.

### 2.3 Fuel Management

Prevents unbounded execution.

| Parameter                 | Default | Configurable |
| ------------------------- | ------- | ------------ |
| `fuel_warning_threshold`  | 5       | Yes          |

**Trigger levels**:

- `fuel_remaining <= fuel_warning_threshold` -- add warning to progress
  output: "Low fuel: N steps remaining"
- `fuel_remaining <= 0` -- force converge

**Fuel Management**

| Parameter | Value |
|-----------|-------|
| Trigger | `fuel_remaining <= 0` |
| Action | Force convergence: verify if artifacts exist, abort if not |
| Rationale | Fuel is the user's budget. The system must deliver the best result possible within the budget |

**Action on fuel exhaustion**:

1. If at least one task completed with `success: true` → enter meta verify phase with current state.
2. If no tasks completed successfully → abort with report.
3. User can add fuel mid-task via `pymh fuel --add N`.

**Injected forced_transition** (in `cursor.json`):

```json
{
  "forced_transition": {
    "type": "verify_or_abort",
    "reason": "fuel_exhausted",
    "invariant": "fuel_management"
  }
}
```

Per [[specs]] C11.

**Rationale**: fuel is the user's budget. The system must respect it.
Running out of fuel is not a failure -- it is a resource boundary that
forces the system to deliver the best result possible with the budget
given.

### Evidence-Conditions Contradiction

This is a pre-processing check during `observe`, not a traditional invariant (it does not inject a forced transition).

When the driver reads an observation, it compares `evidence` fields against `conditions`. If they contradict (e.g., `evidence.tests_passing == false` but `conditions.completeness == "full"`), the driver auto-raises `surprise` to at least `0.7`.

This catches the "self-assessment inflation" problem -- where a subagent reports optimistic conditions that are contradicted by hard evidence. The elevated surprise may then trigger the drift check invariant if the accumulator crosses the threshold.

See [[04-python-driver]] for the full contradiction detection rules. Per [[specs]] D7.

---

## Invariant Actions -- Forced Transitions

When an invariant fires, the driver sets a `forced_transition` field in
`ctrlflow/cursor.json`:

```json
{
  "current_task": "t3c",
  "task_attempts": 3,
  "forced_transition": {
    "type": "escalate",
    "reason": "loop_detected:t3c",
    "invariant": "loop_detection"
  }
}
```

On the next `decide` call, the driver checks `cursor.json` for
`forced_transition` **before** evaluating `on_complete` rules. If present:

1. Return the forced action (ESCALATE, REPLAN, DONE, or ABORT).
2. Clear the `forced_transition` flag.
3. Log the invariant firing in trace.

This design means invariants do not interrupt mid-execution -- they
influence the next decision point. A subagent that is already running will
finish its current step; the corrective action takes effect only when the
driver next evaluates routing.

---

## Escalation Triggers

Three sources can trigger escalation:

| Source        | Trigger                                                                 | Example                                                                                          |
| ------------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| **Subagent**  | `conditions.escalate == true` in observation                            | Subagent is confused, can't make progress, needs human/LLM judgment                              |
| **Driver**    | Can't parse condition expression, invalid graph state, unexpected format | `quality_score` is a string instead of number; `goto` references nonexistent task                 |
| **Invariant** | Loop detection fires                                                    | Same task retried 3 times without progress                                                       |

All three result in the driver writing `escalation.json` and returning
`ESCALATE:<reason>`.

---

## Escalation Protocol

### Step 1: Driver writes escalation.json

```json
{
  "type": "subagent_confused" | "driver_parse_error" | "invariant_violation",
  "source_task": "t3c",
  "context": "Task t3c has been attempted 3 times. Each attempt failed because the worker entry point uses dynamic require() which vite cannot handle. Subagent tried adding @originjs/vite-plugin-commonjs but it did not resolve the issue.",
  "state_snapshot": {
    "step": 8,
    "fuel_remaining": 22,
    "completed_tasks": ["t1", "t2", "t3a", "t3b"],
    "current_task": "t3c",
    "task_attempts": 3,
    "profile_summary": "webpack5->vite6.2, React 18, 3 entries, 2 done"
  },
  "options": [
    "modify_graph",
    "skip_task",
    "write_observation",
    "replan",
    "abort"
  ]
}
```

The `context` field is critical -- it gives the main agent enough
information to reason about the failure without having to re-read all
prior observations. The `state_snapshot` provides a compact view of where
the system currently stands.

### Step 2: Main agent analyzes

The main agent (see [[05-skill-protocol]]):

1. Reads escalation.json.
2. Reads profile.json for full context.
3. Reads recent trace entries.
4. Uses its own judgment to decide the best resolution.

The main agent is not constrained to any particular resolution strategy.
It has the full picture and can apply creative problem-solving that the
driver's mechanical rules cannot.

### Step 3: Main agent writes resolution.json

```json
{
  "decision": "modify_graph",
  "details": {
    "action": "insert_task",
    "new_task": {
      "id": "t3c_workaround",
      "action": "convert dynamic require() calls in worker entry to static imports, then retry migration",
      "on_complete": [
        { "condition": "completeness == \"full\"", "goto": "t3c" },
        { "default": "escalate" }
      ]
    },
    "insert_before": "t3c"
  },
  "reasoning": "The dynamic require issue is addressable by converting to static imports first. Adding a preparatory task rather than skipping the entry point."
}
```

### Step 4: Driver resumes

`pymh resume`:

1. Reads resolution.json.
2. Applies the decision (in this case: insert new task node into
   task-graph.yaml, update cursor).
3. Clears escalation.json and resolution.json.
4. Returns: `RESUMED:DISPATCH:t3c_workaround`.

The driver validates the resolution before applying it. If resolution.json
is malformed or references nonexistent tasks, the driver itself escalates
again with `type: "driver_validation_error"` and the specific failure reason.

**Cleanup behavior**: On successful resolution, the driver deletes both
`escalation.json` and `resolution.json`. On validation failure (re-escalation),
`resolution.json` is preserved for debugging while a new `escalation.json` is written.

### Resolution Validation Rules

Before applying any resolution, the driver performs these checks:

| Resolution Type | Validation |
|----------------|-----------|
| `modify_graph` | No duplicate node-ids. No dangling `goto` references. No cycles. Current cursor node is reachable from the first task via goto traversal. `insert_before` must reference an existing node. Cannot remove the node the cursor currently points to. Per [[specs]] C15, C16. |
| `write_observation` | Observation passes the standard validation rules (see [[03-data-flow]] Section 6). Minimum required fields: `success`, `signal`, `conditions`. |
| `skip_task` | A synthetic observation is provided with at least `success`, `signal`, and default conditions. |
| `replan` | No validation needed — triggers a full replan phase. |
| `abort` | No validation needed — terminates the task. |

If validation fails, the driver rejects the resolution and re-escalates with `type: "driver_validation_error"` and the specific failure reason. The main agent can then fix the resolution and resubmit.

---

## Resolution Types

| Decision            | What Driver Does                                                                                                                                                                         |
| ------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `modify_graph`      | Applies the specified graph modification (insert task, remove task, change transitions). Continues execution from the modified point.                                                    |
| `skip_task`         | Marks the current task as completed with a synthetic observation (provided in details). Moves cursor to next task via normal `on_complete` evaluation.                                    |
| `write_observation` | Writes the provided observation.json as if the subagent produced it. Driver processes it normally (merge profile, update state, evaluate transitions).                                    |
| `replan`            | Transitions to plan phase. Details may include hints for the plan subagent (e.g., "avoid dynamic require approach").                                                                      |
| `abort`             | Marks task as aborted. Generates final report with all completed work preserved.                                                                                                          |

Notes on each:

- **modify_graph** is the most powerful resolution -- it lets the main
  agent reshape the execution plan on the fly. The driver validates the
  structural integrity of the modified graph (no dangling references, no
  cycles) before accepting it.
- **skip_task** is useful when a task is genuinely optional or when its
  goal can be achieved through a different path. The synthetic observation
  must include enough detail for downstream tasks to understand what was
  skipped. The driver always forces `success=True` on the synthetic
  observation to ensure cursor advancement — if the agent wants failure
  behavior, `write_observation` should be used instead. Per [[specs]] C17.
- **write_observation** is the lightest touch -- the main agent essentially
  "plays" the subagent by hand, writing what the observation should have
  been. This is useful when the subagent produced partial results that just
  need to be reformatted.
- **replan** discards the current task graph and returns to the planning
  phase. This is the most expensive resolution but sometimes necessary
  when the original plan was based on incorrect assumptions.
- **abort** is the last resort. It preserves all completed work and
  produces a report explaining what was accomplished and what remains.

---

## Traceability

Both escalation and resolution are recorded in trace:

```jsonl
{"timestamp": "...", "step": 8, "action": "escalate", "type": "invariant_violation", "invariant": "loop_detection", "source_task": "t3c"}
{"timestamp": "...", "step": 8, "action": "resolve", "decision": "modify_graph", "reasoning": "convert dynamic require to static imports"}
```

Additionally, escalation.json and resolution.json are preserved in the
workspace until the next escalation (or task completion). This enables
post-mortem analysis of escalation patterns.

The trace record for invariant firings is particularly useful for tuning
thresholds. If loop detection fires too often with `max_task_attempts=3`,
the user can raise the threshold in config.yaml. If drift check never
fires, the `drift_threshold` may be too high for the project's typical
surprise distribution.

---

## Cross-references

- [[04-python-driver]] -- invariant checks in `observe`, `resume` command, contradiction detection
- [[02-control-flow]] -- fuel semantics, meta verify
- [[05-skill-protocol]] -- main agent's escalation handling procedure
- [[03-data-flow]] -- workspace files for escalation, observation validation rules
- [[specs]] K10, I1 (forced_transition in cursor.json), C11 (fuel exhaustion protocol), I3 (squared surprise accumulator), D7 (evidence-conditions contradiction), I2 (forced_transition schema)
