"""Unit tests for the FastMCP shim's _run_search reshaping.

Run from the repo root:
    uv run --python 3.13 --with mcp --with pytest python -m pytest mcp/test_server.py

These exercise the parse/guard logic in isolation by monkeypatching
subprocess.run and _resolve_binary — no native binary or live sessions needed.
Regression coverage for BUG-mcp-search-stdout-none.md.
"""
import subprocess
from pathlib import Path

import pytest

import server


def _fake_completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["walker"], returncode=returncode, stdout=stdout, stderr=stderr
    )


@pytest.fixture(autouse=True)
def _stub_binary(monkeypatch):
    monkeypatch.setattr(server, "_resolve_binary", lambda: Path("walker"))


def test_stdout_none_raises_runtimeerror_not_attributeerror(monkeypatch):
    # The original bug: stdout came back None on a clean exit and the shim
    # did completed.stdout.splitlines() -> AttributeError. Must now raise a
    # descriptive RuntimeError instead.
    monkeypatch.setattr(server.subprocess, "run",
                        lambda *a, **k: _fake_completed(0, None, "trunc hint"))
    with pytest.raises(RuntimeError) as excinfo:
        server._run_search(["anything"])
    assert "no output" in str(excinfo.value).lower()
    assert "trunc hint" in str(excinfo.value)


def test_empty_stdout_on_clean_exit_raises(monkeypatch):
    monkeypatch.setattr(server.subprocess, "run",
                        lambda *a, **k: _fake_completed(0, "   \n", ""))
    with pytest.raises(RuntimeError):
        server._run_search(["anything"])


def test_parses_hits_and_summary(monkeypatch):
    stdout = (
        '{"type": "hit", "session_id": "s1", "snippet": "found it"}\n'
        '{"type": "summary", "hits": 1, "truncated": false}\n'
    )
    monkeypatch.setattr(server.subprocess, "run",
                        lambda *a, **k: _fake_completed(0, stdout, "narrow with --since"))
    result = server._run_search(["pattern"])
    assert len(result["hits"]) == 1
    assert result["hits"][0]["snippet"] == "found it"
    assert result["summary"]["hits"] == 1
    assert result["note"] == "narrow with --since"


def test_nonzero_exit_raises_with_stderr(monkeypatch):
    monkeypatch.setattr(server.subprocess, "run",
                        lambda *a, **k: _fake_completed(2, "", "bad regex"))
    with pytest.raises(RuntimeError) as excinfo:
        server._run_search(["("])
    assert "exit 2" in str(excinfo.value)
    assert "bad regex" in str(excinfo.value)


def test_no_hits_still_succeeds_via_summary_line(monkeypatch):
    # A real "zero matches" run still emits a summary line, so it must NOT be
    # treated as the empty-stdout failure case.
    monkeypatch.setattr(server.subprocess, "run",
                        lambda *a, **k: _fake_completed(0, '{"type": "summary", "hits": 0}\n', ""))
    result = server._run_search(["pattern"])
    assert result["hits"] == []
    assert result["summary"]["hits"] == 0
    assert result["note"] is None
