# Compressed archive roots for agent-walker

Date: 2026-09-02. Status: implemented by [pull request 47](https://gitea.fleet.sticktoitive.net/schoen/agent-walker/pulls/47); `SPEC.md` is the current contract.

## Goal

Let every agent-walker mode (cost, events, beacons, search) read the
zstd-compressed transcript archive that replica writes, so Claude Code's
`cleanupPeriodDays` cull can drop below its current 365 days without any
session becoming unsearchable. A compressed session is inflated in memory
just before the walk; nothing is written to disk.

Owner decisions captured during brainstorming (2026-09-02):

- Every mode reads the archive, not just search.
- Archive location is discovered implicitly, with a config override.
- zstd is the only compressed format. Replica stops falling back to lzma
  or gzip and fails loudly if the zstandard module is missing.

## Non-goals

- Streaming decompression. Transcripts are single-digit MB; whole-file
  inflate matches how all four impls already read live files.
- Archived Codex or OpenCode transcripts. Replica namespaces non-Claude
  harnesses under `<archive>/<harness>/<hostname>/`; those subtrees contain
  no `.jsonl.zst` at the claude-code layout depth and are skipped
  harmlessly. Codex roots keep their existing discovery.
- Changing the cull period itself. That is an owner setting change gated on
  the follow-through in the last section.

## Archive layout (what replica writes)

```
<archive_root>/<hostname>/<slug>/<session_id>.jsonl.zst
<archive_root>/<hostname>/<slug>/<session_id>/subagents/agent-<id>.jsonl.zst
```

Default `<archive_root>` is `~/claude-archive` (replica's
`archive_root_convention`). `<hostname>/<slug>/...` mirrors the live
`~/.claude/projects/<slug>/...` layout exactly one directory deeper. Each
host's archive is replicated to fleet peers, so one machine's archive root
holds every host's history (today: `chonkers/` and `llamabox/`).

## Root model

A third transcript root format, `claude-archive`, joins `claude-code` and
`codex`. An archive root at `<path>` contributes one claude-code layout root
per immediate subdirectory `<path>/<hostname>/`. Subdirectories are taken as
found; no hostname validation. A missing or unreadable `<path>` is skipped
with a stderr diagnostic, never an error (same rule as unreachable extras).

Archive roots enter the effective root set three ways:

1. **Implicit.** When `--projects-root` is omitted, `~/claude-archive` is
   added as a `claude-archive` root if the directory exists. This holds for
   every mode, mirroring how search adds `~/.codex/sessions`. Supplying
   `--projects-root` keeps single-primary behavior and adds no archive root.
2. **CLI.** `--archive-root <path>`, repeatable, on every subcommand. Adds a
   `claude-archive` root. Conformance uses this to point at fixtures.
3. **Config.** A tagged object in `~/.claude/walker-roots.json`:
   `{"path": "...", "format": "claude-archive"}`. `--no-config` skips it as
   it skips every config extra.

Effective order: primary, CLI extras, config extras, then archive roots in
the order implicit, CLI, config. Dedup by canonical path applies to the
expanded `<path>/<hostname>/` roots, not to the archive root itself, so an
archive root that overlaps a live extra root (an SMB mount of another host's
projects tree) is handled by session-level dedup below rather than by path.

`~` resolution is unchanged (USERPROFILE first on Windows, HOME elsewhere).

## Discovery

Within any claude-code layout root, live or expanded from an archive, a
parent qualifies when its name is `<session_id>.jsonl` or
`<session_id>.jsonl.zst`, and a subagent when it is `agent-<id>.jsonl` or
`agent-<id>.jsonl.zst` under `<session_id>/subagents/`. The session id is the
name with both suffixes stripped. Any other suffix is ignored; each root
reports one stderr line naming how many files it skipped, and only when that
count is non-zero.

Every discovered file carries a session key:

- parent: `(slug, session_id)`
- subagent: `(slug, session_id, agent_id)`

Discovery runs root by root in effective order and keeps the first file to
claim a key. Later duplicates are dropped before any file is opened. Live
roots precede archive roots, so a session present in both is read once from
its live `.jsonl`, and no archive is inflated only to be discarded by the
existing `message.id` dedup. Two archive roots offering the same session
(the same host replicated twice) resolve the same way: first root wins.

The mtime prune (`mtime < min(now - period, win_start)` skips the file)
applies to a `.jsonl.zst` exactly as to a `.jsonl`. Replica stamps archive
files with the source transcript's mtime (see the replica section), so the
prune sees session time, not compression time.

## Reading

A file whose name ends in `.zst` is read whole into memory and decoded as a
single zstd frame sequence into a byte buffer; the existing per-line parser
then runs on that buffer instead of the raw file bytes. A file that fails to
decode (truncated, not zstd, out of memory) is skipped with one stderr line
naming the path; the walk continues. This mirrors the existing "unreadable
file errs on the side of exclusion, malformed line is skipped silently"
posture: a bad archive file must never fail a statusline tick.

Decoders per implementation:

| Lang | Decoder | Dependency shape |
| ---- | ------- | ---------------- |
| Rust | `zstd` crate (`zstd::decode_all`) | Cargo dependency; builds libzstd via `cc` |
| C++  | libzstd `ZSTD_decompress` with `ZSTD_getFrameContentSize`, falling back to streaming decode when the size is unknown | CMake `FetchContent` of facebook/zstd, static, same pattern as simdjson |
| Go   | `github.com/klauspost/compress/zstd` (`Decoder.DecodeAll`) | pure Go module dependency |
| Zig  | `std.compress.zstd` on Zig 0.16 | standard library |

Rust is the reference implementation, as for every walker feature. The Zig
0.16 `std.compress.zstd` API (new `Io.Reader` interface) is the one
implementation risk; if it cannot decode replica's frames (which carry the
content size and use the default window), the Zig port vendors a decoder
rather than changing the archive format.

