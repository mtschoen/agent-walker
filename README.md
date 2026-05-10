# claude-walker

Native pace-walker for Claude Code session JSONLs, implemented side-by-side
in **Rust**, **C++**, **Zig**, and **Go** as a language comparison.

## What it does

Walks `~/.claude/projects/**/*.jsonl` (parent transcripts + their
`subagents/agent-*.jsonl` siblings), filters to a time range, dedupes
assistant turns by `message.id`, and prices each turn against the canonical
Anthropic per-MTok rate table. Emits two dollar totals on stdout.

Drop-in optional speedup for [schoen-claude-status](https://github.com/mtschoen/schoen-claude-status)
(the Python statusline does this in ~250ms with an `orjson` +
`ProcessPoolExecutor` walk; the goal here is **30-80ms**).

## Why four languages

The work is small, well-defined, CPU-bound, parallelizable, and
performance-sensitive — a tight benchmark for "what does it actually feel
like to ship a fast CLI in $LANG." Comparison axes are tracked in
[`RESULTS.md`](RESULTS.md): runtime, binary size, lines of code, build
time, distribution friction.

## Layout

```
shared/
  corpus/        JSONL fixtures with expected outputs (the ground truth)
  conformance.py runs each binary, asserts ±$0.01 agreement
  bench.py       times each binary on the live ~/.claude/projects fleet
rust/            cargo, simd-json or sonic-rs
go/              go modules, encoding/json or sonic-go
cpp/             cmake, simdjson
zig/             build.zig, std.json or hand-rolled
```

See [`SPEC.md`](SPEC.md) for the CLI contract every implementation must
honor and the correctness rules they all share.

## Status

Early — see [`PLAN.md`](PLAN.md). Spec + conformance harness first; Rust
is the first reference implementation; the others land behind it.
