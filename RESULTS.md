# claude-walker — Comparison Results

Side-by-side numbers from running the same workload through 4 native
implementations + the Python reference. Every implementation passes the
shared conformance corpus bit-exact (±$0.01) and produces identical
totals on the live fleet.

## Setup

- Live fleet: ~1500 JSONL files; ~300 survive the weekly mtime filter;
  ~130 distinct session groups.
- 32-core box (Windows 11). 5 warm runs each via `shared/bench.py
  --runs 5`; reporting median wall-clock.

## Headline numbers

| Lang   | Median    | Range       | vs winner | vs slowest | LoC | Binary  |
| ------ | --------: | ----------- | --------: | ---------: | --: | ------: |
| C++    |  **87ms** |  84-95      | 1.00x     | 3.97x      | 599 |  267 KB |
| Rust   |   112ms   | 100-118     | 1.29x     | 3.08x      | 230 |  423 KB |
| Go     |   154ms   | 147-160     | 1.77x     | 2.24x      | 410 |  8.0 MB |
| Zig    |   164ms   | 156-172     | 1.89x     | 2.10x      | 900 |  977 KB |
| Python |   345ms   | 324-362     | 3.97x     | 1.00x      | ~80 |     n/a |

Python reference is the orjson + 8-worker `ProcessPoolExecutor` walker
that ships in [schoen-claude-status](https://github.com/mtschoen/schoen-claude-status).
Original single-thread `json.loads` baseline (not in this table) was
~750ms.

All five implementations agree to the cent on the live fleet
(trailing/window values shift between bench passes as new sessions
land in the corpus, but every impl in a single pass agrees).

## What changed since the first pass

The first pass had C++ at 402ms (slowest) and Go at 373ms because both
were on convenience JSON parsers (`nlohmann/json` DOM, stdlib
`encoding/json` reflection). Swapping each to a hot-path parser flipped
the ranking:

- **C++ (`nlohmann/json` → `simdjson` v4.6.4 on-demand): 402ms → 88ms** (4.6x).
  Now the fastest implementation, beating Rust by 25%.
- **Go (`encoding/json` → `bytedance/sonic` v1.15.1): 373ms → 141ms** (2.6x).
  Now between Zig and Python.

Rust and Zig were unchanged across the two passes — they were already on
parsers with no per-line allocation in the hot path.

## Surprises

**The fastest implementation is C++ on simdjson, by a wide margin.** 88ms
median vs. Rust's 115ms — the absolute scan rate of simdjson on-demand
plus C++'s lack of any iterator-safety overhead is the difference. Rust's
`serde_json` with typed structs is excellent but has more checks per
field access.

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
`03-malformed-lines` conformance fixture. Per-line `parser.iterate(padded_string(line))`
is structurally identical to the original nlohmann code and lands at 88ms
anyway — the on-demand parse skipping 99% of fields by name is what we
were paying for, not the batching.

**JSON parser choice still dominates language choice.** With every
implementation now on a non-allocating hot path (serde_json typed structs,
simdjson on-demand, sonic, std.json manual extraction), the spread
narrows from 3.5x to 1.9x. The remaining gap reflects real differences
between parsers, not language quality.

**Python is still the slowest** at 345ms — every native impl beats it
by 2-4x. Process-pool overhead and per-line orjson cost finally show
through against single-process native walkers that don't pay startup
or IPC costs.

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
- `std.json` dynamic value parsing, manual field extraction
- Cross-platform via `builtin.os.tag` conditional compilation:
  - Windows path: direct Win32 externs (`CreateFileW`, `FindFirstFileW`,
    `QueryPerformanceCounter`) because `std.io` was removed in 0.16 and
    the new `std.Io` context-passing model didn't fit
  - Linux path: `std.os.linux` syscalls (`openat`, `getdents64`, `statx`,
    `clock_gettime`); reads `/proc/self/cmdline` + `/proc/self/environ`
    to avoid a libc dependency
- 900 LoC reflects both platform binding surfaces; without them it'd
  be closer to Rust's count
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
- [ ] Tune Rust further: try `simd-json` instead of `serde_json` (the
      tradeoff for small per-line objects is mixed; worth measuring).
- [ ] Wire the winner (C++ now, was Rust) back into
      schoen-claude-status's `_walk_pace_buckets` as an optional
      detection: if `~/.claude/walker` exists and is executable, use
      it; otherwise the existing Python parallel walker stands.
- [x] ~~Verify all four implementations build on Linux.~~ Done — see
      "Linux verification" above. Zig needed a cross-platform rewrite
      (Win32 → conditional compile w/ Linux syscall path).
- [ ] CI matrix that builds all 5 platforms (`win/mac/linux × x86/arm64`)
      for the chosen winner and attaches binaries to GitHub releases.
