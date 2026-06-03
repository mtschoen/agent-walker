# Performance results

Cross-implementation benchmark of the four walkers (Rust / C++ / Go / Zig)
plus the Python statusline fleet-walk, across all five modes, with a
profile-guided optimization pass on C++ and the next-fastest impl (Rust).

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

### Environment

- Host: chonkers (Windows 11, 32 logical cores). Native impls cap their
  worker pool at `min(8, cores)`, so this is an 8-way-parallel measurement.
- Corpus: 3153 files (2653 parents + 500 subagents), 150 MB, seed 1234.
- All four binaries built Release; all pass `shared/conformance.py` (608/608).
- Medians of 5 runs, interleaved (round-robin after a warm-up so background
  noise smears evenly). Times are end-to-end process wall-clock in ms.

## Baseline (before optimization)

| mode             | cpp   | rust  | go    | zig    | python |
| ---------------- | ----- | ----- | ----- | ------ | ------ |
| cost             | 175   | 243   | 301   | 484    | 975    |
| events           | 783   | 755   | 484   | 3967   | n/a    |
| beacons-history  | 135   | 197   | 316   | 433    | n/a    |
| beacons-latest   | 29    | 96    | 126   | 25     | n/a    |
| search           | 135   | 145   | 370   | 427    | n/a    |

C++ led four of five modes but was **third in `events`** (783 ms vs Go's
484 ms). Python (the statusline parallel fleet-walk, the only remaining
pure-Python walker) is 4-6x slower than the native impls in cost mode, the
only mode with a Python implementation.

## After optimization

| mode             | cpp       | rust      | go    | zig    | python |
| ---------------- | --------- | --------- | ----- | ------ | ------ |
| cost             | 182       | 244       | 310   | 500    | 977    |
| events           | **451**   | **416**   | 465   | 3912   | n/a    |
| beacons-history  | 150       | 215       | 361   | 452    | n/a    |
| beacons-latest   | 29        | 97        | 125   | 25     | n/a    |
| search           | 145       | 155       | 387   | 436    | n/a    |

Headline wins (bolded above):

- **C++ events: 783 -> 451 ms (~42% faster)** - now competitive with / ahead
  of Go, closing the one mode where C++ trailed.
- **Rust events: 755 -> 416 ms (~45% faster)** - now the fastest events impl.

cost / beacons / search were already C++-fastest and untouched (the small
deltas above are run-to-run variance, not regressions).

## What was optimized and why

Both wins came from the same hotspot: **NDJSON emit in `events` mode**, which
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

## Findings (not addressed here)

1. **Zig `events` is ~9x slower than the others (~3900 ms).** Almost certainly
   the same per-line-emit bottleneck; the same buffer-then-single-write fix
   should apply. Zig was out of scope (slowest impl; the brief was C++ then the
   next-fastest, Rust).
2. **Zig `beacons-history` bias divergence.** On this corpus Zig reports
   `bias_factor` 14.94 while Rust/C++/Go all agree on 14.26 (3-vs-1, so Zig is
   the outlier). Conformance passes (608/608) because the beacon fixtures don't
   cover this case - a **conformance gap plus a likely real Zig bug**. Worth a
   dedicated fixture that reproduces it.
   - Lead: this is the begin/end **pairing** algorithm (single in-flight
     `pending_begin`, orphan-on-re-begin, pair on first `end` after a begin) that
     the now-shipped beacon-pairing-fix reworked. Rust/C++/Go agree, so the
     reference algorithm is right; suspect `zig/src/beacons.zig`'s pairing loop
     mishandles an edge the small fixtures miss (multi-lifecycle in one session,
     back-to-back begin/end, orphaned begin/end). Diff Zig's pairing against the
     other three on a session with several lifecycles. A fixture that reproduces
     the split (3 impls vs Zig) is the TDD entry point.
3. **Rust `beacons-latest` is ~3.4x slower than C++/Zig** (97 ms vs ~27 ms) on
   a tiny single-session task. Rust uses the `glob` crate for the targeted
   lookup; replacing it with direct `read_dir` traversal (as C++ does) would
   likely close the gap. Low absolute cost, so deferred.
4. **Formatter-hook footgun.** The repo has no `.clang-format` or `rustfmt.toml`,
   but the local PostToolUse hook runs `clang-format -i` / `cargo fmt` with
   default styles on every C++/Rust edit. Those defaults do not match the
   hand-maintained committed code (2-space vs 4-space C++; collapsed vs
   multi-line Rust signatures), so a one-line edit triggers a whole-file/whole-
   crate reindent. Recommend either committing format configs that match the
   existing style or scoping the hooks to already-formatted files.
