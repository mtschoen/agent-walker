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

## Roots

Every subcommand walks an effective set of project roots assembled as:

1. **Primary root.** From `--projects-root <path>` if given, else
   `~/.claude/projects`.
2. **CLI extras.** Zero or more `--extra-projects-root <path>` flags.
3. **Config extras.** Read from `~/.claude/walker-roots.json` unless
   `--no-config` is passed.

### Config file shape

`~/.claude/walker-roots.json`:

```json
{
  "extra_roots": [
    "/mnt/chonkers/Users/mtsch/.claude/projects"
  ]
}
```

Single key `extra_roots`: array of absolute paths. Per-host; NOT
synced via memory-sync. Missing file → no extras. Malformed JSON →
stderr diagnostic, treat as no extras (must NOT error).

### Resolution

The combined list is:

- Deduplicated by `fs::canonical` (realpath); if `canonical` fails for
  an entry, fall back to its lexically-normalized form.
- Filtered to existing directories. Non-existent extras are skipped
  silently with a stderr diagnostic. (This is the SMB-mount-unreachable
  case — walker must keep going.)
- Order: primary first, CLI extras in order, config extras in order.
  Order is informational; results are aggregated and must not depend on
  it within float epsilon.

Per-group dedup (`seen_ids` on `message.id`) is unchanged. Per-file
mtime filter is unchanged. All applied uniformly across roots.

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

## Subcommands

The bare `walker --period ... --win-start ...` invocation maps to the
`cost` subcommand and stays the back-compat shape for existing callers.
A subcommand is introduced when the first positional argument matches
a known name; otherwise the bare-flag invocation is treated as `cost`.

### `beacons-latest --session-id <id> [--projects-root <path>] [--now <unix>]`

Walks the matching transcript (parent or `subagents/agent-<id>.jsonl`)
backwards, finds the most recent assistant message containing a
`<progress-beacon>...</progress-beacon>` block. The JSON inside must
parse and contain `kind`, `eta_seconds`, `summary`, and `drift` (plus
optional `beats_left`).

Output:

```json
{"beacon": {...} | null, "emitted_at": <unix> | null, "age_seconds": <num> | null, "elapsed_ms": <u64>}
```

If multiple beacons exist in the matching transcript, return the one
with the highest `timestamp`. Malformed JSON or missing required
fields → silently skip (treated as no beacon).

`--now` exists for conformance determinism (otherwise `age_seconds`
varies per wall clock); production callers omit it.

### `beacons-history --period <seconds> [--win-start <unix>] [--projects-root <path>] [--now <unix>]`

Walks the full fleet under the time window. For each session group
(same grouping as `cost` mode) that contains both a `kind: "begin"`
AND a `kind: "end"` beacon within the window, emits a pair with three
elapsed fields:

- `actual_elapsed = end_timestamp - begin_timestamp` (wall-clock)
- `idle_excluded` = sum of gaps inside `[begin_ts, end_ts]` that
  immediately precede a real user prompt (`type: "user"` entries with
  non-`tool_result` content). Tool-result entries don't count as idle
  because they're agent-active time waiting on tool execution.
- `active_elapsed = max(0, actual_elapsed - idle_excluded)`

Computes `bias_factor = median(active_elapsed / begin_eta)` across all
pairs. Even-count median is the mean of the two middle values. The
calculation excludes user-idle time because including it makes the
bias unrepresentative of the agent's actual estimation accuracy — a
session where the user walked away for an hour shouldn't punish the
agent's ETA the same as one where the agent genuinely worked an hour.

Output:

```json
{"pairs": [{"begin_eta": <num>, "actual_elapsed": <num>, "idle_excluded": <num>, "active_elapsed": <num>}, ...], "session_count": <num>, "n_pairs": <num>, "bias_factor": <f64> | null, "elapsed_ms": <u64>}
```

If `n_pairs == 0`, `bias_factor` is `null`.

### `events --period <seconds> [--win-start <unix>] [--projects-root <path>] [--no-config] [--extra-projects-root <path>...] [--now <unix>]`

