"""Synthesize beacon-mode fixtures + expected outputs.

Run after `generate_corpus.py`. Writes to `shared/corpus/beacons/` and two
sibling expected files:
- `shared/corpus/beacons/expected_latest.json`
- `shared/corpus/beacons/expected_history.json`

Beacon fixtures use the same slug-dir layout as cost mode:
    shared/corpus/beacons/<scenario>/<sid>.jsonl          (beacons-latest)
    shared/corpus/beacons/<scenario>/<slug>/<sid>.jsonl   (beacons-history)
so walker's discovery glob finds them when the conformance harness copies one
scenario into a temp tree (latest: scenario dir = slug; history: scenario dir =
projects-root with slug subdirs).

Anchored to the same NOW_UNIX as cost mode so test runs are deterministic.
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CORPUS_BEACONS = ROOT / "corpus" / "beacons"

NOW_UNIX = 1778414400.0  # 2026-05-09 12:00:00 UTC -- matches generate_corpus.py


def iso(unix: float) -> str:
    return datetime.fromtimestamp(unix, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


def assistant_with_text(unix_ts: float, msg_id: str, text: str) -> dict:
    """Build one assistant transcript entry whose content has a single text block."""
    return {
        "type": "assistant",
        "timestamp": iso(unix_ts),
        "message": {
            "id": msg_id,
            "role": "assistant",
            "model": "claude-opus-4-7",
            "content": [{"type": "text", "text": text}],
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        },
    }


def beacon_text(beacon_json_str: str) -> str:
    """Wrap a JSON string in a fenced beacon block embedded in narration."""
    return (
        "Working on it.\n\n"
        f"<progress-beacon>\n{beacon_json_str}\n</progress-beacon>"
    )


def beacon_json(kind: str, eta: int, summary: str, drift: str | None = "nominal") -> str:
    """Serialize a beacon. `drift=None` omits the field (post-fix optional form)."""
    obj = {"kind": kind, "eta_seconds": eta, "summary": summary}
    if drift is not None:
        obj["drift"] = drift
    return json.dumps(obj)


def write_jsonl(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line))
            f.write("\n")


def bias_of(pairs: list[tuple[float, float]]) -> float | None:
    """median(active_elapsed / begin_eta). Even count -> mean of two middle.

    These hand-built fixtures contain no user-prompt entries, so there is no
    idle time to exclude: active_elapsed == actual_elapsed for every pair.
    """
    if not pairs:
        return None
    return statistics.median(sorted(active / eta for eta, active in pairs))


def history_expected(pairs: list[tuple[float, float]], session_count: int) -> dict:
    return {
        "pairs": [{"begin_eta": e, "actual_elapsed": a} for e, a in pairs],
        "session_count": session_count,
        "n_pairs": len(pairs),
        "bias_factor": bias_of(pairs),
    }


# === Scenarios for `beacons-latest` ===

def scenario_clean_lifecycle():
    """begin -> report -> end. Walker returns the end beacon."""
    sid = "session"
    t1, t2, t3 = NOW_UNIX - 600, NOW_UNIX - 350, NOW_UNIX - 100
    lines = [
        assistant_with_text(t1, "msg_clean_001",
            beacon_text(beacon_json("begin", 600, "running tests then committing"))),
        assistant_with_text(t2, "msg_clean_002",
            beacon_text(beacon_json("report", 350, "tests in progress"))),
        assistant_with_text(t3, "msg_clean_003",
            beacon_text(beacon_json("end", 0, "complete, all green"))),
    ]
    expected = {
        "session_id": sid,
        "beacon": {"kind": "end", "eta_seconds": 0,
                   "summary": "complete, all green", "drift": "nominal"},
        "emitted_at": t3,
        "age_seconds": NOW_UNIX - t3,
    }
    return "clean_lifecycle", sid, lines, expected


def scenario_malformed():
    """Beacon block has malformed JSON (unquoted bareword value). Walker skips.

    Note: an earlier version used a trailing comma, but simdjson's on-demand
    parser (cpp) tolerates trailing commas while serde_json (rust) rejects them
    — a divergence the old drift-required check happened to mask. An unquoted
    value is unambiguously invalid and rejected by all four parsers (via the
    tokenizer or the string/number type-check), keeping the impls in lockstep.
    Production beacons are emitted via json.dumps and never hit this path.
    """
    sid = "session"
    t1 = NOW_UNIX - 300
    bad = '{"kind": "begin", "eta_seconds": 180, "summary": broken}'
    lines = [assistant_with_text(t1, "msg_mal_001", beacon_text(bad))]
    expected = {"session_id": sid, "beacon": None,
                "emitted_at": None, "age_seconds": None}
    return "malformed", sid, lines, expected


def scenario_missing_fields():
    """Beacon JSON parses but a STILL-required field (`eta_seconds`) is missing.

    Post-fix, `drift` is optional (covered by `optional_drift`); this fixture
    now omits `eta_seconds` to confirm the parser still rejects genuinely
    incomplete beacons.
    """
    sid = "session"
    t1 = NOW_UNIX - 300
    incomplete = json.dumps({"kind": "begin", "summary": "no eta field",
                             "drift": "nominal"})
    lines = [assistant_with_text(t1, "msg_miss_001", beacon_text(incomplete))]
    expected = {"session_id": sid, "beacon": None,
                "emitted_at": None, "age_seconds": None}
    return "missing_fields", sid, lines, expected


def scenario_optional_drift():
    """Beacon omits `drift` (the new optional form). Walker parses + returns it,
    and the returned `beacon` object omits `drift` too."""
    sid = "session"
    t1 = NOW_UNIX - 300
    lines = [
        assistant_with_text(t1, "msg_opt_001",
            beacon_text(beacon_json("begin", 180, "no drift field", drift=None))),
    ]
    expected = {
        "session_id": sid,
        "beacon": {"kind": "begin", "eta_seconds": 180, "summary": "no drift field"},
        "emitted_at": t1,
        "age_seconds": NOW_UNIX - t1,
    }
    return "optional_drift", sid, lines, expected


def scenario_multiple_in_turn():
    """Three valid beacons (begin, report, report). Walker returns latest."""
    sid = "session"
    t1, t2, t3 = NOW_UNIX - 600, NOW_UNIX - 400, NOW_UNIX - 200
    lines = [
        assistant_with_text(t1, "msg_multi_001",
            beacon_text(beacon_json("begin", 800, "starting up"))),
        assistant_with_text(t2, "msg_multi_002",
            beacon_text(beacon_json("report", 400, "midway"))),
        assistant_with_text(t3, "msg_multi_003",
            beacon_text(beacon_json("report", 200, "almost there"))),
    ]
    expected = {
        "session_id": sid,
        "beacon": {"kind": "report", "eta_seconds": 200,
                   "summary": "almost there", "drift": "nominal"},
        "emitted_at": t3,
        "age_seconds": NOW_UNIX - t3,
    }
    return "multiple_in_turn", sid, lines, expected


# === Scenarios for `beacons-history` ===

def scenario_cross_session_pairs():
    """Two sessions, each one begin+end lifecycle (drift present -> back-compat)."""
    # A: eta=500s, actual=1000s -> ratio 2.0
    a_begin, a_end = NOW_UNIX - 1800, NOW_UNIX - 800
    a_lines = [
        assistant_with_text(a_begin, "msg_a_001",
            beacon_text(beacon_json("begin", 500, "session A start"))),
        assistant_with_text(a_end, "msg_a_002",
            beacon_text(beacon_json("end", 0, "session A done"))),
    ]
    # B: eta=400s, actual=200s -> ratio 0.5
    b_begin, b_end = NOW_UNIX - 600, NOW_UNIX - 400
    b_lines = [
        assistant_with_text(b_begin, "msg_b_001",
            beacon_text(beacon_json("begin", 400, "session B start"))),
        assistant_with_text(b_end, "msg_b_002",
            beacon_text(beacon_json("end", 0, "session B done"))),
    ]
    files = {
        "slug_a/session_a.jsonl": a_lines,
        "slug_b/session_b.jsonl": b_lines,
    }
    return "cross_session_pairs", files, history_expected(
        [(500.0, 1000.0), (400.0, 200.0)], session_count=2)


def scenario_multi_lifecycle():
    """One session, two complete begin->end lifecycles + interleaved reports.

    Beacons omit `drift`, so this also exercises the relaxed required-field set
    in history mode. Lifecycle A: eta=300 actual=200; B: eta=600 actual=600.
    """
    a0, a1, a2 = NOW_UNIX - 3000, NOW_UNIX - 2940, NOW_UNIX - 2800
    b0, b1, b2 = NOW_UNIX - 1200, NOW_UNIX - 900, NOW_UNIX - 600
    lines = [
        assistant_with_text(a0, "msg_ml_001", beacon_text(beacon_json("begin", 300, "lifecycle A", drift=None))),
        assistant_with_text(a1, "msg_ml_002", beacon_text(beacon_json("report", 240, "A midway", drift=None))),
        assistant_with_text(a2, "msg_ml_003", beacon_text(beacon_json("end", 0, "A done", drift=None))),
        assistant_with_text(b0, "msg_ml_004", beacon_text(beacon_json("begin", 600, "lifecycle B", drift=None))),
        assistant_with_text(b1, "msg_ml_005", beacon_text(beacon_json("report", 300, "B midway", drift=None))),
        assistant_with_text(b2, "msg_ml_006", beacon_text(beacon_json("end", 0, "B done", drift=None))),
    ]
    return "multi_lifecycle", {"slug/session.jsonl": lines}, history_expected(
        [(300.0, 200.0), (600.0, 600.0)], session_count=1)


def scenario_orphan_begin():
    """One session with a begin (and report) but no end. No pair emitted."""
    t0, t1 = NOW_UNIX - 1000, NOW_UNIX - 900
    lines = [
        assistant_with_text(t0, "msg_ob_001", beacon_text(beacon_json("begin", 300, "started, never ended", drift=None))),
        assistant_with_text(t1, "msg_ob_002", beacon_text(beacon_json("report", 200, "still going", drift=None))),
    ]
    return "orphan_begin", {"slug/session.jsonl": lines}, history_expected([], session_count=1)


def scenario_orphan_end():
    """One session with an end but no preceding begin. No pair emitted."""
    t0 = NOW_UNIX - 1000
    lines = [
        assistant_with_text(t0, "msg_oe_001", beacon_text(beacon_json("end", 0, "ended with no begin", drift=None))),
    ]
    return "orphan_end", {"slug/session.jsonl": lines}, history_expected([], session_count=1)


def scenario_back_to_back():
    """One session: begin, end, begin, end -> two pairs from one group."""
    t0, t1, t2, t3 = NOW_UNIX - 2000, NOW_UNIX - 1900, NOW_UNIX - 1800, NOW_UNIX - 1500
    lines = [
        assistant_with_text(t0, "msg_bb_001", beacon_text(beacon_json("begin", 100, "first", drift=None))),
        assistant_with_text(t1, "msg_bb_002", beacon_text(beacon_json("end", 0, "first done", drift=None))),
        assistant_with_text(t2, "msg_bb_003", beacon_text(beacon_json("begin", 200, "second", drift=None))),
        assistant_with_text(t3, "msg_bb_004", beacon_text(beacon_json("end", 0, "second done", drift=None))),
    ]
    return "back_to_back", {"slug/session.jsonl": lines}, history_expected(
        [(100.0, 100.0), (200.0, 300.0)], session_count=1)


LATEST_SCENARIOS = (
    scenario_clean_lifecycle,
    scenario_malformed,
    scenario_missing_fields,
    scenario_optional_drift,
    scenario_multiple_in_turn,
)

HISTORY_SCENARIOS = (
    scenario_cross_session_pairs,
    scenario_multi_lifecycle,
    scenario_orphan_begin,
    scenario_orphan_end,
    scenario_back_to_back,
)


def main() -> None:
    if CORPUS_BEACONS.exists():
        for p in sorted(CORPUS_BEACONS.rglob("*"), reverse=True):
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                p.rmdir()
        CORPUS_BEACONS.rmdir()
    CORPUS_BEACONS.mkdir(parents=True, exist_ok=True)

    expected_latest = {}
    for build in LATEST_SCENARIOS:
        scenario, sid, lines, expected = build()
        write_jsonl(CORPUS_BEACONS / scenario / f"{sid}.jsonl", lines)
        expected_latest[scenario] = expected

    expected_history = {}
    for build in HISTORY_SCENARIOS:
        scenario, files, expected = build()
        for rel, lines in files.items():
            write_jsonl(CORPUS_BEACONS / scenario / rel, lines)
        expected_history[scenario] = expected

    meta = {
        "now_unix": NOW_UNIX,
        "note": "Generated by generate_beacon_corpus.py. Do not hand-edit.",
    }
    (CORPUS_BEACONS / "expected_latest.json").write_text(
        json.dumps({"_meta": meta, "fixtures": expected_latest}, indent=2) + "\n",
        encoding="utf-8",
    )
    (CORPUS_BEACONS / "expected_history.json").write_text(
        json.dumps({"_meta": meta, "fixtures": expected_history}, indent=2) + "\n",
        encoding="utf-8",
    )

    file_count = sum(1 for _ in CORPUS_BEACONS.rglob("*.jsonl"))
    print(f"Wrote {file_count} beacon fixture files under {CORPUS_BEACONS}")
    print(f"Wrote {CORPUS_BEACONS / 'expected_latest.json'}")
    print(f"Wrote {CORPUS_BEACONS / 'expected_history.json'}")


if __name__ == "__main__":
    main()
