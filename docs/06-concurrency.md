# 06 — Limited Concurrency

Concurrency in v1 is a **supported but constrained** capability. The task graph can express parallel nodes, and the driver can dispatch them concurrently, but the system does not enforce file-level isolation between parallel subagents. Conflict prevention relies on instruction guidance and meta-verify as a safety net. Per [[specs]] P1.

How Monadic Harness manages parallel execution of task nodes,
merges their observations, and handles failures.

---

## 1. Concurrency Scope

mh manages **task-node-level parallelism** only.
It does NOT manage tool-call-level concurrency --
that is the underlying agent framework's responsibility.

Parallel groups emerge from the task graph structure:

- In the NL plan: `can_parallel_with: [t3b, t3c]` explicitly marks
  parallel-safe tasks.
- The driver compiles these annotations into parallel dispatch groups.
- Independence is **declared by the plan**, not inferred by the driver.

The plan author (or the LLM that generates the plan) is responsible for
asserting that tasks in a parallel group do not have ordering dependencies
on each other. The driver trusts this declaration and dispatches accordingly.

---

## 2. Dispatch Mechanism

In the Claude Code Skill context:

1. Driver issues `PARALLEL:t3a:0:<path>,t3b:0:<path>,t3c:0:<path>`.
2. Main agent spawns 3 subagents in a **single message** with multiple
   Agent tool calls.
3. Claude Code runs them concurrently.
4. Main agent waits for all subagents to complete.
5. Main agent calls `pymh observe` for each completed task.

When the driver dispatches a parallel group, it immediately advances `current_task` in the cursor to the corresponding wait node. This ensures the cursor reflects the system's actual state (waiting for parallel completion) rather than pointing at a dispatched node.

The driver treats the entire parallel dispatch as a single logical step.
It does not interleave other decisions while waiting for the group to finish.

---

## Parallel Group Protocol

### Group Identity

A parallel group is identified by the set of node-ids dispatched together. The driver output includes all nodes with their instruction paths: `PARALLEL:{node-id-1}:{attempt-1}:{path-1},{node-id-2}:{attempt-2}:{path-2},...`

There is no separate group-id — the group is defined by the nodes in the dispatch instruction.

### Dispatch

The main agent spawns all subagents in a single message with multiple Agent tool calls. Each subagent receives its own instruction file and writes to its own observation file.

### Barrier (Completion)

The main agent waits for ALL subagents in the group to complete (allSettled semantics — see below). A parallel group is complete when every subagent has either:
- Written its observation file, or
- Been detected as failed (main agent writes synthetic observation per [[05-skill-protocol]] Completion Check)

### Observe

After all subagents complete, the main agent calls:
```bash
pymh observe --parallel {node-id-1},{node-id-2},...
```

The driver reads each node's observation and validates each individual observation before merge (applying default-filling, type coercion, success/completeness reconciliation, and canonical write-back per [[03-data-flow]] Section 6). This ensures that the merge function operates on normalized data. After individual validation, the driver merges the observations (see Observation Merge below) and processes the merged result.

### Retry Semantics

If some nodes in a parallel group fail:
- The `observe --parallel` call processes the merged observation and runs invariant checks. If invariants fire, a `forced_transition` is written to cursor.
- The wait node's `on_complete` rules then determine the next step.
- If the transition is `retry`, only the **failed** nodes are re-dispatched (successful ones are already in `completed_tasks` and skipped).
- A full group retry (all nodes) only happens if the transition rule explicitly requires it.

---

## 3. Observation Merge

When a parallel group completes, the driver merges their observations
into a single combined observation. This merged observation is used for
invariant checks, state updates, and the trace entry. Per [[specs]] P2, P3.

### Merge function

Input: list of `(node_id, observation)` tuples, sorted by node id.

