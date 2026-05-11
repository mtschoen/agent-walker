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
