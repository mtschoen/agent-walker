# claude-walker — Comparison Results

Side-by-side numbers from running the same workload through 4 native
implementations + the Python reference. Every implementation passes the
shared conformance corpus bit-exact (±$0.01) and produces identical
totals on the live fleet.

## Setup

- Live fleet: ~2800 JSONL files, of which ~2760 fall inside the weekly
  mtime window and form ~1940 distinct session groups (chonkers,
  2026-05-27 — a heavy-usage week, so almost the whole fleet is recent).
- 32-core box (Windows 11). 11 interleaved rounds each via `shared/bench.py
  --runs 11 --interleave` (untimed warm-up); reporting median wall-clock.

> **Re-benched 2026-05-27 (fair `--no-config`).** The number tables below
> were regenerated after `bench.py` was fixed to pass `--no-config` to
> *all* impls — it previously passed it to cpp only, so rust/go/zig also
> folded in a mounted second machine over SMB while cpp didn't, inflating
> their times (worst in cost mode). Absolute numbers are also much higher
> than in this file's git history because the post-filter fleet is far
> larger now (~2760 vs ~300 files). Binary-size columns were **not**
> re-measured this pass. The "What changed in …" sections lower down are
> historical records of past optimization passes and keep their original
> numbers.

## Headline numbers — cost mode

| Lang   | Median     | Range   | vs winner | vs slowest | Binary  |
| ------ | ---------: | ------- | --------: | ---------: | ------: |
| C++    | **151ms**  | 144-158 | 1.00x     | 2.48x      |  267 KB |
| Rust   |   213ms    | 196-225 | 1.41x     | 1.76x      |  423 KB |
| Go     |   276ms    | 269-295 | 1.83x     | 1.36x      |  8.0 MB |
| Zig    |   374ms    | 369-386 | 2.48x     | 1.00x      |  977 KB |
| Python |   354ms    | 288-411 | 2.34x     | 1.06x      |     n/a |

