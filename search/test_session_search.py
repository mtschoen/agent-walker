"""Tests for the session-search CLI and provider result shaping.

Run from the repo root:
    python -m pytest search/test_session_search.py

Native binary calls are faked, so the suite needs neither a compiled walker nor
live sessions. The process boundary returns process_safe.ProcessResult and
raises process_safe.ProcessTimeout, matching the production wrapper's contract.
"""

import io
import json
import sys
from pathlib import Path

import pytest

import session_search
from process_safe import ProcessResult, ProcessTimeout


def _fake_result(returncode=0, stdout="", stderr=""):
    return ProcessResult(returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture(autouse=True)
def _stub_binary(monkeypatch):
    monkeypatch.setattr(session_search, "_resolve_binary", lambda: Path("walker"))


def test_stdout_none_raises_runtimeerror_not_attributeerror(monkeypatch):
    # The original bug: stdout came back None on a clean exit and the shim
    # did completed.stdout.splitlines() -> AttributeError. Must now raise a
    # descriptive RuntimeError instead.
    monkeypatch.setattr(
        session_search,
        "run_captured",
        lambda *a, **k: _fake_result(0, None, "trunc hint"),
    )
    with pytest.raises(RuntimeError) as excinfo:
        session_search._run_search(["anything"])
    assert "no output" in str(excinfo.value).lower()
    assert "trunc hint" in str(excinfo.value)


def test_empty_stdout_on_clean_exit_raises(monkeypatch):
    monkeypatch.setattr(
        session_search, "run_captured", lambda *a, **k: _fake_result(0, "   \n", "")
    )
    with pytest.raises(RuntimeError):
        session_search._run_search(["anything"])


def test_parses_hits_and_summary(monkeypatch):
    stdout = (
        '{"type": "hit", "session_id": "s1", "snippet": "found it"}\n'
        '{"type": "summary", "hits": 1, "truncated": false}\n'
    )
    monkeypatch.setattr(
        session_search,
        "run_captured",
        lambda *a, **k: _fake_result(0, stdout, "narrow with --since"),
    )
    result = session_search._run_search(["pattern"])
    assert len(result["hits"]) == 1
    assert result["hits"][0]["snippet"] == "found it"
    assert result["summary"]["hits"] == 1
    assert result["note"] == "narrow with --since"


def test_nonzero_exit_raises_with_stderr(monkeypatch):
    monkeypatch.setattr(
        session_search, "run_captured", lambda *a, **k: _fake_result(2, "", "bad regex")
    )
    with pytest.raises(RuntimeError) as excinfo:
        session_search._run_search(["("])
    assert "exit 2" in str(excinfo.value)
    assert "bad regex" in str(excinfo.value)


def test_no_hits_still_succeeds_via_summary_line(monkeypatch):
    # A real "zero matches" run still emits a summary line, so it must NOT be
    # treated as the empty-stdout failure case.
    monkeypatch.setattr(
        session_search,
        "run_captured",
        lambda *a, **k: _fake_result(0, '{"type": "summary", "hits": 0}\n', ""),
    )
    result = session_search._run_search(["pattern"])
    assert result["hits"] == []
    assert result["summary"]["hits"] == 0
    assert result["note"] is None


def test_timeout_raises_runtimeerror_not_process_timeout(monkeypatch):
    # process_safe.run_captured raises ProcessTimeout (not
    # subprocess.TimeoutExpired) when the walker binary wedges past
    # SUBPROCESS_TIMEOUT_SECONDS; _run_search must translate it into the CLI's
    # RuntimeError contract rather than leaking a process_safe-specific type.
    def _raise_timeout(*args, **kwargs):
        raise ProcessTimeout(
            ["walker", "search"], session_search.SUBPROCESS_TIMEOUT_SECONDS
        )

    monkeypatch.setattr(session_search, "run_captured", _raise_timeout)
    with pytest.raises(RuntimeError) as excinfo:
        session_search._run_search(["anything"])
    assert "timed out" in str(excinfo.value).lower()
    assert str(session_search.SUBPROCESS_TIMEOUT_SECONDS) in str(excinfo.value)


def test_agent_search_merges_and_sorts_providers(monkeypatch):
    native_formats = []

    def search_native(arguments):
        format_value = arguments[arguments.index("--transcript-format") + 1]
        native_formats.append(format_value)
        return {
            "hits": [
                {
                    "session_id": format_value,
                    "timestamp": (
                        "2024-01-01T00:00:00Z"
                        if format_value == "claude-code"
                        else "2024-06-01T00:00:00Z"
                    ),
                    "line_number": 1,
                }
            ],
            "summary": {
                "hits": 1,
                "sessions_matched": 1,
                "roots_walked": 1,
                "files_walked": 2,
                "truncated": False,
            },
            "note": None,
        }

    monkeypatch.setattr(
        session_search,
        "_run_search",
        search_native,
    )
    monkeypatch.setattr(
        session_search,
        "search_opencode",
        lambda options: {
            "hits": [
                {
                    "provider": "opencode",
                    "session_id": "oc",
                    "timestamp": "2025-01-01T00:00:00Z",
                    "line_number": 2,
                }
            ],
            "summary": {
                "hits": 1,
                "sessions_matched": 1,
                "roots_walked": 1,
                "files_walked": 1,
                "truncated": False,
            },
            "note": None,
        },
    )
    result = session_search._run_agent_search(
        session_search.SearchOptions(pattern="needle"), None
    )
    assert native_formats == ["claude-code", "codex"]
    assert [hit["provider"] for hit in result["hits"]] == [
        "opencode",
        "codex",
        "claude",
    ]
    assert result["summary"]["hits"] == 3
    assert set(result["summary"]["providers"]) == {"claude", "codex", "opencode"}


def test_agent_search_degrades_when_one_provider_fails(monkeypatch):
    monkeypatch.setattr(
        session_search,
        "_run_search",
        lambda arguments: (_ for _ in ()).throw(RuntimeError("binary missing")),
    )
    monkeypatch.setattr(
        session_search,
        "search_opencode",
        lambda options: {
            "hits": [],
            "summary": {
                "hits": 0,
                "sessions_matched": 0,
                "roots_walked": 1,
                "files_walked": 1,
                "truncated": False,
            },
            "note": None,
        },
    )
    result = session_search._run_agent_search(
        session_search.SearchOptions(pattern="needle"), None
    )
    assert result["hits"] == []
    assert "claude: binary missing" in result["note"]
    assert "codex: binary missing" in result["note"]
    assert result["summary"]["providers"]["claude"]["available"] is False
    assert result["summary"]["providers"]["claude"]["error"] == "binary missing"
    assert result["summary"]["providers"]["codex"]["available"] is False


def test_agent_search_raises_when_every_provider_fails(monkeypatch):
    def fail_native(arguments):
        source = arguments[arguments.index("--transcript-format") + 1]
        provider = "claude" if source == "claude-code" else source
        raise RuntimeError(f"{provider} failed")

    monkeypatch.setattr(
        session_search,
        "_run_search",
        fail_native,
    )
    monkeypatch.setattr(
        session_search,
        "search_opencode",
        lambda options: (_ for _ in ()).throw(
            session_search.OpenCodeSearchError("opencode failed")
        ),
    )
    with pytest.raises(
        RuntimeError, match="all selected.*claude failed.*codex failed.*opencode failed"
    ):
        session_search._run_agent_search(
            session_search.SearchOptions(pattern="needle"), None
        )


def test_agent_search_rejects_unknown_or_empty_providers():
    options = session_search.SearchOptions(pattern="needle")
    with pytest.raises(ValueError, match="unknown"):
        session_search._run_agent_search(options, ["pi"])
    with pytest.raises(ValueError, match="empty"):
        session_search._run_agent_search(options, [])


def test_search_argument_builder_maps_every_option():
    options = session_search.SearchOptions(
        pattern="needle",
        regex=True,
        case_sensitive=True,
        role="user",
        since="7d",
        until="1h",
        cwd="project",
        context_turns=2,
        limit=3,
        count_only=True,
        include_tool_blocks=True,
    )
    assert session_search._search_arguments(options) == [
        "needle",
        "--regex",
        "--case-sensitive",
        "--role",
        "user",
        "--since",
        "7d",
        "--until",
        "1h",
        "--cwd",
        "project",
        "--context",
        "2",
        "--limit",
        "3",
        "--count-only",
        "--include-tool-blocks",
    ]
    assert session_search._search_arguments(options, "codex")[-2:] == [
        "--transcript-format",
        "codex",
    ]


def test_merge_count_only_and_global_truncation():
    result = session_search._merge_provider_results(
        {
            "claude": {
                "hits": [
                    {"timestamp": "bad-time", "session_id": "a", "line_number": 1},
                    {
                        "timestamp": "2025-01-01T00:00:00Z",
                        "session_id": "b",
                        "line_number": 2,
                    },
                ],
                "summary": {"hits": 7, "sessions_matched": 2, "truncated": False},
                "note": "note",
            },
        },
        limit=1,
        count_only=True,
        elapsed_ms=4,
    )
    assert result["hits"] == []
    assert result["summary"]["hits"] == 7
    assert result["summary"]["truncated"] is True
    assert result["note"] == "claude: note"


def test_agent_search_applies_global_limit_after_native_source_selection(monkeypatch):
    calls = []

    def search_native(arguments):
        source = arguments[arguments.index("--transcript-format") + 1]
        calls.append(arguments)
        return {
            "hits": [
                {
                    "session_id": source,
                    "timestamp": (
                        "2024-01-01T00:00:00Z"
                        if source == "claude-code"
                        else "2025-01-01T00:00:00Z"
                    ),
                    "line_number": 1,
                }
            ],
            "summary": {"hits": 1, "sessions_matched": 1, "truncated": True},
            "note": None,
        }

    monkeypatch.setattr(session_search, "_run_search", search_native)
    result = session_search._run_agent_search(
        session_search.SearchOptions(pattern="needle", limit=1), ["claude", "codex"]
    )
    assert [call[call.index("--transcript-format") + 1] for call in calls] == [
        "claude-code",
        "codex",
    ]
    assert all(call[call.index("--limit") + 1] == "1" for call in calls)
    assert [hit["provider"] for hit in result["hits"]] == ["codex"]
    assert set(result["summary"]["providers"]) == {"claude", "codex"}
    assert result["summary"]["truncated"] is True


def test_agent_search_count_only_sums_separate_native_sources(monkeypatch):
    calls = []

    def search_native(arguments):
        calls.append(arguments)
        return {
            "hits": [],
            "summary": {"hits": 7, "sessions_matched": 2, "truncated": False},
            "note": None,
        }

    monkeypatch.setattr(session_search, "_run_search", search_native)
    result = session_search._run_agent_search(
        session_search.SearchOptions(pattern="needle", count_only=True),
        ["claude", "codex"],
    )
    assert len(calls) == 2
    assert all("--count-only" in call for call in calls)
    assert result["hits"] == []
    assert result["summary"]["hits"] == 14
    assert result["summary"]["sessions_matched"] == 4


def test_agent_search_validates_numeric_options():
    with pytest.raises(ValueError, match="limit"):
        session_search._run_agent_search(
            session_search.SearchOptions(pattern="x", limit=0), ["opencode"]
        )
    with pytest.raises(ValueError, match="context_turns"):
        session_search._run_agent_search(
            session_search.SearchOptions(pattern="x", context_turns=-1), ["opencode"]
        )


def test_agent_search_honors_single_provider_selection(monkeypatch):
    calls = []
    monkeypatch.setattr(
        session_search,
        "_run_search",
        lambda arguments: (
            calls.append(arguments[arguments.index("--transcript-format") + 1])
            or {
                "hits": [],
                "summary": {},
                "note": None,
            }
        ),
    )
    monkeypatch.setattr(
        session_search,
        "search_opencode",
        lambda options: (
            calls.append("opencode")
            or {
                "hits": [],
                "summary": {},
                "note": None,
            }
        ),
    )
    options = session_search.SearchOptions(pattern="x")
    session_search._run_agent_search(options, ["claude"])
    assert calls == ["claude-code"]
    calls.clear()
    session_search._run_agent_search(options, ["codex"])
    assert calls == ["codex"]
    calls.clear()
    session_search._run_agent_search(options, ["opencode"])
    assert calls == ["opencode"]


def _legacy_console(monkeypatch):
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)
    return raw


