# Design Specifications

Architectural specifications for pymh v1.

## Architecture

| ID | Spec | Rationale |
|----|------|-----------|
| A1 | **Form factor**: Claude Code Skill first, keep OpenClaw compatibility | Lightweight, immediately usable; OpenClaw uses compatible skill format |
| A2 | **Framework scope**: Claude Code primary, executor layer abstracted for future portability | Avoid tight coupling with one framework while shipping fast |
| A3 | **Naming**: Repo `monadic-harness`, short `mh`, PyPI `pymh`, skill `/mh` | Clear, available names across all registries |
| A4 | **Core value**: Entropy management — structured reduction of state/control/side-effect/context/goal entropy | More fundamental than "structured exploration" — captures what mh actually does |
| A5 | **Workspace location**: Global `~/.mh/` (not project-local) | Task artifacts are ephemeral; global avoids polluting project directories |
| A6 | **Project structure**: Modular `src/pymh/` with ~12 Python modules; skill/ and templates/ as package data | Each subsystem testable in isolation; standard Python packaging |
| A7 | **Dev tooling**: uv + pytest + ruff + mypy; CI on GitHub Actions | Modern, fast, well-supported Python toolchain |
| A8 | **Installation**: `pip install pymh` + `pymh setup` (explicit command); auto-create `~/.mh/` on first init as fallback | pip cannot reliably write to user directories; explicit setup is idempotent |

## Control Flow

| ID | Spec | Rationale |
|----|------|-----------|
| C1 | **Meta control flow**: Simplified `plan -> exec -> verify -> (done \| replan)` | Outer loop should be minimal; complexity lives in the task layer |
| C2 | **Task control flow**: Condition-driven state machine (not static DAG with deps) | Conditions express branches, retries, dynamic termination — richer than fixed deps |
| C3 | **Condition syntax**: `var op val` with `and`/`or`, 1-3 clauses, no nesting | Simple enough for LLM to produce and driver to parse; covers practical cases |
| C4 | **Condition precedence**: `and` binds tighter than `or`; no parentheses; max 3 clauses | Matches programming intuition; left-to-right evaluation was ambiguous |
| C5 | **Condition extensibility**: Fixed 6 core conditions + open-ended `tags` field | Stable core schema + arbitrary extensibility without breaking observation contract |
| C6 | **Two-pass plan generation**: LLM writes NL plan, driver compiles to condition-driven graph | Trades ~100 lines of driver code for massive LLM output reliability improvement |
| C7 | **Compilation tolerance**: Lenient compilation with auto-filled defaults; recoverable via replan | Wrong compilation is recoverable via replan; blocking too often is worse |
| C8 | **No-match behavior**: Compiler injects `default` rule on every `on_complete`; runtime never encounters no-match | Eliminates ambiguity between "implicit retry" and "escalate"; testable and deterministic |
| C9 | **Default injection**: Subagent writes observation file directly; driver validates with default-filling | Avoids main agent bottleneck; malformed output handled by existing validation |
| C10 | **Fuel semantics**: 1 fuel = 1 driver cycle; parallel dispatch = 1 fuel | Measures logical progress, not resource consumption; encourages parallelism |
| C11 | **Fuel exhaustion**: If artifacts exist, enter verify; if no artifacts, abort. Single path | Converges toward best possible outcome; unifies control flow definitions |
| C12 | **Fuel convergence is runtime only**: Runtime invariant in `observe`, not a compiled rule injected into task graphs | Invariants handle cross-cutting concerns; redundant per-node fuel rules add no value |
| C13 | **Granularity**: Adaptive — mh only intervenes where the agent alone would fail | Match agent capability; avoid over-controlling what agents do well |
| C14 | **Control flow format**: Agent plans "what to do"; Python driver converts to executable control flow | Separation of concerns: LLM is good at planning, programs are good at interpreting |
| C15 | **Graph modification: cursor reachability validated**: After patching graph, verify cursor node is reachable from first task via goto traversal | Prevents orphaned cursor pointing to unreachable node |
| C16 | **Graph modification: edge case validation**: `insert_before` nonexistent node errors; `remove_task` on current cursor node errors; no-successor remove redirects to "done" | Catches common agent mistakes; other behaviors are documented |
| C17 | **skip_task forces success=True**: Always override to `success=True` to ensure cursor advancement | Intent of skip is to advance; use `write_observation` for failure behavior |
| C18 | **DONE finalization**: `decide` clears `cursor.current_task`, sets `meta.status` to `"done"`, updates history, and appends a trace entry when returning DONE | State machine must close cleanly; no bandaid auto-correction needed downstream |

## Data Flow