Python reference is the orjson + 8-worker `ProcessPoolExecutor` walker
that ships in [schoen-claude-status](https://github.com/mtschoen/schoen-claude-status).
Original single-thread `json.loads` baseline (not in this table) was
~750ms.

All four native implementations agree to the cent on the live fleet
(trailing/window values shift between bench passes as new sessions
land in the corpus, but every impl in a single pass agrees).

## Cross-mode performance

The status line drives three different walker subcommands. Numbers here
are interleaved 11-round wall-clock medians on the same live fleet
(1942 distinct session groups, 2760 files post-mtime-filter). Wall-clock
includes process startup + filesystem discovery + walk + output — i.e.
what a status-line caller actually feels.

| Mode                          | cpp        | rust       | go       | zig      |
| ----------------------------- | ---------: | ---------: | -------: | -------: |
| cost (8 workers)              | **151ms**  | 213ms      | 276ms    | 374ms    |
| beacons-history (8 workers)   | **216ms**  | 329ms      | 820ms    | 936ms    |
| search `"TODO" --limit 1`     | **187ms**  | 424ms      | 622ms    | 811ms    |

cpp leads every mode after the perf-pass-2 cleanup (zero per-line
allocation via `padded_string_view`, search mmap rewrite, hand-rolled
scanner for the beacon envelope, regex-free search literal path; see
"What changed in cpp perf-pass-2" below for the breakdown). rust is
second in all three modes; go is third and zig fourth in all three, with
go reliably faster than zig on every Windows mode (the two only trade
places on Linux — see Linux verification).

Methodology: an untimed warm-up, then 11 interleaved rounds — one round
= run every (impl, mode) cell back-to-back, so background noise hits all
cells equally. cost and beacons-history run through `shared/bench.py
--runs 11 --interleave` (median + min/max over the 11 timed rounds);
search runs through a one-off interleaved timer (bench.py has no search
mode), reporting the median + range of the kept 9 after dropping the
single high/low outlier. Observed ranges:

| Mode             | cpp range | rust range | go range  | zig range |
| ---------------- | --------: | ---------: | --------: | --------: |
| cost             |  144-158  |  196-225   |  269-295  |  369-386  |
| beacons-history  |  202-238  |  319-358   |  784-918  |  917-961  |
| search           |  182-190  |  410-430   |  620-651  |  795-862  |

Ranges are non-overlapping between adjacent impls in every mode except
go↔zig in beacons-history, where they just abut (go 784-918, zig
917-961) — go is still reliably faster on the median. All four impls
report identical trailing/window/files/groups/n_pairs/bias_factor on the
same fleet under the same `--now` (sanity-checked before bench).

Bench harness: `.claude/scripts/bench-interleaved.py` (gitignored —
lives outside the tracked tree; recreate from the methodology above if
missing). It runs N interleaved rounds across the four impls,
drops top+bottom, and reports median + walker-internal `elapsed_ms`
alongside wall-clock so you can see how much of the wall is per-process
startup. `shared/bench.py` runs sequentially per impl and is fine for
single-impl tuning, but doesn't isolate cross-impl noise.

### Historical: rust leadership window

For the period between the cpp/rust/go parallelization pass and
cpp perf-pass-2, rust briefly led every mode. Rust's typed
`serde_json::Deserialize` had lower per-line allocation cost than
cpp's `simdjson::ondemand::parser::iterate(padded_string(line))` —
because cpp was allocating a fresh `padded_string` per JSONL line,
copying the line bytes and padding them. That allocation was not
inherent to simdjson (it accepts `padded_string_view` pointing into
an already-padded buffer), just a quirk of how the code had been
written. Removing it (perf-pass-2 commit `b8705ec`) restored cpp's
lead and then some. See "What changed in cpp perf-pass-2" below.

## What changed since the first pass

The first pass had C++ at 402ms (slowest) and Go at 373ms because both
were on convenience JSON parsers (`nlohmann/json` DOM, stdlib
`encoding/json` reflection). Swapping each to a hot-path parser flipped
the ranking:

- **C++ (`nlohmann/json` → `simdjson` v4.6.4 on-demand): 402ms → 88ms** (4.6x).
  Now the fastest implementation, beating Rust by 25%.
- **Go (`encoding/json` → `bytedance/sonic` v1.15.1): 373ms → 141ms** (2.6x).
  Now between Zig and Python.

Rust was unchanged across the two passes — already on a non-allocating
parser (typed serde_json).

## What changed in the Zig perf-gap pass

After the beacons-history and search subcommands landed, zig was 4-20×
slower than cpp across the three modes because `std.json.parseFromSlice`
materializes a full `Value` tree (`ObjectMap` allocation plus dup of
every string) for every JSONL line. Two changes closed the gap:

1. **Scanner streaming (all three hot paths).** Replaced
   `parseFromSlice(Value, ...)` with `std.json.Scanner` token streaming.
   The scanner walks tokens against the input slice with
   `.alloc_if_needed` semantics, so the five fields we care about per
   line (`role`, `id`, `model`, `timestamp`, `usage.*`) come back as
   zero-copy slices into the input when escape-free. No `ObjectMap`,
   no per-string dup, no recursive `Value` construction. Closed
   ~60-70% of the gap before any threading change:
   - cost-mode: 313ms → 163ms
   - beacons-history: 13.8s → 5.6s
   - search: 11.75s → 4.5s
2. **Worker pool in beacons-history + search.** Both subcommands were
   single-threaded; cost mode already had a pool. Lifted the pattern,
   gave each worker its own arena (arena allocators are not
   thread-safe), and merged per-worker result lists at the end. The
   per-worker arenas stay alive until the output JSON is written
   because hit/pair fields slice into them. On the 8-worker pool
   (`min(8, ncpu)`):
   - beacons-history: 5.6s → 769ms (7.3× on top of Scanner)
   - search: 4.5s → 573ms (7.9× on top of Scanner)

Search also got a one-pass content-walker collapse: the prior code
called `extractText(default)` + `extractText(with_tools)` +
`isOnlyToolBlocks` (three iterations over `message.content`); the
Scanner walker builds both text variants and counts tool-block ratio in
a single pass.

Net: zig went from "slowest in every mode" to "fastest in search,
second-fastest in beacons-history, within 2× of cpp in cost." All on
stdlib only — no `zimdjson` dependency, no PCRE library.

## What changed in the parallelize-cpp/rust/go pass

After the Zig pass, three of the four impls were still single-threaded
in `search` and `beacons-history` — only cost mode had a worker pool.
Zig was the only impl parallel in all three modes, and it was winning
search by a wide margin for that reason alone. This pass added a
`min(8, ncpu)` worker pool to the remaining nine `<impl> × <mode>`
cells, using each language's idiomatic pattern. Per-impl before/after
(each impl bench'd against its own pre-pass build, on the same live
fleet within the same hour to keep variance controlled):

| Impl  | search before → after | beacons-history before → after |
| ----- | --------------------: | -----------------------------: |
| cpp   |  2353ms → 375ms (6.3×) | 1117ms → 285ms (3.9×)         |
| rust  |  1096ms → 220ms (5.0×) |  965ms → 242ms (4.0×)         |
| go    |  1383ms → 410ms (3.4×) | 1250ms → 631ms (2.0×)         |

Patterns used:

- **cpp**: atomic-index worker pool mirroring `main.cpp::run_cost`. Per-thread
  `Hit` vectors (search) or `pairs`/`pair_meta` vectors (beacons-history)
  merged before sort. `simdjson::dom::parser` is constructed locally per
  call to `scanFile()`, so no shared parser state. `std::regex` const ops
  are thread-safe per spec, so one shared compiled regex is fine.
- **rust**: `rayon::ThreadPoolBuilder` + `par_iter().map(...).reduce(...)`
  inside `pool.install()`. The reduce closure short-circuits when the
  accumulator is empty (returns `next` directly) to avoid unnecessary
  copies. beacons-history reduces a `(Vec, Vec)` tuple — rayon handles it
  cleanly. Already on typed `serde_json::Deserialize`, so per-line cost
  was lower than cpp's simdjson; ratio is smaller (5.0× vs cpp's 6.3×)
  but the absolute landing point is faster than every other impl.
