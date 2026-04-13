# 05 - Skill Protocol

How mh presents itself as a Claude Code skill, how the main agent orchestrates
the four phases, and how subagents receive context.

---

## 1. Skill Identity

```yaml
name: mh
description: "Monadic Harness: structured entropy management for long-horizon agent tasks"
argument-hint: "[goal description]"
allowed-tools: [Bash, Read, Write, Agent, Glob, Grep]
trigger: /mh
```

The skill is invoked when the user types `/mh` followed by a goal description.
Claude Code loads SKILL.md as the entry point, then the main agent drives the
full lifecycle: init, plan, execute, verify.

---

## 2. SKILL.md Layering

Modular file structure (per [[specs]] K1):

```
~/.claude/skills/mh/
├── SKILL.md              # Main orchestration protocol (~150 lines)
├── principles.md         # Monadic design principles, entropy management theory
├── observation-schema.md # Full observation JSON schema + examples
├── plan-format.md        # Natural language plan format + examples
└── templates/            # Symlink to ~/.mh/templates/
```

### Loading Strategy

SKILL.md is always loaded --- it is the skill entry point and the only file
Claude Code loads automatically. Everything else is loaded on demand.

The main agent's context carries only SKILL.md (~150 lines). This keeps the
top-level context lean. Heavier reference material flows to subagents via
driver-assembled instructions:

| Subagent | Receives | Source |
|----------|----------|--------|
| Plan subagent | Plan format spec, examples | plan-format.md |
| Exec subagent | Observation schema, examples | observation-schema.md |
| Any subagent (when needed) | Design philosophy | principles.md |

Files are included via `@` references in SKILL.md or inlined into the
instruction strings that the driver produces for each subagent. The main agent
never needs to read these files itself --- the driver handles context assembly.

---

## 3. Orchestration Protocol

The four phases as the main agent executes them.

### Phase 1: Initialize

1. User provides goal via `/mh "migrate project from webpack to vite"`
2. Main agent runs:
   ```bash
   pymh init --goal "<goal>" --fuel 30
   ```
3. Report to user: task initialized, workspace path, fuel budget.
4. Proceed to Phase 2.

### Phase 2: Plan

1. Request planning instruction from the driver:
   ```bash
   pymh decide --phase plan
   ```
   The driver returns a fully assembled instruction string for the plan
   subagent, including goal context, profile entries, template content (if a
   matching template exists), and plan-format.md reference material.

2. Spawn plan subagent:
   ```
   Agent(prompt=<instruction from driver>,
         subagent_type="general-purpose",
         description="mh plan")
   ```

3. Plan subagent writes a natural language plan to `ctrlflow/plans/current.yaml`
   in the workspace. The plan follows the format specified in plan-format.md:
   steps with action, success_criteria, retry_strategy, and can_parallel_with
   fields.

4. Compile the plan into a task graph:
   ```bash
   pymh compile-plan
   ```

5. If compilation errors: adjust and retry. If repeated failures, escalate to
   the user with the error details.

6. Report plan summary to user (number of tasks, estimated fuel cost,
   parallelism opportunities).

7. Proceed to Phase 3.

### Phase 3: Execute

Numbered protocol — the main agent follows these steps exactly:

1. Call `pymh decide` to get the next dispatch instruction.

2. Parse the driver's stdout:

   | Driver Returns | Main Agent Does |
   |----------------|-----------------|
   | `DISPATCH:{node-id}:{attempt}:{instruction-path}` | Read instruction file, spawn one exec subagent |
   | `PARALLEL:{node-id-1}:{attempt-1}:{path-1},{node-id-2}:{attempt-2}:{path-2},...` | Read each instruction file, spawn all exec subagents **in one message** |
   | `DONE` | Proceed to Phase 4 |
   | `REPLAN` | Go back to Phase 2 |
   | `ESCALATE:{reason}` | Handle escalation (see Section 5) |
   | `BLOCKED:{reason}` | Report to user, wait for input |

3. Call observe (the driver automatically synthesizes a failure observation if the
   file is missing — per [[specs]] C9):
   - Single: `pymh observe --node {node-id} --attempt {attempt}`
   - Parallel: `pymh observe --parallel {node-id-1},{node-id-2},...`

4. Display progress to user (step completed, conditions, fuel remaining, next action).

5. Repeat from step 1.

### Phase 4: Meta Verify

1. Call `pymh decide --phase verify` to get the verify instruction.

2. Spawn a verify subagent with the instruction. The verify subagent receives:
   - Original goal and acceptance criteria
   - List of produced artifacts
   - Summary of completed tasks
   - Evidence summary from observations

3. The verify subagent writes a structured observation to `dataflow/observations/verify-{attempt}.json` that must include:

   | Field | Type | Description |
   |-------|------|-------------|
   | `goal_met` | bool | Is the original goal satisfied? |
   | `accepted_artifacts` | string[] | Artifacts that meet quality standards |
   | `missing_items` | string[] | What is still needed |
   | `evidence_summary` | object | Key evidence from execution (tests, builds, etc.) |
   | `recommended_action` | enum | `"done"` \| `"repair"` \| `"replan"` \| `"abort"` |