| ID | Spec | Rationale |
|----|------|-----------|
| D1 | **Observation schema**: Structured conditions + signal + surprise + profile_updates + narrative + tags | Conditions for driver; narrative for replan context; tags for extensibility |
| D2 | **Observation validation**: Default-filling + logging when malformed | Safe defaults are conservative; rewrite requests are expensive and may fail again |
| D3 | **Profile**: Separate `profile.json` from `state.json` for easy subagent access | Subagent can `Read profile.json` directly without parsing state |
| D4 | **Profile lifecycle**: No size cap; mechanical merge (add/update, never delete); last-write-wins for parallel | Unlikely to exceed a few KB in practical tasks; revisit with real data |
| D5 | **Evidence-conditions split**: `conditions` (self-assessment) + `evidence` (hard signals) as separate blocks | Enables driver to detect contradictions; evidence-first decision making |
| D6 | **Evidence not in condition space**: `evidence` fields cannot be referenced in `on_complete` condition rules | Clean separation: conditions for routing, evidence for safety checks |
| D7 | **Contradiction detection**: Driver auto-raises surprise to 0.7+ when evidence contradicts conditions | Zero-token safety net against self-assessment inflation; no extra subagent needed |
| D8 | **Bool coercion**: `observe` normalizes string booleans/nulls/numerics for the 6 core condition fields | LLMs commonly produce `"true"` instead of `true`; coercion prevents silent evaluation failures |
| D9 | **Namespace collision prevention**: `observe` strips system condition names from `observation.conditions` with warning | Prevents subagent from accidentally shadowing `fuel_remaining` etc. in condition engine |
| D10 | **Instruction template**: Simple markdown with labeled sections (Action, Context, Previous Attempt, Output) | Minimal, functional; subagent reads as context, not structured schema |
| D11 | **Plan file location**: `ctrlflow/plans/current.yaml` + versioned history (`v1.yaml`, `v2.yaml`, ...) | Plan is control-plane data; versioning enables replan diagnostics |
| D12 | **Node-id vs task-id**: Graph nodes use `node-id` (t1, t2, t3a); workspace uses `task-id` (uuid). File paths use `{node-id}-{attempt}` | Prevents naming collision between workspace identity and graph nodes |
| D13 | **Observe command**: Explicit `--node <id> --attempt <n>` required; no "read latest" behavior. Parallel: `--parallel <id1>,<id2>,...` | Eliminates stale-read risk; makes protocol machine-verifiable |
| D14 | **Completeness enum**: `"full"` \| `"partial"` \| `"none"` | Synthesized failures and catastrophic failures need a distinct value below "partial" |
| D15 | **Templates**: `~/.mh/templates/` with presets + user custom | Provides starting points for plan subagent without over-engineering |
| D16 | **Replan failure summary**: Driver auto-generates `failure_summary` from trace when entering replan | Gives replan subagent structured input instead of raw narrative; zero extra LLM cost |
| D17 | **failure_summary schema**: Includes failed_nodes, failure_signals, evidence_contradictions, profile_facts, steps, fuel | Consumed by plan instruction; replan subagent needs structured context |
| D18 | **Canonical observation write-back**: `observe` writes the normalized observation back to the same file after validation; `decide` reads canonical form | Trust boundary is at normalization; eliminates divergence between raw subagent output and what the driver evaluates |
| D19 | **Success/completeness reconciliation**: `success=true` + `completeness!="full"` sets completeness to `"full"`; `success=false` + `completeness="full"` sets success to `true` | These fields must be consistent; reconciliation prevents contradictory state from reaching condition evaluation |

## Skill Protocol

