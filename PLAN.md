# claude-walker — Plan

## Done

- [x] Conformance corpus + harness (`shared/corpus/`, `shared/conformance.py`)
- [x] Live-fleet bench (`shared/bench.py`)
- [x] Rust impl (`rust/`)
- [x] Go impl (`go/`)
- [x] C++ impl (`cpp/`)
- [x] Zig impl (`zig/`)
- [x] `RESULTS.md` comparison table

## Inbox

### Handoff: upgrade C++ to simdjson and re-bench

Next-session task. Self-contained.

**Why.** Current C++ uses `nlohmann/json` v3.11.3 and lands at 402ms
median — slowest of all 5 implementations including Python parallel.
Per-line full-DOM allocation dominates. The original C++ author chose
nlohmann because FetchContent wiring was turnkey on Windows (single-include
zip), and judged simdjson's static-library build not worth the
incremental setup at the time.

**Goal.** Rebuild on simdjson's on-demand API. Conjecture: lands at
80-150ms (matches or beats Rust). Even if it lands at 200ms it confirms
the diagnosis "JSON parser dominates language choice."

**What to change.**
- `cpp/CMakeLists.txt`: swap `FetchContent_Declare(nlohmann_json ...)`
  for simdjson. Two install paths to try, in order:
  1. `FetchContent_Declare(simdjson GIT_REPOSITORY https://github.com/simdjson/simdjson.git GIT_TAG v3.x.y)` — pulls source, builds the static lib in-tree. Adds maybe 10-20s to clean build.
  2. If FetchContent is slow / flaky: `find_package(simdjson)` after
     installing via vcpkg (`vcpkg install simdjson:x64-windows`).
  Stick with FetchContent unless it breaks — keeps the repo
  zero-prerequisite.
- `cpp/main.cpp`: rewrite the per-line parse path to use simdjson's
  on-demand API:
  ```cpp
  simdjson::ondemand::parser parser;
  auto doc = parser.iterate(line_padded);
  // navigate doc["message"]["role"] etc., extract only the fields we need
  ```
  Important: simdjson on-demand requires SIMDJSON_PADDED input
  (typically `simdjson::padded_string` per line, or a single big
  `simdjson::padded_string` for the whole file with `iterate_many`).
  `iterate_many` is the right call — single allocation per file, then
  iterate per-document.
- Keep all other logic (grouping, dedup, mtime filter, pricing) identical.
  The conformance corpus is the safety net.

**How to verify.**
1. `cd cpp/build && cmake --build . --config Release`
2. `python C:/Users/mtsch/claude-walker/shared/conformance.py cpp` —
   must print " OK ".
3. `python C:/Users/mtsch/claude-walker/shared/bench.py --runs 5 cpp` —
   median should drop from 402ms toward Rust's 115ms.
4. Update `RESULTS.md` with the new median, binary size (likely grows
   from 175 KB), and a one-line note like "rebuilt on simdjson on-demand
   — Nx faster than nlohmann/json on this workload."

**Possible gotchas.**
- simdjson requires C++17 minimum and prefers C++20. CMakeLists.txt
  already targets C++20 — should be fine.
- On Windows + MSVC, simdjson detects AVX2/etc. at build time. If the
  binary needs to run on older CPUs, force a baseline like
  `set(SIMDJSON_TARGET_VERSION "haswell")` in CMake.
- If the binary balloons past 1 MB, it may be dragging in the runtime
  detection paths for all CPU architectures — see simdjson's CMake
  options for trimming.

### Handoff: upgrade Go to sonic-go (or json-iterator) and re-bench

Next-session task. Self-contained.

**Why.** Current Go uses stdlib `encoding/json`, lands at 373ms median.
The original Go author flagged that swapping to `github.com/bytedance/sonic`
or `github.com/json-iterator/go` would be a near-drop-in change closing
the gap to ~1.2-1.5x of Rust (so ~140-180ms).

**Goal.** Rebuild on sonic-go (preferred — SIMD-based, faster on small
objects) with json-iterator-go as fallback if sonic has Windows/ARM
issues. Conjecture: lands at 130-180ms.

**What to change.**
- `cd C:/Users/mtsch/claude-walker/go && go get github.com/bytedance/sonic`
- `go/main.go`: swap `encoding/json` for sonic. The struct-based
  unmarshal path stays identical:
  ```go
  import "github.com/bytedance/sonic"
  // replace json.Unmarshal(line, &entry) with sonic.Unmarshal(line, &entry)
  ```
  Sonic's API is intentionally `encoding/json`-compatible at the call
  site so the diff should be 1-2 lines.
- Tags-based fallback: if sonic blows up at runtime (it has CPU feature
  detection), wrap the import behind a build tag and ship a stdlib
  fallback file. Likely not needed on this Windows/x64 machine.

**How to verify.**
1. `cd go && go build -o walker.exe .`
2. `python C:/Users/mtsch/claude-walker/shared/conformance.py go` —
   must print " OK ".
3. `python C:/Users/mtsch/claude-walker/shared/bench.py --runs 5 go` —
   median should drop from 373ms.
4. Update `RESULTS.md` with the new median (binary size will grow from
   3.3 MB by maybe 1-2 MB).

**If sonic doesn't pan out.** Try `github.com/json-iterator/go`
(`jsoniter.ConfigFastest.Unmarshal`). Pure-Go, no SIMD, no CGO.
Slightly slower than sonic but fewer surprises.

**Stretch goal.** If Go still trails Rust by >2x after sonic, look at
the goroutine fan-out — `runtime.GOMAXPROCS` and the worker count cap.
Author's pool was 8 workers (matching Rust); that should be right but
worth confirming with `--workers N` flag if added.

### Standing item

- [ ] Wire optional native-walker detection into
      [schoen-claude-status](https://github.com/mtschoen/schoen-claude-status)'s
      `_walk_pace_buckets`: if `~/.claude/walker` exists and is
      executable, subprocess it; on any failure fall back to the
      existing Python parallel walker. Stays optional — no install
      friction added to schoen-claude-status. Wait until C++ and Go
      upgrades are in so the "winner" choice is informed.
