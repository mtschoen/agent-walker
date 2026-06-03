# Performance results

Cross-implementation benchmark of the four walkers (Rust / C++ / Go / Zig)
plus the Python statusline fleet-walk, across all five modes.

> **Correction (2026-06-02):** every Zig number in the "baseline" section
> below was unknowingly a **Debug** build. `zig build` defaulted to Debug
> (`standardOptimizeOption`), and neither `bench.py` nor `conformance.py`
> builds Zig - they just run whatever sits at `zig/zig-out/bin/`. So Zig was
> racing Release C++/Rust/Go with a 3-9x handicap, which is why it looked
> like the slowest impl. After defaulting the build to ReleaseFast (see
> "The Zig build was Debug" below), **Zig is the fastest impl in 4 of 5
> modes.** The C++/Rust events optimization further down is real and was
> always measured on Release binaries.

## How to reproduce

```bash
# 1. Generate the fixed synthetic corpus (deterministic, gitignored, ~150 MB).
python shared/generate_perf_corpus.py            # writes shared/corpus-perf/

# 2. Bench every built impl across every mode.
python shared/bench.py --runs 5 --all-modes --interleave
```

`bench.py` auto-generates the corpus if it is missing. Pass `--mode <mode>`
for a single mode, `--live` to bench the live `~/.claude/projects` fleet
instead, `--regen` to rebuild the corpus, `--target-mb N` to resize it.

### Why a synthetic corpus

The live fleet (~985 MB, ~3300 files) changes every session, so it can't
give comparable run-to-run numbers. `generate_perf_corpus.py` writes a fixed,
seeded tree that mirrors the real layout and exercises every mode: assistant
`usage` turns across all model families (cost/events), `<progress-beacon>`
lifecycles (beacons), and prose + tool blocks + queue-ops with a known
seeded token (search). A `manifest.json` pins the seed, window, file counts,
a sample beacon session-id, and the search pattern so the bench is
self-describing. The corpus is **never committed** (`.gitignore`).

**Dense beacon session, decoupled into its own root (revised 2026-06-03).** The
generator emits one large beacon-packed transcript
(`corpus-perf-beacons/perf-beacon-stress/sid-beacon-stress.jsonl`, 48 MB /
~11.7k begin->report*->end lifecycles, `--beacon-session-mb`, default 48) and
pins it as the `beacons-latest` sample. Without it, `beacons-latest` read a
single ~7 KB transcript and timed almost pure directory traversal + process
startup, masking any parse work.

Crucially the dense session lives in a **separate root** (`corpus-perf-beacons/`,
a sibling of the main corpus), passed to the walker **only** for the
`beacons-latest` run via `--extra-projects-root`. Earlier it sat inside the main
corpus, which made it a single-file straggler for every parallel full-fleet mode
(`cost`/`events`/`search`/`beacons-history` parse *every* file, so one worker
chewing a 20-48 MB transcript dominated the slow-parse impls) and, once grown,
leaked its seconds-apart begin/end pairs into the `beacons-history` window and
crushed `bias_factor` (11.9 -> 0.16 at 32 MB). In its own root none of that
happens: only `beacons-latest` parses it, so it can be sized freely (48 MB lifts
**all four impls above the noisy sub-100ms range** - cpp 108, zig 169, rust 237,
go 286) without moving any other mode's numbers, and `beacons-history`'s bias
stays driven purely by the realistic ordinary sessions. It is also generated
**after** the main fill loop, so `--beacon-session-mb` no longer perturbs the
main corpus RNG stream. `--beacon-rate` (default 0.18) tunes how many *ordinary*
sessions carry a lifecycle (this is what gives `beacons-history` its work).

### Environment

- Host: chonkers (Windows 11, 32 logical cores). Native impls cap their
  worker pool at `min(8, cores)`, so this is an 8-way-parallel measurement.
- Corpus: main corpus 3153 files (2653 parents + 500 subagents), 150 MB of
  purely ordinary sessions, seed 1234; plus a separate `corpus-perf-beacons/`
  root holding only the 48 MB dense beacon session (parsed only by
  `beacons-latest`). (Earlier the dense session lived inside the main corpus, so
  the main corpus was 150 MB *including* it; moving it out and generating it
  after the fill loop changed the file count and `bias_factor` once - both are
  now independent of `--beacon-session-mb`.)