def test_cli_json_maps_every_flag_and_returns_provider_result(monkeypatch):
    raw = _legacy_console(monkeypatch)
    captured = {}
    payload = {
        "hits": [
            {"provider": "claude", "session_id": "session-1", "snippet": "日本語 🎉"}
        ],
        "summary": {"type": "summary", "hits": 1},
        "note": None,
    }

    def fake_search(options, providers):
        captured["options"] = options
        captured["providers"] = providers
        return payload

    monkeypatch.setattr(session_search, "_run_agent_search", fake_search)
    exit_code = session_search.main(
        [
            "--query",
            "find me",
            "--providers",
            "claude",
            "opencode",
            "--regex",
            "--case-sensitive",
            "--role",
            "user",
            "--since",
            "7d",
            "--until",
            "1h",
            "--cwd",
            "project",
            "--context-turns",
            "2",
            "--limit",
            "3",
            "--count-only",
            "--include-tool-blocks",
            "--opencode-db",
            "db.sqlite",
            "--json",
        ]
    )

    assert exit_code == 0
    assert json.loads(raw.getvalue().decode("utf-8")) == payload
    assert captured["providers"] == ["claude", "opencode"]
    assert captured["options"] == session_search.SearchOptions(
        pattern="find me",
        regex=True,
        case_sensitive=True,
        role="user",
        since="7d",
        until="1h",
        cwd="project",
        context_turns=2,
        limit=3,
        count_only=True,
        include_tool_blocks=True,
        db_path="db.sqlite",
    )


