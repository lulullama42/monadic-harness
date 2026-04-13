# Entropy Sources and Monadic Structure

This document defines the five entropy sources that degrade long-horizon agent performance and shows how **monadic-harness (mh)** manages each one through explicit, structured mechanisms inspired by Haskell's monad patterns.

For project overview and motivation, see [[00-overview]].

---

## 1. Five Entropy Sources

Any agent executing a multi-step task accumulates entropy from five distinct sources. Left unmanaged, each source independently degrades performance; together, they compound into the familiar failure mode of long-horizon agents: confident, expensive drift into irrelevance.

| Entropy Source | Definition | Example | Unmanaged Consequence |
|---|---|---|---|
| **State entropy** | Agent's accumulated knowledge is scattered across context, gets harder to find | Agent forgets a key discovery from step 3 by step 20 | Repeated work, contradictory decisions |
| **Control entropy** | Agent doesn't know "what to do next", re-decides every step | Agent retries the same failing approach repeatedly | Wasted fuel, looping |
| **Side-effect entropy** | Tool call results are unpredictable, introduce unknowns | An unexpected 403 causes 10 steps to handle what should take 1 | Cascading confusion |
| **Context entropy** | Context window grows, signal-to-noise ratio drops | LLM decision quality degrades as irrelevant information accumulates | Worse decisions over time |
| **Goal entropy** | Task goal drifts as execution unfolds | Final output doesn't match original request | Wasted effort on wrong target |

These five sources are exhaustive in practice. Every failure mode we have observed in long-running Claude Code sessions traces back to one or more of them.

---

## 2. How mh Reduces Each Entropy Source

Each entropy source maps to a concrete mh mechanism. The driver orchestrates these mechanisms deterministically — the LLM never needs to "remember" to apply them.

| Entropy Source  | mh Mechanism                                                          | How It Reduces Entropy                                                                                                                                          |
| --------------- | --------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **State**       | `profile.json` (structured, append-only knowledge)                    | Knowledge is always structured, searchable, never compressed away. Discoveries persist as typed facts, not buried paragraphs.                                   |
| **Control**     | Condition-driven task graph                                           | "What next" is determined by explicit conditions, not LLM guessing. The driver evaluates conditions and selects the next task deterministically.                |
| **Side-effect** | Observation protocol (every side effect produces a structured signal) | Surprises are quantified; high-entropy events auto-trigger re-evaluation. The driver can distinguish routine results from anomalies without LLM interpretation. |
| **Context**     | Context bundles (each subagent sees only what it needs)               | Signal-to-noise ratio is managed by the driver, not left to grow unbounded. Irrelevant history is excluded, not just hoped-to-be-ignored.                       |
| **Goal**        | Invariant system (drift check, fuel management)                       | Periodic goal-alignment checks run at zero token cost. Forced correction on drift before the agent can wander further.                                          |

The key insight: none of these mechanisms rely on the LLM being disciplined or self-aware. They are external constraints imposed by the Python driver.

---

## 3. The Monad Connection

The mapping from mh mechanisms to Haskell monad patterns is not superficial analogy — it reflects a deep structural correspondence. Each monad pattern solves exactly one category of computational effect management. mh applies the same separation to agent execution.

### State Monad -> State Entropy

The State monad makes state transitions explicit and trackable. A computation `s -> (a, s)` takes a state in, produces a value and a new state out. The state is never implicit; it is always a first-class value flowing through the computation.

In mh, `profile.json` and `state.json` serve this role. The agent's accumulated knowledge is not scattered across the context window — it is an explicit, structured value that the driver threads through every step. When a subagent discovers something, it returns a profile update as part of its typed output. The driver merges it into the profile deterministically. Knowledge never exists only "somewhere in the conversation history."

### Reader Monad -> Context Entropy

The Reader monad runs each computation in a controlled, read-only environment. The computation cannot modify the environment; it can only read from what it was given. Different computations can receive different environments.

In mh, the driver assembles a focused **context bundle** for each subagent invocation. The plan subagent sees the goal and current profile. The exec subagent sees its task's action description and the relevant scratchpad entries. The verify subagent sees the goal and produced artifacts. No computation sees the full context dump. The driver decides what each subagent needs, and the subagent operates within that scoped environment. This is how context entropy is structurally prevented rather than retroactively managed.

### Writer Monad -> Side-effect Entropy

The Writer monad requires every computation to produce a structured log alongside its return value. Side effects are not invisible — they are typed, accumulated outputs that the caller can inspect and process.

In mh, every subagent produces a structured **observation** — a typed record containing signal (what happened), surprise (how unexpected, 0.0-1.0), conditions met or unmet, and narrative context. Side effects are not free-form text that the driver must parse or hope to understand. They are typed, quantified outputs that the driver processes deterministically. A surprise value of 0.8 triggers re-evaluation automatically; a surprise of 0.1 proceeds normally. The driver never needs to "read" the narrative to decide what to do.

### Either Monad -> Control Entropy

The Either monad makes failure a typed value, not an exception. A computation returns `Right value` on success or `Left error` on failure. The caller pattern-matches on the result and handles both paths explicitly.

