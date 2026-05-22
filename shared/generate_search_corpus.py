"""Synthesize search-subcommand fixtures + expected outputs.

Writes to `shared/corpus/search/<scenario>/`:
- one or more JSONL transcript files (named `sid1.jsonl`, `sid2.jsonl`, ...)
- a sibling `expected.json` mapping flag-combo names to {pattern, flags, hits,
  summary}.

The conformance harness picks one scenario at a time, copies it into a temp
tree (so the scenario dir doubles as the cwd-slug), and runs
    walker search <pattern> <flags> --format jsonl
        --projects-root <tmp> --now <NOW_UNIX>
per combo. Structural-JSON equality vs. `hits`/`summary` after stripping
`host_root`, `file_path`, `elapsed_ms`, and `files_walked` (those vary per
run).

Anchored to the same NOW_UNIX as the beacon corpus (2026-05-09 12:00:00 UTC)
so the time-window scenario is reproducible.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CORPUS_SEARCH = ROOT / "corpus" / "search"

NOW_UNIX = 1778414400.0  # 2026-05-09 12:00:00 UTC -- matches beacon corpus

DAY = 86400.0


def iso(unix: float) -> str:
    return datetime.fromtimestamp(unix, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


def assistant_text(unix_ts: float, msg_id: str, text: str) -> dict:
    """Assistant entry with a single text block."""
    return {
        "type": "assistant",
        "timestamp": iso(unix_ts),
        "message": {
            "id": msg_id,
            "role": "assistant",
            "model": "claude-opus-4-7",
            "content": [{"type": "text", "text": text}],
            "usage": {
                "input_tokens": 50,
                "output_tokens": 25,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        },
    }


def user_text_array(unix_ts: float, text: str) -> dict:
    """User entry with an array of text blocks (modern format)."""
    return {
        "type": "user",
        "timestamp": iso(unix_ts),
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
    }


def user_bare_string(unix_ts: float, text: str) -> dict:
    """User entry with bare-string content (legacy format)."""
    return {
        "type": "user",
        "timestamp": iso(unix_ts),
        "message": {
            "role": "user",
            "content": text,
        },
    }


def user_tool_result(unix_ts: float, tool_use_id: str, content: str) -> dict:
    """User entry whose content is purely a tool_result block.

    Default search skips this; --include-tool-blocks picks it up.
    """
    return {
        "type": "user",
        "timestamp": iso(unix_ts),
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
            ],
        },
    }


def write_jsonl(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line))
            f.write("\n")


# Hit-record helpers --------------------------------------------------------

def find_offset(text: str, needle: str, *, case_sensitive: bool = False) -> tuple[int, int]:
    if case_sensitive:
        idx = text.find(needle)
    else:
        idx = text.casefold().find(needle.casefold())
    assert idx >= 0, f"needle {needle!r} not in {text!r}"
    return (idx, idx + len(needle))


def hit(
    *,
    session_id: str,
    cwd_slug: str,
    line_number: int,
    timestamp: str,
    role: str,
    snippet: str,
    match_offsets: list[list[int]],
    context_before: list[dict] | None = None,
    context_after: list[dict] | None = None,
) -> dict:
    return {
        "type": "hit",
        "session_id": session_id,
        "cwd_slug": cwd_slug,
        "line_number": line_number,
        "timestamp": timestamp,
        "role": role,
        "snippet": snippet,
        "match_offsets": match_offsets,
        "context_before": context_before or [],
        "context_after": context_after or [],
    }


def ctx(role: str, text: str, timestamp: str) -> dict:
    return {"role": role, "text": text, "timestamp": timestamp}


def summary(*, hits: int, sessions_matched: int, roots_walked: int = 1, truncated: bool = False) -> dict:
    return {
        "type": "summary",
        "hits": hits,
        "sessions_matched": sessions_matched,
        "roots_walked": roots_walked,
        "truncated": truncated,
    }


# Scenarios -----------------------------------------------------------------

def scenario_01_basic():
    """Three sessions, one match each, default flags."""
    scenario = "01-basic"
    t1, t2, t3 = NOW_UNIX - 3000, NOW_UNIX - 2000, NOW_UNIX - 1000
    text1 = "first session mentions needle one time"
    text2 = "second session also contains needle here"
    text3 = "third session has the needle word"
    files = {
        "sid1.jsonl": [assistant_text(t1, "msg_01_s1", text1)],
        "sid2.jsonl": [assistant_text(t2, "msg_01_s2", text2)],
        "sid3.jsonl": [assistant_text(t3, "msg_01_s3", text3)],
    }
    o1 = find_offset(text1, "needle")
    o2 = find_offset(text2, "needle")
    o3 = find_offset(text3, "needle")
    # Newest-first ordering.
    hits = [
        hit(
            session_id="sid3", cwd_slug=scenario, line_number=1,
            timestamp=iso(t3), role="assistant", snippet=text3,
            match_offsets=[[o3[0], o3[1]]],
        ),
        hit(
            session_id="sid2", cwd_slug=scenario, line_number=1,
            timestamp=iso(t2), role="assistant", snippet=text2,
            match_offsets=[[o2[0], o2[1]]],
        ),
        hit(
            session_id="sid1", cwd_slug=scenario, line_number=1,
            timestamp=iso(t1), role="assistant", snippet=text1,
            match_offsets=[[o1[0], o1[1]]],
        ),
    ]
    expected = {
        "default": {
            "description": "Default flags: 3 hits across 3 sessions, newest first.",
            "pattern": "needle",
            "flags": [],
            "hits": hits,
            "summary": summary(hits=3, sessions_matched=3),
        },
    }
    return scenario, files, expected


def scenario_02_multi_match_per_session():
    """One session, three matching messages — verify all emitted, newest first."""
    scenario = "02-multi-match-per-session"
    t1, t2, t3 = NOW_UNIX - 3000, NOW_UNIX - 2000, NOW_UNIX - 1000
    text1 = "first mention of needle in turn one"
    text2 = "second mention of needle in turn two"
    text3 = "third mention of needle in turn three"
    files = {
        "sid1.jsonl": [
            assistant_text(t1, "msg_02_m1", text1),
            assistant_text(t2, "msg_02_m2", text2),
            assistant_text(t3, "msg_02_m3", text3),
        ],
    }
    o1 = find_offset(text1, "needle")
    o2 = find_offset(text2, "needle")
    o3 = find_offset(text3, "needle")
    # Newest first; each hit's context is its immediate neighbors.
    hits = [
        hit(
            session_id="sid1", cwd_slug=scenario, line_number=3,
            timestamp=iso(t3), role="assistant", snippet=text3,
            match_offsets=[[o3[0], o3[1]]],
            context_before=[ctx("assistant", text2, iso(t2))],
            context_after=[],
        ),
        hit(
            session_id="sid1", cwd_slug=scenario, line_number=2,
            timestamp=iso(t2), role="assistant", snippet=text2,
            match_offsets=[[o2[0], o2[1]]],
            context_before=[ctx("assistant", text1, iso(t1))],
            context_after=[ctx("assistant", text3, iso(t3))],
        ),
        hit(
            session_id="sid1", cwd_slug=scenario, line_number=1,
            timestamp=iso(t1), role="assistant", snippet=text1,
            match_offsets=[[o1[0], o1[1]]],
            context_before=[],
            context_after=[ctx("assistant", text2, iso(t2))],
        ),
    ]
    expected = {
        "default": {
            "description": "Three matches in one session, newest first, with --context=1 neighbors.",
            "pattern": "needle",
            "flags": [],
            "hits": hits,
            "summary": summary(hits=3, sessions_matched=1),
        },
    }
    return scenario, files, expected


def scenario_03_bare_string_user_content():
    """Older-format user message with bare-string content — must NOT be dropped."""
    scenario = "03-bare-string-user-content"
    t1, t2 = NOW_UNIX - 2000, NOW_UNIX - 1000
    assistant_msg = "no match here in the assistant turn"
    user_msg = "please find the needle in this haystack"
    files = {
        "sid1.jsonl": [
            assistant_text(t1, "msg_03_a1", assistant_msg),
            user_bare_string(t2, user_msg),
        ],
    }
    o = find_offset(user_msg, "needle")
    hits = [
        hit(
            session_id="sid1", cwd_slug=scenario, line_number=2,
            timestamp=iso(t2), role="user", snippet=user_msg,
            match_offsets=[[o[0], o[1]]],
            context_before=[ctx("assistant", assistant_msg, iso(t1))],
            context_after=[],
        ),
    ]
    expected = {
        "default": {
            "description": "Bare-string user content matches; strict typing would drop it.",
            "pattern": "needle",
            "flags": [],
            "hits": hits,
            "summary": summary(hits=1, sessions_matched=1),
        },
    }
    return scenario, files, expected


def scenario_04_role_filter():
    """Pattern in both user and assistant turns; --role partitions."""
    scenario = "04-role-filter"
    t1, t2, t3, t4 = (
        NOW_UNIX - 4000,
        NOW_UNIX - 3000,
        NOW_UNIX - 2000,
        NOW_UNIX - 1000,
    )
    a1 = "assistant talks about needle first"
    u2 = "user mentions needle as a reply"
    a3 = "assistant restates needle later"
    u4 = "user says nothing relevant"
    files = {
        "sid1.jsonl": [
            assistant_text(t1, "msg_04_a1", a1),
            user_text_array(t2, u2),
            assistant_text(t3, "msg_04_a3", a3),
            user_text_array(t4, u4),
        ],
    }
    oa1 = find_offset(a1, "needle")
    ou2 = find_offset(u2, "needle")
    oa3 = find_offset(a3, "needle")
    hit_a1 = hit(
        session_id="sid1", cwd_slug=scenario, line_number=1,
        timestamp=iso(t1), role="assistant", snippet=a1,
        match_offsets=[[oa1[0], oa1[1]]],
        context_before=[],
        context_after=[ctx("user", u2, iso(t2))],
    )
    hit_u2 = hit(
        session_id="sid1", cwd_slug=scenario, line_number=2,
        timestamp=iso(t2), role="user", snippet=u2,
        match_offsets=[[ou2[0], ou2[1]]],
        context_before=[ctx("assistant", a1, iso(t1))],
        context_after=[ctx("assistant", a3, iso(t3))],
    )
    hit_a3 = hit(
        session_id="sid1", cwd_slug=scenario, line_number=3,
        timestamp=iso(t3), role="assistant", snippet=a3,
        match_offsets=[[oa3[0], oa3[1]]],
        context_before=[ctx("user", u2, iso(t2))],
        context_after=[ctx("user", u4, iso(t4))],
    )
    expected = {
        "default": {
            "description": "Default role=both: 3 hits (a1, u2, a3), newest first.",
            "pattern": "needle",
            "flags": [],
            "hits": [hit_a3, hit_u2, hit_a1],
            "summary": summary(hits=3, sessions_matched=1),
        },
        "role-user": {
            "description": "--role user: just the user turn.",
            "pattern": "needle",
            "flags": ["--role", "user"],
            "hits": [hit_u2],
            "summary": summary(hits=1, sessions_matched=1),
        },
        "role-assistant": {
            "description": "--role assistant: just the two assistant turns, newest first.",
            "pattern": "needle",
            "flags": ["--role", "assistant"],
            "hits": [hit_a3, hit_a1],
            "summary": summary(hits=2, sessions_matched=1),
        },
    }
    return scenario, files, expected


def scenario_05_tool_block_skip():
    """Pattern only inside a tool_result; default skips, --include-tool-blocks finds."""
    scenario = "05-tool-block-skip"
    t1, t2 = NOW_UNIX - 2000, NOW_UNIX - 1000
    assistant_msg = "I'll search for it now"
    tool_output = "needle: found at line 42"
    files = {
        "sid1.jsonl": [
            assistant_text(t1, "msg_05_a1", assistant_msg),
            user_tool_result(t2, "toolu_05_01", tool_output),
        ],
    }
    o = find_offset(tool_output, "needle")
    hits_with_tool = [
        hit(
            session_id="sid1", cwd_slug=scenario, line_number=2,
            timestamp=iso(t2), role="user", snippet=tool_output,
            match_offsets=[[o[0], o[1]]],
            context_before=[ctx("assistant", assistant_msg, iso(t1))],
            context_after=[],
        ),
    ]
    expected = {
        "default": {
            "description": "Default skips tool_result-only messages: 0 hits.",
            "pattern": "needle",
            "flags": [],
            "hits": [],
            "summary": summary(hits=0, sessions_matched=0),
        },
        "include-tool-blocks": {
            "description": "--include-tool-blocks picks up tool_result content.",
            "pattern": "needle",
            "flags": ["--include-tool-blocks"],
            "hits": hits_with_tool,
            "summary": summary(hits=1, sessions_matched=1),
        },
    }
    return scenario, files, expected


def scenario_06_regex():
    """--regex 'foo\\d+' against foo1, foo, foo42; expect 2 matches."""
    scenario = "06-regex"
    t1, t2, t3 = NOW_UNIX - 3000, NOW_UNIX - 2000, NOW_UNIX - 1000
    text1 = "calling foo1 now"
    text2 = "just foo without digits"  # no digit follow-up, no match
    text3 = "calling foo42 here"
    files = {
        "sid1.jsonl": [
            assistant_text(t1, "msg_06_a1", text1),
            assistant_text(t2, "msg_06_a2", text2),
            assistant_text(t3, "msg_06_a3", text3),
        ],
    }
    o1 = (text1.find("foo1"), text1.find("foo1") + 4)
    o3 = (text3.find("foo42"), text3.find("foo42") + 5)
    hit_t1 = hit(
        session_id="sid1", cwd_slug=scenario, line_number=1,
        timestamp=iso(t1), role="assistant", snippet=text1,
        match_offsets=[[o1[0], o1[1]]],
        context_before=[],
        context_after=[ctx("assistant", text2, iso(t2))],
    )
    hit_t3 = hit(
        session_id="sid1", cwd_slug=scenario, line_number=3,
        timestamp=iso(t3), role="assistant", snippet=text3,
        match_offsets=[[o3[0], o3[1]]],
        context_before=[ctx("assistant", text2, iso(t2))],
        context_after=[],
    )
    expected = {
        "regex": {
            "description": "--regex 'foo\\\\d+' matches foo1 and foo42, skips bare foo.",
            "pattern": "foo\\d+",
            "flags": ["--regex"],
            "hits": [hit_t3, hit_t1],
            "summary": summary(hits=2, sessions_matched=1),
        },
    }
    return scenario, files, expected


def scenario_07_case():
    """Case-insensitive default returns 3 hits; --case-sensitive returns 1."""
    scenario = "07-case"
    t1, t2, t3 = NOW_UNIX - 3000, NOW_UNIX - 2000, NOW_UNIX - 1000
    text1 = "found Needle uppercase N"
    text2 = "found NEEDLE all caps"
    text3 = "found needle lowercase"
    files = {
        "sid1.jsonl": [
            assistant_text(t1, "msg_07_a1", text1),
            assistant_text(t2, "msg_07_a2", text2),
            assistant_text(t3, "msg_07_a3", text3),
        ],
    }
    # Case-insensitive: substring "needle" matches all three.
    o1_ci = find_offset(text1, "needle")
    o2_ci = find_offset(text2, "needle")
    o3_ci = find_offset(text3, "needle")
    hit_t1_ci = hit(
        session_id="sid1", cwd_slug=scenario, line_number=1,
        timestamp=iso(t1), role="assistant", snippet=text1,
        match_offsets=[[o1_ci[0], o1_ci[1]]],
        context_before=[],
        context_after=[ctx("assistant", text2, iso(t2))],
    )
    hit_t2_ci = hit(
        session_id="sid1", cwd_slug=scenario, line_number=2,
        timestamp=iso(t2), role="assistant", snippet=text2,
        match_offsets=[[o2_ci[0], o2_ci[1]]],
        context_before=[ctx("assistant", text1, iso(t1))],
        context_after=[ctx("assistant", text3, iso(t3))],
    )
    hit_t3_ci = hit(
        session_id="sid1", cwd_slug=scenario, line_number=3,
        timestamp=iso(t3), role="assistant", snippet=text3,
        match_offsets=[[o3_ci[0], o3_ci[1]]],
        context_before=[ctx("assistant", text2, iso(t2))],
        context_after=[],
    )
    # Case-sensitive: only the lowercase one.
    o3_cs = find_offset(text3, "needle", case_sensitive=True)
    hit_t3_cs = hit(
        session_id="sid1", cwd_slug=scenario, line_number=3,
        timestamp=iso(t3), role="assistant", snippet=text3,
        match_offsets=[[o3_cs[0], o3_cs[1]]],
        context_before=[ctx("assistant", text2, iso(t2))],
        context_after=[],
    )
    expected = {
        "default": {
            "description": "Default case-insensitive: all three variants match.",
            "pattern": "needle",
            "flags": [],
            "hits": [hit_t3_ci, hit_t2_ci, hit_t1_ci],
            "summary": summary(hits=3, sessions_matched=1),
        },
        "case-sensitive": {
            "description": "--case-sensitive: only the lowercase variant matches.",
            "pattern": "needle",
            "flags": ["--case-sensitive"],
            "hits": [hit_t3_cs],
            "summary": summary(hits=1, sessions_matched=1),
        },
    }
    return scenario, files, expected


def scenario_08_time_window():
    """Three matches at 30d/14d/3d ago; --since 7d returns only the 3d-old hit."""
    scenario = "08-time-window"
    t_30d = NOW_UNIX - 30 * DAY
    t_14d = NOW_UNIX - 14 * DAY
    t_3d = NOW_UNIX - 3 * DAY
    text1 = "needle thirty days ago"
    text2 = "needle fourteen days ago"
    text3 = "needle three days ago"
    files = {
        "sid1.jsonl": [
            assistant_text(t_30d, "msg_08_a1", text1),
            assistant_text(t_14d, "msg_08_a2", text2),
            assistant_text(t_3d, "msg_08_a3", text3),
        ],
    }
    o1 = find_offset(text1, "needle")
    o2 = find_offset(text2, "needle")
    o3 = find_offset(text3, "needle")
    hit_30d = hit(
        session_id="sid1", cwd_slug=scenario, line_number=1,
        timestamp=iso(t_30d), role="assistant", snippet=text1,
        match_offsets=[[o1[0], o1[1]]],
        context_before=[],
        context_after=[ctx("assistant", text2, iso(t_14d))],
    )
    hit_14d = hit(
        session_id="sid1", cwd_slug=scenario, line_number=2,
        timestamp=iso(t_14d), role="assistant", snippet=text2,
        match_offsets=[[o2[0], o2[1]]],
        context_before=[ctx("assistant", text1, iso(t_30d))],
        context_after=[ctx("assistant", text3, iso(t_3d))],
    )
    hit_3d = hit(
        session_id="sid1", cwd_slug=scenario, line_number=3,
        timestamp=iso(t_3d), role="assistant", snippet=text3,
        match_offsets=[[o3[0], o3[1]]],
        context_before=[ctx("assistant", text2, iso(t_14d))],
        context_after=[],
    )
    # Context is positional and unfiltered — it shows the transcript neighbors
    # of a hit regardless of which filter (role, time-window, etc.) excluded
    # those neighbors from emitting their own hits. This matches the precedent
    # set by the role-filter scenario, where context crosses the --role
    # boundary. Spec edge case to confirm at review time.
    hit_3d_with_filtered_ctx = hit(
        session_id="sid1", cwd_slug=scenario, line_number=3,
        timestamp=iso(t_3d), role="assistant", snippet=text3,
        match_offsets=[[o3[0], o3[1]]],
        context_before=[ctx("assistant", text2, iso(t_14d))],
        context_after=[],
    )
    expected = {
        "default": {
            "description": "No --since: all three hits, newest first.",
            "pattern": "needle",
            "flags": [],
            "hits": [hit_3d, hit_14d, hit_30d],
            "summary": summary(hits=3, sessions_matched=1),
        },
        "since-7d": {
            "description": "--since 7d: only the 3d-old hit survives the time filter.",
            "pattern": "needle",
            "flags": ["--since", "7d"],
            "hits": [hit_3d_with_filtered_ctx],
            "summary": summary(hits=1, sessions_matched=1),
        },
    }
    return scenario, files, expected


def scenario_09_count_only():
    """5 matching messages; default returns 5 hits + summary, --count-only just summary."""
    scenario = "09-count-only"
    timestamps = [NOW_UNIX - (5 - i) * 500 for i in range(5)]
    texts = [f"needle hit number {i + 1}" for i in range(5)]
    files = {
        "sid1.jsonl": [
            assistant_text(timestamps[i], f"msg_09_a{i + 1}", texts[i])
            for i in range(5)
        ],
    }
    hits_default = []
    # Build newest-first hits with context (prev/next text turn).
    for i in reversed(range(5)):
        ts = timestamps[i]
        text = texts[i]
        o = find_offset(text, "needle")
        cb = [ctx("assistant", texts[i - 1], iso(timestamps[i - 1]))] if i - 1 >= 0 else []
        ca = [ctx("assistant", texts[i + 1], iso(timestamps[i + 1]))] if i + 1 < 5 else []
        hits_default.append(hit(
            session_id="sid1", cwd_slug=scenario, line_number=i + 1,
            timestamp=iso(ts), role="assistant", snippet=text,
            match_offsets=[[o[0], o[1]]],
            context_before=cb,
            context_after=ca,
        ))
    expected = {
        "default": {
            "description": "Default: 5 hits + summary.",
            "pattern": "needle",
            "flags": [],
            "hits": hits_default,
            "summary": summary(hits=5, sessions_matched=1),
        },
        "count-only": {
            "description": "--count-only: no hit records, just the summary.",
            "pattern": "needle",
            "flags": ["--count-only"],
            "hits": [],
            "summary": summary(hits=5, sessions_matched=1),
        },
    }
    return scenario, files, expected


def scenario_10_context_zero():
    """7-turn session, hit in middle; --context 0 returns hit with empty context arrays."""
    scenario = "10-context-zero"
    timestamps = [NOW_UNIX - (7 - i) * 500 for i in range(7)]
    # Turns alternate user/assistant. Hit is in turn 4 (index 3).
    a1 = "intro from assistant"
    u1 = "user prompt one"
    a2 = "assistant reply one"
    a3 = "assistant says needle in middle"
    u2 = "user follow-up"
    a4 = "assistant reply two"
    u3 = "user thanks"
    entries = [
        assistant_text(timestamps[0], "msg_10_a1", a1),
        user_text_array(timestamps[1], u1),
        assistant_text(timestamps[2], "msg_10_a2", a2),
        assistant_text(timestamps[3], "msg_10_a3", a3),
        user_text_array(timestamps[4], u2),
        assistant_text(timestamps[5], "msg_10_a4", a4),
        user_text_array(timestamps[6], u3),
    ]
    files = {"sid1.jsonl": entries}
    o = find_offset(a3, "needle")
    # --context 0: empty context arrays.
    hit_ctx0 = hit(
        session_id="sid1", cwd_slug=scenario, line_number=4,
        timestamp=iso(timestamps[3]), role="assistant", snippet=a3,
        match_offsets=[[o[0], o[1]]],
        context_before=[],
        context_after=[],
    )
    # Default --context 1: one turn before, one after.
    hit_ctx1 = hit(
        session_id="sid1", cwd_slug=scenario, line_number=4,
        timestamp=iso(timestamps[3]), role="assistant", snippet=a3,
        match_offsets=[[o[0], o[1]]],
        context_before=[ctx("assistant", a2, iso(timestamps[2]))],
        context_after=[ctx("user", u2, iso(timestamps[4]))],
    )
    expected = {
        "default": {
            "description": "Default --context 1: one neighbor on each side.",
            "pattern": "needle",
            "flags": [],
            "hits": [hit_ctx1],
            "summary": summary(hits=1, sessions_matched=1),
        },
        "context-zero": {
            "description": "--context 0: hit message only, empty context arrays.",
            "pattern": "needle",
            "flags": ["--context", "0"],
            "hits": [hit_ctx0],
            "summary": summary(hits=1, sessions_matched=1),
        },
    }
    return scenario, files, expected


SCENARIOS = [
    scenario_01_basic,
    scenario_02_multi_match_per_session,
    scenario_03_bare_string_user_content,
    scenario_04_role_filter,
    scenario_05_tool_block_skip,
    scenario_06_regex,
    scenario_07_case,
    scenario_08_time_window,
    scenario_09_count_only,
    scenario_10_context_zero,
]


def main() -> None:
    if CORPUS_SEARCH.exists():
        for p in sorted(CORPUS_SEARCH.rglob("*"), reverse=True):
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                p.rmdir()
        CORPUS_SEARCH.rmdir()
    CORPUS_SEARCH.mkdir(parents=True, exist_ok=True)

    meta = {
        "now_unix": NOW_UNIX,
        "note": "Generated by generate_search_corpus.py. Do not hand-edit.",
    }

    total_files = 0
    for build in SCENARIOS:
        scenario, files, combos = build()
        for rel, lines in files.items():
            write_jsonl(CORPUS_SEARCH / scenario / rel, lines)
            total_files += 1
        expected_path = CORPUS_SEARCH / scenario / "expected.json"
        expected_path.write_text(
            json.dumps({"_meta": meta, "combos": combos}, indent=2) + "\n",
            encoding="utf-8",
        )

    print(f"Wrote {total_files} JSONL fixtures across {len(SCENARIOS)} scenarios")
    print(f"  under {CORPUS_SEARCH}")


if __name__ == "__main__":
    main()