- C++/Rust/Go built Release; **Zig must be built `-Doptimize=ReleaseFast`**
  (now the default for a bare `zig build` - see below). All pass
  `shared/conformance.py`.
- Medians of 5 runs, interleaved (round-robin after a warm-up so background
  noise smears evenly). Times are end-to-end process wall-clock in ms.

## Baseline (before optimization)

**The Zig column here is a Debug build (the bug described in the correction
note above), so it is not comparable to the Release C++/Rust/Go columns.**

| mode             | cpp   | rust  | go    | zig (Debug) | python |
| ---------------- | ----- | ----- | ----- | ----------- | ------ |
| cost             | 175   | 243   | 301   | 484         | 975    |
| events           | 783   | 755   | 484   | 3967        | n/a    |
| beacons-history  | 135   | 197   | 316   | 433         | n/a    |
| beacons-latest   | 29    | 96    | 126   | 25          | n/a    |
| search           | 135   | 145   | 370   | 427         | n/a    |

C++ led four of five modes but was **third in `events`** (783 ms vs Go's
484 ms). Python (the statusline parallel fleet-walk, the only remaining
pure-Python walker) is 4-6x slower than the native impls in cost mode, the
only mode with a Python implementation. Zig appears to trail everywhere -
but that is the Debug-build artifact, not a real characteristic of the impl.

## After optimization

Current full-run state (medians of 7 interleaved runs on the **decoupled-beacon
corpus**, 2026-06-03), with **all four impls built optimized** (Zig
ReleaseFast). Bold = fastest in that mode:

| mode             | cpp     | rust  | go    | zig      | python |
| ---------------- | ------- | ----- | ----- | -------- | ------ |
| cost             | **164** | 326   | 336   | 196      | ~1100  |
| events           | 470     | 492   | 557   | **435**  | n/a    |
| beacons-history  | **128** | 241   | 299   | 207      | n/a    |
| beacons-latest   | **108** | 237   | 286   | 169      | n/a    |
| search           | 153     | 162   | 237   | **120**  | n/a    |

(Rust includes `panic = "abort"`; C++ cost includes the discovery fix; C++
beacons include the walker buffer/parser reuse; Go + Rust got a dedicated pass
2026-06-03 - see "Go + Rust pass" below. **These absolutes are NOT comparable to
the pre-2026-06-03 board**: the dense beacon session was moved out of the main
corpus into its own root - see "Beacon corpus" above - so the main corpus is now
150 MB of *purely ordinary* sessions instead of 150 MB *including* a 6 MB dense
file. That extra ~6 MB of ordinary content (plus a fresh RNG instance) lifts the
cost/events/search/beacons-history absolutes a notch for every impl; the Go+Rust
win deltas below were measured pre-decouple. The headline change here is
**beacons-latest: all four impls now clear 100 ms** (was a noisy 38-63 ms) via
the 48 MB decoupled sample - cpp 38->108, zig 39->169, rust 46->237, go 63->286 -
while `beacons-history` bias stays realistic (no straggler, no pollution).)

The Go + Rust pass closed most of Go's gap: every Go mode dropped 20-60%
(cost 361->247 now ties Rust, beacons-latest 171->63, search 366->213,
beacons-history 305->237), and Rust beacons-latest fell 126->46 (glob removed).
Go is no longer the across-the-board slowest - it leads Rust on cost and trails
only on parse-bound search (now JSON-parse-bound, not regex-bound - see below).

**The board is still split: C++ wins cost + both beacon modes, Zig wins events
+ search (and effectively ties C++ on beacons-latest, 39 vs 40).** C++
reclaimed cost (the status-line-critical mode) 193->134 via the discovery fix
below. The headline threads:

- **The Zig build was Debug** (the big one). Defaulting `zig build` to
  ReleaseFast took Zig cost 521->160, events 1291->386, search 443->118,
  beacons-history 469->169. No source change - just the optimizer.
- **The Zig events walk was serial.** Independently of build mode, `events`
  walked the discover groups single-threaded while cost mode already fanned out
  across threads. Parallelizing it (below) keeps ReleaseFast events at ~386 ms
  instead of the ~1.5-2 s a serial Release walk would cost.
