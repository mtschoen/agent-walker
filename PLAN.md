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

## Inbox

### Port idle-exclusion in `bias_factor` from Rust to C++/Go/Zig

Rust impl (`rust/src/beacons.rs`) now subtracts user-idle time from
`bias_factor`'s `actual_elapsed`. C++/Go/Zig still ship the old
wall-clock-only formula. `install.bat` builds C++, so until C++ has
parity the next `install.bat` reverts live behavior to old. As a
stop-gap, this session left the Rust binary at
`~/.local/bin/claude-walker.exe`.

**The change.** For each begin/end pair in a session group, walk the
JSONL events and sum gaps that immediately precede a *real* user prompt
(`type: "user"` entries whose `message.content` is NOT a tool_result
block — tool_results are agent-active time waiting on tool execution,
not user-idle). Subtract that sum from `end_ts - begin_ts` to get
`active_elapsed`. Then `bias_factor = median(active_elapsed / begin_eta)`.

Pair JSON now exposes three fields (SPEC.md updated):
- `actual_elapsed` — wall clock, unchanged semantics
- `idle_excluded` — subtracted seconds
- `active_elapsed` — actual minus idle

**Algorithm.**
1. Walk session JSONL once, collect `(timestamp, is_real_user)` for every
   entry. `is_real_user` is true ONLY if `type: "user"` AND content does
   NOT contain any `tool_result` block. Bare-string content counts as a
   real user prompt.
2. For each begin/end pair, iterate the sorted event list. For each event
   with `is_real_user == true`, the preceding gap = `event[i].ts -
   event[i-1].ts`, clipped to `[begin_ts, end_ts]`. Sum those gaps.
3. Median is the sample median (even-count = mean of two middles).

**Two gotchas the Rust port hit:**
1. `message.content` is sometimes a bare string (older user-prompt format)
   instead of a Vec. Strict typed deserialization (e.g. Rust serde's
   `Vec<ContentBlock>`) silently fails the WHOLE entry, dropping ~10% of
   real user prompts. Fix: parse `content` as an untyped value first,
   then extract structure as needed.
2. `type: "user"` is dominated by tool_result entries (97/112 in one real
   session). Forgetting to filter them gives wildly wrong idle counts.
   The wrong direction matters: counting tool_results AS idle = idle
   over-counted = active under-counted = bias too LOW. Not counting them
   at all = wall == active = idle exclusion is a no-op. The first was
   the bug the original Python prototype hit.

**Reference impl.** `rust/src/beacons.rs`:
- `Entry.entry_type` (added `#[serde(rename = "type")]`)
- `Message.content: Option<Value>` (was `Option<Vec<ContentBlock>>`)
- `extract_text(&Value)` (rewritten from `&[ContentBlock]`)
- `user_content_is_tool_result(content: Option<&Value>) -> bool`
- `collect_session_events_in_path` (returns beacons + sorted events)
- `compute_idle_in_window(events, lo, hi) -> f64`
- `run_history` uses `collect_session_events_in_path`, calls
  `compute_idle_in_window`, pushes `(begin_eta, active)` to `pairs`,
  carries `(wall, idle, active)` in `pair_meta` for JSON output

**Verification.**
- `cargo build --release` (or equivalent) → `python shared/conformance.py`.
  Existing fixture (`cross_session_pairs`) has no user events, so the
  expected `bias_factor=1.25` stays correct under either old or new
  formula. Conformance must stay green.
- Live cross-impl check: run each impl against your own corpus,
  `<impl>/walker beacons-history --period 1209600 --win-start 0`. Rust
  reports ~1.69× on the past 14d (varies as fresh sessions accumulate);
  other impls should match within ±0.05× (tie-breaking on even-count
  medians can drift slightly).
- Diagnostic harness:
  `~/skills-dev/.claude/scripts/analyze_bias_correction.py` runs the
  Python prototype of this algorithm at multiple idle thresholds (0/60/
  120/300s). Useful for sanity-checking each impl's median against the
  prototype's median. Gitignored under skills-dev/.claude/.

**Suggested order:** C++ first (unblocks `install.bat`), then Go and Zig.
Each port lands as its own commit.

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
