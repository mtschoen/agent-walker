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
- [x] Add macOS support to C++ and Zig impls. Apple
      Clang's libc++ lacks std::chrono::clock_cast; cpp
      uses a portable file_clock→system_clock offset trick.
      Zig adds a parallel Darwin code path via std.c
      (libSystem) alongside the existing Linux raw-syscall
      path; build.zig links libc only on macOS targets.
      All four impls pass shared/conformance.py on macOS
      arm64 (54/54).

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
