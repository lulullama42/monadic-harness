# Monadic Harness — Design Principles

Five principles guide all implementation decisions. When a choice is ambiguous, these resolve it.

## 1. Deterministic Where Possible, LLM Only Where Necessary

The Python driver handles all decidable logic — condition evaluation, state transitions, invariant checks, context bundle assembly, fuel accounting — at **zero token cost**. The LLM is invoked only for inherently creative or uncertain work: planning, executing tasks that require reasoning, resolving genuine ambiguity.

Every decision delegated to the LLM is subject to entropy. Minimizing LLM decision surface minimizes entropy exposure.

## 2. Every Side Effect Produces Structured Signal

No tool call result enters the system as unstructured text. The observation protocol requires every subagent output to carry typed conditions, a quantified surprise value (0.0–1.0), structured profile updates, and a narrative summary.

The driver never parses natural language to make control decisions. It reads typed fields. Narrative exists for human auditability, not machine consumption.

## 3. Context Is a Managed Resource

Each subagent receives a tailored context bundle. The driver controls what information flows to which computation. Irrelevant context is excluded, not just tolerated.

This prevents the fundamental failure mode of long-horizon agents: context windows filling with noise until the LLM cannot distinguish signal from irrelevant history.

## 4. Invariants Run at Zero Token Cost

Loop detection, drift checks, and fuel management are pure Python checks inside the driver. They execute on every step. They do not depend on LLM self-awareness — even if the LLM is lost, invariants catch the problem externally.

## 5. Concurrency Is Structural

Parallelism emerges from the task graph's dependency structure. Independent nodes are dispatched concurrently. This is safe by construction: independent nodes do not share mutable state. The task graph makes independence explicit and machine-verifiable.