- **go**: goroutines + `sync.WaitGroup` mirroring `main.go::runCost`'s
  pattern. Per-worker accumulator slices indexed by `tid`, merged after
  `wg.Wait()`. `bytedance/sonic`'s `Unmarshal` is thread-safe per its
  docs (already used in cost mode), so no per-goroutine parser needed.
  `regexp.Regexp` is documented thread-safe for `FindAll*`.

Cross-impl invariants enforced: hit ordering in search is deterministic
across runs (sort by `(timestamp DESC, session_id ASC, line_number ASC)`
after the parallel reduce, before truncation to `--limit`). Pair order
in beacons-history doesn't matter because the conformance harness sorts
pairs by `(begin_eta, actual_elapsed)` before comparing
(`shared/conformance.py:246`).

Net: rust pulls ahead of cpp in every mode after this pass. cpp's
absolute landing point in search (~375ms wall, ~36ms of which is the
walker's internal work — the rest is subprocess launch + filesystem
discovery) hits the floor where per-process startup dominates; further
in-binary wins would be invisible to status-line callers.

## What changed in cpp perf-pass-2

After the parallelize pass, rust led cpp in every mode (see the
"Historical: rust leadership window" subsection above for the
working theory at the time). That theory was that `simdjson::ondemand::
parser::iterate(padded_string)` had to allocate a padded buffer per
line — which turned out to be true but **not inherent to simdjson**.
The
parser also accepts a `padded_string_view` that points into an
already-padded buffer (e.g., the whole-file `padded_string` we load
with `padded_string::load()`), and `simdjson::SIMDJSON_PADDING` tail
bytes of zero-padding live just past `data.size()`. So for any line
at offset `o`, we can hand simdjson a view with capacity `data.size() -
o + SIMDJSON_PADDING` — zero allocation, same per-line iterate
semantics (which we need to keep, since `iterate_many` aborts the
stream on a single malformed line per the `03-malformed-lines`
fixture).

A pass on the three hot paths (cost / beacons-{latest,history}) plus a
collection of smaller wins on search:

