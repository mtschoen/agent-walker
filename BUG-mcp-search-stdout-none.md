# BUG: MCP `claude_walker_search` crashes with `'NoneType' object has no attribute 'splitlines'`

**Status:** fixed 2026-06-08 — `mcp/server.py` now guards `stdout`, decodes
explicitly as UTF-8 (`errors="replace"`), and raises a descriptive `RuntimeError`
when a clean exit yields no output; regression tests in `mcp/test_server.py`. See
"Resolution" at the end.
**Component:** `mcp/server.py` (the FastMCP shim), not the native binary.
**Severity:** medium — intermittently makes `search` unusable from the MCP tool, with a
misleading error that gives the caller no idea what went wrong.

## Symptom

Calling the `claude_walker_search` MCP tool fails with:

```
Error executing tool claude_walker_search: 'NoneType' object has no attribute 'splitlines'
```

Logged in `~/.claude-walker-mcp.log` as:

```json
{"event": "error", "tool": "claude_walker_search", "duration_ms": 37,
 "error_type": "AttributeError", "error": "'NoneType' object has no attribute 'splitlines'"}
```

It is **intermittent**. In the session that found it, these calls behaved as follows:

| Call | Result |
| ---- | ------ |
| `count_only=true` (any pattern) | ✅ works |
| `role="user"`, `limit=5`, `context_turns=0` (1 hit) | ✅ works |
| `role="assistant"`, `context_turns=0` | ❌ crash |
| `role="both"`, `context_turns=1` | ❌ crash |

## Root cause (immediate)

`mcp/server.py:172`:

```python
for line in completed.stdout.splitlines():   # <-- completed.stdout is None here
```

The adjacent line 163 already guards the sibling stream —
`stderr = (completed.stderr or "").strip()` — but **stdout is dereferenced
unguarded**. So whenever `subprocess.run(...).stdout` comes back `None`, the shim
raises `AttributeError` instead of degrading gracefully. The author clearly knew
these streams can be `None` (hence the `or ""` on stderr) and just missed stdout.

## Why stdout is `None` (the real trigger)

The native binary is **not** at fault. Running the exact command the shim builds
for a "crashing" call, directly against the installed binary
(`~/.local/bin/claude-walker.exe`):

```
claude-walker search "nonexistent-pkg" --role assistant --context 0 --limit 5 \
    --include-tool-blocks --format jsonl
```

succeeds: exit 0, ~3.4 KB of valid JSONL on stdout, plus a truncation hint on
stderr. So the binary returns fine.

The crash correlated with the **target session `.jsonl` being actively written by
another live Claude session that had not yet flushed/closed the file**. When that
session later flushed, re-running the identical query succeeded. Leading
hypothesis: under that transient condition, `subprocess.run(..., capture_output=True,
text=True)` on Windows (launched via `uv run --python 3.13`, see the `claude-walker`
entry in `~/.claude.json`) returned a `CompletedProcess` with `returncode == 0` but
`stdout is None`. The error is `AttributeError`, **not** `UnicodeDecodeError`, so it is
not a decode-strictness failure — stdout genuinely came back `None`, which the
`returncode != 0` check on line 164 does not catch.

Note the shim uses `text=True` with **no explicit `encoding=`**, so on Windows it
decodes the binary's UTF-8 JSONL with the locale codec (cp1252). That is a separate
latent fragility worth fixing in the same pass even though it is not the proven
trigger here.

## Suggested fix

1. **Guard stdout like stderr** (the minimal, correct-regardless fix):

   ```python
   stdout = completed.stdout or ""
   ...
   for line in stdout.splitlines():
   ```

2. **Decode explicitly as UTF-8** so the shim matches what every impl emits and
   stops depending on the Windows locale codec:

   ```python
   completed = subprocess.run(
       command, capture_output=True, encoding="utf-8", errors="replace",
       timeout=SUBPROCESS_TIMEOUT_SECONDS,
   )
   ```

3. **Surface a real error instead of an NPE** — if `returncode == 0` but stdout is
   empty/None while stderr is non-empty, raise a `RuntimeError` that includes stderr
   and the command, so the caller learns *why* rather than seeing
   "'NoneType' object has no attribute 'splitlines'".

## Repro / verification for the fresh session

- Tail `~/.claude-walker-mcp.log` for the `error_type: AttributeError` records.
- The intermittent trigger is hard to reproduce on demand (it needs a session file
  mid-write). Easier path: add a unit test that feeds the parse loop a
  `CompletedProcess(returncode=0, stdout=None, stderr="...")` and asserts the shim
  does not raise `AttributeError`.
- After fixing, confirm `claude_walker_search` with `role="assistant"` /
  `context_turns=1` returns hits instead of crashing.

## How it was found

While debugging an unrelated mystery in `C:\UnitySrc\xr3` (stray `PackageOnboarder`
`ONBOARDING-REPORT-*.md` files), `claude_walker_search` was used to find which
session generated them. The search kept crashing on the render path; the creator
turned out to be a still-open session in `C:\UnitySrc\U-Assistant` whose transcript
had not been flushed to disk yet — the same "file mid-write" condition that triggers
this bug.

## Independent confirmation (2026-06-08, second session)

Re-validated from a separate U-assistant session while chasing the same "live
session is invisible to walker" symptom. A tempting alternative theory — that
discovery relies on `sessions-index.json` and skips active sessions — was checked
against the code and **ruled out**:

- **Discovery is index-free.** `go/search.go::searchDiscoverFiles` globs
  `<root>/<slug>/*.jsonl` via `os.ReadDir` and never consults
  `sessions-index.json` (no source file references it). An *active* session's
  transcript is therefore fully discoverable — this is not a discovery/indexing
  problem.
- **The scanner tolerates mid-write files.** `searchScanFile` skips any line that
  fails to JSON-parse (`sonic.Unmarshal ... continue`), so a partial trailing
  line in a file being written is dropped while completed turns still match.

So the only thing between a caller and a live session's transcript is the shim's
unguarded `completed.stdout` (line 172). The root cause and suggested fix above
are correct and sufficient; no change is needed to the binary or to discovery.

## Resolution (2026-06-08)

Applied all three suggested fixes in `mcp/server.py::_run_search`:

1. `subprocess.run(..., encoding="utf-8", errors="replace")` instead of bare
   `text=True`, so the shim no longer depends on the Windows locale codec.
2. `stdout = (completed.stdout or "")` before parsing — the original NPE site.
3. Because a successful run always emits at least a summary line, empty stdout on
   exit 0 is now raised as a descriptive `RuntimeError` (mentions the live-session
   race + includes stderr and the command) rather than silently returning zero
   hits or throwing `AttributeError`.

Regression coverage in `mcp/test_server.py` (5 tests): the `stdout=None` case now
asserts a `RuntimeError` (not `AttributeError`), plus normal hit/summary parsing,
zero-hits-via-summary, and non-zero-exit paths. Run:
`uv run --python 3.13 --with mcp --with pytest python -m pytest mcp/test_server.py`.
