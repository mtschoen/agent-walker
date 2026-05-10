# claude-walker — Interface & Correctness Spec

Every implementation in this repo MUST honor this spec. The conformance
harness (`shared/conformance.py`) verifies it.

## CLI contract

### Invocation

```
walker --period <seconds> --win-start <unix-epoch> [--projects-root <path>]
```

| Flag             | Required | Type    | Default                 |
| ---------------- | -------- | ------- | ----------------------- |
| `--period`       | yes      | u64     | —                       |
| `--win-start`    | yes      | f64     | —                       |
| `--projects-root`| no       | path    | `~/.claude/projects`    |
| `--now`          | no       | f64     | current wall clock      |

`--win-start` and `--now` accept Unix epochs with optional fractional
seconds. `--now` exists so conformance tests can pin "now" to a fixed
moment; production callers omit it.

### Output

One JSON line on stdout, exit 0:

```json
{"trailing_usd": 1480.150500, "window_usd": 1480.150500, "files_walked": 294, "groups": 129, "elapsed_ms": 47}
```

Fields:

| Field         | Required | Type | Notes                                       |
| ------------- | -------- | ---- | ------------------------------------------- |
| `trailing_usd`| yes      | f64  | Cost in `[now - period, now]`               |
| `window_usd`  | yes      | f64  | Cost in `[win_start, ∞)`                    |
| `files_walked`| no       | u32  | Diagnostic — files that survived mtime skip |
| `groups`      | no       | u32  | Diagnostic — distinct (slug, session) keys  |
| `elapsed_ms`  | no       | u64  | Diagnostic — wall-clock from arg parse to print |

Unknown fields are reserved; consumers ignore them.

### Errors

Anything other than exit 0 with a single JSON line on stdout is "fall back
to caller's reference path." Stderr is for diagnostics. The walker MUST
NOT panic, hang, or write partial output. Bad input means clean error +
non-zero exit.

## Discovery

Glob `<projects-root>/*/*.jsonl` for parents and
`<projects-root>/*/*/subagents/agent-*.jsonl` for subagents. Group by
`(parent_dir_name, session_id)` where `session_id` is the parent file's
stem or the subagent's grandparent dir name.

## Filters

### File-level (mtime)

Skip any file where `mtime < min(now - period, win_start)`. Prunes the
~80% of historical transcripts that can't possibly contain in-range
entries.

### Line-level

Within each surviving file, accept a line iff:

1. It parses as JSON (skip silently otherwise).
2. `entry.message.role == "assistant"` (skip otherwise).
3. If `entry.message.id` is set and already seen **in this group's
   dedup set**, skip.
4. `entry.timestamp` parses as ISO 8601 (with optional `Z` suffix,
   interpreted as UTC). Skip otherwise.
5. `entry.timestamp >= min(period_cutoff, win_start)`.

## Pricing

Per-MTok input/output rates by family (substring match on lowercased
model id):

| Family | Input | Output |
| ------ | ----- | ------ |
| opus   | 5.0   | 25.0   |
| sonnet | 3.0   | 15.0   |
| haiku  | 1.0   | 5.0    |

- `cache_read = input_rate × 0.10`
- `cache_write = input_rate × 1.25`
- Unknown family falls back to **sonnet** rates (matches Python).
- Opus 1M-tier doubling is **not** applied here. Matches the statusline's
  documented under-estimate for big-context Opus.

Cost for one assistant turn (all token counts default to 0 if missing):

```
cost = (
    input_tokens * input_rate
  + cache_read_tokens * input_rate * 0.10
  + cache_write_tokens * input_rate * 1.25
  + output_tokens * output_rate
) / 1_000_000
```

Token field names in the JSONL `usage` object:
`input_tokens`, `output_tokens`, `cache_read_input_tokens`,
`cache_creation_input_tokens`.

## Bucketing

For each accepted assistant turn:

```
if ts >= now - period:   trailing += cost
if ts >= win_start:      window   += cost
```

Both can be true (overlapping ranges); the same cost contributes to both
buckets.

## Dedup scope

Per **session group**. The dedup `seen_ids` set is local to one
`(slug, session_id)` group; it covers `parent.jsonl` and any
`subagents/agent-*.jsonl` under the same session_id. Cross-session dedup
is NOT performed — the only collision pattern observed in real corpora
is parent ↔ its-own-acompact-subagent, which session-grouping handles.

Within a group, files are walked in any order; a single shared
`seen_ids` set is consulted before counting.

## Concurrency

Free choice per implementation. Goal: pin all available cores on the
parse work. Recommended starting point: spawn min(8, ncpu) workers; one
group per work unit; merge sums after.

Determinism: the same input MUST produce the same output regardless of
how groups are scheduled. Float addition order within a group is
deterministic (sequential walk inside a group); cross-group sums use
fully-associative addition over a small set so reordering is acceptable
within float epsilon (±$0.01 budget covers it).

## Conformance fixtures

`shared/corpus/<NN>-<name>.jsonl` (and optional `<NN>-<name>/subagents/`)
plus `shared/corpus/expected.json` mapping fixture name → expected
output. The harness invokes each binary against the corpus and asserts
agreement to ±$0.01.

## Versioning

Each binary supports `--version` printing `<lang>/<version>` (e.g.
`rust/0.1.0`). Future ABI changes bump a `spec_version` field in the
output JSON.
