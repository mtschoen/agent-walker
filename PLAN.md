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

### Finish beacon-pairing-fix rollout (walker DONE on branch `fix/beacon-pairing`, unmerged)

The walker beacons-history pairing fix is **implemented and conformant** on local
branch `fix/beacon-pairing` (5 commits off e46d8ac), NOT merged or installed. The old
"earliest begin + latest end per session" rule is replaced by timestamp-ordered
iteration with a single in-flight `pending_begin` (one pair per closed begin->end
lifecycle); the beacon parser no longer requires `drift` (accepted + passed through;
omitted from beacons-latest output when absent). All four impls (rust/cpp/go/zig) pass
`python shared/conformance.py rust cpp go zig` (0 fail) with new fixtures
(multi_lifecycle / orphan_begin / orphan_end / back_to_back / optional_drift;
missing_fields repurposed to missing-eta; malformed hardened to an unquoted bareword).
Live-fleet validation: `bias_factor` 3.82 -> 1.70, n_pairs 23 -> 44.

Spec/plan: `docs/superpowers/specs|plans/beacon-pairing-fix.md`. Memory:
`project_beacon_pairing_fix.md`, `reference_json_parser_leniency_conformance.md`.

Remaining (in order):

- [x] **Merge `fix/beacon-pairing` -> main.** Rebased onto `5bb4cd7` (clean, only
  PLAN.md drift); merged `--no-ff` -> `d416ac9`; conformance green on merged tree
  (196 OK / 0 fail, all four impls); pushed to BOTH remotes (origin + gitea at
  `d416ac9`). 2026-05-27.
- [x] **Rebuild + reinstall** the cpp binary via `install.bat` -- production
  `~/.local/bin/claude-walker.exe` now has the new pairing (rebuilt 04:15, smoke ok;
  verified `multi_lifecycle --no-config` -> n_pairs=2 bias=0.8333). A stale
  `cpp/build/Release/walker.exe` process had locked the linker output (LNK1104); killed
  it and the build went through.
- [ ] **macOS conformance** (different machine; no macOS CI runner): after pulling the
  merged main, `python shared/conformance.py rust cpp go zig` + the zig-Darwin grep
  guard (CLAUDE.md macOS section). STILL PENDING -- can't run from chonkers/Windows.
- [ ] **Then: web-search cost fix** (the "Add per-request web-search cost" Inbox
  section below) -- user chose "both, beacons first" this session, so it is the next
  bug after this rollout lands.
- [ ] **Downstream repos (land AFTER walker reinstall):** schoen-claude-status
  `feat/objective-drift` (statusline objective-drift changes already in working tree,
  uncommitted -- commit them); skills-dev/progress-beacon `feat/drop-drift-field` (drop
  `drift` from SKILL.md required fields + examples; objective-math note trigger).
- [x] Flip `## Test plan summary` checkboxes + prune completed phases in
  `docs/superpowers/plans/beacon-pairing-fix.md` (walker phase marked DONE; bias
  criterion corrected to the 1.70 fleet reality).

Note: spec predicted bias ~0.5 (58-session snapshot); current 1943-session fleet lands
at 1.70 -- both sane, do not chase 0.5 in the statusline work.
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

### Finish root-resolution fix rollout — DONE 2026-05-27 (macOS check pending)

Three root-discovery bugs fixed in-tree on `main` (commit ba22515):

1. rust silently dropped mapped network-drive roots (the `fs::canonicalize`
   verbatim/UNC trap broke the `glob` discovery). Fixed by walking the original
   path and using canonical only as the dedup key.
2. HOME-vs-USERPROFILE precedence was Windows-wrong (rust+cpp HOME-first for
   both default root and config; go internally inconsistent). Unified on a
   platform-gated helper: Windows -> USERPROFILE then HOME, else HOME then
   USERPROFILE, in all four impls + SPEC.md.
3. conformance never exercised config/home resolution (every runner forced
   `--no-config`). Added `check_config_resolution`.

See `~/.claude/notes/idioms_windows_home_and_canonicalize.md` for the reusable
gotchas.

Rollout status:

- [x] **Pushed** to `origin` (GitHub) AND `gitea`. Both remotes had advanced to
  `3722732` (a parallel session added a beacons-history plan commit + MIT
  LICENSE), so ba22515+edde4c7 were rebased onto it (clean PLAN.md auto-merge) →
  both remotes now at `24688c3`.
- [x] **CI green** on run 8032: Build + Conformance (Linux) AND (Windows) both
  `success`. The new config-resolution test passes on both — Bug B's
  windows-latest-only failure does not reproduce.
- [x] **Bug A re-verified** against the live Y: drive: rust and cpp both walk
  855 files / 318 groups and agree to the cent ($3767.80) via
  `walker --projects-root Y:/.claude/projects --no-config`. (No CI guard — no
  mapped drive on runners; re-verify after any `walker_roots`/discovery change.)
- [x] **RESULTS.md re-benched** (fair `--no-config`-for-all): headline cost,
  cross-mode, and ranges tables regenerated 2026-05-27. zig/go dropped relative
  to cpp in cost mode (zig 5.2×→2.48×) as expected; absolute numbers rose
  because the post-filter fleet grew (~300→~2760 files). Python now edges out
  zig on the larger fleet. Historical "What changed" sections left intact.
  (RESULTS.md + this PLAN update uncommitted pending push decision.)
- [ ] **macOS conformance — PENDING (different machine).** Can't run from
  chonkers. On the Mac, after pulling `24688c3`: `python shared/conformance.py
  rust cpp go zig`, plus grep new zig files for `linux\.openat|linux\.statx|
  linux\.getdents|main\.platform\.linux` in `is_windows` else-branches (per
  CLAUDE.md macOS section).