| Fix                                                | mode             | before → after |
| -------------------------------------------------- | ---------------- | -------------: |
| `padded_string_view` into whole-file buffer        | cost             |   186 → 148ms  |
|                                                    | beacons-history  |   193 → 148ms  |
| search: `padded_string::load` + view (was getline) | search           |   357 → 136ms  |
| search: skip `text_with_tools` when not requested  | search           |   136 → 118ms  |
| Hand-rolled scanners (replace `std::regex`)        | search           |   118 →  99ms  |
|                                                    | beacons-history  |   ~ no change  |
| `parse_iso8601` / `rates_for` zero-alloc           | all              |  within noise  |
| Remove dead `ThreadPool` (code health)             | n/a              |  −88 LoC       |

Cumulative on ~900-group live fleet (9-round interleaved bench, drop
top+bottom, median of 7 — variance ranges do not overlap):

| Mode             | baseline | perf-pass-2 |  Δ   | speedup |
| ---------------- | -------: | ----------: | ---: | ------: |
| cost             |  201ms   |     149ms   | −26% |  1.35×  |
| beacons-history  |  187ms   |     138ms   | −26% |  1.36×  |
| search           |  356ms   |      97ms   | −73% |  3.67×  |

**Patterns:** every per-line `sj::padded_string padded(line)` got
replaced with a `sj::padded_string_view` pointing into the whole-file
buffer. The `<progress-beacon>{...}</progress-beacon>` regex got
replaced with a `find` of the OPEN tag, `find` of the CLOSE tag, and
whitespace-trimmed `{...}` shape check — equivalent to the original
regex's non-greedy `\{[\s\S]*?\}` semantics (the non-greedy `*?`
expands until `\s*</tag>` matches, which IS "shortest body ending in
`}` immediately before the close tag"). The search literal path
(default, no `--regex`) now uses `std::string_view::find` or
`std::search + tolower` instead of compiling a regex with all
meta-chars escaped.

**Surprise:** beacons-history barely moved from replacing `std::regex`
specifically (~158 vs 148ms before), because most assistant texts
don't contain `<progress-beacon>` at all; `text.find("<progress-beacon>",
pos)` short-circuits to `npos` faster than even a compiled regex's
prefix scan. The big beacons-history win came from the per-line
allocation fix, not the regex fix. The hand-rolled scanner is still
worth keeping for clarity and to avoid MSVC `std::regex` compile
overhead on hot paths.

**Conformance:** all 7 cost fixtures + 4 beacons-latest scenarios +
beacons-history + 2 multi-root + 16 search combos green before AND
after each individual commit. No tolerance changes.

## Surprises

**Rust briefly won everything, then cpp took it back.** Pre-parallelization,
cpp on simdjson was fastest in cost mode (88ms vs rust 115ms). After
the parallelize pass, rust pulled ahead in every mode (cost 221, beacons
242, search 220 vs cpp 333/286/352). The diagnosis was that rust's
typed `serde_json::Deserialize` had lower per-line allocation cost than
cpp's simdjson on-demand walks. That diagnosis was half right — cpp's
real problem was that the simdjson code was allocating a
`padded_string` per JSONL line instead of using `padded_string_view`
into the whole-file buffer (an avoidable quirk, not inherent to the
parser). perf-pass-2 fixed it and cpp now leads every mode again
(139ms cost, 146 beacons, 103 search). The lesson: when a parser
"can't beat" another, check whether you're paying for the
*implementation choice* or the parser itself.

**The C++ → simdjson rewrite is more code, not less.** 545 LoC →
599 LoC. simdjson's on-demand API is forward-only, so each field has
explicit error-checked extraction — no `if (msg.contains("usage") &&
msg["usage"].is_object())` ergonomics. The verbosity is worth 4.6x; the
benchmarks don't lie.

**Sonic adds ~4.7 MB to the Go binary.** 3.3 MB → 8.0 MB. That's the JIT
assembler, decoder generators, and CPU feature detection. Worth it if
JSON parse cost is on the hot path; not worth it if you only deserialize
config files at startup.

**Per-line `simdjson::iterate` is the right call, not `iterate_many`.**
The handoff doc suggested `iterate_many` for the perf win of single-allocation
batched parsing. In practice `iterate_many` aborts the entire stream on
the first malformed line and can't resume, which kills the
`03-malformed-lines` conformance fixture. Per-line `parser.iterate(view)`
is structurally identical to the original nlohmann code, recovers from
bad lines, and (since perf-pass-2) costs zero allocations per line by
feeding the parser a `padded_string_view` into the whole-file buffer
rather than a fresh `padded_string` copy of each line.

**JSON parser choice still dominates language choice.** With every
implementation now on a non-allocating hot path (serde_json typed structs,
simdjson on-demand, sonic, std.json manual extraction), the spread
narrows from 3.5x to 1.9x. The remaining gap reflects real differences
between parsers, not language quality.

**Python no longer trails every native impl (fleet-size dependent).** On
the small (~300-file) fleet this file originally measured, the orjson +
8-worker ProcessPoolExecutor walker was slowest at 345ms — every native
impl beat it 2-4×. On the much larger fleet measured 2026-05-27 (~2760
post-filter files) its process-pool parallelism now edges out
single-process zig (python 354ms vs zig 374ms), landing between go and
zig; cpp and rust still beat it comfortably (151ms / 213ms). More files
amortize python's process-pool startup + IPC overhead.

## Per-implementation notes

### C++ (`cpp/`)

- C++20, MSVC via `cmake -G "Visual Studio 17 2022"` (auto-discovers
  `cl.exe`, no PATH manipulation)
- `simdjson` v4.6.4 via FetchContent, built from source (~20s clean build)
- Per-line `parser.iterate(padded_string(line))` — switched from `iterate_many`
  after that path lost the `03-malformed-lines` fixture
- Custom `std::thread` pool (no rayon-equivalent in the stdlib)
- 267 KB binary — still the smallest by far; simdjson static lib adds ~92 KB
- Author's note: the on-demand top-level field iteration with `unescaped_key()`
  + dispatch was less ergonomic than expected, but it's the right shape
  for forward-only access

### Rust (`rust/`)

- `serde_json` with typed `#[derive(Deserialize)]` structs (no DOM, no
  reflection)
