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

- [ ] Implement the beacons-history pairing fix (specced + planned 2026-05-18, walker portion confirmed UNimplemented 2026-05-27). Replace "earliest begin + latest end per session" pairing with a single in-flight `pending_begin` (consecutive begin→end pairs, orphans dropped), and drop `drift` from the required-field set (keep accepting it for back-compat). Currently `bias_factor` is directionally wrong (~3.45 vs measured ~0.5), so status-line calibrated ETAs come out 5–10× too high. Touches SPEC.md + all four impls (cpp/rust/go/zig) + conformance corpus (add multi_lifecycle / orphan_begin / orphan_end / back_to_back fixtures). Spec: docs/superpowers/specs/beacon-pairing-fix.md; plan: docs/superpowers/plans/beacon-pairing-fix.md; memory: project_beacon_pairing_fix.md.
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

### Interleaved perf report -- standard output format (partly done)

DONE (commit ba22515): the round-robin interleaving + walker `elapsed_ms`
column from the old `.claude/scripts/bench-interleaved.py` are merged into
`shared/bench.py` as a `--interleave` flag (with an untimed warm-up); the
throwaway script is deleted. `bench.py` now also passes `--no-config` to ALL
impls so the comparison is apples-to-apples (was cpp-only, which compared
unequal work and made go/zig look slow when they were just walking the SMB
drive cpp skipped).

Still open:

- Define a standard output format for perf runs: a timestamped `BENCH-RESULTS.md`
  table (keep `RESULTS.md` narrative). `bench.py` is the live-fleet quick-check,
  not a disciplined report generator.
- Document when to re-run: before/after any perf-affecting PR; optionally a CI
  `workflow_dispatch` invocation (do not gate merges on it; cross-impl perf
  varies per runner).

### Finish root-resolution fix rollout (commit ba22515, local/unpushed)

Three root-discovery bugs fixed in-tree on `main` this session:

1. rust silently dropped mapped network-drive roots (the `fs::canonicalize`
   verbatim/UNC trap broke the `glob` discovery). Fixed by walking the original
   path and using canonical only as the dedup key.
2. HOME-vs-USERPROFILE precedence was Windows-wrong (rust+cpp HOME-first for
   both default root and config; go internally inconsistent). Unified on a
   platform-gated helper: Windows -> USERPROFILE then HOME, else HOME then
   USERPROFILE, in all four impls + SPEC.md.
3. conformance never exercised config/home resolution (every runner forced
   `--no-config`). Added `check_config_resolution`.

All four pass `python shared/conformance.py rust cpp go zig` locally on Windows.
See `~/.claude/notes/idioms_windows_home_and_canonicalize.md` for the reusable
gotchas. Remaining:

- **Push** ba22515 (+ the wrap-hygiene commit) to `origin` (GitHub) AND `gitea`.
  Remotes are SWAPPED vs skills-dev: `git remote -v` first.
- **Watch CI** (ubuntu + windows runners): the new config-resolution test is the
  cross-platform guard for the precedence fix; Bug B's Windows failure only
  reproduces on the windows-latest runner.
- **macOS:** after pulling, run `python shared/conformance.py rust cpp go zig`
  (no macOS CI runner; per CLAUDE.md macOS section).
- **Bug A (mapped drive) has NO CI guard** (no mapped network drive on runners).
  Its only regression check is local/live: `python shared/bench.py` against the
  live fleet, or `walker --projects-root Y:\.claude\projects --no-config` must
  walk >0 files in rust. Re-verify after any `walker_roots`/discovery change.
- **Check `RESULTS.md`** for now-stale bench numbers (predates the fair
  `--no-config`-for-all fix; go/zig were never actually 3-12x slower).