- **C++ discovery did a redundant stat per file.** cost/events called the
  `fs::last_write_time(path)` *free function* (a fresh CreateFile/query/close
  per `.jsonl`) instead of the cached `directory_entry::last_write_time()`
  method. Fixing it cut C++ cost 193->134 - back ahead of Zig. (search already
  used the cached method; its remaining gap is `directory_iterator` overhead vs
  Zig's raw `FindFirstFileEx`, addressable by a raw-syscall directory helper if
  search ever needs to win too.)

Headline wins for the earlier C++/Rust events pass (real, always Release):

- **C++ events: 783 -> 451 ms (~42% faster)** - closed the one mode where C++
  trailed.
- **Rust events: 755 -> 416 ms (~45% faster)** - now on par with the fastest
  events impl.

Go's events was already competitive; its `json.Encoder` change is a wall-clock
wash (lower allocation only). cost / beacons / search were never the C++/Rust
optimization target.

## The Zig build was Debug (the real story)

`zig/build.zig` used `b.standardOptimizeOption(.{})`, which **defaults a bare
`zig build` to Debug**. `bench.py` and `conformance.py` never build Zig - they
locate `zig/zig-out/bin/walker.exe` and run it. So whatever Debug binary was
last built is what got benched against ReleaseFast C++ / `cargo build
--release` Rust / `go build` Go. Zig Debug carries full safety checks (bounds,
overflow, undefined) and no optimization - a 3-9x penalty here.

Fix: read `-Doptimize` directly and default it to ReleaseFast:

```zig
const optimize = b.option(std.builtin.OptimizeMode, "optimize",
    "Optimization mode (default: ReleaseFast)") orelse .ReleaseFast;
```

(`standardOptimizeOption`'s `preferred_optimize_mode` does **not** do this - it
only changes what an explicit `-Drelease` flag maps to and still defaults a bare
build to Debug. Confirmed empirically: the bench stayed slow until the
`b.option` form above.) `shared/coverage.py` still passes `-Doptimize=Debug`
explicitly for kcov line mapping, so the coverage path is unaffected.

So the headline question "is it the JSON parser?" was answered no: in
ReleaseFast, Zig's `std.json.Scanner` beats simdjson (cost 173 vs C++ 215) and
sonic (vs Go 374). No parser swap needed. zimdjson / simdjzon (SIMD Zig ports of
simdjson) exist if a future need arises, but the data does not call for one.

## What was optimized and why

Both C++/Rust wins came from the same hotspot: **NDJSON emit in `events` mode**,
which
writes one line per assistant turn (~233k lines for this corpus). Phase
timers (`WALKER_PROFILE=1`, added to `cpp/events.cpp`) pinned it precisely -
emit was ~450 ms of a ~650 ms run; discover + walk + sort together were ~200 ms.

### C++ (`cpp/events.cpp`, `cpp/json_writer.hpp`, `cpp/main.cpp`)

1. `std::ios_base::sync_with_stdio(false)` in `main()` - the default sync makes
   every `operator<<` a synchronized C-stdio call.
2. Build the whole payload in one `std::string` and write it with a single
   `std::fwrite`, instead of ~5 `std::cout <<` calls per record (each
   re-applying the `std::fixed`/`setprecision` manipulators). Added a
   `std::string`-appending overload of `write_json_string`.
3. `std::to_chars(..., chars_format::fixed, 6)` instead of `snprintf("%.6f")`
   for the two doubles per record (verified byte-identical output via a
   multiset diff against the pre-change, conformance-passing output).

Emit phase: 450 ms -> 153 ms (buffer + fwrite) -> 97 ms (to_chars).

### Rust (`rust/src/events.rs`)

`StdoutLock` is unbuffered, so the per-record `writeln!` issued one syscall
per line. Now `serde_json::to_writer` appends every record into one `Vec<u8>`
(no per-record `String` allocation) followed by a single `write_all`. The
broken-pipe behavior (`walker events | head`) is preserved: one failed write
attempt, silently absorbed (the existing `emit_records` test still passes).

Both optimizations are profile-guided and stop where gains flattened (emit is
now on par with the discover/walk phases). The `WALKER_PROFILE` phase-timer
hook is left in C++ for future profiling; it is a no-op unless the env var is set.

### Zig (`zig/src/events.zig` + `zig/build.zig`)

Two independent fixes, both needed:

0. **ReleaseFast build default** (`build.zig`, above) - the dominant factor.