def test_cli_defaults_to_human_output(monkeypatch):
    raw = _legacy_console(monkeypatch)
    captured = {}

    def fake_search(options, providers):
        captured["options"] = options
        captured["providers"] = providers
        return {
            "hits": [
                {
                    "provider": "claude",
                    "session_id": "session-1",
                    "timestamp": "2025-01-01T00:00:00Z",
                    "snippet": "the decision 日本語 🎉",
                }
            ],
            "summary": {"hits": 1, "sessions_matched": 1, "truncated": False},
            "note": "narrow the search",
        }

    monkeypatch.setattr(session_search, "_run_agent_search", fake_search)
    assert session_search.main(["--query", "decision"]) == 0

    output = raw.getvalue().decode("utf-8")
    assert "claude session-1 2025-01-01T00:00:00Z" in output
    assert "the decision 日本語 🎉" in output
    assert "hits: 1" in output
    assert "sessions_matched: 1" in output
    assert "note: narrow the search" in output
    assert captured == {
        "options": session_search.SearchOptions(pattern="decision"),
        "providers": None,
    }


def test_cli_reports_search_errors_without_a_traceback(monkeypatch, capsys):
    monkeypatch.setattr(
        session_search,
        "_run_agent_search",
        lambda options, providers: (_ for _ in ()).throw(
            RuntimeError("all selected search providers failed")
        ),
    )

    assert session_search.main(["--query", "decision", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "all selected search providers failed" in captured.err
    assert "Traceback" not in captured.err
