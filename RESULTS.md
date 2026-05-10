# claude-walker — Comparison Results

Side-by-side numbers as implementations land. Conformance is mandatory
(±$0.01); other axes are descriptive.

## Reference baselines (Python in [schoen-claude-status](https://github.com/mtschoen/schoen-claude-status))

| Variant                                  | Median walk (ms) | Notes                                       |
| ---------------------------------------- | ---------------: | ------------------------------------------- |
| `_walk_pace_buckets` original (stdlib)   | 750              | Single-thread, `json.loads`                 |
| `_walk_pace_buckets` orjson single-thread| 524              | Drop-in `orjson` swap                       |
| `_walk_pace_buckets` orjson + 8-worker pool | **248**       | Current shipping version                    |

Live fleet snapshot at time of measurement: 1462 JSONL files (~500 MB on
disk); 295 survive the weekly mtime filter (~143 MB); 129 distinct session
groups. 32-core box.

## Native implementations

| Lang | Conform | Median walk (ms) | Binary size | Build time | LoC | Build cmd                              |
| ---- | :-----: | ---------------: | ----------: | ---------: | --: | -------------------------------------- |
| Rust |   ✓     | **139**          | TBD         | 12s cold   | 230 | `cargo build --release`                |
| Go   |    -    |                — |           — |          — |   — | (not yet implemented)                  |
| C++  |    -    |                — |           — |          — |   — | (not yet implemented)                  |
| Zig  |    -    |                — |           — |          — |   — | (not yet implemented)                  |

Rust observations (first pass, naive serde_json + rayon):

- Cold build: 21 deps + 12s with full LTO and `codegen-units=1`.
- ~2x faster than the Python parallel walker.
- Per-line `serde_json::from_str` likely the bottleneck; switching to
  `simd-json` or `sonic-rs` is the obvious next swing.
- Rayon adds ~150KB to the binary; could swap for `std::thread` if the
  comparison wants to highlight binary size.

## Comparison axes (to fill in as implementations land)

- **Median walk wall-clock** on the live fleet (3 warm runs, take min).
- **Conformance**: pass / fail against `shared/corpus/`.
- **Binary size**: stripped release binary in bytes.
- **Cold build time**: from `cargo new` / `go mod init` / etc., without
  cached deps.
- **Lines of code**: source file LoC excluding tests and build config.
- **Ergonomics**: one-line subjective note per language.