1. **Parallelize the walk.** Independently of the build mode, Zig's `events`
   walked the discovered groups in a single-threaded loop while cost mode
   already fanned out across threads. Mirror cost-mode's lock-free group queue
   (`EventsQueue` + `fetchAdd` cursor) and fan out to `min(8, cpu)` workers.
   Each worker owns a private arena and a local record list; the main thread
   merges, sorts, and emits. The worker arenas are deinit'd at *function* scope
   (not inside the parallel block) because they own the emitted `model` strings
   - a block-scoped `defer` frees them before emit reads them (use-after-free;
   caught immediately by the `04-multi-session` fixture). Without this, a
   ReleaseFast serial walk would still be ~1.5-2 s; with it, events is ~370 ms.
2. **Drop per-field emit allocation.** The old emit `allocPrint`-ed each `ts`
   and `usd` into a fresh arena buffer (~466k tiny allocations). Now they
   format into one reused stack buffer via `bufPrint`; the whole payload is
   still built once and written with a single `writeStdout`.

Net (Debug serial -> ReleaseFast parallel): events 3912 -> 371 ms (~10x).
Output is byte-identical (same `{d}` float format); conformance still compares
events as a multiset, so the parallel merge's tie reordering is irrelevant.

### Go (`go/events.go`) - second pass

Go's events was already competitive (buffered `bufio` writer, fast `sonic`
parse), so there was no emit syscall problem to fix. The one remaining waste
was a per-record `json.Marshal`, which allocates a fresh `[]byte` (plus
reflection) each call. Switched to a single reused `json.Encoder`, which pools
its buffer across records and appends the line terminator itself. Output is
byte-identical (same HTML escaping and float formatting as `Marshal`).
Wall-clock is a wash on this corpus (events stays parse-dominated at ~470-490
ms), but allocation/GC pressure drops; kept as the faithful analog of the
C++/Rust "one buffer, no per-record alloc" change.

## Beacon walker optimization (C++, 2026-06-03)

Once the dense beacon session made `beacons-latest` exercise real parse work
(rather than timing directory traversal), the two beacon walkers in
`cpp/beacons.cpp` got a profile-obvious pass:

1. **Per-line buffer reuse.** `walk_assistant_entries` and
   `walk_entries_for_history` declared `combined_text` / `ts_str` /
   `entry_type` / the per-block `text_value` *inside* the line loop, so each
   of the corpus's ~225k assistant entries malloc'd and freed fresh strings.
   Hoisted them above the loop and `clear()` per line - capacity is retained,
   the allocator churn is gone.
2. **Per-beacon parser reuse.** `parse_beacon_body` constructed a fresh
   `simdjson::ondemand::parser` (and its internal buffers) for *every* beacon
   body - thousands of them in the dense session. Now a `thread_local` parser
   is reused; `thread_local` keeps each `beacons-history` worker isolated (a
   simdjson parser is not shareable across threads) while `beacons-latest`
   (single-threaded) just reuses the one.

Measured same-corpus (C++ only, dense-beacon corpus, walker-reported ms):

- **beacons-history: 137 -> 112 ms walker (~18% faster).** This walker fires
  for every file in the corpus, so the saved per-line/per-beacon allocations
  compound. C++ extends its lead to 2.7x over Go.