- `rayon::par_iter` for the parallel reduce
- Clean cold build, ~21 deps, full LTO + `codegen-units=1`
- One-line `par_iter().reduce()` is the most concise parallel pattern
  of any of the four
- 423 KB binary

### Zig (`zig/`)

- Zig 0.16, `winget install zig.zig` on Windows; tarball under `~/.local/zig`
  on Linux
- `std.json.Scanner` token streaming with `.alloc_if_needed` (zero-copy
  slices into the input buffer when no escape decoding is needed).
  Shared helpers (`enterObject`, `parseObjectKey`, `parseStringValue`,
  `parseU64Value`) in `main.zig` are reused from `beacons.zig` and
  `search.zig`
- 8-worker `std.Thread` pool in all three subcommands (cost,
  beacons-history, search). Each worker owns a private
  `ArenaAllocator` and a local result list; main thread merges after
  `join`
- Cross-platform via `builtin.os.tag` conditional compilation:
  - Windows path: direct Win32 externs (`CreateFileW`, `FindFirstFileW`,
    `QueryPerformanceCounter`) because `std.io` was removed in 0.16 and
    the new `std.Io` context-passing model didn't fit
  - Linux path: `std.os.linux` syscalls (`openat`, `getdents64`, `statx`,
    `clock_gettime`); reads `/proc/self/cmdline` + `/proc/self/environ`
    to avoid a libc dependency
- Compiler error messages were excellent throughout
- 977 KB binary
- The cross-platform restructure cost about 18% on Windows perf
  (135ms → 164ms median); fair price for honest portability. Caught
  a real bug in the process: `statx` MTIME mask was `0x20` (which is
  ATIME); should be `0x40` — fixed by switching to the typed
  `STATX{ .MTIME = true }` struct literal.

### Go (`go/`)

- `bytedance/sonic` v1.15.1 (drop-in replacement for `encoding/json`)
- `filepath.Glob` doesn't support `**` so subagent discovery is two
  manual `ReadDir` walks
- 8.0 MB binary (Go runtime + sonic JIT + 7 transitive deps)
- The sonic swap was a 2-line diff (1 import, 1 function call); cleanest
  upgrade of the four

