# Observation Schema

Every exec subagent must write a structured observation JSON file. This is the contract between subagents and the driver.

## Full Schema

```json
{
  "success": true,
  "signal": "migrated app entry point to vite format",

  "conditions": {
    "quality_score": 85,
    "completeness": "full",
    "blocker": null,
    "confidence": "high",
    "needs_replan": false,
    "escalate": false
  },

  "evidence": {
    "tests_passing": true,
    "build_success": true,
    "command_exit_codes": [0, 0],
    "artifact_exists": true
  },

  "tags": {},

  "surprise": 0.3,

  "profile_updates": {
    "vite_version": "6.2",
    "config_format": "ts"
  },

  "files_changed": ["vite.config.ts", "package.json"],
  "new_tasks": [],

  "narrative": "Installed vite successfully. The existing webpack config uses a custom resolve alias setup that will need special handling."
}
```

## Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `success` | bool | Overall success/failure |
| `signal` | string | One-line summary for progress display and trace |
| `conditions` | object | 6 core self-assessment values (see below) |
| `surprise` | float 0–1 | How unexpected the result was; high values trigger re-evaluation |

## Core Conditions

| Field | Type | Values |
|-------|------|--------|
| `quality_score` | int 0–100 | Quality of work produced |
| `completeness` | enum | `"full"`, `"partial"`, `"none"` |
| `blocker` | string\|null | Blocking issue identifier, or null |
| `confidence` | enum | `"high"`, `"medium"`, `"low"` |
| `needs_replan` | bool | Whether the plan needs revision |
| `escalate` | bool | Whether to escalate to the main agent |

## Optional Fields

| Field | Type | Description |
|-------|------|-------------|
| `evidence` | object | Hard signals: test results, build status, exit codes |
| `tags` | object | Open-ended key-value pairs for custom conditions |
| `profile_updates` | object | Key-value pairs to merge into profile.json |
| `files_changed` | string[] | Paths of modified files |
| `new_tasks` | object[] | Suggested new task nodes |
| `narrative` | string | Free-form context for humans and replan subagents |

## Notes

- The driver trusts `evidence` over `conditions` when they contradict.
- `narrative` is for human review — the driver does NOT parse it for decisions.
- If you cannot determine a value, use conservative defaults: `success: false`, `confidence: "low"`, `surprise: 0.5`.
