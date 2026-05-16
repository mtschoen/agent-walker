# Zig Search Subcommand Port — Handoff

**Session:** 2026-05-15  
**Status:** C++ and Go implementations complete and passing conformance. Zig stub remains.  
**Next step:** Complete Zig port to reach parity with all four implementations.

## What's done

- `zig/src/search.zig` — stub that exits with error 2, dispatch wired in `main.zig`, registered in `build.zig`
- `shared/conformance.py` — does not include `zig` in `IMPLS_WITH_SEARCH` (tests skipped, exit 2 treated as skip)
- `rust/src/search.rs`, `go/search.go`, `cpp/search.cpp` — all passing 18/18 conformance tests

## What's blocking the Zig port

Zig 0.16 has significant std lib changes that broke the initial port attempts:

### 1. File I/O (`std.fs` → `std.Io`)
- `std.fs.openDir()` and `std.fs.openDirAbsolute()` removed
- `std.fs.openFileAbsolute()` removed
- Need to use `std.Io` which requires an `Io` instance with a `Dir` provider
- Cross-platform file operations must use platform-specific APIs (e.g., `std.os.linux.getdents64`, `std.os.windows.FindFirstFileW`)

### 2. Memory APIs
- `std.mem.dupe()` removed — use `alloc.dupe(T, slice)` or `std.mem.dupeAlloc(alloc, T, slice)`
- `std.mem.dupeAlloc()` signature changed (3 args: allocator, type, slice)
- `alloc.dupe()` still exists but needs the type as the second positional arg

### 3. ArrayList initialization
- Changed from `.init(alloc)` to `.empty` (zero-arg)
- `.append(alloc, item)` and `.deinit(alloc)` now require allocator parameter
- Declaration must be typed: `var list: std.ArrayList(T) = .empty;`

### 4. Time APIs
- `std.time.milliTimestamp()` removed
- Use `main.perfNow()` (wall-clock ms from session start) or platform-specific APIs
- `std.time.parseIso8601` removed — need custom ISO8601 parser (already implemented in `main.zig`)

### 5. String APIs
- `text.toLowerAlloc(alloc)` removed
- Use `std.ascii.lowerString(buf, text)` on a pre-allocated buffer
- `slice.find()` removed — use `std.mem.indexOf(T, slice, pattern)`

### 6. Other breaking changes
- `std.mem.copyForwards(u8, dst, src)` preferred over `@memcpy(dst, src)` for clarity
- Union field access restrictions in `@typeInfo()` (e.g., `.pointer` vs `.array`)

## Reference implementations

Use these for logic parity (same output, same matching, same JSONL structure):

| Lang | Path | Notes |
|------|------|-------|
| Go | `go/search.go` | Cleanest logic to port; uses `regexp` |
| C++ | `cpp/search.cpp` | Uses `simdjson` DOM API |
| Rust | `rust/src/search.rs` | Original reference |

### Key logic points to preserve
- Content extraction: handle both legacy bare strings and modern `ContentBlock` arrays
- `is_only_tool_blocks` filter: skip messages that are tool blocks unless `--include-tool-blocks`
- Role filtering: scan ALL user+assistant messages in `scanFile`, apply filter in `processFile` (context turns require both roles)
- Regex mode: if `--regex` flag, compile with `std.regs` or use `std.ascii.equalsIgnoreCase` for substring
- Context turns: `context_before` and `context_after` computed from surrounding turns in same file
- Snippet generation: character-limited, with `match_offsets` for highlighting
- JSONL output: `hit` and `summary` types with specified fields (see `SPEC.md`)
- `--count-only`: output only summary, skip hits
- `--format pretty|jsonl`: pretty for human-readable, JSONL for machine parsing

## Testing

Run conformance from repo root:
```
python shared/conformance.py zig
```

All 18 search tests should pass with hit counts matching Rust reference exactly.

Tolerance: exact hit counts, exact JSONL field presence (stripped keys per `SEARCH_HIT_STRIP_KEYS`/`SEARCH_SUMMARY_STRIP_KEYS`).

## Implementation order recommendation

1. Get the stub to compile (fix ArrayList init, use `std.mem.indexOf`, fix `lowerString`)
2. Implement file discovery using `std.Io` (this is the biggest lift)
3. Port content extraction from Go reference (simplest logic)
4. Port matching/snippet generation
5. Add arg parsing and main dispatch
6. Run conformance, iterate on failures
