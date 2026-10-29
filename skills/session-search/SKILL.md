---
name: session-search
description: Use when the user refers to an earlier conversation, asks what was decided before, or wants to find a past agent session.
---

# Session search

Use `agent-walker-search` to recall historical Claude Code, Codex, and OpenCode sessions. Always request `--json` when another agent will consume the result.

Search all providers for a decision:

```bash
agent-walker-search --query "what we decided about cache invalidation" --limit 10 --json
```

Scope recall to Claude Code and Codex, with two neighboring messages of context:

```bash
agent-walker-search --query "migration rollback" --providers claude codex --context-turns 2 --limit 5 --json
```

Use `--regex` for an RE2 pattern, `--case-sensitive` for exact case, `--role user|assistant|both` to select speakers, and `--since` or `--until` for RFC3339 or relative time bounds such as `7d` or `12h`. Use `--cwd` to narrow by project and `--count-only` to count matches without returning hits.

The JSON result contains `hits`, `summary`, and `note`. Each hit retains its provider and source provenance. Per-provider availability and errors live under `summary.providers`; inspect them before treating an empty result as evidence that a conversation did not happen.

OpenCode supports literal substring search. For regex queries, Claude Code and Codex continue normally while OpenCode is reported unavailable. Use `AGENT_WALKER_OPENCODE_DB` or `--opencode-db` to override OpenCode database discovery.

This command is for historical recall only. Do not use it for statusline costs, events, progress beacons, or active-session state.