## Per-mode behavior

No mode-specific rules. Cost, events, beacon, and search all walk the same
effective root set and apply the same discovery, dedup, prune, and read
path. Consequences worth stating:

- Cost mode on a statusline tick inflates only archive files that survive
  the mtime prune and are not shadowed by a live file. With a 365-day cull
  that is close to zero files; as the cull shortens, it grows to "sessions
  culled inside the trailing window", which is still bounded by the window.
- Search over history now covers every host in the archive, not only the
  local live tree and whichever peer trees are mounted.
- `beacons-latest` looks up a single session id; the archive is consulted only if
  no live root claims that id. `beacons-history` walks the window like cost.

Search hit output gains nothing new. `file_path` already reports the path
read; for an archive hit that is the `.jsonl.zst` path, and `host_root` is
the expanded `<archive>/<hostname>` root.

## Replica changes (land first, separate PR in schoen-lab)

1. `compress.py`: `compressor_for` supports only `"zstd"`. Importing
   zstandard is unconditional; a missing module raises ImportError from the
   hook with the module name in the message. The `compression` config value
   keeps its name; any value other than `zstd` is a config error.
2. `transcripts.py`: after writing an archive file, set its mtime (and
   atime) to the source's `st_mtime` with `os.utime`. The manifest already
   records that value per file.
3. New `replica transcripts restamp` subcommand: for every manifest entry
   whose archive file exists, set the archive file's mtime from the manifest.
   One-off repair for archives produced before change 2 (every `replica seed`
   backfill carries a seed-time stamp today). Idempotent; prints the count
   restamped.

Existing archives need no re-encoding: they are already zstd.

## Cross-implementation rollout

Follows the agent-walker AGENTS.md procedure:

1. `SPEC.md`: add `claude-archive` to the Roots section, the `--archive-root`
   flag to every subcommand's usage block, the `.jsonl.zst` suffix rule and
   session-key dedup to Discovery, and the decode-failure rule to Filters.
2. Fixtures: `shared/generate_search_corpus.py` gains an archive scenario
   set (`shared/corpus/search_archive/`), and `shared/generate_corpus.py`
   gains cost scenarios with an archive root. Generators write `.jsonl.zst`
   with the Python zstandard module (already a replica dependency in the
   workspace venv). Scenarios cover: archive-only session, live-plus-archive
   duplicate (live wins, counted once), two hosts in one archive root,
   subagent under an archive, corrupt `.zst` skipped, unrelated suffix
   ignored, `--projects-root` given (no implicit archive), config-tagged
   archive root, mtime prune on an archive file.
3. `shared/conformance.py`: pass `--archive-root` for the new scenarios and
   assert the pinned expected output for all four impls (fails first).
4. Rust implementation, then C++, Go, Zig in parallel worktree lanes.
5. `--no-ff` merges, full conformance on the merged tree, coverage back to
   100 percent per impl via `shared/coverage.py`.

The MCP server (`mcp/server.py`) needs no change: it shells out to
`walker search` without `--projects-root`, so the implicit archive root
applies. Its 30 second subprocess timeout is the budget to check in the
smoke test against the real 784 MB archive.

## Testing

- Conformance scenarios above are the contract; every impl must match the
  pinned expected files.
- Rust unit tests for `walker_roots.rs` (tag parsing, implicit root
  presence and absence, `--archive-root` ordering) and for the session-key
  dedup.
- Replica: `test_compress.py` drops the fallback cases and asserts the
  ImportError message; `test_transcripts.py` asserts the archive mtime
  equals the source mtime after compress and after `restamp`. Clock is
  never measured; mtimes are set explicitly on fixture files.
- Smoke: `walker search <known phrase from a culled session>` returns a hit
  whose `file_path` ends in `.jsonl.zst`, and a cost-mode tick over the real
  tree stays within its current wall-clock (measure before and after).

## Cull follow-through (after this lands)

Lowering `cleanupPeriodDays` is safe only when every session is archived
before Claude Code deletes it. The SessionEnd hook covers sessions that end
normally; a session that dies without SessionEnd is captured only by a later
`replica run`. No periodic `replica run` is scheduled on either machine
today. Before trimming the cull: schedule `replica run` daily on both hosts
(scheduled task on chonkers, systemd timer on llamabox), run `replica
transcripts restamp` once per host, verify a culled-only session is
searchable, then lower the period.
