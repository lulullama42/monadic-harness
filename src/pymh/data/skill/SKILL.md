---
name: mh
description: "Monadic Harness: structured entropy management for long-horizon agent tasks"
argument-hint: "[goal description]"
allowed-tools: [Bash, Read, Write, Agent, Glob, Grep]
trigger: /mh
---

# Monadic Harness — Orchestration Protocol

You are orchestrating a structured task using the Monadic Harness (mh). Follow this protocol exactly.

## References

- `~/.claude/skills/mh/principles.md` — design principles (read when making judgment calls)
- `~/.claude/skills/mh/observation-schema.md` — observation JSON format (pass to exec subagents)
- `~/.claude/skills/mh/plan-format.md` — NL plan YAML format (pass to plan subagents)

---

## Phase 1: Initialize

```bash
pymh init --goal "<goal from user>" --fuel 30
```

Parse output: `INIT:{task_id}:{workspace_path}`. Save both values. Report to user: task initialized, workspace, fuel budget.

---

## Phase 2: Plan

1. Get plan instruction:
   ```bash
   pymh decide --phase plan --workspace {workspace}
   ```
   Output: `DISPATCH:plan:{n}:{instruction_path}`

2. Read the instruction file. Spawn a plan subagent:
   ```
   Agent(prompt=<instruction content>, subagent_type="general-purpose", description="mh plan")
   ```

3. The subagent writes `ctrlflow/plans/current.yaml` in the workspace.

4. Compile the plan:
   ```bash
   pymh compile-plan --workspace {workspace}
   ```
   Output: `COMPILED:{n} tasks, {m} parallel groups`

5. If compilation fails, ask the plan subagent to fix and retry. After 2 failures, ask the user.

6. Report plan summary to user. Proceed to Phase 3.

---

## Phase 3: Execute

Repeat this loop:

**Step 1** — Get next instruction:
```bash
pymh decide --workspace {workspace}
```

**Step 2** — Parse output and act:

| Output | Action |
|--------|--------|
| `DISPATCH:{node}:{attempt}:{path}` | Read instruction file, spawn one exec subagent |
| `PARALLEL:{n1}:{a1}:{path1},{n2}:{a2}:{path2},...` | Read each instruction file, spawn all subagents **in one message** |
| `DONE` | Go to Phase 4 |
| `REPLAN` | Go back to Phase 2 |
| `ESCALATE:{reason}` | Handle escalation (see below) |
| `BLOCKED:{reason}` | Report to user, wait for input |

**Step 3** — Process observation (the driver handles missing observation files automatically):
- Single: `pymh observe --node {node} --attempt {attempt} --workspace {workspace}`
- Parallel: `pymh observe --parallel {n1},{n2},... --workspace {workspace}`

**Step 4** — Display progress to user. Repeat from Step 1.

---

## Phase 4: Verify

1. Get verify instruction:
   ```bash
   pymh decide --phase verify --workspace {workspace}
   ```

2. Spawn verify subagent with the instruction. The verify subagent must write an observation containing:

   | Field | Type | Description |
   |-------|------|-------------|
   | `goal_met` | bool | Is the original goal satisfied? |
   | `accepted_artifacts` | string[] | Artifacts meeting quality standards |
   | `missing_items` | string[] | What is still needed |
   | `evidence_summary` | object | Key evidence from execution |
   | `recommended_action` | enum | `"done"` \| `"repair"` \| `"replan"` \| `"abort"` |

3. Process verify observation:
   ```bash
   pymh observe --node verify --attempt {n} --workspace {workspace}
   ```

4. Based on `recommended_action`:
   - `"done"` → `pymh report --workspace {workspace}`, display report. Done.
   - `"repair"` → back to Phase 3
   - `"replan"` → back to Phase 2
   - `"abort"` → `pymh abort --workspace {workspace}`

---

## Escalation Handling

When you receive `ESCALATE:{reason}`:

1. Read `ctrlflow/escalation.json` for problem details.
2. Read `state.json`, `profile.json`, recent `trace/trace.jsonl` entries.
3. Analyze the situation and decide on a resolution:
   - `replan` — go back to planning phase
   - `abort` — stop the task
   - `skip_task` — skip current node with synthetic observation
   - `write_observation` — inject an observation manually
   - `modify_graph` — insert/remove/update task graph nodes
4. Write your decision to `ctrlflow/resolution.json`.
5. Resume: `pymh resume --workspace {workspace}`

**Important**: Do NOT modify `state.json`, `cursor.json`, or `phase.json` directly. Write `resolution.json` and let the driver apply changes.

---

## User Commands

Route user requests during execution:

| User intent | Action |
|-------------|--------|
| "status", "how's it going?" | `pymh status --workspace {workspace}` |
| "abort", "stop", "cancel" | `pymh abort --workspace {workspace}` |
| "add fuel N", "more steps" | `pymh fuel --add N --workspace {workspace}` |
| "show profile" | Read `profile.json` |
| "show trace" | Read `trace/trace.jsonl` |
| "show plan" | Read `ctrlflow/task-graph.yaml` |
| Direction change ("also do X", "change approach") | Pause exec, trigger replan with user's new direction |
| General conversation (unrelated to task) | Answer briefly, then resume the exec loop |

**Never silently ignore user input.** If classification is ambiguous, ask for clarification before resuming.

---

## Context Assembly

The `decide` command produces fully assembled instruction files for each subagent. These include the task action, relevant profile entries, template content (for plan), and previous attempt narrative (for retries). **Read the instruction file and pass its content as the subagent prompt as-is.** Do not manually inject additional context — the driver handles context assembly.

---

## Recovery

All execution state lives on disk. You can always recover.

### Context Loss

If you lose track of the current task (e.g., after context compaction):

1. Read `~/.mh/history.jsonl` — find the most recent entry with `status: "running"`.
2. Workspace: `~/.mh/tasks/{task_id}/`
3. Read `state.json` (step, fuel), `cursor.json` (position), `phase.json` (current phase).
4. Resume the appropriate phase from the current position.

### State Anomaly

If driver returns an unexpected error, or state files appear corrupted:

1. Read `trace/trace.jsonl` to find the last known good state.
2. Read `cursor.json` and `state.json` to understand the current position.
3. If cursor or state is inconsistent with the trace, write a `resolution.json` with the appropriate fix (e.g., `write_observation` to supply a missing observation, or `replan` to start fresh).
4. Call `pymh resume --workspace {workspace}` to apply the fix through the driver.
