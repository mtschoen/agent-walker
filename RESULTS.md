# claude-walker — Comparison Results

Side-by-side numbers from running the same workload through 4 native
implementations + the Python reference. Every implementation passes the
shared conformance corpus bit-exact (±$0.01) and produces identical
totals on the live fleet.

## Setup

- Live fleet: 1462 JSONL files (~500 MB on disk); 295 survive the weekly
  mtime filter (~143 MB); 129 distinct session groups.
- 32-core box (Windows 11). 5 warm runs each via `shared/bench.py
  --runs 5`; reporting median wall-clock.

## Headline numbers

| Lang   | Median | Range       | vs Rust | vs slowest | LoC | Binary | Build  |
| ------ | -----: | ----------- | ------: | ---------: | --: | -----: | -----: |
| Rust   | **115ms** | 108-128  | 1.00x   | 3.50x      | 230 | TBD    | 12s    |
| Zig    |  142ms | 133-167     | 1.23x   | 2.83x      | 601 | 917 KB | TBD    |
| Python |  356ms | 325-372     | 3.10x   | 1.13x      |  ~80| n/a    | n/a    |
| Go     |  373ms | 365-380     | 3.24x   | 1.08x      | 409 | 3.3 MB | TBD    |
| C++    |  402ms | 382-473     | 3.50x   | 1.00x      | 545 | 175 KB | TBD    |

Python reference is the orjson + 8-worker `ProcessPoolExecutor` walker
that ships in [schoen-claude-status](https://github.com/mtschoen/schoen-claude-status).
Original single-thread `json.loads` baseline (not in this table) was
~750ms.

## Surprises

**The fastest language is Rust, but the slowest isn't Python — it's C++.**
And the gap isn't subtle: C++ comes in 3.5x slower than Rust, behind
Python's parallel walker. The cause isn't C++ the language; it's
`nlohmann/json`'s DOM-parse cost. Every line allocates a full JSON tree
then we throw 99% of it away. Swapping to `simdjson`'s on-demand API
would close most of the gap, at the cost of build complexity that the
agent author judged not worth it for the comparison's first pass.

**Zig is essentially tied with Rust** despite Zig 0.16 having just
landed with a complete I/O subsystem rewrite. The agent had to bypass
`std.io` entirely and call Win32 externs directly because the new
`std.Io` context-passing pattern wasn't viable mid-port. Lesson: pick
your Zig version carefully right now.

**Go's stdlib `encoding/json` is the bottleneck for Go**, not goroutines
or anything Go-specific. The author flagged that `bytedance/sonic` or
`json-iterator/go` would close the 3x gap to ~1.2x. Pure stdlib was a
deliberate constraint.

**JSON parser choice dominates language choice.** The two fast
implementations both use parsers with no per-line allocation in the hot
path (`serde_json` with typed structs in Rust; `std.json` with manual
field extraction in Zig). The two slow ones use convenient DOM parsers
(`encoding/json` reflection, `nlohmann/json` allocation). Rebuilding C++
on simdjson and Go on sonic-go would likely re-rank everything.

**Python is the third-fastest implementation here**, beating both Go and
C++. Process-pool parallelism + `orjson`'s SIMD parser scales well on a
32-core box; the GIL never bites because work is split across processes.

## Per-implementation notes

### Rust (`rust/`)

- `serde_json` with typed `#[derive(Deserialize)]` structs (no DOM, no
  reflection)
- `rayon::par_iter` for the parallel reduce
- Clean cold build, ~21 deps, full LTO + `codegen-units=1`
- One-line `par_iter().reduce()` is the most concise parallel pattern
  of any of the four

### Zig (`zig/`)

- Zig 0.16, `winget install zig.zig`
- `std.json` dynamic value parsing, manual field extraction
- I/O via direct Win32 externs (NtCreateFile, etc.) because `std.io` was
  removed in 0.16 and the new `std.Io` context-passing model didn't fit
- 601 LoC reflects the Win32 binding surface; without that it'd be
  closer to Rust's count
- Compiler error messages were excellent throughout the port

### Python parallel reference (in schoen-claude-status)

- `orjson` for parse, falling back to stdlib `json`
- 8-worker `ProcessPoolExecutor`, work-unit = one session group
- Per-session-group dedup catches the parent ↔ acompact-subagent
  collision pattern (146 real instances in this corpus)
- Reduced from 750ms → 248ms by this refactor; the bench above re-measured
  at 356ms because it ran with C++/Go/Zig benches sharing CPU

### Go (`go/`)

- Stdlib only: `encoding/json`, `flag`, `sync.WaitGroup`, `filepath`
- `filepath.Glob` doesn't support `**` so subagent discovery is two
  manual `ReadDir` walks
- 3.3 MB binary (Go runtime baseline)
- Code feels clean — closest to "obvious" stdlib code of the four

### C++ (`cpp/`)

- C++20, MSVC via `cmake -G "Visual Studio 17 2022"` (auto-discovers
  `cl.exe`, no PATH manipulation)
- `nlohmann/json` v3.11.3 via FetchContent (single-include zip)
- Custom `std::thread` pool (no rayon-equivalent in the stdlib)
- 175 KB binary — the smallest by far
- Author flagged `nlohmann` as the perf bottleneck and `simdjson` as
  the obvious fix

## What's next

- [ ] Rebuild C++ on `simdjson` and Go on `sonic-go` — does the ranking
      change? My guess: C++ overtakes Rust by 10-30%; Go closes to
      ~150ms.
- [ ] Tune Rust further: try `simd-json` instead of `serde_json` (the
      tradeoff for small per-line objects is mixed; worth measuring).
- [ ] Wire whichever wins (probably Rust as-is) back into
      schoen-claude-status's `_walk_pace_buckets` as an optional
      detection: if `~/.claude/walker` exists and is executable, use
      it; otherwise the existing Python parallel walker stands.
- [ ] CI matrix that builds all 5 platforms (`win/mac/linux × x86/arm64`)
      for the chosen winner and attaches binaries to GitHub releases.