Emits one JSON object per line (NDJSON) on stdout — one entry per
accepted assistant turn. Reuses cost-mode's parse, dedup, filter, and
pricing logic verbatim; only aggregation differs (per-turn output
instead of accumulated totals).

Flag summary:

| Flag                    | Required | Type    | Default              |
| ----------------------- | -------- | ------- | -------------------- |
| `--period`              | yes      | u64     | —                    |
| `--win-start`           | no       | f64     | `now - period`       |
| `--projects-root`       | no       | path    | `~/.claude/projects` |
| `--no-config`           | no       | bool    | false                |
| `--extra-projects-root` | no       | path[]  | (empty)              |
| `--now`                 | no       | f64     | current wall clock   |

`--no-config` suppresses loading `~/.claude/walker-roots.json`.
`--extra-projects-root` may be repeated; each value appends an extra
root (same semantics as cost mode). `--now` exists for conformance
determinism; production callers omit it.

**Window predicate.** A turn is emitted iff:

```
ts >= min(now - period, win_start)
```

When `--win-start` is omitted, `win_start` defaults to `now - period`,
so the predicate simplifies to `ts >= now - period`.

Every turn that passes this predicate (the same one used by cost
mode's step-5 line filter) is emitted as its own NDJSON line. There
is no further bucketing into `trailing` vs `window` totals — that
split is cost mode's aggregation, not events'.

**Output format.** One JSON object per accepted turn, field order
fixed (matters for line-equality conformance):

```json
{"ts": 1716480000.123, "usd": 0.004217, "model": "claude-sonnet-4-6", "session_id": "abc123", "slug": "C--Users-mtsch--claude-projects--myproject"}
```

Fields:

| Field        | Type   | Notes                                                        |
| ------------ | ------ | ------------------------------------------------------------ |
| `ts`         | f64    | Unix epoch (seconds, fractional) of the assistant turn       |
| `usd`        | f64    | Cost of this turn in USD, computed by the Pricing formula    |
| `model`      | string | Lowercased model id from the transcript; empty string if absent |
| `session_id` | string | Session identifier — same grouping key as cost mode          |
| `slug`       | string | Parent directory name of the `.jsonl` file                   |

No summary or final line is emitted. The consumer reads until EOF.
Exit 0 even when the output stream is empty.

**Ordering.** Implementations MAY emit lines in any order. Conformance
compares event sets as a multiset, sorted by `(ts, session_id, model)`
for tie-breaking stability.
### `search <pattern> [flags]`

Content search over transcripts for the recall problem ("you said X a few
sessions ago, but didn't commit it to memory"). The differentiator is
**cross-root / cross-machine** lookup: search inherits the multi-root
resolution from `## Roots`, so a query reaches into mounted remote-host
transcripts when configured. Read-only — search MUST NOT write to a
transcript or to memory. `<pattern>` is required and positional; an empty
pattern is an error.

| Flag | Default | Notes |
| ---- | ------- | ----- |
| `--regex` | false | Treat pattern as an RE2 regex (no lookaround/backreferences). |
| `--case-sensitive` | false | Default is case-insensitive (the usual recall case). |
| `--role <user\|assistant\|both>` | both | Restrict by message role. |
| `--since <t>` / `--until <t>` | none / now | RFC3339 timestamp or relative (`7d`, `12h`). |
| `--cwd <slug>` | any | Restrict to one project slug (the `~/.claude/projects/<slug>` dir name). |
| `--context <N>` | 1 | Turns of context before AND after each hit (`0` = hit only). |
| `--limit <N>` | 50 | Soft cap; overflow sets `truncated` and emits a stderr narrowing hint. |
| `--count-only` | false | Emit only the summary record — a cheap pre-flight to size a query. |
| `--include-tool-blocks` | false | Also search inside `tool_use` / `tool_result` blocks. |
| `--format <pretty\|jsonl>` | pretty | `jsonl` is agent-consumable (one record per line). |
| `--snippet-chars <N>` | 240 | Max snippet preview chars per hit. |

Filters apply cheapest-first per `## Filters`: file mtime, slug, role,
tool-block exclusion, time window, then the pattern match.

**Content extraction.** `message.content` is sometimes a bare string (older
user-prompt format) and sometimes an array of content blocks; parse it untyped
and concatenate the `{"type":"text"}` blocks — strict typed deserialization
silently drops ~10% of older user prompts. Search reaches `role: user` and
`role: assistant` messages only.

Output (`--format jsonl`): one hit record per line, a summary record last.

```json
{"type":"hit","session_id":"...","cwd_slug":"...","host_root":"...","file_path":"...","line_number":147,"timestamp":"...","role":"assistant","snippet":"...","match_offsets":[[1,16]],"context_before":[{"role":"...","text":"...","timestamp":"..."}],"context_after":[{"role":"...","text":"...","timestamp":"..."}]}
{"type":"summary","hits":3,"sessions_matched":4,"roots_walked":2,"files_walked":218,"truncated":false,"elapsed_ms":142}
```

`host_root` is the killer field — it names which machine's mount the hit came
from, closing the "agent didn't think to check the other host" gap. Ordering
is newest-first, tiebroken by `(timestamp DESC, session_id ASC, line_number
ASC)`. `pretty` mode renders the same data human-readably.

Errors (exit 2, stderr diagnostic): empty pattern (`pattern must be
non-empty`), unparseable regex (`bad regex: <why>`), unparseable
`--since`/`--until` (`bad time: ...`). Malformed JSONL lines are skipped, never
a panic.

Decided constraints: the regex surface is RE2 (no lookaround/backreferences)
across all four impls; substring matching treats newlines as whitespace and
regex honors an explicit `(?m)` in the pattern. No on-disk index, no semantic
search, no mutation.

## MCP shim

`mcp/server.py` is a FastMCP stdio server exposing one tool,
`claude_walker_search`, that subprocesses `walker search ... --format jsonl`
and reshapes the output into `{hits, summary, note}`. It exists so agents get
auto-discovered, cross-cwd recall without constructing a CLI string — the
cross-machine miss is exactly what an always-present tool closes.

- **Binary discovery** (first hit wins): `$CLAUDE_WALKER_BINARY`,
  `~/.claude/walker[.exe]`, `~/.local/bin/claude-walker[.exe]`, then `PATH`.
- **Tool parameters** mirror the CLI flags: `pattern` (required), `regex`,
  `case_sensitive`, `role`, `since`, `until`, `cwd_slug`, `context_turns`,
  `limit`, `count_only`, `include_tool_blocks`.
- **Errors:** a non-zero walker exit (bad input) or a 30 s subprocess timeout
  raises an MCP tool error carrying the walker's stderr; the truncation hint
  from a successful run is passed through as `note`.
- **Logging:** one JSONL event per call (`session_start`/`call`/`return`/
  `error`) at `~/.claude-walker-mcp.log`, mirroring projdash — tail it to trace
  a hang.
- **Registration:** user-scope via
  `claude mcp add --scope user claude-walker -- python <repo>/mcp/server.py`,
  so it's available from every cwd. Launched by absolute script path, not
  `python -m mcp`, to avoid colliding with the `mcp` SDK package name.

## Conformance fixtures

`shared/corpus/<NN>-<name>/<sid>.jsonl` (and optional `<NN>-<name>/subagents/`)
plus `shared/corpus/expected.json` mapping fixture name → expected
output for cost mode. Beacon fixtures live under
`shared/corpus/beacons/<scenario>/<sid>.jsonl` with sibling
`expected_latest.json` and `expected_history.json`. Search fixtures live under
`shared/corpus/search/<scenario>/` with sibling `expected.json`; the harness
structurally compares the JSONL hit/summary records, ignoring `elapsed_ms` and
`files_walked` (they vary per run). The harness
invokes each binary against the corpus and asserts agreement to
±$0.01 for cost and ±0.001 for `bias_factor`.

## Versioning

Each binary supports `--version` printing `<lang>/<version>` (e.g.
`rust/0.1.0`). Future ABI changes bump a `spec_version` field in the
output JSON.