```python
def _merge_parallel(observations: list[tuple[str, dict]]) -> dict:
    merged = {
        "success": all(obs.get("success", False) for _, obs in observations),
        "signal": "; ".join(obs.get("signal", "") for _, obs in observations),
        "conditions": {},
        "evidence": {},
        "tags": {},
        "surprise": max(obs.get("surprise", 0.5) for _, obs in observations),
        "profile_updates": {},
        "files_changed": [],
        "narrative": "\n\n".join(
            obs.get("narrative", "") for _, obs in observations if obs.get("narrative")
        ),
    }

    # Conditions: per-field merge strategies
    scores = [obs.get("conditions", {}).get("quality_score", 50) for _, obs in observations]
    merged["conditions"]["quality_score"] = max(scores)                    # MAX

    all_full = all(
        obs.get("conditions", {}).get("completeness") == "full" for _, obs in observations
    )
    merged["conditions"]["completeness"] = "full" if all_full else "partial"  # ALL-or-nothing

    blockers = [
        obs.get("conditions", {}).get("blocker")
        for _, obs in observations
        if obs.get("conditions", {}).get("blocker") is not None
    ]
    merged["conditions"]["blocker"] = "; ".join(str(b) for b in blockers) if blockers else None

    conf_order = {"low": 0, "medium": 1, "high": 2}
    confs = [obs.get("conditions", {}).get("confidence", "low") for _, obs in observations]
    merged["conditions"]["confidence"] = min(confs, key=lambda c: conf_order.get(c, 0))  # WORST

    merged["conditions"]["needs_replan"] = any(
        obs.get("conditions", {}).get("needs_replan", False) for _, obs in observations
    )                                                                       # ANY
    merged["conditions"]["escalate"] = any(
        obs.get("conditions", {}).get("escalate", False) for _, obs in observations
    )                                                                       # ANY

    # Evidence: keyed by node-id (decision P3)
    for nid, obs in observations:
        if obs.get("evidence"):
            merged["evidence"][nid] = obs["evidence"]

    # Tags, profile_updates: last-write-wins in sorted order
    for _, obs in observations:
        merged["tags"].update(obs.get("tags", {}))
        merged["profile_updates"].update(obs.get("profile_updates", {}))
        merged["files_changed"].extend(obs.get("files_changed", []))

    return merged
```

### Merge semantics per field

| Field              | Strategy                          | Rationale                                                        |
| ------------------ | --------------------------------- | ---------------------------------------------------------------- |
| `success`          | ALL (logical AND)                 | Any failure means partial failure                                |
| `signal`           | JOIN (semicolon-separated)        | Preserve all signals                                             |
| `surprise`         | MAX                               | Highest surprise determines re-evaluation need                   |
| `quality_score`    | MAX (P2)                          | Best quality from any parallel member                            |
| `completeness`     | ALL-or-nothing                    | All must be "full" for group to be "full"                        |
| `blocker`          | JOIN (semicolon-separated non-null)| Collect all blockers                                            |
| `confidence`       | WORST (min by low < medium < high)| Most conservative confidence                                     |
| `needs_replan`     | ANY (logical OR)                  | One node needing replan triggers replan for group                |
| `escalate`         | ANY (logical OR)                  | One escalation escalates the group                               |
| `evidence`         | Keyed by node-id (P3)             | `{"t3a": {...}, "t3b": {...}}` — preserves per-node granularity  |
| `profile_updates`  | MERGE (last-write-wins by node id)| Sorted by node id for determinism                                |
| `files_changed`    | UNION                             | All changed files matter                                         |
| `tags`             | MERGE (last-write-wins by node id)| Same as profile                                                  |
| `narrative`        | JOIN (double-newline-separated)   | Each subagent's narrative preserved                              |

### Conflict handling for `profile_updates`

If `t3a` writes `framework: "React 18"` and `t3b` writes
`framework: "React 18 with SSR"`, then `t3b` wins because it has the
higher alphabetical node id.

This is a known v1 limitation:
- **Determinism** is guaranteed (sorted node id order, last-write-wins).
- **Semantic correctness** is not guaranteed -- the "right" value depends
  on context that the merge function does not have.
- Conflicts are logged in the trace so they can be reviewed during
  the verify phase or post-run analysis.

---

## 4. Failure Semantics

**allSettled** (per [[specs]] C7, area "concurrency"):

- All spawned parallel subagents run to completion, regardless of
  individual failures.
- **No fail-fast**: a failure in `t3a` does NOT interrupt `t3b` or `t3c`.
- After all parallel tasks complete, the driver evaluates the merged
  observation.
- If merged `success` is `false`, the wait node's transition rules apply
  (typically: retry the failed tasks, or escalate to the user).

### Rationale

Subagents are already running -- interrupting them wastes the work
already done. Each subagent's output may still contain useful
observations (files created, partial progress, diagnostic signals).
The verify/replan phase catches systemic issues that emerge from
partial failure.

### What happens after partial failure

The wait node receives a merged observation with `success: false`.
Its transition rules determine the next step:

- **retry**: re-dispatch only the failed tasks (successful ones are
  already in `completed_tasks`).
- **escalate**: surface the failure to the user with accumulated
  context from all tasks in the group.