4. Call `pymh observe --node verify --attempt {n}`.

5. Based on the verify result:
   - `recommended_action == "done"` → call `pymh report`, display report. Task complete.
   - `recommended_action == "repair"` → the driver re-enters exec phase for targeted fixes.
   - `recommended_action == "replan"` → go back to Phase 2 (replan with failure summary).
   - `recommended_action == "abort"` → call `pymh abort`.

Verify costs 1 fuel. Per [[specs]] K4, K5.

---

## 4. Subagent Roles and Context

Three subagent types, each with different context and expectations.

### Plan Subagent

- **Role**: produce a natural language task plan.
- **Context provided by driver**:
  - Goal description
  - `profile.json` (accumulated project knowledge)
  - Previous plan failures (if replanning)
  - Template content (if a matching template exists in `~/.mh/templates/`)
  - Plan format specification from plan-format.md
- **Output**: writes NL plan YAML to `ctrlflow/plans/current.yaml`.
- **Expectations**: follow the plan format. Each step must have:
  - `action` --- what to do
  - `success_criteria` --- how to know it worked
  - `retry_strategy` --- what to try if it fails
  - `depends_on_completion_of` --- which steps must complete first
  - `can_parallel_with` --- which steps can run concurrently

### Exec Subagent

- **Role**: execute one task node.
- **Context provided by driver**:
  - Task action description
  - Relevant `profile.json` entries
  - Relevant scratchpad files from `dataflow/scratchpad/`
  - Observation schema from observation-schema.md
- **Output**:
  - Writes `observation.json` to `dataflow/observations/`
  - May write intermediate files to `dataflow/scratchpad/`
  - May produce deliverables in `dataflow/artifacts/`
- **Expectations**: produce a structured observation containing:
  - `conditions` --- what was discovered (factual)
  - `signal` --- success / partial / failure
  - `surprise` --- anything unexpected (triggers profile updates)
  - `narrative` --- human-readable summary of what happened

### Verify Subagent

Invoked only when confidence is low or problems arise (per [[specs]] K3).
Not spawned on every step.

- **Role**: independently assess work quality.
- **Context provided by driver**:
  - Original goal and acceptance criteria
  - Produced artifacts
  - Summary of completed tasks
- **Output**: observation with conditions, particularly `quality_score` and
  `completeness`.
- **Trigger conditions**:
  - Exec subagent reports low confidence in its observation
  - Driver detects ambiguity or conflicting signals across tasks
  - Final verification in Phase 4

---

## 5. Escalation Handling

When the main agent receives `ESCALATE:<reason>` from the driver:

1. **Read context**: `ctrlflow/escalation.json` contains the problem
   description written by the driver. Also read `state.json`, `profile.json`,
   and recent `trace/trace.jsonl` entries to understand the full situation.

2. **Analyze**: the main agent uses its own judgment --- this is intentional.
   The driver identifies the problem; the main agent decides the resolution.
   This separation keeps the driver deterministic and the resolution flexible.

3. **Decide on resolution**. Options include:
   - Modify the task graph (skip a task, add a task, change dependencies)
   - Write a manual observation (main agent substitutes for a failed subagent)
   - Trigger a full replan (go back to Phase 2)
   - Abort the task (with a partial report)
   - Ask the user for guidance

4. **Write resolution**: write the decision to `ctrlflow/resolution.json`.

5. **Resume**:
   ```bash
   pymh resume
   ```
   The driver reads `resolution.json` and applies the changes to state.

**Key discipline**: the main agent does NOT directly modify `state.json`,
`profile.json`, or `cursor.json`. It writes `resolution.json` and lets the
driver apply the changes. This preserves the invariant that all state
transitions flow through the driver. See [[07-invariants-and-escalation]] for
the full escalation protocol.

---

## 6. Subagent Context Isolation

Per [[specs]] K2:

- **CLAUDE.md access**: allowed (Option A). Subagents can read CLAUDE.md. It
  contains useful project information (build commands, conventions, etc.) and
  blocking it would reduce subagent effectiveness.

- **Memory file access**: allowed for now. There is potential overlap between
  mh's `profile.json` and Claude Code's memory files. Optimizing this overlap
  is deferred to a future version.

- **Global Claude Code configuration**: subagents may see it. This is
  acceptable and often helpful (e.g., tool preferences, shell configuration).

- **Primary context control mechanism**: the instruction itself. Each subagent
  gets a focused, driver-assembled instruction that emphasizes what to pay
  attention to. The instruction is the main lever for controlling what the
  subagent does --- not file access restrictions.

