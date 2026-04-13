# Plan Format

Plans are written as YAML files. The file **must** have a `plan:` root key containing `goal` (string) and `steps` (list).

## Structure

```yaml
plan:
  goal: "Description of what we're trying to accomplish"
  steps:
    - id: t1
      action: "What to do in this step"
      success_criteria: "How to know it worked"
      retry_strategy: "What to try if it fails"
    - id: t2
      action: "Next step"
      success_criteria: "Expected outcome"
      retry_strategy: "Fallback approach"
```

## Required Step Fields

| Field | Description |
|-------|-------------|
| `id` | Unique identifier (e.g., `t1`, `t2a`). Must be unique across all steps. |
| `action` | What the subagent should do. Be specific and actionable. |
| `success_criteria` | Observable condition that indicates success. |
| `retry_strategy` | One of: `"try a different approach"`, `"proceed with what we have"`, `"stop and escalate"` |

## Optional Step Fields

| Field | Description |
|-------|-------------|
| `can_parallel_with` | List of step IDs that can run concurrently with this one. |

## Parallel Steps

Steps that can run concurrently declare each other in `can_parallel_with`:

```yaml
    - id: t2a
      action: "Implement login endpoint"
      success_criteria: "POST /login returns JWT"
      can_parallel_with: [t2b]
      retry_strategy: "proceed with what we have"
    - id: t2b
      action: "Implement registration endpoint"
      success_criteria: "POST /register creates user"
      can_parallel_with: [t2a]
      retry_strategy: "proceed with what we have"
```

## Guidelines

- Keep steps focused — one clear action per step.
- 3–8 steps is typical. More than 10 usually means steps are too granular.
- Step ordering is determined by sequence position. Steps execute top to bottom.
- Parallelize independent work to save fuel.
- Success criteria should be observable (tests pass, file exists, build succeeds) not subjective.
