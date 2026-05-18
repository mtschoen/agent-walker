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

## Inbox

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

### Close the Zig perf gap

- [ ] Replace `std.json.parseFromSlice(std.json.Value, ...)` with
      `std.json.Scanner` zero-copy token streaming on all three Zig
      hot paths (`main.zig` cost, `beacons.zig` history+latest,
      `search.zig` scan). Reuse one arena per file (reset between
      lines via `_ = arena.reset(.retain_capacity)`), not per line.
- [ ] Add a `min(8, ncpu)` worker pool to `beacons.zig::runHistory`
      and `search.zig::run` matching the existing cost-mode pool in
      `main.zig`. One group per work unit; merge results after.
- [ ] Re-run `shared/bench.py --mode cost` + `--mode beacons-history`
      plus the search ad-hoc bench. Target: Zig within 2x of C++ in
      every mode. Update `RESULTS.md` after.

**Context.** Live-fleet bench medians after the Zig search port
(2026-05-18):

| Mode                       | rust   | cpp        | go     | zig    |
| -------------------------- | ------ | ---------- | ------ | ------ |
| cost (8 workers)           | 119ms  | **75ms**   | 115ms  | 313ms  |
| beacons-history (1 thread) | 976ms  | **690ms**  | 1172ms | 13.8s  |
| search `TODO --count-only` | **1139ms** | 2411ms | 1335ms | 11.75s |

Zig is 4x slower than cpp in cost mode and 20x slower in
beacons-history. The gap is *not* a parallelism gap — rust/cpp/go
beacons + search are all single-threaded too. It's per-line JSON parse
cost: rust uses typed `serde_json`, cpp uses `simdjson` on-demand, go
uses `bytedance/sonic`. Zig uses `std.json.parseFromSlice(Value, ...)`
which allocates an `ObjectMap` (StringArrayHashMap) per object plus
dupes every string — the slowest architecture of the four. The Broch
Web "Optimizing a JSON parser in Zig" piece reports ~86x speedup just
from switching to an arena allocator; switching off `Value` entirely
should compound that.

**Diagnosis (per Zig hot path).**

- `main.zig::processLine` — `parseFromSlice(Value, alloc, line, ...)`
  + `defer parsed.deinit()` per line, even though the per-group arena
  is already pooling. Touches `message.role`, `message.id`,
  `timestamp`, `message.model`, `message.usage.{input,output,cache_read,cache_write}_tokens`
  — five string keys, four numbers. Five-field Scanner walk is a
  drop-in replacement.
- `beacons.zig::classifyEntry` — same `parseFromSlice(Value, ...)`
  then walks `message.content` to extract text blocks AND dupes
  `kind` / `summary` / `drift` strings per Beacon. Per-beacon string
  dup is unnecessary when the beacon is consumed immediately by
  `writeJson`.
- `search.zig::scanFile` (this PR) — same pattern. Worse: calls
  `extractText` twice per line (once for default, once for
  with-tools) PLUS `isOnlyToolBlocks` — three passes over the content
  array. Defer redundant passes.

**Fix path, cheapest → most invasive.**

1. **Scanner streaming.** Replace `parseFromSlice(Value)` with
   `std.json.Scanner.initCompleteInput(alloc, line)` and dispatch on
   token type. Token strings reference the input slice (zero-copy)
   when allocation is `.alloc_if_needed` and the input is held in
   memory — which it is, since we read the whole file into a `[]u8`.
   Expected: 3-10x speedup on the parse step alone, no library
   dependency added.
2. **Arena hygiene.** Hoist the `ArenaAllocator` out of per-line
   scope; reset between lines instead of init/deinit per line.
   Already done in `main.zig` (per-group arena via `Wctx`) but NOT
   in `beacons.zig::scanEntry` (uses `parsed.deinit()` per line) or
   `search.zig::scanFile` (single big arena, but `parsed.deinit` per
   line). Reuse the arena, drop the per-line deinit.
3. **Worker pool for beacons + search.** Lift `main.zig`'s `Queue`
   + `Accum` + `doWork` pattern into a generic helper, fan groups
   out across `min(8, ncpu)` workers. Group-level work is already
   independent (per-group dedup + per-group beacon collection).
   Expected: roughly Nx where N is core count, on top of the parse
   speedup.
4. **One-pass content scan in search.** Combine `extractText(default)`
   + `extractText(with_tools)` + `isOnlyToolBlocks` into a single
   walk over `message.content`. Currently three iterations; one is
   enough. Smaller win (~10-20%) but easy.
5. **Last resort: zimdjson.** If steps 1-4 don't close the gap, the
   simdjson-port [zimdjson](https://github.com/EzequielRamis/zimdjson)
   (active, Zig 0.14+) is the heavyweight fallback. Adds a build
   dependency and pulls Zig out of "zero deps" territory — only do
   this if the stdlib `Scanner` approach plateaus above the 2x-of-cpp
   target. Worth a microbench-of-one-fixture first so we know whether
   the dependency is buying real perf or marginal noise.

**Risks / non-obvious.**

- `std.json.Scanner` with `.alloc_if_needed` only avoids dup when a
  token doesn't span a buffer boundary. With `initCompleteInput`
  (whole line in memory) this should always be the case, but
  field-name comparisons need to handle the slice-into-input lifetime
  correctly — extract by reference, copy out only the fields kept
  past the line (`timestamp_str`, `role`, the joined text). Test the
  bare-string user-content path explicitly (the Rust port's
  string-vs-array fallback) since Scanner doesn't auto-coerce.
- Worker-pool addition for `beacons-history` interacts with the
  global event-sort step (each group sorts its own events;
  cross-group ordering doesn't matter for `bias_factor` calc).
  Verify with a conformance rerun after the change — the harness's
  order-independent `pairs_key` sort guards against ordering churn.
- The cost-mode arena is reset *per group*, not per file. Beacons +
  search currently allocate per *call*. Don't blindly copy cost's
  pattern — search's per-file scan accumulates `ScanMessage`s that
  are consumed in `processFile` and then becomes garbage; per-file
  arena reset is the right granularity there.

**Verification.**

- `python shared/conformance.py rust cpp go zig` must stay clean
  after every step (cost + beacons-latest + beacons-history + 17
  search combos). Don't merge a perf change that breaks conformance
  even by one fixture.
- `python shared/bench.py --mode cost rust cpp go zig --no-python`
  + `--mode beacons-history` re-run with 5-run medians.
- Re-create the search ad-hoc bench (was a one-off, deleted at PR
  close) — small script under `.claude/scripts/` is fine; don't
  promote to `shared/bench.py` yet.
- Update `RESULTS.md` with the new numbers and a language-level note
  (e.g. "std.json materializes a Value tree; Scanner streaming closed
  N% of the gap before any threading change").

**Out of scope here.** Don't rewrite C++/Rust/Go — they're at the
ceiling of their respective JSON libraries. Don't switch Zig away
from "zero deps + stdlib only" unless step 5 is taken. Don't touch
the regex matcher in `search.zig` — its allocator hot path is
negligible vs JSON parsing (the disproportionate beacons-history gap,
which uses no regex, proves where the cost is).