### Python parallel reference (in schoen-claude-status)

- `orjson` for parse, falling back to stdlib `json`
- 8-worker `ProcessPoolExecutor`, work-unit = one session group
- Per-session-group dedup catches the parent ↔ acompact-subagent
  collision pattern
- Reduced from 750ms → 248ms by the recent refactor; bench above shows
  ~345ms because it shared CPU with concurrent native benches

## Linux verification

All four impls also build natively on Linux (verified on llamabox,
x86_64). Conformance passes 32/32 bit-exact (4 impls × 8 cases).
Live-fleet bench against llamabox's local fleet (different data, so
absolute numbers don't compare to the Windows table above):

| Lang | Median | Binary |
| ---- | -----: | -----: |
| C++  | 22ms | 339 KB |
| Rust | 33ms | 585 KB |
| Go   | 89ms | 7.9 MB |
| Zig  | 157ms | 3.9 MB |

Same ranking as Windows except Go and Zig swap places — Zig drops to
last on Linux. The Linux Zig binary is 4x larger than Windows likely
because Linux Zig 0.16 currently embeds debug info even in
`-Doptimize=ReleaseSafe`; not investigated.

### Building on Linux

```bash
# Prerequisites: gcc/g++, cmake 3.20+, rustc/cargo, python3, plus:
# Go 1.26+:  extract go*.linux-amd64.tar.gz to ~/.local/go,  add ~/.local/go/bin to PATH
# Zig 0.16+: extract zig-x86_64-linux-*.tar.xz to ~/.local/zig, add to PATH

cd rust && cargo build --release && cd ..
cd go   && go build -o walker .  && cd ..
cd cpp  && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j && cd ..
cd zig  && zig build -Doptimize=ReleaseSafe && cd ..

python3 shared/conformance.py
python3 shared/bench.py --runs 5 --no-python
```

## What's next

- [x] ~~Rebuild C++ on `simdjson` and Go on `sonic-go` — does the ranking
      change?~~ Yes: C++ overtakes Rust by 25%; Go closes to ~141ms.
- [x] ~~Close the Zig perf gap with Scanner streaming + worker pool.~~
      Done — see "What changed in the Zig perf-gap pass" above. Net
      result: zig now fastest in search, second-fastest in
      beacons-history, within 2× of cpp on cost mode.
- [x] ~~Parallelize search + beacons-history in cpp/rust/go.~~ Done —
      see "What changed in the parallelize-cpp/rust/go pass" above. Net
      result: rust pulls ahead of cpp in every mode; absolute floor in
      search hit by all impls (~200-400ms is now per-process startup
      plus discovery, not in-binary work).
- [x] ~~Close cpp's per-line allocation gap.~~ Done — see "What changed
      in cpp perf-pass-2" above. cpp cost −26%, beacons-history −26%,
      search −73% on the same fleet.
- [x] ~~Refresh the Cross-mode performance table by re-benching rust /
      go / zig under identical conditions to the perf-pass-2 cpp
      numbers.~~ Done — see refreshed "Cross-mode performance" table
      above. cpp leads every mode; rust second in all three; go and
      zig swap last/3rd by mode. Ranges don't overlap, so the
      ranking is robust.
- [ ] Tune Rust further: try `simd-json` instead of `serde_json` (the
      tradeoff for small per-line objects is mixed; worth measuring).
      Pass-2 result raises the question of whether rust could also
      pull back ahead by switching parsers, or whether forward-only
      simdjson on-demand has a structural advantage for this access
      pattern.
- [ ] Wire the winner (C++) back into schoen-claude-status's
      `_walk_pace_buckets` as an optional detection: if
      `~/.claude/walker` exists and is executable, use it; otherwise
      the existing Python parallel walker stands.
- [x] ~~Verify all four implementations build on Linux.~~ Done — see
      "Linux verification" above. Zig needed a cross-platform rewrite
      (Win32 → conditional compile w/ Linux syscall path).
- [ ] CI matrix that builds all 5 platforms (`win/mac/linux × x86/arm64`)
      for the chosen winner and attaches binaries to GitHub releases.
