# claude-walker — Plan

## Done

- [x] Conformance corpus + harness (`shared/corpus/`, `shared/conformance.py`)
- [x] Live-fleet bench (`shared/bench.py`)
- [x] Rust impl (`rust/`)
- [x] Go impl (`go/`) — stdlib `encoding/json`, then upgraded to
      `bytedance/sonic` v1.15.1 (373ms → 141ms)
- [x] C++ impl (`cpp/`) — `nlohmann/json`, then upgraded to `simdjson`
      v4.6.4 on-demand (402ms → 88ms; now the fastest implementation)
- [x] Zig impl (`zig/`)
- [x] `RESULTS.md` comparison table (rerun after C++/Go upgrades)
- [x] Port idle-exclusion in `bias_factor` from Rust to C++/Go/Zig.
      All four impls now share `active_elapsed / begin_eta` median for
      `bias_factor` and expose `idle_excluded` / `active_elapsed` in
      pair JSON. Live 14d corpus produces identical `bias=1.7645675...`
      across rust/cpp/zig and a 4e-10 delta from go (JSON-roundtrip
      noise, well inside the 0.001 tolerance). Conformance harness
      gained per-impl scoping for `--no-config` / `--extra-projects-root`
      so rust/go/zig stop erroring on cpp-only flags.
- [x] Close the Zig perf gap (Scanner streaming + worker pool). All
      three hot paths ported from `parseFromSlice(Value, ...)` to
      `std.json.Scanner` token streaming; beacons-history + search now
      have a `min(8, ncpu)` worker pool matching cost mode's. Net:
      cost 313ms→148ms (2.1×), beacons-history 13.8s→769ms (18×),
      search 11.75s→573ms (20×). Zig is now fastest in search (4.16×
      cpp), second-fastest in beacons-history (1.12× cpp), within 2×
      of cpp on cost. PR #6 on gitea.
- [x] Parallelize search + beacons-history in cpp/rust/go. All three
      impls now have `min(8, ncpu)` worker pools in all three hot modes;
      zig already did from the prior pass. Per-impl ratios:
      cpp search 2353→375ms (6.3×) / beacons-history 1117→285ms (3.9×);
      rust search 1096→220ms (5.0×) / beacons-history 965→242ms (4.0×);
      go search 1383→410ms (3.4×) / beacons-history 1250→631ms (2.0×).
      Surprise: rust pulls ahead of cpp in every mode after the pass
      (typed serde_json + rayon beat simdjson on-demand + std::thread
      at 8× concurrency). Landed as commits 98dd5d6 / 69bd1c2 / 87c4e24,
      merged as f971156 + 730518a. RESULTS.md updated.

## Inbox

### Investigate cpp cost-mode per-session regression vs rust — before promoting rust to primary

After the parallelize-cpp/rust/go pass landed, the interleaved 11-round
bench (RESULTS.md "Headline numbers") showed rust ahead in every mode.
For `search` and `beacons-history` that's explainable — the pass closed
the parallelism gap and rust's typed `serde_json::Deserialize` has
lower per-line parse cost than cpp's `simdjson::ondemand::iterate(padded_string)`.

But `cost` mode is the worrying one: **we did not touch cost-mode code
in this pass at all** (`git diff 50d0a93..dcbe91e -- cpp/main.cpp
rust/src/main.rs go/main.go zig/src/main.zig` returns empty). Yet:

| Impl  | Old fleet (130 grps) | New fleet (900 grps) | per-group, old → new |
| ----- | -------------------: | -------------------: | -------------------: |
| cpp   |  81ms                | 333ms                | 0.62 → 0.37 ms/grp   |
| rust  | 106ms                | 206ms                | 0.82 → 0.23 ms/grp   |
| go    | 110ms                | 306ms                | 0.85 → 0.34 ms/grp   |

Both got faster per-session (warm-cache benefit from a bigger fleet
hitting OS file cache, probably), but **rust got 3.6× faster per
session while cpp got only 1.7× faster**. With the parser code
unchanged on either side, that asymmetry has to be a real scaling
difference in how the two parse the per-line work — and it's the lever
we'd want to pull before adopting rust as the install target.

**Before promoting rust to install.bat / install.sh, take a stab at cpp.**
Some hypotheses to chase, in rough order of suspicion:

1. **Per-line `padded_string` allocation cost.** `cpp/main.cpp::walk_group`
   builds a `simdjson::padded_string(line)` for every JSONL line. That's
   one heap allocation + memcpy per line — at 900 groups × ~100 lines/group
   ≈ 90K allocations per cost run. Compare against rust's
   `serde_json::from_slice(&line_bytes)` which uses the input bytes
   in-place. Profile with `valgrind --tool=cachegrind` or just
   instrument with `std::chrono` around the allocation to confirm. If
   this is the bottleneck, the fix is to reuse a single `padded_string`
   buffer per thread, resizing in place via
   `parser.allocate(buffer_capacity)` once and writing into it. Or
   look into `parser.iterate_padded(line)` if it exists in v4.6.4
   — some versions accept a non-owning buffer with manual padding
   provided.

