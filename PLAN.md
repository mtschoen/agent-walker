# claude-walker — Plan

## Done

- [x] Add macOS support to C++ and Zig impls. Apple
      Clang's libc++ lacks std::chrono::clock_cast; cpp
      uses a portable file_clock→system_clock offset trick.
      Zig adds a parallel Darwin code path via std.c
      (libSystem) alongside the existing Linux raw-syscall
      path; build.zig links libc only on macOS targets.
- [x] Close macOS gaps surfaced by merging origin/main
      (search subcommand + walker_roots port). Three new
      sites called raw `std.os.linux.*` syscalls on the
      `else` branch of `is_windows` checks (main.discover,
      search.discoverFiles, walker_roots.isExistingDir),
      which aborts silently on Darwin. Added Darwin branches
      using std.c opendir/readdir/fstatat, mirroring the
      existing discoverDarwin family. All four impls pass
      shared/conformance.py on macOS arm64 (128/128).

## Inbox

### Add per-request web-search cost ($0.01) to the pricing model

The Python reference (`~/schoen-claude-status/statusline_lib.py`,
`_cost_for_turn`) now charges **$0.01 per server-side web search request** on
top of token cost, read from `usage.server_tool_use.web_search_requests`
(billed at $10 / 1,000). The walker's `cost` / `events` / `beacons` modes still
price tokens only, so their `trailing_usd` / `window_usd` under-count any
search-heavy session (mostly background haiku agents) by 30-45%.

Validated against `~/.claude.json`'s authoritative per-model `costUSD`
(`projects.<path>.lastModelUsage`): token-only cost lands at 0.56-0.69×
authoritative on search rows, and adding $0.01/search closes every one to
**exactly 1.000**. So the rate is confirmed, not a guess.

**To implement (all four impls + spec + corpus):**

- `cost_for(...)` in `cpp/pricing.hpp` (and rust/go/zig equivalents): accept a
  `web_search_requests` count and add `count * 0.01`. Token formula unchanged.
- Parse `usage.server_tool_use.web_search_requests` (uint, default 0) in each
  per-turn extractor, alongside the existing `usage.*` token fields.
- `SPEC.md` §Pricing: document the per-request term + the new field name (the
  bullets there already flag this as pending and correct the now-false 1M-tier
  under-estimate note).
- Add a conformance corpus case with `web_search_requests > 0` so all impls are
  checked against the Python reference (`shared/conformance.py`).

### Rainy-day: roll our own JSON parser in each language

Even with every impl now on a hot-path parser (`simdjson`, `sonic`,
`serde_json` typed, `std.json` manual), the comparison still partly
reflects parser-library choice. A fair "language vs language" benchmark
would have each impl use a hand-rolled, purpose-built parser that knows
it only needs to extract five fields per line (`message.role`,
`message.id`, `message.model`, `message.usage.{...}`, `timestamp`).

A purpose-built scanner that searches for those five field names and
skips the rest of the line could be much faster than any general
parser, and would normalize the comparison: every language judged on
its primitives (string scanning, integer parse, conditional dispatch)
rather than on which JSON library it shipped with.

Lots of work for "fairness," and the practical answer (use the fastest
JSON lib in each language) is what production code would do anyway.
File this under "fun exercise for a rainy day."

### Promote `bench-interleaved.py` to a checked-in script + standard report

`.claude/scripts/bench-interleaved.py` is currently an untracked 89-line
one-off used during PR #8 (cpp perf-pass-2) for the 11-round interleaved
median measurements. The logic is generic and worth keeping, but per
CLAUDE.md `.claude/scripts/` is for delete-after-use one-offs.

**Promote to a real script:**

- Move to `shared/bench-interleaved.py` (or `shared/perf-report.py`) and
  commit. It already covers all four impls × `cost` / `beacons-history` /
  `search` modes with round-robin scheduling + outlier trim. `shared/bench.py`
  stays as the live-fleet quick-check; this is the disciplined perf-report
  generator.
- Define a standard output format — either a new `BENCH-RESULTS.md` (timestamped
  table per run) or append-style entries to `RESULTS.md`. Lean toward a separate
  file so `RESULTS.md` stays narrative.
- Document when to re-run: at minimum, before/after any perf-affecting PR;
  consider a CI invocation on `workflow_dispatch` (don't gate merges on it —
  cross-impl perf varies per runner).
- The script currently prints walker `elapsed_ms` alongside wall-clock —
  preserve that, it's the per-process-startup-overhead vs in-binary-work
  signal that diagnosed PR #8.
