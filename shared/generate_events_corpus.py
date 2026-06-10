"""Synthesize the events conformance corpus and compute expected outputs in one pass.

Run from anywhere; writes to `shared/corpus/events/` and
`shared/corpus/events/expected_events.json` relative to this file. Re-run any
time the fixtures or pricing change.

Each fixture is a JSONL file (or directory of files) under
`shared/corpus/events/<NN>-<name>/`. The expected output lists the per-turn
records the `events` subcommand must emit for each fixture.
Languages must reproduce these values to within $0.01 per turn.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parent
EVENTS_CORPUS = ROOT / "corpus" / "events"
EXPECTED_PATH = EVENTS_CORPUS / "expected_events.json"

# Pinned constants for conformance (mirror the plan's PIN_* names).
PIN_NOW = 1_747_900_000.0
PIN_PERIOD = 86_400
PIN_WIN_START = PIN_NOW - PIN_PERIOD  # == now - period (default win_start)


# ---------------------------------------------------------------------------
# Pricing — must match SPEC.md §Pricing exactly.
# ---------------------------------------------------------------------------

RATES = {
    "opus": (5.0, 25.0),
    "sonnet": (3.0, 15.0),
    "haiku": (1.0, 5.0),
}

WEB_SEARCH_COST_USD = 0.01  # flat charge per server-side web search request


def _rates_for(model_id: str) -> tuple[float, float]:
    name = (model_id or "").lower()
    for family, rates in RATES.items():
        if family in name:
            return rates
    return RATES["sonnet"]


def cost(model: str, in_tok: int, out_tok: int,
         cache_read: int = 0, cache_write: int = 0,
         web_search_requests: int = 0) -> float:
    """Compute USD cost for one assistant turn per SPEC.md §Pricing."""
    input_rate, output_rate = _rates_for(model)
    token_cost = (
        in_tok * input_rate
        + cache_read * input_rate * 0.10
        + cache_write * input_rate * 1.25
        + out_tok * output_rate
    ) / 1_000_000
    return token_cost + web_search_requests * WEB_SEARCH_COST_USD


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def iso_z(unix: float) -> str:
    """Format Unix epoch as ISO 8601 UTC string with .000Z suffix."""
    return datetime.fromtimestamp(unix, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


def turn(message_id: str, ts: float, model: str,
         in_tok: int, out_tok: int,
         cache_read: int = 0, cache_write: int = 0,
         web_search_requests: int = 0) -> dict:
    """Build an assistant-turn dict matching the JSONL schema."""
    usage: dict = {
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write,
    }
    if web_search_requests:
        usage["server_tool_use"] = {"web_search_requests": web_search_requests}
    return {
        "type": "assistant",
        "timestamp": iso_z(ts),
        "message": {
            "id": message_id,
            "role": "assistant",
            "model": model,
            "usage": usage,
        },
    }


def write_fixture(name: str, slug: str, session_id: str,
                  lines: "Sequence[dict | str | bytes]") -> None:
    """Write a fixture JSONL file at corpus/events/<name>/<slug>/<session_id>.jsonl.

    Dicts are json-dumped, raw strings written verbatim, raw bytes as-is
    (invalid-UTF-8 rungs). Binary mode keeps byte rungs and embedded CR exact."""
    target = EVENTS_CORPUS / name / slug / f"{session_id}.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "wb") as f:
        for line in lines:
            if isinstance(line, bytes):
                f.write(line)
            else:
                text = line if isinstance(line, str) else json.dumps(line)
                f.write(text.encode("utf-8"))
            f.write(b"\n")


def expected_record(turn_dict: dict, slug: str, session_id: str) -> dict:
    """Build the events-output record the walker should emit for this turn.

    Field order is fixed per SPEC.md: ts, usd, model, session_id, slug.
    The slug is the parent directory name of the .jsonl file, which in our
    fixture layout is the <slug> component passed here (i.e. the directory
    immediately above <session_id>.jsonl).
    """
    msg = turn_dict["message"]
    usage = msg.get("usage") or {}
    ts = datetime.fromisoformat(
        turn_dict["timestamp"].replace("Z", "+00:00")
    ).timestamp()
    usd = cost(
        msg.get("model") or "",
        int(usage.get("input_tokens") or 0),
        int(usage.get("output_tokens") or 0),
        int(usage.get("cache_read_input_tokens") or 0),
        int(usage.get("cache_creation_input_tokens") or 0),
        int((usage.get("server_tool_use") or {}).get("web_search_requests") or 0),
    )
    # Field order fixed: ts, usd, model, session_id, slug
    return {
        "ts": ts,
        "usd": round(usd, 6),
        "model": (msg.get("model") or "").lower(),
        "session_id": session_id,
        "slug": slug,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Convenient time anchor: 60 seconds before now, well inside the window.
_IN_WINDOW = PIN_NOW - 60


def fixture_01_empty():
    """Single turn whose timestamp is before the window — expect no output."""
    name = "01-empty"
    slug = "project-alpha"
    session_id = "sess-001"
    ts_prewindow = PIN_NOW - PIN_PERIOD - 100  # 100s before window opens
    lines = [turn("msg-001", ts_prewindow, "claude-sonnet-4-6", 500, 50)]
    write_fixture(name, slug, session_id, lines)
    return name, []  # pre-window → no expected records


def fixture_02_single():
    """One in-window sonnet turn — expect exactly one record."""
    name = "02-single"
    slug = "project-beta"
    session_id = "sess-002"
    t = turn("msg-002", _IN_WINDOW, "claude-sonnet-4-6", 1000, 100)
    write_fixture(name, slug, session_id, [t])
    return name, [expected_record(t, slug, session_id)]


def fixture_03_dedup():
    """Same message.id appears twice in the same session group — only first counts."""
    name = "03-dedup"
    slug = "project-gamma"
    session_id = "sess-003"
    t1 = turn("msg-dup", _IN_WINDOW, "claude-sonnet-4-6", 1000, 100)
    t2 = turn("msg-dup", _IN_WINDOW, "claude-sonnet-4-6", 1000, 100)  # duplicate id
    write_fixture(name, slug, session_id, [t1, t2])
    # Second occurrence is deduplicated — only first record emitted.
    return name, [expected_record(t1, slug, session_id)]


def fixture_04_multi_session():
    """Two sessions under same slug, one opus + one sonnet turn — expect both."""
    name = "04-multi-session"
    slug = "project-delta"
    session_a = "sess-004a"
    session_b = "sess-004b"
    ta = turn("msg-004a", _IN_WINDOW, "claude-opus-4-7", 2000, 300)
    tb = turn("msg-004b", _IN_WINDOW, "claude-sonnet-4-6", 800, 120)
    write_fixture(name, slug, session_a, [ta])
    write_fixture(name, slug, session_b, [tb])
    return name, [
        expected_record(ta, slug, session_a),
        expected_record(tb, slug, session_b),
    ]


def fixture_05_cache_mix():
    """One sonnet turn with cache_read and cache_write tokens."""
    name = "05-cache-mix"
    slug = "project-epsilon"
    session_id = "sess-005"
    t = turn("msg-005", _IN_WINDOW, "claude-sonnet-4-6",
             in_tok=1000, out_tok=200,
             cache_read=10000, cache_write=200)
    write_fixture(name, slug, session_id, [t])
    return name, [expected_record(t, slug, session_id)]


def fixture_06_malformed_mixed():
    """Bad JSON line followed by one valid in-window turn — only the good one emits."""
    name = "06-malformed-mixed"
    slug = "project-zeta"
    session_id = "sess-006"
    good = turn("msg-006", _IN_WINDOW, "claude-haiku-4-5", 500, 50)
    lines: list[dict | str] = [
        "this is not JSON",
        good,
    ]
    write_fixture(name, slug, session_id, lines)
    return name, [expected_record(good, slug, session_id)]


def fixture_07_web_search():
    """One in-window opus turn with server-side web searches.

    usd must include $0.01 per request on top of token cost. Exercises the
    events-mode usage parser's descent into the nested server_tool_use object
    (cpp/events.cpp and zig/events.zig parse usage separately from cost mode,
    so this is the only conformance check covering that event-path code).
    """
    name = "07-web-search"
    slug = "project-eta"
    session_id = "sess-007"
    t = turn("msg-007", _IN_WINDOW, "claude-opus-4-7",
             in_tok=1000, out_tok=500, web_search_requests=4)
    write_fixture(name, slug, session_id, [t])
    return name, [expected_record(t, slug, session_id)]


def fixture_08_dirty_ladder():
    """A8: dirty-line skip ladder for the events scanner. Same shape as the
    cost-mode dirty ladder — every bad-line variety the line filter must
    reject, sandwiching one valid turn that should be emitted.
    """
    name = "08-dirty-ladder"
    slug = "project-theta"
    session_id = "sess-008"
    good = turn("msg-008-good", _IN_WINDOW, "claude-sonnet-4-6", 1000, 100)
    lines: list[dict | str] = [
        "",
        "   ",
        '{"type":"assistant","timestamp":',
        "this is not JSON at all",
        json.dumps([1, 2, 3]),
        json.dumps({
            "type": "user", "timestamp": iso_z(_IN_WINDOW),
            "message": {"role": "user", "content": "hi"},
        }),
        json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "id": "no-ts-events",
                        "model": "claude-opus-4-7", "usage": {"input_tokens": 99}},
        }),
        json.dumps({
            "type": "assistant", "timestamp": "definitely-not-iso",
            "message": {"role": "assistant", "id": "bad-ts-events",
                        "model": "claude-opus-4-7", "usage": {"input_tokens": 99}},
        }),
        json.dumps({
            "type": "assistant", "timestamp": iso_z(_IN_WINDOW),
            "message": {"role": "user", "id": "role-mismatch-events",
                        "model": "claude-opus-4-7", "usage": {"input_tokens": 99}},
        }),
        good,
    ]
    write_fixture(name, slug, session_id, lines)
    return name, [expected_record(good, slug, session_id)]


def fixture_09_subagent():
    """A9: assistant turn lives in a subagent transcript
    (`<slug>/<sid>/subagents/agent-*.jsonl`). Events scanner must discover
    and emit it.
    """
    name = "09-subagent"
    slug = "project-iota"
    session_id = "sess-009"
    parent_turn = turn("msg-009-parent", _IN_WINDOW, "claude-opus-4-7", 1000, 200)
    sub_turn = turn("msg-009-sub", _IN_WINDOW, "claude-haiku-4-5", 300, 50)
    # Parent file: <slug>/<session>.jsonl
    write_fixture(name, slug, session_id, [parent_turn])
    # Subagent file: <slug>/<session>/subagents/agent-x.jsonl
    sub_path = EVENTS_CORPUS / name / slug / session_id / "subagents" / "agent-x.jsonl"
    sub_path.parent.mkdir(parents=True, exist_ok=True)
    with open(sub_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(sub_turn))
        f.write("\n")
    return name, [
        expected_record(parent_turn, slug, session_id),
        expected_record(sub_turn, slug, session_id),
    ]


def fixture_10_escape_chars():
    """A13: assistant turn whose model id contains characters that need JSON
    escaping (quotes, backslash, control bytes). Events output must remain
    valid JSON and round-trip exactly.

    Note: the slug also exercises escaping — it comes from a directory
    name, and our fixture loader uses a normal directory name; we exercise
    the model-id and session-id paths here.
    """
    name = "10-escape-chars"
    # Slug is a directory name, so we can't use control bytes here.
    slug = "project-kappa"
    # Session id is a filename stem on disk — keep it Windows-safe (no
    # `\ " : < > | ? *`). The escape-output code path is exercised by the
    # `nasty_model` field below, which sees the same JSON writer.
    session_id = "sess-010-escape-chars"
    nasty_model = ("claude-sonnet-quote\"backslash\\newline\ntab\t"
                   "bs\bff\fcr\rctl\x01")
    t = turn("msg-010", _IN_WINDOW, nasty_model, 1000, 100)
    write_fixture(name, slug, session_id, [t])
    return name, [expected_record(t, slug, session_id)]


def fixture_11_iso_variants():
    """A14: events scanner must accept non-Z ISO offsets and fractional seconds,
    and skip malformed timestamps. Three turns: +05:30 offset, fractional-Z,
    and a malformed timestamp (skipped).
    """
    name = "11-iso-variants"
    slug = "project-lambda"
    session_id = "sess-011"
    from datetime import timedelta
    fresh_utc = datetime.fromtimestamp(_IN_WINDOW, tz=timezone.utc)
    offset = timedelta(hours=5, minutes=30)
    local = fresh_utc + offset
    iso_offset = local.strftime("%Y-%m-%dT%H:%M:%S") + "+05:30"
    iso_frac = fresh_utc.strftime("%Y-%m-%dT%H:%M:%S.123456Z")
    # Build turns by hand to control the timestamp string format exactly.
    t_offset = {
        "type": "assistant", "timestamp": iso_offset,
        "message": {"id": "msg-011-offset", "role": "assistant",
                    "model": "claude-opus-4-7",
                    "usage": {"input_tokens": 1000, "output_tokens": 500,
                              "cache_read_input_tokens": 0,
                              "cache_creation_input_tokens": 0}},
    }
    t_frac = {
        "type": "assistant", "timestamp": iso_frac,
        "message": {"id": "msg-011-frac", "role": "assistant",
                    "model": "claude-sonnet-4-6",
                    "usage": {"input_tokens": 500, "output_tokens": 100,
                              "cache_read_input_tokens": 0,
                              "cache_creation_input_tokens": 0}},
    }
    t_malformed = {
        "type": "assistant", "timestamp": "2026-13-99T99:99:99Z",
        "message": {"id": "msg-011-malformed", "role": "assistant",
                    "model": "claude-opus-4-7",
                    "usage": {"input_tokens": 999999}},
    }
    write_fixture(name, slug, session_id, [t_offset, t_frac, t_malformed])
    # Expected records: only the two well-formed turns. Compute expected
    # records by reusing expected_record (which parses the timestamp).
    return name, [
        expected_record(t_offset, slug, session_id),
        expected_record(t_frac, slug, session_id),
    ]


def fixture_12_many_sessions():
    """Eight session groups under one slug. Forces the parallel-walk path in
    every impl (worker pools spawn only when the group count exceeds one),
    including the Zig events thread pool that a 1-2 group corpus never hit.
    """
    name = "12-many-sessions"
    slug = "project-mu"
    expected = []
    for n in range(8):
        session_id = f"sess-012-{n}"
        t = turn(f"msg-012-{n}", _IN_WINDOW - n, "claude-sonnet-4-6",
                 500 + n, 50)
        write_fixture(name, slug, session_id, [t])
        expected.append(expected_record(t, slug, session_id))
    return name, expected


def fixture_13_byte_edges():
    """Byte-level edge shapes for the events scanner: a CRLF-terminated good
    turn, an invalid-UTF-8 line, a lone-surrogate root key, message as a bare
    string, and non-string / >=19-char-malformed timestamps. Only the CRLF
    turn emits a record."""
    name = "13-byte-edges"
    slug = "project-nu"
    session_id = "sess-013"
    good = turn("msg-013-good", _IN_WINDOW, "claude-opus-4-7", 800, 80)
    lines: list[dict | str | bytes] = [
        json.dumps(good) + "\r",
        b'\xff\xfe invalid utf-8 line',
        '{"\\ud800": "lone surrogate key"}',
        json.dumps({"type": "assistant", "timestamp": iso_z(_IN_WINDOW),
                    "message": "bare string"}),
        json.dumps({"type": "assistant", "timestamp": 12345,
                    "message": {"role": "assistant", "id": "ev-num-ts",
                                "model": "claude-opus-4-7",
                                "usage": {"input_tokens": 5}}}),
        json.dumps({"type": "assistant", "timestamp": "2026-XX-09TZZ:00:00Z",
                    "message": {"role": "assistant", "id": "ev-long-bad-ts",
                                "model": "claude-opus-4-7",
                                "usage": {"input_tokens": 5}}}),
        # Lone-surrogate key inside message, and NO role: strict parsers
        # reject the line, per-key parsers skip the key and then drop the
        # entry for missing role. Either way no record.
        ('{"type": "assistant", "timestamp": "%s", "message": '
         '{"\\ud800": 1, "model": "claude-opus-4-7", '
         '"usage": {"input_tokens": 5}}}' % iso_z(_IN_WINDOW)),
    ]
    write_fixture(name, slug, session_id, lines)
    return name, [expected_record(good, slug, session_id)]


def fixture_14_wrong_typed_usage():
    """SPEC "Lenient per-field parsing" (COVERAGE-PLAN item 4): the events
    scanner emits a record for every assistant turn whose wrong-typed fields
    are treated as absent. Mirrors cost fixture 12-wrong-typed-usage; per-turn
    records make the per-field semantics directly assertable. Also includes
    an id-less turn (dedup skipped), unknown usage / server_tool_use keys,
    and the out-of-range numbers that crashed the zig walker pre-fix."""
    name = "14-wrong-typed-usage"
    slug = "project-omicron"
    session_id = "sess-014"

    def rec(ts: float, usd: float, model: str) -> dict:
        return {"ts": ts, "usd": round(usd, 6), "model": model,
                "session_id": session_id, "slug": slug}

    t = _IN_WINDOW
    lines: list[dict | str] = [
        {"type": "assistant", "timestamp": iso_z(t),
         "message": {"role": "assistant", "id": "ev-wt-1",
                     "model": "claude-sonnet-4-6", "usage": "not-an-object"}},
        {"type": "assistant", "timestamp": iso_z(t + 1),
         "message": {"role": "assistant", "id": "ev-wt-2",
                     "model": "claude-sonnet-4-6",
                     "usage": {"input_tokens": "abc", "output_tokens": 50}}},
        {"type": "assistant", "timestamp": iso_z(t + 2),
         "message": {"role": "assistant", "id": "ev-wt-3",
                     "model": "claude-sonnet-4-6",
                     "usage": {"input_tokens": 1.5, "output_tokens": 2e2}}},
        {"type": "assistant", "timestamp": iso_z(t + 3),
         "message": {"role": "assistant", "id": "ev-wt-4",
                     "model": "claude-sonnet-4-6",
                     "usage": {"input_tokens": -5, "output_tokens": 50,
                               "server_tool_use": "nope"}}},
        {"type": "assistant", "timestamp": iso_z(t + 4),
         "message": {"role": "assistant", "id": "ev-wt-5", "model": 7,
                     "usage": {"input_tokens": 100, "output_tokens": 10,
                               "server_tool_use": {"web_search_requests": "x",
                                                   "other": 1}}}},
        {"type": "assistant", "timestamp": iso_z(t + 5),
         "message": {"role": "assistant", "id": 42,
                     "model": "claude-opus-4-7",
                     "usage": {"input_tokens": 100, "output_tokens": 10,
                               "service_tier": "standard"}}},
        {"type": "assistant", "timestamp": iso_z(t + 6),
         "message": {"role": "assistant", "id": "ev-wt-7",
                     "model": "claude-sonnet-4-6",
                     "usage": {"input_tokens": 1e300, "output_tokens": 5}}},
        ('{"type": "assistant", "timestamp": "%s", "message": '
         '{"role": "assistant", "id": "ev-wt-8", "model": "claude-sonnet-4-6", '
         '"usage": {"input_tokens": 99999999999999999999999, '
         '"output_tokens": 5}}}' % iso_z(t + 7)),
        {"type": "assistant", "timestamp": iso_z(t + 8),
         "message": {"role": "assistant", "id": "ev-wt-9",
                     "model": "claude-sonnet-4-6",
                     "usage": {"server_tool_use":
                               {"web_search_requests": 2.9}}}},
        {"type": "assistant", "timestamp": iso_z(t + 9),
         "message": {"role": "assistant",
                     "model": "claude-haiku-4-5",
                     "usage": {"input_tokens": 100, "output_tokens": 50}}},
    ]
    write_fixture(name, slug, session_id, lines)
    son_in, son_out = RATES["sonnet"]
    opus_in, opus_out = RATES["opus"]
    haiku_in, haiku_out = RATES["haiku"]
    return name, [
        rec(t, 0.0, "claude-sonnet-4-6"),
        rec(t + 1, 50 * son_out / 1e6, "claude-sonnet-4-6"),
        rec(t + 2, (1 * son_in + 200 * son_out) / 1e6, "claude-sonnet-4-6"),
        rec(t + 3, 50 * son_out / 1e6, "claude-sonnet-4-6"),
        # Wrong-typed model -> empty string -> sonnet rates.
        rec(t + 4, (100 * son_in + 10 * son_out) / 1e6, ""),
        rec(t + 5, (100 * opus_in + 10 * opus_out) / 1e6, "claude-opus-4-7"),
        rec(t + 6, 5 * son_out / 1e6, "claude-sonnet-4-6"),
        rec(t + 7, 5 * son_out / 1e6, "claude-sonnet-4-6"),
        rec(t + 8, 2 * WEB_SEARCH_COST_USD, "claude-sonnet-4-6"),
        rec(t + 9, (100 * haiku_in + 50 * haiku_out) / 1e6, "claude-haiku-4-5"),
    ]


FIXTURES = [
    fixture_01_empty,
    fixture_02_single,
    fixture_03_dedup,
    fixture_04_multi_session,
    fixture_05_cache_mix,
    fixture_06_malformed_mixed,
    fixture_07_web_search,
    fixture_08_dirty_ladder,
    fixture_09_subagent,
    fixture_10_escape_chars,
    fixture_11_iso_variants,
    fixture_12_many_sessions,
    fixture_13_byte_edges,
    fixture_14_wrong_typed_usage,
]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    # Wipe and regenerate the events corpus so deletions take effect.
    if EVENTS_CORPUS.exists():
        for path in sorted(EVENTS_CORPUS.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        EVENTS_CORPUS.rmdir()
    EVENTS_CORPUS.mkdir(parents=True, exist_ok=True)

    fixtures_map: dict[str, list[dict]] = {}
    total_records = 0

    for build in FIXTURES:
        name, records = build()
        fixtures_map[name] = records
        total_records += len(records)

    expected = {
        "pin_now": PIN_NOW,
        "pin_period": PIN_PERIOD,
        "pin_win_start": PIN_WIN_START,
        "fixtures": fixtures_map,
    }

    with open(EXPECTED_PATH, "w", encoding="utf-8") as f:
        json.dump(expected, f, indent=2)
        f.write("\n")

    print(
        f"Wrote {total_records} expected records across {len(FIXTURES)} fixtures "
        f"to {EXPECTED_PATH}"
    )


if __name__ == "__main__":
    main()