In mh, every subagent output carries explicit success/failure typing: `success: false`, `blocker: "..."`, `escalate: true`. The condition-driven task graph handles failure paths as first-class transitions — retry with different parameters, switch strategy, escalate to the user, trigger replanning. Failure is never "the LLM failed and we hope it recovers on the next step." It is a structured signal that the driver routes through predetermined control flow. See [[02-control-flow]] for condition evaluation and transition mechanics in detail.

### The Composition

What makes monads powerful in Haskell is not any single pattern — it is their **composition** via monad transformers. Similarly, mh's power comes from composing all four patterns simultaneously. A single subagent invocation:

- Receives a scoped context bundle (**Reader**)
- Operates on explicit state (**State**)
- Produces structured observations (**Writer**)
- Returns a typed success/failure result (**Either**)

The driver orchestrates this composition. Each subagent call is a pure function from the driver's perspective: context in, structured result out.

---

## 4. Comparison with Existing Approaches

| Dimension | ReAct (raw agent) | LangGraph / CrewAI | monadic-harness |
|---|---|---|---|
| **Control flow** | Implicit (LLM remembers) | Explicit but predefined (state machine / DAG) | Explicit and dynamically growing (agent plan + driver compile) |
| **Data flow** | Full context | Framework-managed state | File system + context bundle selection |
| **Side-effect mgmt** | None | Partial (retry / fallback) | Structured observe + surprise + profile |
| **Concurrency** | None / ad-hoc | Framework-level | Task-graph-level, declarative |
| **Invariants** | None | None / custom guard | Built-in, zero token |
| **Deployment** | Built into framework | Standalone framework | **Lightweight skill** — does not replace the framework |

The last row deserves emphasis. LangGraph and CrewAI are **agent frameworks** — they own the execution loop, define the agent abstraction, and require you to build within their paradigm. monadic-harness is none of these things. It is an **enhancement layer** — an exoskeleton that wraps around any existing agent (including a raw Claude Code session) and provides entropy management without replacing the underlying execution model.

This means:

- mh works with Claude Code's native tool-calling, not against it.
- mh does not require migrating to a new framework.
- mh can be adopted incrementally — you can use the profile system alone, or the full driver, depending on the task.
- mh composes with other tools and skills rather than competing with them.

The right mental model is not "mh vs. LangGraph" but "mh as a structured discipline layer that any agent benefits from."

---

## 5. Design Principles

Five principles guide every implementation decision in mh. When a design choice is ambiguous, these principles resolve it.

### Principle 1: Deterministic Where Possible, LLM Only Where Necessary

The Python driver handles all decidable logic — condition evaluation, state transitions, invariant checks, context bundle assembly, fuel accounting — at **zero token cost**. The LLM is invoked only for inherently creative or uncertain work: planning a novel approach, executing a task that requires reasoning, resolving genuine ambiguity.

This is not an optimization. It is a correctness requirement. Every decision delegated to the LLM is a decision subject to context entropy, state entropy, and goal entropy. Minimizing LLM decision surface minimizes entropy exposure.

### Principle 2: Every Side Effect Produces Structured Signal

No tool call result enters the system as raw, unstructured text. The observation protocol requires every subagent output to carry:

- Typed conditions (met/unmet, with identifiers the driver can match)
- A quantified surprise value (0.0-1.0)
- Structured profile updates (facts discovered, keyed and typed)
- A narrative summary (for human review, not driver logic)

The driver never parses natural language to make control decisions. It reads typed fields. Natural language exists for human auditability, not machine consumption.

### Principle 3: Context Is a Managed Resource, Not an Append-Only Dump

Each subagent receives a **tailored context bundle**. The driver controls what information flows to which computation. Irrelevant context is excluded, not just tolerated.

This directly addresses the fundamental failure mode of long-horizon agents: context windows fill with noise, and the LLM cannot distinguish signal from irrelevant history. mh prevents this by never exposing the full history to any single computation. See [[03-data-flow]] for the workspace and context bundle design.

### Principle 4: Invariants Run at Zero Token Cost on Every Step

Loop detection, drift checks, and fuel management are **pure Python checks** inside the driver. They execute before and after every subagent invocation. They do not depend on LLM "self-awareness" — even if the LLM is completely lost, confused, or hallucinating, invariants catch the problem externally.

This is the safety net that makes long-horizon execution viable. The agent does not need to know it is looping; the driver detects the loop. The agent does not need to notice goal drift; the driver measures it. The agent does not need to track fuel; the driver enforces the budget.

### Principle 5: Concurrency Is Structural, Not Ad-Hoc

Parallelism emerges from the task graph's dependency structure. The driver identifies independent task nodes — nodes with no mutual data or control dependencies — and dispatches them concurrently. This is not "the LLM happens to call multiple tools at once." It is "the task structure guarantees these subtasks are independent, and the driver exploits that independence."

This means concurrency is safe by construction. There are no race conditions to debug because independent nodes, by definition, do not share mutable state. The task graph makes independence explicit and machine-verifiable.

---

## Cross-References

- [[00-overview]] — Project motivation and architecture overview
- [[02-control-flow]] — Condition-driven task graph, Either Monad in practice, transition mechanics
- [[03-data-flow]] — Workspace design, State/Reader/Writer implementation, observation protocol details