| ID | Spec | Rationale |
|----|------|-----------|
| K1 | **SKILL.md layering**: Modular — SKILL.md (~150 lines) + principles.md + observation-schema.md + plan-format.md | Progressive loading; main agent context stays small |
| K2 | **Subagent context isolation**: Allow CLAUDE.md access; memory management deferred | CLAUDE.md has useful project info; over-isolation hurts more than mild leaking |
| K3 | **Exec subagent self-assesses**: Verify subagent invoked only when confidence is low or problems arise | Balances accuracy and token cost |
| K4 | **Verify structured output**: Must include goal_met, accepted_artifacts, missing_items, evidence_summary, recommended_action | Transforms verify from yes/no to actionable assessment |
| K5 | **Verify fuel cost**: Verify spawns a subagent and costs 1 fuel | Consistent with all other subagent invocations; keeps main agent lean |
| K6 | **Meta verify**: Lightweight entropy check — "is the goal solved?" Route to replan if not | Not a deep verification; just goal-alignment gating |
| K7 | **Dynamic mutation**: Small changes via subagent suggestion + driver approval; large changes via replan at meta layer | Proportional response — minor adjustments don't need full replan |
| K8 | **Recovery sub-skill**: Recovery subagent defined in SKILL.md; invoked by driver on state anomalies (missing files, context loss) | Higher-privilege repair agent; all state on disk enables reconstruction |
| K9 | **LLM fallback**: Driver does deterministic work; LLM steps in when format is broken | Not a principle issue; if LLM can do it well, let it |
| K10 | **Escalation protocol**: escalation.json -> resolution.json -> `pymh resume` | Fully traceable; clear contract between driver and main agent |
| K11 | **Resume output**: `RESUMED:` prefix + any decide output (`DISPATCH`, `ESCALATE`, `REPLAN`, `DONE`, `ABORT`) | Main agent re-parses suffix with same logic as decide; prefix distinguishes "after resume" from "fresh" |
| K12 | **Abort trace**: Both cli and resume abort include source field (`"cli"` or `"resume"`) | Enables trace analysis to distinguish user abort from resolution abort |
| K13 | **Verify observation schema in SKILL.md**: Verify subagent must produce goal_met, accepted_artifacts, missing_items, evidence_summary, recommended_action | Schema table present in SKILL.md Phase 4 |
| K14 | **Context assembly**: `decide` produces fully assembled instructions; main agent passes as-is | Prevents main agent from manually injecting extra context |

## Concurrency

| ID | Spec | Rationale |
|----|------|-----------|
| P1 | **Parallel dispatch**: Supported with clear scope constraints | Native parallelism is a major advantage; only requires well-defined subagent I/O |
| P2 | **Merge strategies**: quality_score=MAX, completeness=ALL-or-nothing; conditions are merged with per-field strategies | MAX is intentional: group succeeds if any member achieves high quality; failures caught by completeness |
| P3 | **Merged evidence keyed by node-id**: `{"t2a": {...}, "t2b": {...}}` avoids field conflicts | Two parallel nodes may both have `tests_passing` with different values; keying preserves both |
| P4 | **Wait nodes**: Pure join point — only check completion status, no observation merge | Merge already done by `observe --parallel`; avoids duplicate work |
| P5 | **Fuel cost**: Parallel dispatch = 1 fuel (same as sequential dispatch) | Measures logical progress, not resource consumption; encourages parallelism |
| P6 | **File conflicts**: Instruction guidance only; higher-order harness design deferred | Prompting is cheap and usually sufficient; true isolation adds significant complexity |
| P7 | **pending_parallel**: Cleared when wait node proceeds; documents parallel group membership | Prevents stale state; gives observe context about which nodes are parallel |
| P8 | **completed_tasks: success only**: Only observations with `success == true` add to completed_tasks | Failed observations should not unblock wait nodes; retries should be possible |
| P9 | **Parallel cursor advancement**: When dispatching a parallel group, `decide` advances `current_task` to the wait node immediately | Cursor reflects actual system state (waiting); prevents stale cursor pointing at a dispatched node |
| P10 | **Pre-validate parallel observations**: Each individual observation is validated before merge in `process_parallel_observations()` | Merge function must operate on normalized data; raw observations may have missing fields or type mismatches |

## Invariants

| ID | Spec | Rationale |
|----|------|-----------|
| I1 | **forced_transition storage**: Stored in `ctrlflow/cursor.json`, consumed after use | Cursor is "next-hop control signal"; state.json is global state, not per-step control |
| I2 | **forced_transition schema**: Always dict `{"type": "escalate" \| "replan" \| "verify_or_abort", "reason": "..."}` — never a bare string | Consistent schema; `decide` needs only one code path; reason always available for logging |
| I3 | **Surprise accumulator**: Sum of squared surprise values (`sum(surprise^2)`), not linear sum | Weights high-surprise events exponentially more than low ones; 10x0.1 != 1x1.0 |
| I4 | **Surprise accumulator reset**: On success, accumulator resets to 0.0 then adds current surprise^2 | Even successful observations carry surprise signal; low surprise effectively clears it |
| I5 | **Loop/drift detection**: Built-in 3 invariants — loop detection, drift check, fuel management | Covers common failure modes with zero extra subagent cost |
| I6 | **INVARIANT output**: `observe` emits `INVARIANT:{reason}` to stderr when invariant fires | Main agent can relay to user; next `decide` handles the forced transition |
| I7 | **Invariant config location**: Thresholds read from `config.defaults`, not a separate `invariants` section | Simpler config structure; all thresholds co-located |
| I8 | **Unified attempt threshold**: `max_task_attempts` from config is used by both the loop detection invariant and the compiler's retry rules | Single source of truth; prevents divergence between invariant trigger and compiled retry threshold |
| I9 | **Replan resets state counters**: Entering replan resets `consecutive_failures` to 0 and `surprise_accumulator` to 0.0 | New plan starts with a clean slate; prevents old failures from triggering immediate re-replan |
| I10 | **Observe node_id mismatch warning**: `observe` warns to stderr if `node_id` doesn't match `cursor.current_task` | Defensive check; doesn't block (parallel/resume paths legitimately observe non-cursor nodes) |
| I11 | **Parallel node_ids mismatch warning**: `observe --parallel` warns to stderr if node_ids don't match `cursor.pending_parallel` | Catches wrong parallel group being observed |