- **beacons-latest: ~38 -> ~33 ms walker (within noise).** This mode is
  traversal-bound: ~28 ms is `std::filesystem::directory_iterator` stat-ing
  every slug + every session subdir for the target session-id, and only ~10 ms
  is the 6 MB parse the optimization touches. The next lever here is the
  directory walk, not the parser (see Findings #3 and #5).

Conformance stays green (608/608) - both changes are pure allocation hygiene,
no output change.

## Go + Rust pass (2026-06-03)

Go was the across-the-board slowest impl and Rust trailed on `beacons-latest`;
neither had been touched since the events pass. This pass closed both gaps with
three independent changes - no algorithm changes, output byte-identical,
conformance 216/216 green.

### Go `search`: literal pre-filter (276 -> 213 ms walker 251->187)

A CPU profile (the new opt-in `WALKER_CPUPROFILE` hook on `runSearch`, mirroring
C++'s `WALKER_PROFILE`) of the post-single-parse binary pinned the dominant cost
at **~50% in Go's `regexp` backtracking engine** (`backtrack`/`tryBacktrack`
cum + `unicode.SimpleFold` for the default `(?i)` case-folding), run at every
position of every message's text - even though the bench pattern is a literal
(`ZEBRAFINCH`) that almost never occurs. JSON parse (sonic) was ~22%, GC ~14%.

Fix: `searchMatcher` wraps the compiled regex with an **allocation-free
pre-filter** for plain ASCII literal patterns (`go/search.go`). Before running
the regex it scans the text once: a pure-ASCII text with no case-folded
occurrence of the literal cannot match (over ASCII text the default `(?i)`
reduces to ASCII case-folding), so the regex is skipped on the common no-match
case. Any text containing a non-ASCII byte falls back to the regex, so full
Unicode case-folding semantics are preserved **exactly** - a `TestSearchMatcher
FastPathParity` unit test locks this with the U+212A KELVIN SIGN (which
`(?i)k` fold-matches): the matcher must defer to the regex and agree with it.
After the pre-filter, regex dropped to ~13% of CPU and `search` is now
**JSON-parse-bound** (sonic `decode_` + cgocall ~40%) - the remaining gap to
C++/Zig is the two sonic passes per line (root + content), a harder lever than
the regex was.

### Go `search`: one content parse instead of three (366 -> 276 ms walker 345->251)

`searchScanFile` parsed each line's root into a `map[string]json.RawMessage`
and then **re-parsed the `content` RawMessage three separate times** - once for
the default text, once for the tool-inclusive text, and once for the
only-tool-blocks check (`searchExtractText` x2 + `searchIsOnlyToolBlocks`). Rust
(the lean reference) parses the line once into a `serde_json::Value` and does
three cheap DOM walks, so Go was doing ~4 full JSON parses per line where Rust
did 1.

Fixes, all in `go/search.go`:
1. **Unified `searchExtractAll`** computes default text, tool-inclusive text,
   and only-tool-blocks from a **single** parse of the content array. The
   tool-inclusive text is only built when `--include-tool-blocks` is set (the
   uncommon case), so the default path is one parse, not three. The two old
   helpers were deleted and their unit tests repointed at `searchExtractAll`.
2. **Typed root struct** (`type`/`message`/`timestamp`/`content` as fields)
   replaces the `map` plus the per-field re-parses of `type` and `timestamp`.

### Go: `scanner.Bytes()` instead of `scanner.Text()` + `[]byte(line)` (cost, beacons)

Every Go walk did `strings.TrimSpace(scanner.Text())` (allocates a string) then
`sonic.Unmarshal([]byte(line), ...)` (copies it back to bytes) - two
allocations per line purely for the buffer dance. Switched to
`bytes.TrimSpace(scanner.Bytes())`, which aliases the scanner's buffer with no
allocation and feeds `sonic.Unmarshal` directly. Safe because the only
`RawMessage` fields (`message`/`content`) are consumed within the same loop
iteration, before the next `Scan()` reuses the buffer. Applied in
`main.go` (cost: 361->247), `beacons.go` (history: 305->237; latest scan path),
and the search rewrite above.

### Go + Rust `beacons-latest`: direct `read_dir` instead of glob (Go 171->63, Rust 126->46)

Both impls located a single session's transcripts by compiling two glob
patterns (`<root>/*/<sid>.jsonl` and `<root>/*/*/subagents/agent-<sid>.jsonl`).
Glob lists every slug dir's full contents (and, for the subagent pattern,
re-lists root and every slug a second time, then lists each `subagents/` dir).
Replaced with a `discover_latest_paths` / `discoverLatestPaths` helper that
walks the tree directly and **probes the parent file with a single stat per
slug dir** instead of listing it. Result order is irrelevant - the caller keeps
the highest-timestamp beacon. This was Rust's documented Finding #3 and the same
lever applied to Go. `beacons-history`'s glob discovery was left alone (it
genuinely needs every file, so there is nothing to prune).

## Compile-flag exploration

After the Zig Debug fix, swept the other three for "are we leaving optimizer
wins on the table" (the same question that surfaced the Zig bug).

