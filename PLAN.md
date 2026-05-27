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
- [x] **Beacon-pairing-fix rollout — DONE 2026-05-27** (merged `d416ac9`).
      `beacons-history` now pairs via a single in-flight `pending_begin` (one
      pair per closed begin→end lifecycle, orphans dropped); the beacon parser
      dropped the `drift` requirement (accepted + passed through, omitted from
      beacons-latest when absent). All four impls conformant; cpp reinstalled.
      Live fleet: bias_factor 3.82→1.70, n_pairs 23→44. Downstream landed too —
      schoen-claude-status objective-drift (already done by a parallel session,
      `14c9a56`) + progress-beacon SKILL.md drift removal (submodule `0337307`).
      Full record: `~/.claude/notes/project_beacon_pairing_fix.md`.
- [x] **Per-request web-search cost ($0.01) — DONE 2026-05-27** (commit `178da9d`).
      Flat $0.01/server-side-web-search added to the one shared pricing formula in
      all four impls, parsing nested `usage.server_tool_use.web_search_requests`;
      SPEC §Pricing updated; cost fixture `08-web-search` + events fixture
      `07-web-search` (the latter covers cpp/zig's separate event-path parsers).
      Conformance 204 OK / 0 fail; cpp reinstalled (aggregate $0.27925 incl. the
      $0.08 web-search). Matches the Python `_cost_for_turn` reference, verified to
      the cent vs `~/.claude.json` costUSD. Also fixed a `generate_corpus.py`
      footgun: it wiped the whole `shared/corpus/` tree (killing
      beacons/events/search/multi_root) — now scoped to its own cost-slug dirs,
      mirroring the sibling generators.
- [x] **Root-resolution fix rollout — DONE 2026-05-27** (commit ba22515, pushed
      `24688c3`, CI green run 8032). Three bugs: rust dropped mapped network-drive
      roots (the `fs::canonicalize` verbatim/UNC trap); HOME-vs-USERPROFILE
      precedence was Windows-wrong (unified on a platform-gated helper — Windows
      USERPROFILE-first, else HOME-first); conformance never exercised config/home
      resolution (added `check_config_resolution`). Bug A re-verified on the live
      Y: drive (855 files / 318 groups, $3767.80 to the cent). RESULTS.md re-benched
      fair `--no-config`-for-all. Reusable gotchas:
      `~/.claude/notes/idioms_windows_home_and_canonicalize.md`.

## Inbox

### macOS conformance — PENDING (cross-machine; covers all three 2026-05-27 rollouts)

Can't run from chonkers/Windows. On the Mac, after pulling latest `main`:
`python shared/conformance.py rust cpp go zig`, plus grep new/changed zig files
for `linux\.openat|linux\.statx|linux\.getdents|main\.platform\.linux` sitting in
the `else` branch of `is_windows` checks (per the CLAUDE.md macOS section). One
run covers the beacon-pairing fix, the web-search cost change, AND the
root-resolution fix — they all share this single check. The web-search zig edits
add no syscalls (pure JSON parsing), so no new Darwin-syscall risk is expected.

### Follow-up: should the status line call the walker for cost?

Raised during the web-search work. The cost formula is now 5-way duplicated
(Python `_cost_for_turn` + the four walker impls), kept in lockstep via the
conformance corpus. Tempting to DRY it by having the status line shell out to the
walker — BUT the existing boundary holds for a reason: the status line keeps its
per-render / per-session cost (and the beacon-anchor wall-clock scan) in Python
because subprocess start-up beats the actual work at small N; only the amortized
fleet/calibration workloads go through the walker subprocess. Don't reopen without
a fresh measurement on the *status-line* workload (not the bench harness, which
runs the binary once over a large fleet — different regime). See
`~/.claude/notes/project_claude_walker_status_line_integration.md`.

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