## Report & UI

| ID | Spec | Rationale |
|----|------|-----------|
| R1 | **Report generation**: Driver auto-generates `task-report.md` from trace on completion or abort | Structured summary of task execution without extra LLM cost |
| R2 | **Report timeline**: Single step number, merged result, group ID for parallel (not per-node rows) | Parallel group is collapsed at observe layer; reconstructing per-node would need extra I/O |
| R3 | **Key decisions display**: Report reads `observation_summary` from trace (not raw `signal`) | Report operates on trace entries; `observation_summary` is the trace-level equivalent |
| R4 | **Surprise threshold**: Hardcoded at 0.5 for display purposes | Adding config for display-only threshold is over-engineering |
| R5 | **Replan failure summary in report**: Driver includes failure_summary when entering replan | Gives visibility into why replan was triggered |
| R6 | **MVP scope**: pymh + SKILL.md + one template + one end-to-end demo | Ship fast, validate core loop, iterate |

## Packaging

| ID | Spec | Rationale |
|----|------|-----------|
| G1 | **Dependency**: PyYAML as only external dependency; pure stdlib otherwise | YAML is better for LLM-produced task graphs; minimal install footprint |
| G2 | **Skill files install**: `pymh setup` installs to `~/.claude/skills/mh/`; skips existing files, `--force` for overwrites | Prevents losing user edits on repeated setup |
| G3 | **Package data**: skill/ and templates/ included as package data | Standard Python packaging; hatchling includes all files under packages by default |
| G4 | **Observation schema in instructions**: 8-line condensed schema summary appended to every exec instruction | Subagents need to know the required observation format |
| G5 | **PARALLEL format**: `PARALLEL:{n1}:{a1}:{path1},{n2}:{a2}:{path2},...` includes instruction paths | Main agent needs paths to read instruction files; parity with DISPATCH |

## Deferred Items

| Item | Why Deferred | Trigger to Revisit |
|------|-------------|---------------------|
| Adaptive granularity auto-tuning | Interface reserved; need real execution data to calibrate | After collecting task completion stats across diverse tasks |
| OpenClaw compatibility testing | Primary target is Claude Code; same format should work but untested | When OpenClaw adoption grows or a user requests it |
| Memory management integration | CLAUDE.md allowed for now; may overlap with profile | When profile and memory show redundancy in practice |
| Hybrid LLM classification for compilation | Lenient defaults sufficient; would break "zero token cost" driver principle | When compilation misclassification causes visible task failures |
| File isolation via worktrees for concurrency | Instruction guidance sufficient for current parallel tasks | When parallel subagents cause repeated file conflicts |
| Profile size caps and summarization | Profile unlikely to exceed a few KB in practical tasks | When a task produces 50+ profile keys |
| Multiple executor adapters | Only Claude Code subagent for now | When porting to a second framework |
| User-defined custom invariants | Built-in 3 invariants cover common cases | When users report needing domain-specific invariants |
| Verify subagent as permanent role | Exec self-assesses; verify invoked on demand | When quality_score inflation causes visible problems |
| Validator subagent layer | Evidence-contradiction check in driver is sufficient | When false-positive completions are a high-frequency problem |
| Cross-task learning interface (`~/.mh/learning/`) | Goal is "run one task well"; learning requires execution data | After collecting 20+ task runs with diverse failure modes |
| Offline evaluation / replay benchmark | No tasks have run yet; premature to design replay | After real trace data exists |
| `insufficient_evidence` as system state | Surprise escalation on contradiction achieves similar effect without new state | When surprise-based detection proves too coarse |
| Phase-level and system-level evaluation | Step observation + task verify + report covers current needs | When systematic quality regression is observed across tasks |
| Failure taxonomy auto-routing | Driver generates failure_summary; human interprets for now | When failure patterns are well-understood enough to automate routing |
| Shared-file reservation for concurrency | Prompt guidance + verify sufficient for current scope | When parallel subagents cause repeated file conflicts despite guidance |