2. **`std::string` model construction.** `walk_group` calls
   `model.assign(model_view.data(), model_view.size())` for every
   assistant line. The lifetime of the underlying `string_view` is
   the simdjson parser's internal buffer, which gets reused on the
   next `iterate()`. Hence the assign-into-std::string. But this is
   another heap alloc per assistant line. Could be replaced by
   matching the model substring against a small string table inline
   (sonnet/opus/haiku — three string prefixes are all we care about),
   avoiding the `std::string` entirely.

3. **`std::transform(::tolower)` in `rates_for`.** Called per assistant
   line; allocates+copies the model string just to lowercase it. ASCII
   prefix-match on the original would skip the copy. Trivial fix.

4. **`std::unordered_set<std::string> seen_ids` per group.** Each
   `insert(std::move(mid))` heap-allocates the string. With 4-byte SSO
   the typical Anthropic message-id is too long for inline storage.
   Consider `absl::flat_hash_set<uint64_t>` keyed on a FNV-1a or
   wyhash of the id bytes — collisions would be statistically zero at
   this corpus size.

5. **simdjson per-thread parser construction overhead.** Each thread
   gets a fresh `simdjson::ondemand::parser`. That parser allocates
   internal buffers on first `iterate()`. With 900 groups split across
   8 threads, that's ~112 group-files per thread sequentially — first
   iterate per group on a thread is potentially expensive. Pre-warm
   the parser with a dummy iterate at thread start, or reuse the same
   parser across all groups assigned to a thread (already the case
   — verify).

6. **discovery vs walk split.** What fraction of the 333ms is
   `discover_groups` (filesystem stat-walk) vs `walk_group` (parsing)?
   Instrument both and compare to rust's. If discovery is the
   bottleneck on Windows specifically (mtime filter walking 1500 files
   with a stat each), the fix is independent of the parser hypothesis
   above.

**Methodology.**

- Use `.claude/scripts/bench-interleaved.py` (created this session
  but gitignored under .claude/; recreate if missing) — it interleaves
  rounds across all four impls so noise is balanced. 11 rounds, drop
  top+bottom, report median.
- Profile with the OS tool of choice: Windows `Windows Performance
  Recorder` + `Windows Performance Analyzer` (free, captures call
  stacks), or just sprinkle `std::chrono::steady_clock` around the
  suspected hot regions and emit timings to stderr. Don't reach for
  `perf` — this is a Windows-primary investigation.
- Each hypothesis should be measured *in isolation* (one change at a
  time) so attribution is clean. If hypothesis 1 closes 70% of the
  gap and hypothesis 2 closes 5% more, do the cheap ones first.
- Once cpp matches or beats rust per-session, re-run the interleaved
  bench and update RESULTS.md's headline table.

**Decision gate.** This investigation is a prerequisite to switching
install.bat / install.sh to deploy rust. If cpp closes the gap, the
install target stays as is — cpp's binary is the smallest (267 KB vs
rust 423 KB), warmup cost is lower, and there's institutional
familiarity with the simdjson-on-demand code path. If after the
optimizations cpp still loses by ≥10% per-session, then promote rust.

**Out of scope.**

- Don't touch `search` or `beacons-history` in cpp — those just landed
  and are at acceptable parallel-mode floors. The asymmetry of
  interest is in `cost`.
- Don't refactor away from `simdjson::ondemand`. The on-demand API is
  the right shape for forward-only field extraction; the suspected
  bottlenecks are allocations *around* the parser, not the parser
  itself.
- Don't bench against the per-machine production fleet to claim
  speedups — use the conformance corpus or generate a synthetic
  fleet of known size so the absolute numbers are reproducible.

**Pointer for the next agent.** Read `cpp/main.cpp::walk_group`
(roughly lines 149-277) end-to-end; that's the per-line hot loop.
Compare against `rust/src/main.rs`'s walk function for shape. The
rust impl is roughly 2× as compact for the same work — that's where
the per-allocation accounting lives.

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

### Standing item