The practical implication: subagents have broad file access but narrow
*attention*. The driver crafts instructions that focus the subagent on its
specific task, relevant profile entries, and the correct output format. Extra
context from CLAUDE.md or memory files is available but not highlighted.

---

## 7. User Commands During Execution

The user can interact with mh during task execution by speaking to the main
agent. The main agent interprets these as commands:

| User Says | Main Agent Does |
|-----------|-----------------|
| "status" | `pymh status` --- display compact status |
| "abort" | `pymh abort` --- stop task, generate final report |
| "add fuel 10" | `pymh fuel --add 10` --- extend budget |
| "show profile" | Read `profile.json` --- display accumulated knowledge |
| "show trace" | Read `trace/trace.jsonl` --- display execution history |
| "show plan" | Read `ctrlflow/task-graph.yaml` --- display current task graph |

These are not formal commands with parsing --- the main agent pattern-matches
on user intent and runs the appropriate pymh subcommand or file read. The
user can also ask freeform questions ("what task is running?", "why did task 3
fail?") and the main agent will consult state files to answer.

---

## 8. Interrupt Protocol

When the user sends a message during task execution, the main agent classifies it and responds accordingly:

| User Intent | Classification | Main Agent Action |
|-------------|---------------|-------------------|
| Status query ("how's it going?", "what step?") | `status` | Call `pymh status`, display result, resume loop |
| Abort request ("stop", "abort", "cancel") | `abort` | Call `pymh abort`, display report |
| Fuel extension ("add fuel 10", "more steps") | `fuel` | Call `pymh fuel --add N`, confirm, resume loop |
| Information query ("why did t3 fail?", "show profile") | `info` | Read relevant state files, answer question, resume loop |
| Direction change ("also migrate the CSS", "change approach") | `replan` | Pause the exec loop. Write the user's new direction into the replan context. Trigger a replan phase (go to Phase 2) |
| General conversation (unrelated to task) | `passthrough` | Answer briefly, resume loop |

The main agent should never silently ignore user input. If classification is ambiguous, ask the user for clarification before resuming.

---

## 9. Recovery Sub-Skill

When the driver detects state anomalies that prevent normal execution, it can request the main agent to spawn a recovery subagent. This is a higher-privilege subagent designed to repair the execution state.

### Trigger Conditions

- Observation file missing after subagent completion (already handled by Completion Check)
- `state.json` or `cursor.json` corrupted or inconsistent
- Main agent context loss (detected when the main agent cannot recall the current task-id or phase)
- Driver returns an unexpected error code

### Recovery Protocol

1. The main agent detects the anomaly (or the driver returns `ESCALATE:state_anomaly`).
2. The main agent spawns a recovery subagent with a focused instruction:
   - Read `state.json`, `cursor.json`, `phase.json`, and recent `trace.jsonl` entries
   - Identify the last known good state
   - Determine the appropriate recovery action
3. The recovery subagent can:
   - Reconstruct `cursor.json` from trace data
   - Write a synthetic observation for a missing node
   - Recommend resuming from a specific point
4. The main agent applies the recovery and resumes the normal loop.

### Context Loss Recovery

All mh execution state lives on disk. If the main agent's context is compacted by Claude Code, the main agent can recover by:

1. Reading `~/.mh/tasks/` to find the active task workspace
2. Reading `state.json` for current step and fuel
3. Reading `cursor.json` for the current execution position
4. Reading `phase.json` for the current meta phase
5. Resuming the loop from the appropriate phase and step

The SKILL.md instruction should include a "recovery check" at the start: "If you do not have the current task-id in context, read `~/.mh/history.jsonl` to find the most recent running task and reconstruct your position from its workspace files."

Per [[specs]] K8.

---

## 10. OpenClaw Compatibility

The skill format (prompt file + Python scripts) is compatible with OpenClaw's
skill system. Compatibility means:

- **Same SKILL.md**: can be used as an OpenClaw skill prompt without
  modification. The orchestration protocol described in SKILL.md is
  framework-agnostic.

- **Same pymh**: the Python driver works in both environments. It
  reads and writes files --- it does not depend on Claude Code APIs or
  internals.

- **Different dispatch mechanism**: OpenClaw may use different syntax for
  spawning subagents, but the driver's output format (`DISPATCH`, `PARALLEL`,
  `DONE`, `REPLAN`, `ESCALATE`, `BLOCKED`) is agnostic. The main agent (or
  OpenClaw equivalent) translates these into the appropriate spawn calls.

Full OpenClaw compatibility testing is deferred to post-MVP. See [[specs]]
deferred items for the tracking entry.

---

## Cross-References

- [[04-python-driver]] --- commands referenced in this protocol
- [[03-data-flow]] --- context bundle details, workspace layout
- [[01-entropy-and-monads]] --- source material for principles.md
- [[07-invariants-and-escalation]] --- escalation protocol details
- [[specs]] --- design decisions referenced throughout (K3, K1, K2, I1, D13, K4, K8, K5, C9)
