# Monadic Harness (pymh)

[![CI](https://github.com/lulullama42/monadic-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/lulullama42/monadic-harness/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/pymh)](https://pypi.org/project/pymh/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Structured entropy management for long-horizon agent tasks. pymh is a [Claude Code](https://docs.anthropic.com/en/docs/claude-code) Skill that wraps agent execution with a deterministic control loop — plan, execute, verify — with invariant guardrails, surprise-driven re-evaluation, and fuel management.

## Why "Monadic"?

Long-horizon agent tasks fail because entropy accumulates unchecked — the agent forgets earlier discoveries, retries the same failing approach, drowns in its own context, and drifts from the original goal. Current agents rely on a single ReAct loop plus hope that the LLM stays on track.

pymh borrows the core insight from functional programming: **manage computational effects with explicit, composable structures instead of implicit side effects**. Each of the five entropy sources maps to a classic monad pattern:

| Entropy Source | Problem | Monad Pattern | pymh Mechanism |
|---|---|---|---|
| **State** | Knowledge scattered across context, lost over time | State monad (`s → (a, s)`) | Structured `profile.json` — discoveries are typed facts, not buried paragraphs |
| **Control** | Agent re-decides "what next" every step, loops | Free monad (reified program) | Condition-driven task graph compiled from a natural language plan |
| **Side-effects** | Tool results are unpredictable, cascade confusion | Writer monad (structured log) | Observation protocol — every action produces a typed signal with surprise score |
| **Context** | Context window grows, signal-to-noise drops | Reader monad (scoped env) | Context bundles — each subagent sees only what it needs, not the full history |
| **Goal** | Task goal drifts as execution unfolds | Except monad (checked abort) | Invariant system — loop detection, drift check, fuel bounds, zero token cost |

The key: none of these rely on the LLM being disciplined. They are **external constraints imposed by a deterministic Python driver**. The LLM does what it's good at (reasoning, coding); the driver handles everything else.

> **On the name**: We designed from the monad mental model, but the actual effect-handling pattern is closer to [Algebraic Effects](https://overreacted.io/algebraic-effects-for-the-rest-of-us/): side effects are described as data (instruction files), handlers are replaceable (the adapter layer), and resumption is achieved through the observation protocol. We chose "monadic" because it describes the derivation path, not the implementation detail.

For the full theoretical framework, see [Entropy Sources and Monadic Structure](docs/01-entropy-and-monads.md).

## Quick Start

```bash
pip install pymh
pymh setup
```

Then in Claude Code:

```
/mh migrate the project from webpack to vite
```

The `/mh` skill drives a structured 4-phase loop: plan the work, execute step by step (with parallel support), verify the result, and generate a report.

## How It Works

```
plan ──→ exec ──→ verify ──┬──→ done
  ^                        │
  └────────────────────────┘  (verify fails → replan)

  + invariant guardrails at every step
  + fuel management (bounded execution)
  + surprise-driven re-evaluation
```

**Phase 1: Init** — User provides a goal. pymh creates a workspace with state files, fuel budget, and execution trace.

**Phase 2: Plan** — A plan subagent writes a YAML task plan. The compiler converts it to a task graph with conditional transitions and parallel groups.

**Phase 3: Execute** — The driver dispatches tasks one at a time (or in parallel). Each subagent writes a structured observation. The driver evaluates conditions, updates state, checks invariants (loop detection, drift, fuel), and advances the cursor.

**Phase 4: Verify** — A verify subagent independently assesses whether the goal was met. If not, the system replans with failure context.

### Key Concepts

- **Fuel** — A bounded execution budget. Each dispatch costs 1 fuel. When fuel runs out, the task escalates. This prevents unbounded agent loops.
- **Surprise** — A float (0–1) attached to each observation. High surprise (> 0.5) triggers re-evaluation. Surprise accumulates across steps; accumulated surprise > threshold triggers replan.
- **Invariants** — Safety checks that run after every observation: loop detection (same task failing repeatedly), drift detection (too many replans), and fuel exhaustion.
- **Observations** — Structured JSON that subagents write after each step. Includes success/failure, quality score, completeness, confidence, and a human-readable narrative.

## CLI Reference

| Command | Description |
|---------|-------------|
| `pymh init --goal "..." --fuel 30` | Initialize a new task workspace |
| `pymh setup` | Install skill files and templates |
| `pymh decide [--phase plan\|verify]` | Get next dispatch instruction |
| `pymh observe --node ID --attempt N` | Process a subagent observation |
| `pymh compile-plan` | Compile NL plan to task graph |
| `pymh status` | Show current task state |
| `pymh report` | Generate task completion report |
| `pymh resume` | Resume after escalation resolution |
| `pymh fuel --add N` | Add fuel to running task |
| `pymh abort` | Abort task and generate report |

All commands accept `--workspace PATH` to target a specific task. Without it, pymh auto-detects the most recent running task.

## Workspace Layout

Each task gets a workspace under `~/.mh/tasks/<task-id>/`:

```
workspace/
├── meta.json               # task identity: id, goal, created, status
├── state.json              # runtime state: fuel, step, surprise accumulator
├── profile.json            # accumulated knowledge (typed facts)
├── ctrlflow/
│   ├── phase.json          # current phase + replan count
│   ├── cursor.json         # current task, attempts, completed_tasks
│   ├── plans/current.yaml  # natural language plan
│   └── task-graph.yaml     # compiled condition-driven task graph
├── dataflow/
│   ├── observations/       # t1-0.json, t1-1.json, ...
│   ├── instructions/       # dispatched instruction files
│   ├── scratchpad/         # subagent working space
│   └── artifacts/          # produced artifacts + task-report.md
└── trace/
    └── trace.jsonl         # append-only execution log
```

## Templates

pymh ships with 3 plan templates:

| Template            | Strategy                                           |
| ------------------- | -------------------------------------------------- |
| `general` (default) | analyze → execute → verify                         |
| `migration`         | research guides → plan → parallel migrate → verify |
| `research`          | search → evaluate → synthesize → verify            |

Use a template: `pymh init --goal "..." --template migration`

Custom templates: add YAML files to `~/.mh/templates/`. See `~/.claude/skills/mh/plan-format.md` for the format spec.

## Development

```bash
git clone https://github.com/lulullama42/monadic-harness.git
cd monadic-harness
uv sync --dev
uv run pytest tests/ -v
```

Requires Python 3.9+. Only runtime dependency: PyYAML.

## Architecture

Design documentation lives in [`docs/`](docs/):

| Document                                                             | Topic                                                   |
| -------------------------------------------------------------------- | ------------------------------------------------------- |
| [00-overview](docs/00-overview.md)                                   | Architecture overview and problem statement             |
| [01-entropy-and-monads](docs/01-entropy-and-monads.md)               | Theoretical framework: entropy as a first-class concern |
| [02-control-flow](docs/02-control-flow.md)                           | Meta control flow, task graph, and state machine        |
| [03-data-flow](docs/03-data-flow.md)                                 | Workspace layout, observation schema, profile           |
| [04-python-driver](docs/04-python-driver.md)                         | Driver commands and CLI protocol                        |
| [05-skill-protocol](docs/05-skill-protocol.md)                       | Claude Code skill integration                           |
| [06-concurrency](docs/06-concurrency.md)                             | Parallel dispatch and merge strategies                  |
| [07-invariants-and-escalation](docs/07-invariants-and-escalation.md) | Safety guardrails and escalation                        |
| [08-user-interface](docs/08-user-interface.md)                       | Progress display and user commands                      |
| [specs](docs/specs.md)                                               | 95 design decisions with rationale                      |
| future-work                                                          | Deferred features and roadmap (internal)                |

## License

[MIT](LICENSE)