- [ ] Wire optional native-walker detection into
      [schoen-claude-status](https://github.com/mtschoen/schoen-claude-status)'s
      `_walk_pace_buckets`: if `~/.claude/walker` exists and is
      executable, subprocess it; on any failure fall back to the
      existing Python parallel walker. Stays optional — no install
      friction added to schoen-claude-status. **Winner is C++** (88ms
      median); package as `~/.claude/walker.exe` from
      `cpp/build/Release/walker.exe`.

### Port walker-roots / cross-machine to non-cpp impls

- [ ] Add `walker-roots.json` reading + `--extra-projects-root` (repeatable)
      + `--no-config` flag to **rust**, **go**, and **zig**, so all four
      impls support cross-machine root resolution and conformance can drop
      the per-impl scoping (`IMPLS_WITH_NO_CONFIG` / `IMPLS_WITH_EXTRA_ROOTS`
      in `shared/conformance.py`) and the cpp-only `multi_root` scenarios
      become a full cross-impl conformance bar.

**Context.** When the search subcommand landed (this branch), cpp was the
only impl that read `~/.claude/walker-roots.json`. The decision then was:
cpp is the production binary (`install.bat` deploys it to
`~/.claude/walker.exe`, which the MCP shim subprocesses), so cross-machine
search worked end-to-end through cpp without rust/go/zig needing parity.
Rust stayed the reference impl with single-root only. This entry exists
because that decision left a capability gap in the reference impl that
should be closed for symmetry.

**Reference.** Port `cpp/walker_roots.hpp` (~125 lines):

- `walker_config_path()` → `$HOME/.claude/walker-roots.json` (Linux/macOS)
  or `$USERPROFILE/.claude/walker-roots.json` (Windows).
- `read_extra_roots_from_config()` → parse `{"extra_roots": [...]}`,
  malformed JSON / missing key / wrong type all degrade silently with a
  stderr diagnostic (must NOT error). Single-key `extra_roots`; missing
  file is a quiet zero-extras case.
- `resolve_roots(primary, cli_extras, read_config)` → primary + CLI extras
  + (config extras unless `--no-config`), deduped via canonical path,
  filtered to existing directories. Primary is allowed to not exist
  (empty-fleet case); other roots get a "skipping" stderr line if missing.

**Surface to extend.** Both `cost` mode and `search` consume the resolved
root list. Cost-mode discovery glob `<root>/*/<sid>.jsonl` becomes a fan-out
across roots. Search's `host_root` field in JSONL output (per the search
spec) needs to be set to the root the hit's file was discovered under —
already trivial in cpp, needs the same plumbing in each port.

**Harness cleanup after.** Once all four impls support the flags, in
`shared/conformance.py`:

- Drop `IMPLS_WITH_NO_CONFIG` and `IMPLS_WITH_EXTRA_ROOTS` allow-lists;
  always pass `--no-config` (so test runs don't inherit the user's local
  `walker-roots.json`) and always pass `--extra-projects-root` for the
  `multi_root` scenarios.
- The `multi_root` corpus directories become a real cross-impl test bar
  instead of a cpp-only check.

**Out of scope here.** Don't rev the search spec or rebuild the search
corpus — search's conformance fixtures use single-root tempdirs and stay
valid. The cross-machine smoke test in the search spec's `## Verification`
section targets the cpp production binary; once this port lands, it can
optionally target rust/go/zig too.

### Parallelize search + beacons-history in C++ / Rust / Go — DONE

This entire section is closed; see the matching `[x]` entry at the
top of the file for the result summary. Kept here for archival
context (the design notes were accurate; only the prediction about
cpp reclaiming the lead was wrong — rust took it instead).

- [x] Worker pool added to `cpp/search.cpp::run` and
      `cpp/beacons.cpp::runHistory`. Atomic-index pattern mirroring
      `main.cpp::run_cost`. Per-thread simdjson `parser` (constructed
      locally per `scanFile()` call), shared compiled regex (const ops
      are thread-safe).
- [x] `rayon::par_iter().reduce()` added to `rust/src/search.rs` and
      `rust/src/beacons.rs::run_history`. Inside `pool.install()`,
      mirroring `main.rs::run_cost`.
- [x] Goroutine fan-out added to `go/search.go::Run` and
      `go/beacons.go::RunHistory`. `sync.WaitGroup` + per-worker
      accumulator indexed by `tid`, merged after `wg.Wait()`. Mirrors
      `main.go::runCost`.
- [x] Re-benched all four impls; `RESULTS.md` updated.

**Context.** After PR #6 (Zig perf-gap close), zig is fastest in
search by a wide margin because it's the only impl with a worker pool
in that mode. The other three impls are single-threaded in search and
beacons-history; only cost mode is parallel. Current state:

| Mode                       | rust    | cpp       | go      | zig          |
| -------------------------- | ------: | --------: | ------: | -----------: |
| cost (8 workers)           | 106ms   | **81ms**  | 110ms   | 148ms        |
| beacons-history            | 1004ms  | **685ms** | 1196ms  | 769ms (8w)   |
| search `TODO --count-only` | 1171ms  | 2385ms    | 1322ms  | **573ms** (8w) |

