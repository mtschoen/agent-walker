#!/usr/bin/env python3
"""Provider-aware historical agent-session search CLI.

Claude Code and Codex search are delegated to the native agent-walker binary
(installed as `claude-walker` as a back-compat alias). OpenCode search reads
its SQLite database through the sibling opencode_search module. Cost, event,
and beacon modes remain Claude-only.

The installed command is `agent-walker-search`. Use `--json` for the structured
`{hits, summary, note}` result.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

# Source checkouts keep process_safe in shared/. Installed commands use the
# sibling copy beside this script.
_SHARED_DIR = Path(__file__).resolve().parent.parent / "shared"
if _SHARED_DIR.is_dir() and str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))

from opencode_search import (
    OpenCodeSearchError,
    SearchOptions,
    search as search_opencode,
)
from process_safe import ProcessTimeout, run_captured

# The walker should never take this long unless a network root is hung; the
# CLI is supposed to degrade gracefully, so the timeout is just the safety net.
SUBPROCESS_TIMEOUT_SECONDS = 30


def _binary_candidates() -> list[Path]:
    """Discovery chain, first existing wins (per SPEC.md CLI section).

    1. $AGENT_WALKER_BINARY (exact path).
    2. $CLAUDE_WALKER_BINARY (exact path; back-compat alias for #1).
    3. ~/.claude/walker[.exe]
    4. ~/.local/bin/agent-walker[.exe]
    5. ~/.local/bin/claude-walker[.exe] (back-compat alias)
    6. PATH lookup for agent-walker / claude-walker / walker.
    """
    candidates: list[Path] = []

    override = os.environ.get("AGENT_WALKER_BINARY") or os.environ.get(
        "CLAUDE_WALKER_BINARY"
    )
    if override:
        candidates.append(Path(override))

    home = Path.home()
    for directory, stem in (
        (home / ".claude", "walker"),
        (home / ".local" / "bin", "agent-walker"),
        (home / ".local" / "bin", "claude-walker"),
    ):
        candidates.append(directory / f"{stem}.exe")
        candidates.append(directory / stem)

    for name in ("agent-walker", "claude-walker", "walker"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))

    return candidates


def _resolve_binary() -> Path:
    for candidate in _binary_candidates():
        if candidate.is_file():
            return candidate
    raise RuntimeError(
        "agent-walker binary not found. Install it (run install.sh / install.bat in the "
        "agent-walker repo), or set AGENT_WALKER_BINARY (or the legacy CLAUDE_WALKER_BINARY) "
        "to its path. Looked in $AGENT_WALKER_BINARY, $CLAUDE_WALKER_BINARY, ~/.claude/, "
        "~/.local/bin/, and PATH."
    )


def _run_search(arguments: list[str]) -> dict[str, Any]:
    """Run `walker search ... --format jsonl` and reshape the output.

    Returns {"hits": [...], "summary": {...} | None, "note": str | None}.
    `note` carries the walker's stderr (for example, the truncation hint) on
    success. A non-zero exit or subprocess timeout raises RuntimeError.

    process_safe.run_captured prevents a grandchild that inherits the stdout
    pipe on Windows from stalling the CLI beyond the timeout below.
    """
    binary = _resolve_binary()
    command = [str(binary), "search", *arguments, "--format", "jsonl"]
    try:
        completed = run_captured(
            command,
            encoding="utf-8",
            errors="replace",
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except ProcessTimeout as error:
        raise RuntimeError(
            f"walker search timed out after {SUBPROCESS_TIMEOUT_SECONDS}s; a configured "
            f"root may be unreachable. Command: {' '.join(command)}"
        ) from error

    stderr = (completed.stderr or "").strip()
    if completed.returncode != 0:
        # Walker uses exit 2 for bad input (empty pattern, bad regex/time, unknown flag).
        raise RuntimeError(
            f"walker search failed (exit {completed.returncode}): {stderr or 'no stderr output'}"
        )

    # A successful run always emits at least a summary line. Treat empty stdout
    # as a transient read failure, which can occur while a live session is
    # writing a target transcript.
    stdout = (completed.stdout or "").strip()
    if not stdout:
        raise RuntimeError(
            "walker search returned no output on a clean exit. This can happen "
            "transiently when a target transcript is being written by a live "
            f"session; retry shortly. stderr: {stderr or 'none'}. "
            f"Command: {' '.join(command)}"
        )

    hits: list[dict[str, Any]] = []
    summary: dict[str, Any] | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        if record.get("type") == "summary":
            summary = record
        else:
            hits.append(record)

    # On success, stderr is the truncation hint (or empty), not a failure.
    return {"hits": hits, "summary": summary, "note": stderr or None}


def _search_arguments(
    options: SearchOptions,
    transcript_format: Literal["claude-code", "codex"] | None = None,
) -> list[str]:
    arguments: list[str] = [options.pattern]
    if options.regex:
        arguments.append("--regex")
    if options.case_sensitive:
        arguments.append("--case-sensitive")
    if options.role != "both":
        arguments += ["--role", options.role]
    if options.since is not None:
        arguments += ["--since", options.since]
    if options.until is not None:
        arguments += ["--until", options.until]
    if options.cwd is not None:
        arguments += ["--cwd", options.cwd]
    arguments += [
        "--context",
        str(options.context_turns),
        "--limit",
        str(options.limit),
    ]
    if options.count_only:
        arguments.append("--count-only")
    if options.include_tool_blocks:
        arguments.append("--include-tool-blocks")
    if transcript_format is not None:
        arguments += ["--transcript-format", transcript_format]
    return arguments


def _hit_sort_key(hit: dict[str, Any]) -> tuple[float, str, str, int]:
    timestamp = str(hit.get("timestamp", ""))
    try:
        timestamp_value = datetime.fromisoformat(
            timestamp.replace("Z", "+00:00")
        ).timestamp()
    except ValueError:
        timestamp_value = 0.0
    return (
        -timestamp_value,
        str(hit.get("provider", "")),
        str(hit.get("session_id", "")),
        int(hit.get("line_number", 0)),
    )


def _merge_provider_results(
    results: dict[str, dict[str, Any]],
    limit: int,
    count_only: bool,
    elapsed_ms: int,
) -> dict[str, Any]:
    hits: list[dict[str, Any]] = []
    notes: list[str] = []
    summaries: dict[str, dict[str, Any]] = {}
    for provider, result in results.items():
        summary = dict(result.get("summary") or {})
        summary.setdefault("provider", provider)
        summary.setdefault("available", True)
        summaries[provider] = summary
        for hit in result.get("hits", []):
            normalized = dict(hit)
            normalized.setdefault("provider", provider)
            hits.append(normalized)
        if result.get("note"):
            notes.append(f"{provider}: {result['note']}")
    hits.sort(key=_hit_sort_key)
    provider_truncated = any(
        summary.get("truncated", False) for summary in summaries.values()
    )
    globally_truncated = len(hits) > limit
    if count_only:
        merged_hits: list[dict[str, Any]] = []
        hit_count = sum(int(summary.get("hits", 0)) for summary in summaries.values())
    else:
        merged_hits = hits[:limit]
        hit_count = len(merged_hits)
    summary = {
        "type": "summary",
        "hits": hit_count,
        "sessions_matched": sum(
            int(provider_summary.get("sessions_matched", 0))
            for provider_summary in summaries.values()
        ),
        "roots_walked": sum(
            int(provider_summary.get("roots_walked", 0))
            for provider_summary in summaries.values()
        ),
        "files_walked": sum(
            int(provider_summary.get("files_walked", 0))
            for provider_summary in summaries.values()
        ),
        "truncated": provider_truncated or globally_truncated,
        "elapsed_ms": elapsed_ms,
        "providers": summaries,
    }
    return {"hits": merged_hits, "summary": summary, "note": "; ".join(notes) or None}


def _run_agent_search(
    options: SearchOptions,
    providers: list[str] | None,
) -> dict[str, Any]:
    selected = ["claude", "codex", "opencode"] if providers is None else providers
    native_formats: dict[str, Literal["claude-code", "codex"]] = {
        "claude": "claude-code",
        "codex": "codex",
    }
    unknown = sorted(set(selected) - {*native_formats, "opencode"})
    if unknown:
        raise ValueError(f"unknown search providers: {', '.join(unknown)}")
    if not selected:
        raise ValueError("providers must not be empty")
    if options.limit < 1:
        raise ValueError("limit must be at least 1")
    if options.context_turns < 0:
        raise ValueError("context_turns must not be negative")
    started = time.monotonic()
    results: dict[str, dict[str, Any]] = {}
    failures: dict[str, str] = {}
    for provider, transcript_format in native_formats.items():
        if provider not in selected:
            continue
        try:
            results[provider] = _run_search(
                _search_arguments(options, transcript_format)
            )
        except RuntimeError as error:
            failures[provider] = str(error)
    if "opencode" in selected:
        try:
            results["opencode"] = search_opencode(options)
        except OpenCodeSearchError as error:
            failures["opencode"] = str(error)
    if not results:
        details = "; ".join(
            f"{provider}: {error}" for provider, error in failures.items()
        )
        raise RuntimeError("all selected search providers failed: " + details)
    for provider, error in failures.items():
        results[provider] = {
            "hits": [],
            "summary": {
                "type": "summary",
                "provider": provider,
                "available": False,
                "hits": 0,
                "sessions_matched": 0,
                "roots_walked": 0,
                "files_walked": 0,
                "truncated": False,
                "error": error,
            },
            "note": error,
        }
    merged = _merge_provider_results(
        results,
        options.limit,
        options.count_only,
        int((time.monotonic() - started) * 1000),
    )
    return merged


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-walker-search",
        description="Search historical Claude Code, Codex, and OpenCode sessions.",
    )
    parser.add_argument("--query", required=True, help="substring or RE2 regex")
    parser.add_argument(
        "--providers",
        nargs="+",
        choices=("claude", "codex", "opencode"),
        help="providers to search; default is all three",
    )
    for flag, help_text in (
        ("--regex", "treat query as regex"),
        ("--case-sensitive", "match case exactly"),
        ("--count-only", "return counts without hits"),
        ("--include-tool-blocks", "search tool inputs and outputs"),
        ("--json", "emit the structured search result"),
    ):
        parser.add_argument(flag, action="store_true", help=help_text)
    parser.add_argument("--role", choices=("user", "assistant", "both"), default="both")
    parser.add_argument(
        "--since", help="RFC3339 timestamp or relative value such as 7d"
    )
    parser.add_argument("--until", help="RFC3339 timestamp or relative upper bound")
    parser.add_argument("--cwd", help="project identifier or path filter")
    parser.add_argument("--context-turns", type=int, default=1)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--opencode-db", help="OpenCode database path override")
    return parser


def _format_human(result: dict[str, Any]) -> str:
    lines: list[str] = []
    for hit in result["hits"]:
        lines.append(
            " ".join(
                str(hit.get(field, ""))
                for field in ("provider", "session_id", "timestamp")
            ).rstrip()
        )
        if hit.get("snippet"):
            lines.append(str(hit["snippet"]))
    summary = result["summary"]
    lines.append(f"hits: {summary.get('hits', 0)}")
    lines.append(f"sessions_matched: {summary.get('sessions_matched', 0)}")
    if summary.get("truncated"):
        lines.append("truncated: true")
    if result.get("note"):
        lines.append(f"note: {result['note']}")
    return "\n".join(lines)


def _write_utf8(text: str) -> None:
    """Write text plus a newline as UTF-8 regardless of the stdout encoding.

    A legacy console codec (for example cp1252) cannot encode CJK or emoji, and
    JSON output must stay lossless for a UTF-8 consumer.
    """
    stream = sys.stdout
    buffer = getattr(stream, "buffer", None)
    if buffer is None:
        stream.write(text + "\n")
        return
    stream.flush()
    buffer.write((text + "\n").encode("utf-8"))
    buffer.flush()


def main(arguments: list[str] | None = None) -> int:
    parser = _argument_parser()
    namespace = parser.parse_args(arguments)
    options = SearchOptions(
        pattern=namespace.query,
        regex=namespace.regex,
        case_sensitive=namespace.case_sensitive,
        role=namespace.role,
        since=namespace.since,
        until=namespace.until,
        cwd=namespace.cwd,
        context_turns=namespace.context_turns,
        limit=namespace.limit,
        count_only=namespace.count_only,
        include_tool_blocks=namespace.include_tool_blocks,
        db_path=namespace.opencode_db,
    )
    try:
        result = _run_agent_search(options, namespace.providers)
    except (RuntimeError, ValueError) as error:
        print(f"{parser.prog}: error: {error}", file=sys.stderr)
        return 1
    if namespace.json:
        _write_utf8(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _write_utf8(_format_human(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
