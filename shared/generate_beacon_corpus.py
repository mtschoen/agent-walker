"""Synthesize beacon-mode fixtures + expected outputs.

Run after `generate_corpus.py`. Writes to `shared/corpus/beacons/` and two
sibling expected files:
- `shared/corpus/beacons/expected_latest.json`
- `shared/corpus/beacons/expected_history.json`

Beacon fixtures use the same slug-dir layout as cost mode:
    shared/corpus/beacons/<scenario>/<sid>.jsonl
so walker's discovery glob (`<projects-root>/*/<sid>.jsonl`) finds them when
the conformance harness copies one scenario into a temp tree.

Anchored to the same NOW_UNIX as cost mode so test runs are deterministic.
"""
from __future__ import annotations

import json
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


def beacon_text(beacon_json: str) -> str:
    """Wrap a JSON string in a fenced beacon block embedded in narration."""
    return (
        "Working on it.\n\n"
        f"<progress-beacon>\n{beacon_json}\n</progress-beacon>"
    )


def beacon_json(kind: str, eta: int, summary: str, drift: str = "nominal") -> str:
    return json.dumps({
        "kind": kind,
        "eta_seconds": eta,
        "summary": summary,
        "drift": drift,
    })


def write_jsonl(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line))
            f.write("\n")


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
    """Beacon block has malformed JSON (trailing comma). Walker silently skips."""
    sid = "session"
    t1 = NOW_UNIX - 300
    bad = '{"kind": "begin", "eta_seconds": 180, "summary": "broken",}'
    lines = [assistant_with_text(t1, "msg_mal_001", beacon_text(bad))]
    expected = {"session_id": sid, "beacon": None,
                "emitted_at": None, "age_seconds": None}
    return "malformed", sid, lines, expected


def scenario_missing_fields():
    """Beacon JSON parses but `drift` missing. Walker silently skips."""
    sid = "session"
    t1 = NOW_UNIX - 300
    incomplete = json.dumps({"kind": "begin", "eta_seconds": 180,
                             "summary": "no drift field"})
    lines = [assistant_with_text(t1, "msg_miss_001", beacon_text(incomplete))]
    expected = {"session_id": sid, "beacon": None,
                "emitted_at": None, "age_seconds": None}
    return "missing_fields", sid, lines, expected


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


# === Scenario for `beacons-history` ===

def scenario_cross_session_pairs():
    """Two sessions, each begin+end. bias_factor = median(actual/eta)."""
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
    expected = {
        "pairs": [
            {"begin_eta": 500.0, "actual_elapsed": 1000.0},
            {"begin_eta": 400.0, "actual_elapsed": 200.0},
        ],
        "session_count": 2,
        "n_pairs": 2,
        "bias_factor": 1.25,
    }
    files = {
        "slug_a/session_a.jsonl": a_lines,
        "slug_b/session_b.jsonl": b_lines,
    }
    return "cross_session_pairs", files, expected


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
    for build in (
        scenario_clean_lifecycle,
        scenario_malformed,
        scenario_missing_fields,
        scenario_multiple_in_turn,
    ):
        scenario, sid, lines, expected = build()
        write_jsonl(CORPUS_BEACONS / scenario / f"{sid}.jsonl", lines)
        expected_latest[scenario] = expected

    history_scenario, history_files, history_expected = scenario_cross_session_pairs()
    for rel, lines in history_files.items():
        write_jsonl(CORPUS_BEACONS / history_scenario / rel, lines)

    meta = {
        "now_unix": NOW_UNIX,
        "note": "Generated by generate_beacon_corpus.py. Do not hand-edit.",
    }
    (CORPUS_BEACONS / "expected_latest.json").write_text(
        json.dumps({"_meta": meta, "fixtures": expected_latest}, indent=2) + "\n",
        encoding="utf-8",
    )
    (CORPUS_BEACONS / "expected_history.json").write_text(
        json.dumps({"_meta": meta, "fixture": history_expected}, indent=2) + "\n",
        encoding="utf-8",
    )

    file_count = sum(1 for _ in CORPUS_BEACONS.rglob("*.jsonl"))
    print(f"Wrote {file_count} beacon fixture files under {CORPUS_BEACONS}")
    print(f"Wrote {CORPUS_BEACONS / 'expected_latest.json'}")
    print(f"Wrote {CORPUS_BEACONS / 'expected_history.json'}")


if __name__ == "__main__":
    main()