| impl | knob tried | verdict |
| ---- | ---------- | ------- |
| Rust | `panic = "abort"` | **Adopted** - cost 293->277, events 460->409 (~10% on the parse-heavy modes). Removing unwind landing pads lets LLVM inline more. Safe for a CLI (error paths use `process::exit`, never panic-recovery). |
| Go   | PGO (`default.pgo`) | **Rejected** - measured 301->294 ms cost (~2%), below the ~15% run-to-run noise floor on this host. sonic's hot path is already hand-tuned SIMD; PGO mainly helps Go-code inlining, of which little is hot here. Not worth committing+regenerating a binary profile artifact. |
| C++  | `-flto` (GCC/Clang) | **Adopted (Linux/CI only)** - the non-MSVC Release branch was `-O3` with no LTO, while MSVC already had `/GL`+`/LTCG`. Added `-flto` to the GCC/Clang compile+link so the Linux CI and `install.sh` binary get whole-program inlining too. Unmeasured on this Windows host (MSVC path unchanged); CI validates correctness. |
| all  | native-CPU (`-march=native`, `/arch:AVX2`, `GOAMD64=v3`, `target-cpu=native`) | **Rejected** - modest gains, but they bake the build host's CPU into the artifact. C++ is the *shipped* binary (`install.sh` -> `~/.local/bin`); a native build would fault on an older CPU. Same class of bug as a hard-coded path. simdjson already does runtime SIMD dispatch on the C++ hot path regardless. |

Rust was otherwise already maxed (`opt-level=3`, `lto="fat"`, `codegen-units=1`,
`strip`). The headline remains: these are single-digit-% increments; the only
large structural win was the Zig Debug-build fix.

## Findings (not addressed here)

1. ~~**Zig `events` is ~9x slower than the others (~3900 ms).**~~ **Resolved.**
   Two causes: (a) the binary was a Debug build (fixed by defaulting
   `zig build` to ReleaseFast), and (b) the events walk was serial (fixed by
   parallelizing it). Zig events is now ~392 ms - the *fastest* impl. The
   "slower JSON parser" hypothesis was wrong: ReleaseFast `std.json.Scanner`
   beats simdjson/sonic on this corpus.
2. **Zig `beacons-history` bias divergence.** Reproduces on the dense-beacon
   corpus: Zig reports `bias_factor` 12.2442 while Rust/C++/Go all agree on
   11.8921 (3-vs-1, so Zig is the outlier). (The pre-2026-06-03 corpus showed
   14.94 vs 14.26 - the absolute values are corpus-instance-dependent; the
   3-vs-1 split is the durable signal.) Conformance passes (608/608) because
   the beacon fixtures don't cover this case - a **conformance gap plus a
   likely real Zig bug**. Worth a dedicated fixture that reproduces it.
   - Lead: this is the begin/end **pairing** algorithm (single in-flight
     `pending_begin`, orphan-on-re-begin, pair on first `end` after a begin) that
     the now-shipped beacon-pairing-fix reworked. Rust/C++/Go agree, so the
     reference algorithm is right; suspect `zig/src/beacons.zig`'s pairing loop
     mishandles an edge the small fixtures miss (multi-lifecycle in one session,
     back-to-back begin/end, orphaned begin/end). Diff Zig's pairing against the
     other three on a session with several lifecycles. A fixture that reproduces
     the split (3 impls vs Zig) is the TDD entry point.
3. ~~**Rust `beacons-latest` is ~3.5x slower than C++/Zig** (126 ms vs ~40 ms).~~
   **Resolved 2026-06-03** (Go + Rust pass above). Swapped the `glob` crate for
   a direct `read_dir` walk that probes the parent file with one stat per slug
   dir: Rust 126->46 ms, Go 171->63 ms. Both now within ~1.2-1.6x of C++/Zig
   (the residual is the directory-traversal floor flagged in Finding #5).
5. **C++ `beacons-latest` is traversal-bound (~28 of ~33 ms walker).** With the
   dense session in place, the remaining cost is
   `std::filesystem::directory_iterator` stat-ing every slug dir + every
   session subdir to locate the target session-id's files (most stats miss).
   The buffer/parser reuse above doesn't touch it. The lever is a raw-syscall
   directory walk (`FindFirstFileEx` on Windows / `getdents` on Linux), the
   same approach flagged for `search` - worth doing once and sharing across
   both modes. Low absolute cost, so still deferred.
4. **Formatter-hook footgun.** The repo has no `.clang-format` or `rustfmt.toml`,
   but the local PostToolUse hook runs `clang-format -i` / `cargo fmt` with
   default styles on every C++/Rust edit. Those defaults do not match the
   hand-maintained committed code (2-space vs 4-space C++; collapsed vs
   multi-line Rust signatures), so a one-line edit triggers a whole-file/whole-
   crate reindent. Recommend either committing format configs that match the
   existing style or scoping the hooks to already-formatted files.
