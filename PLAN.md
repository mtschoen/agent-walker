# claude-walker — Plan

## Inbox

- [ ] Build the conformance corpus: synthesize JSONL fixtures covering the
      parent ↔ acompact dedup case, malformed lines, missing timestamps,
      mixed model families, and an mtime-pruned file. Lock expected
      outputs in `shared/corpus/expected.json`.
- [ ] `shared/conformance.py`: runs `<binary> --period --win-start
      --projects-root shared/corpus` against each language; asserts
      `(trailing_usd, window_usd)` matches expected to ±$0.01.
- [ ] `shared/bench.py`: times each binary against the live
      `~/.claude/projects` fleet, 3 warm runs each, prints a side-by-side
      table.
- [ ] Rust implementation (`rust/`).
- [ ] Go implementation (`go/`).
- [ ] C++ implementation (`cpp/`) — needs VS Developer environment.
- [ ] Zig implementation (`zig/`) — needs `zig` install (winget).
- [ ] `RESULTS.md`: comparison table (runtime, binary size, LoC, build
      time, install friction, ergonomics one-liner per language).
- [ ] Wire optional native-walker detection into
      [schoen-claude-status](https://github.com/mtschoen/schoen-claude-status)'s
      `_walk_pace_buckets` once the winning implementation is chosen.