Cpp at 2385ms in search is roughly 4× slower than zig's 573ms — that
gap is entirely parallelism. Per-file simdjson parse cost is already
near optimal; throwing 8 cores at it should drop cpp to ~300ms (best-
case linear scaling, more realistically ~400-450ms accounting for
discovery overhead). Same logic applies to rust and go's
beacons-history + search.

**Reference.** Existing cost-mode parallelism patterns:

- `cpp/main.cpp` — `std::thread` pool, `std::atomic<size_t>` for
  queue index, `std::mutex` on the accumulator. Each thread holds its
  own `simdjson::ondemand::parser`.
- `rust/src/main.rs` — `rayon::par_iter().reduce(|| Acc::default(),
  ...)`. One line if the work unit is independent.
- `go/main.go` — goroutines + `sync.WaitGroup`. Each goroutine has a
  local accumulator; merge after `wg.Wait()`.
- `zig/src/beacons.zig::runHistory` (this PR) and
  `zig/src/search.zig::run` (this PR) — for cross-language reference
  on per-worker arena + atomic-index queue + result merge.

**Fix path, cheapest → most invasive.**

1. **cpp search.** Most impactful (biggest current gap). Take
   `cpp/main.cpp`'s thread pool, extract to a small shared helper,
   reuse in `search.cpp::run`. Each thread needs its own
   `simdjson::ondemand::parser` (the parser holds intermediate state
   in `string_buf` that is not thread-safe). Local hits list per
   thread, merge before sort.
2. **cpp beacons-history.** Same pattern, work unit = session group
   from `discover()`. Pairs are pure f64 — no string-lifetime issues
   on merge.
3. **rust search + beacons-history.** Add `rayon::par_iter` over the
   file list (search) / group list (beacons-history). Hit/pair types
   already own their fields; merge is a flat-map.
4. **go search + beacons-history.** Goroutine fan-out + buffered
   channel + waitgroup. Sonic's `Unmarshal` is thread-safe per
   docs/source — no per-goroutine parser needed.

**Risks / non-obvious.**

- **simdjson parser is NOT thread-safe** even for read-only use:
  `parser` owns mutable internal buffers reused across `iterate`
  calls. Per-thread parser is mandatory; this is the only real
  porting surface for cpp.
- **`std::regex` is the second bottleneck in cpp search,** not the
  parser. The 2385ms median splits roughly 60% simdjson + 40%
  std::regex (estimate; verify with a profiler before assuming).
  Parallelization closes the parse half; a separate follow-up could
  swap `std::regex` for `re2` or a custom matcher. **Decide whether
  to fold that in or punt it to a separate PR after measuring.**
- **rust's serde_json is already typed-Deserialize**, so per-line
  parse cost is lower than cpp's simdjson, but rust is still
  single-threaded in search. The lower per-line cost means rayon's
  win may be smaller in absolute ms; benchmark before claiming a
  target speedup.
- **go's discovery is a single goroutine** walking the filesystem
  via `filepath.Walk`. With parallel scanning, the discovery becomes
  the new bottleneck — measure whether it's worth parallelizing the
  walk too (probably not; disk seek is sequential anyway).
- **Hit ordering must be deterministic** across runs. After the
  fan-out, sort hits using the existing `hitLessThan` (or per-lang
  equivalent) before truncation to `--limit`. Conformance fixtures
  pin output order via the harness's order-independent diff, but
  human users expect "most recent first" stability.

**Verification.**

- `python shared/conformance.py rust cpp go zig` clean across all 4
  impls after each parallelization step.
- `python shared/bench.py --mode cost rust cpp go zig --no-python` +
  `--mode beacons-history`: re-run with 5-run medians.
- `python .claude/scripts/bench-search.py` (kept from PR #6): 5-run
  search bench against the live fleet.
- Update `RESULTS.md`:
  - Refresh the cross-mode table at the top
  - Add a "What changed in the parallelize-cpp/rust/go pass" section
    mirroring the "Zig perf-gap pass" section's structure
- Spot-check `--limit` ordering by comparing pre/post-PR hit
  sequences for `walker search TODO --limit 5`: top-N should be
  identical.

**Out of scope here.** Don't replace `std::regex` with re2 unless
profiling proves it's blocking the parallelization win. Don't touch
zig — it's already parallel. Don't refactor discovery into a thread
pool (disk-bound, not CPU-bound).

**Pointer for the next agent.** Read the worker-pool pattern in
`zig/src/beacons.zig::runHistory` (just-shipped in PR #6, lines
~536-590) and `zig/src/search.zig::run` (lines ~1130-1160) as the
cross-language reference. The shape is identical across all four
languages: per-worker arena/state, atomic queue index, local results
list, merge-then-sort after join.
