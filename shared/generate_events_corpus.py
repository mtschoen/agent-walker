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
import os
from datetime import datetime, timezone
from pathlib import Path

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
                  lines: list[dict | str]) -> None:
    """Write a fixture JSONL file at corpus/events/<name>/<slug>/<session_id>.jsonl."""
    target = EVENTS_CORPUS / name / slug / f"{session_id}.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(line if isinstance(line, str) else json.dumps(line))
            f.write("\n")


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


FIXTURES = [
    fixture_01_empty,
    fixture_02_single,
    fixture_03_dedup,
    fixture_04_multi_session,
    fixture_05_cache_mix,
    fixture_06_malformed_mixed,
    fixture_07_web_search,
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