- **continue**: if the plan marks the failed task as optional, proceed
  to the next phase anyway.

---

## 5. File System Conflict Management

Per [[specs]] P6: **instruction guidance only** for v1.

### How it works

When the driver compiles a parallel group, it can analyze task actions
to infer likely file overlap:

- If two parallel tasks mention the same file or directory, add a
  warning to both task instructions:
  > "Note: another parallel task is also working in this area.
  > Do NOT modify [shared file]. Only modify files in your assigned scope."
- The driver does not enforce this -- it is a **prompt-level guideline**.

### Shared-File Awareness

While v1 does not enforce file-level isolation, the driver provides awareness:

When compiling a parallel group, the driver scans task actions for likely file overlap. If detected, each affected task's instruction includes a warning:

> **Parallel conflict warning**: Another task in this group may also modify `{filename}`. To avoid conflicts:
> - Do NOT modify this file directly.
> - If you must modify it, note the change in your observation narrative.
> - Prefer creating new files over modifying shared ones.

**Known high-risk files** (documented in instruction guidance):
- `package.json`, `package-lock.json`, `yarn.lock`
- `tsconfig.json`, `vite.config.ts`, `webpack.config.js`
- Barrel/index files (`index.ts`, `index.js`)
- Shared configuration files

This is prompt-level guidance, not enforcement. Meta-verify is expected to catch unresolved conflicts. Worktree-based isolation is deferred (see [[specs]] § Deferred Items).

### Future direction

A higher-order harness design could introduce true isolation:

- Git worktrees per subagent (each works on a branch).
- Merge branches after all parallel tasks complete.
- Detect and resolve merge conflicts before proceeding.

This is deferred per [[specs]] § Deferred Items.

---

## 6. Fuel Cost

Per [[specs]] C10:

- A parallel dispatch of N subagents costs **1 fuel**.
- One driver cycle: decide -> dispatch N -> observe N -> merge.
- Fuel measures **logical progress**, not resource consumption.

### Design intent

This deliberately encourages parallelism -- users are not punished
for structural concurrency in their task graphs.

Real resource cost (tokens, API calls) scales with N, but that is a
**resource budget** concern, not a fuel concern. The two are tracked
separately:

- **Fuel** bounds the number of driver cycles (progress steps).
- **Resource budget** bounds the total tokens/API calls consumed.

A plan with 10 sequential tasks costs 10 fuel.
A plan with 10 parallel tasks costs 1 fuel.
Both may consume similar total tokens, but the parallel plan completes
in fewer logical steps.

---

## 7. Wait Nodes

Wait nodes are synthetic join points in the task graph, generated
during plan compilation.

### Structure

```yaml
- id: t4_wait
  wait_for: [t3a, t3b, t3c]
  goto: t4
```

### Behavior

- A wait node has no `action` -- it is **not** dispatched to a subagent.
- The driver checks: are ALL tasks in `wait_for` present in
  `completed_tasks`?
  - If **yes** -> proceed to `goto` target.
  - If **no** -> return `BLOCKED:waiting` (main agent retries on
    next cycle).

### Wait node as pure join point

Wait nodes are **structural join points**, not evaluation points. Per [[specs]] P4:

- The wait node does NOT read or merge individual observations.
- Observation merging happens earlier, in `observe --parallel`, which processes the merged observation through validation, state updates, and invariant checks.
- By the time `decide` reaches the wait node, all necessary processing is done. The wait node only checks whether all dependencies are in `completed_tasks`.
- When all dependencies are met, the wait node clears `pending_parallel` and evaluates its `on_complete` rules against a synthetic condition space with `completeness = "full"`.

This design keeps the wait node logic simple and avoids duplicating the merge work already done by `observe --parallel`.

---

## Cross-references

- [[02-control-flow]] -- parallel groups in the task graph, condition space
- [[04-python-driver]] -- `PARALLEL` dispatch command, observe for parallel groups
- [[05-skill-protocol]] -- completion check, synthetic observation on subagent failure
- [[specs]] -- C7 (allSettled semantics), P6 (file conflict strategy), P9 (parallel cursor advancement), P10 (pre-validate parallel observations), C10 (fuel cost for parallel dispatch), D13 (observation schema split), D12 (node-id terminology), D5 (evidence field), D7 (shared-file awareness), P1 (limited concurrency framing), P2 (quality_score MAX, conditions ARE merged), P3 (evidence keyed by node-id), P4 (wait node as pure join point)
- [[specs]] § Deferred Items -- worktree-based isolation, stronger file conflict prevention
