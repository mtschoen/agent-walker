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


def write_jsonl_mixed(path: Path, entries: list) -> None:
    """Like write_jsonl but accepts a mix of dicts (json-dumped), raw strings
    (written verbatim), and raw bytes (for invalid-UTF-8 lines). Used by the
    A8 dirty-ladder fixtures so we can inject genuinely malformed / blank
    lines around valid entries. Binary mode keeps the bytes exact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        for entry in entries:
            if isinstance(entry, bytes):
                f.write(entry)
            else:
                text = entry if isinstance(entry, str) else json.dumps(entry)
                f.write(text.encode("utf-8"))
            f.write(b"\n")


def dirty_ladder_lines(good_entry: dict) -> list:
    """Reusable A8 ladder of bad/dirty lines surrounding one valid entry.
    Each kind of bad line maps to a `skip` branch in the line filter:
      1. blank line
      2. whitespace-only line
      3. malformed JSON (unclosed object)
      4. malformed JSON (bareword garbage)
      5. non-object root (JSON array)
      6. non-assistant role entry
      7. assistant missing `timestamp`
      8. assistant with unparseable timestamp
      9. assistant with type=assistant but role=user (role mismatch)
    Note: an assistant with empty content but valid metadata IS counted as a
    zero-cost turn per SPEC §Filters (no content-emptiness check), so it
    doesn't belong in the skip-ladder — handled separately if needed.

    Extended rungs (10+): per-field malformed shapes. None of them carries a
    beacon, so impls that reject the whole line (serde) and impls that skip
    only the bad field (simdjson on-demand) observe the same output.
     10. invalid UTF-8 bytes (line-level parse/read failure)
     11. lone-surrogate escape in a root key (key-unescape failure)
     12. message is a bare string, not an object
     13. message is null
     14. timestamp is an empty string
     15. timestamp is a number, not a string
     16. timestamp is a >=19-char string with non-digit date fields
     17. assistant whose content is a bare string (not an array)
     18. content array with non-object / wrong-typed blocks
     19. role is a number
     20. CRLF-terminated but otherwise valid beacon-free assistant turn
    """
    return [
        "",
        "   ",
        '{"type":"assistant","timestamp":',
        "this is not JSON at all",
        json.dumps([1, 2, 3]),
        {"type": "user", "timestamp": iso(NOW_UNIX - 800),
         "message": {"role": "user", "content": "hi"}},
        {"type": "assistant",
         "message": {"role": "assistant", "id": "no-ts",
                     "model": "claude-opus-4-7", "usage": {"input_tokens": 99}}},
        {"type": "assistant", "timestamp": "definitely-not-iso",
         "message": {"role": "assistant", "id": "bad-ts",
                     "model": "claude-opus-4-7", "usage": {"input_tokens": 99}}},
        {"type": "assistant", "timestamp": iso(NOW_UNIX - 500),
         "message": {"role": "user", "id": "role-mismatch",
                     "model": "claude-opus-4-7", "usage": {"input_tokens": 99}}},
        b'\xff\xfe invalid utf-8 line',
        '{"\\ud800": "lone surrogate key"}',
        {"type": "assistant", "timestamp": iso(NOW_UNIX - 480),
         "message": "not an object"},
        {"type": "assistant", "timestamp": iso(NOW_UNIX - 470),
         "message": None},
        {"type": "assistant", "timestamp": "",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "empty ts"}]}},
        {"type": "assistant", "timestamp": 12345,
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "numeric ts"}]}},
        {"type": "assistant", "timestamp": "2026-XX-09TZZ:00:00Z",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "long bad iso"}]}},
        {"type": "assistant", "timestamp": iso(NOW_UNIX - 460),
         "message": {"role": "assistant", "content": "bare string content"}},
        {"type": "assistant", "timestamp": iso(NOW_UNIX - 450),
         "message": {"role": "assistant",
                     "content": ["bare block", 5,
                                 {"type": 7, "text": "type not a string"},
                                 {"type": "text", "text": 9},
                                 {"text": "typeless block"}]}},
        {"type": "assistant", "timestamp": iso(NOW_UNIX - 440),
         "message": {"role": 5,
                     "content": [{"type": "text", "text": "numeric role"}]}},
        json.dumps({"type": "assistant", "timestamp": iso(NOW_UNIX - 430),
                    "message": {"role": "assistant",
                                "content": [{"type": "text",
                                             "text": "crlf line, no beacon"}]}}
                   ) + "\r",
        # Lone-surrogate keys at message and block depth. Strict parsers
        # reject the line; per-key parsers skip the key and find no beacon.
        ('{"type": "assistant", "timestamp": "%s", "message": '
         '{"\\ud800": 1, "role": "assistant", "content": '
         '[{"type": "text", "text": "no beacon, bad msg key"}]}}'
         % iso(NOW_UNIX - 425)),
        ('{"type": "assistant", "timestamp": "%s", "message": '
         '{"role": "assistant", "content": '
         '[{"\\ud800": 2, "type": "text", "text": "no beacon, bad block key"}]}}'
         % iso(NOW_UNIX - 420)),
        # Two text blocks, both beacon-free: exercises the newline-join arm.
        {"type": "assistant", "timestamp": iso(NOW_UNIX - 415),
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "block one"},
                                 {"type": "text", "text": "block two"}]}},
        good_entry,
    ]


def bias_of(pairs: list[tuple[float, float]]) -> float | None:
    """median(active_elapsed / begin_eta). Even count -> mean of two middle.

    These hand-built fixtures contain no user-prompt entries, so there is no
    idle time to exclude: active_elapsed == actual_elapsed for every pair.
    """
    if not pairs:
        return None
    # Per SPEC: when all begin_eta <= 0, the bias is undefined → return None.
    if all(eta <= 0 for eta, _ in pairs):
        return None
    # Compute ratios skipping pairs whose eta <= 0 to avoid div-by-zero.
    ratios = [active / eta for eta, active in pairs if eta > 0]
    if not ratios:
        return None
    return statistics.median(sorted(ratios))


def history_expected(pairs: list[tuple[float, float]], session_count: int) -> dict:
    return {
        "pairs": [{"begin_eta": e, "actual_elapsed": a} for e, a in pairs],
        "session_count": session_count,
        "n_pairs": len(pairs),
        "bias_factor": bias_of(pairs),
    }


def history_expected_with_idle(
    pairs_full: list[tuple[float, float, float]], session_count: int
) -> dict:
    """Pairs encoded as (begin_eta, actual_elapsed, idle_excluded). Used when
    a fixture exercises the idle-gap exclusion path (A15)."""
    pairs = [(eta, actual) for eta, actual, _ in pairs_full]
    if not pairs:
        bias = None
    elif all(eta <= 0 for eta, _, _ in pairs_full):
        bias = None
    else:
        ratios = []
        for eta, actual, idle in pairs_full:
            if eta <= 0:
                continue
            active = max(0.0, actual - idle)
            ratios.append(active / eta)
        bias = statistics.median(sorted(ratios)) if ratios else None
    return {
        "pairs": [{"begin_eta": e, "actual_elapsed": a} for e, a in pairs],
        "session_count": session_count,
        "n_pairs": len(pairs),
        "bias_factor": bias,
    }


def assistant_usage_only(unix_ts: float, msg_id: str) -> dict:
    """An assistant entry with usage but NO content key at all (the corpus's
    'usage-only turn' shape). Per the reference impls (rust/cpp/go) this entry
    is NOT an idle-detection event: only assistant entries WITH content count.
    """
    return {
        "type": "assistant",
        "timestamp": iso(unix_ts),
        "message": {
            "id": msg_id,
            "role": "assistant",
            "model": "claude-opus-4-7",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        },
    }


def assistant_tool_use_only(unix_ts: float, msg_id: str) -> dict:
    """An assistant entry whose content is a single tool_use block - content
    key PRESENT but no text blocks. Per the reference impls this entry IS an
    idle-detection event (the criterion is content presence, not text)."""
    entry = assistant_usage_only(unix_ts, msg_id)
    entry["message"]["content"] = [
        {"type": "tool_use", "id": "toolu_fixture", "name": "Bash",
         "input": {"command": "echo hi"}}
    ]
    return entry


def user_entry(unix_ts: float, msg_id: str, content) -> dict:
    """A `type: "user"` entry. content may be a bare string (real user prompt)
    or a list of content blocks (e.g., tool_result blocks). Used by A15 to
    distinguish real-user idle gaps from agent-waiting-on-tool gaps."""
    return {
        "type": "user",
        "timestamp": iso(unix_ts),
        "message": {
            "id": msg_id,
            "role": "user",
            "content": content,
        },
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


def scenario_end_without_eta():
    """begin -> end where the end omits `eta_seconds`. Per SPEC the field is
    optional for kind=end (defaults to 0): agents routinely omit it on end
    beacons, and rejecting them left lifecycles permanently open (the
    recency-nudge hook then nagged forever). Walker must return the end
    beacon with eta_seconds normalized to 0."""
    sid = "session"
    t1, t2 = NOW_UNIX - 500, NOW_UNIX - 100
    end_no_eta = json.dumps({"kind": "end", "summary": "done, eta omitted"})
    lines = [
        assistant_with_text(t1, "msg_ene_001",
            beacon_text(beacon_json("begin", 400, "lifecycle with eta-less end",
                                    drift=None))),
        assistant_with_text(t2, "msg_ene_002", beacon_text(end_no_eta)),
    ]
    expected = {
        "session_id": sid,
        "beacon": {"kind": "end", "eta_seconds": 0,
                   "summary": "done, eta omitted"},
        "emitted_at": t2,
        "age_seconds": NOW_UNIX - t2,
    }
    return "end_without_eta", sid, lines, expected


def scenario_report_without_eta():
    """`eta_seconds` stays REQUIRED for non-end kinds: a report omitting it
    must be rejected, leaving the earlier complete beacon as latest."""
    sid = "session"
    t1, t2 = NOW_UNIX - 500, NOW_UNIX - 100
    report_no_eta = json.dumps({"kind": "report", "summary": "no eta report"})
    lines = [
        assistant_with_text(t1, "msg_rne_001",
            beacon_text(beacon_json("begin", 400, "valid begin", drift=None))),
        assistant_with_text(t2, "msg_rne_002", beacon_text(report_no_eta)),
    ]
    expected = {
        "session_id": sid,
        "beacon": {"kind": "begin", "eta_seconds": 400, "summary": "valid begin"},
        "emitted_at": t1,
        "age_seconds": NOW_UNIX - t1,
    }
    return "report_without_eta", sid, lines, expected


def scenario_end_with_bad_eta():
    """An end beacon whose `eta_seconds` is PRESENT but non-numeric must be
    rejected (type mismatch is not the same as absence — only absence gets
    the kind=end default). The earlier valid begin stays latest."""
    sid = "session"
    t1, t2 = NOW_UNIX - 500, NOW_UNIX - 100
    bad_end = json.dumps({"kind": "end", "eta_seconds": "soon",
                          "summary": "bad eta type"})
    lines = [
        assistant_with_text(t1, "msg_ebe_001",
            beacon_text(beacon_json("begin", 400, "before bad-eta end",
                                    drift=None))),
        assistant_with_text(t2, "msg_ebe_002", beacon_text(bad_end)),
    ]
    expected = {
        "session_id": sid,
        "beacon": {"kind": "begin", "eta_seconds": 400,
                   "summary": "before bad-eta end"},
        "emitted_at": t1,
        "age_seconds": NOW_UNIX - t1,
    }
    return "end_with_bad_eta", sid, lines, expected


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


def scenario_two_text_blocks():
    """An assistant turn whose content has TWO text blocks; the beacon sits in
    the second. Impls must join text blocks with a newline before matching,
    so the beacon is still found (exercises the multi-block join branch)."""
    sid = "session"
    t1 = NOW_UNIX - 150
    entry = {
        "type": "assistant",
        "timestamp": iso(t1),
        "message": {
            "id": "msg_ttb_001",
            "role": "assistant",
            "model": "claude-opus-4-7",
            "content": [
                {"type": "text", "text": "First block narration."},
                {"type": "text", "text": beacon_text(
                    beacon_json("report", 120, "beacon in second block"))},
            ],
            "usage": {"input_tokens": 100, "output_tokens": 50},
        },
    }
    expected = {
        "session_id": sid,
        "beacon": {"kind": "report", "eta_seconds": 120,
                   "summary": "beacon in second block", "drift": "nominal"},
        "emitted_at": t1,
        "age_seconds": NOW_UNIX - t1,
    }
    return "two_text_blocks", sid, [entry], expected


def scenario_wrong_typed_fields():
    """Beacon bodies that parse as JSON but carry wrong-typed fields: kind as
    a number, summary as an array, drift as a number, and a begin whose
    eta_seconds is a string. Every impl must reject all four (silently skip),
    leaving beacon null."""
    sid = "session"
    t = NOW_UNIX - 400
    bodies = [
        json.dumps({"kind": 5, "eta_seconds": 60, "summary": "kind not str"}),
        json.dumps({"kind": "begin", "eta_seconds": 60, "summary": ["arr"]}),
        json.dumps({"kind": "begin", "eta_seconds": 60, "summary": "x",
                    "drift": 9}),
        json.dumps({"kind": "begin", "eta_seconds": "soon", "summary": "x"}),
    ]
    lines = [
        assistant_with_text(t + 10 * i, f"msg_wt_{i:03d}", beacon_text(body))
        for i, body in enumerate(bodies)
    ]
    expected = {"session_id": sid, "beacon": None,
                "emitted_at": None, "age_seconds": None}
    return "wrong_typed_fields", sid, lines, expected


def scenario_malformed_bodies():
    """Beacon bodies that fail at JSON tokenization or carry structural
    surprises: an unterminated string, two concatenated objects (the first a
    non-beacon, so impls that tolerate trailing content still find nothing),
    a bare-key object, and a lone-surrogate key alongside a begin that lacks
    eta_seconds. Every impl must skip them all: beacon null."""
    sid = "session"
    t = NOW_UNIX - 400
    bodies = [
        '{"kind": "x", "summary": "oops}',
        '{"a":1}{"b":2}',
        '{"a"}',
        '{"\\ud800": 1, "kind": "begin", "summary": "x"}',
        '{"eta_seconds": 5, "summary": "no kind"}',
        '{"kind": "begin", "eta_seconds": 5}',
    ]
    lines = [
        assistant_with_text(t + 10 * i, f"msg_mb_{i:03d}", beacon_text(body))
        for i, body in enumerate(bodies)
    ]
    expected = {"session_id": sid, "beacon": None,
                "emitted_at": None, "age_seconds": None}
    return "malformed_bodies", sid, lines, expected


def scenario_end_int_eta():
    """A lifecycle closed by an end beacon whose eta_seconds is present as a
    JSON integer (regression guard around the d905408 optional-eta fix: the
    present-int form must parse, not fall into the absent-or-rejected arms)."""
    sid = "session"
    t1, t2 = NOW_UNIX - 500, NOW_UNIX - 100
    end_int = json.dumps({"kind": "end", "eta_seconds": 7,
                          "summary": "int eta on end"})
    lines = [
        assistant_with_text(t1, "msg_eie_001",
            beacon_text(beacon_json("begin", 400, "before int-eta end",
                                    drift=None))),
        assistant_with_text(t2, "msg_eie_002", beacon_text(end_int)),
    ]
    expected = {
        "session_id": sid,
        "beacon": {"kind": "end", "eta_seconds": 7,
                   "summary": "int eta on end"},
        "emitted_at": t2,
        "age_seconds": NOW_UNIX - t2,
    }
    return "end_int_eta", sid, lines, expected


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


# --- A9 / A10 / A12 / A13 / A14 scenarios for beacons-latest ---------------

def scenario_subagent_latest():
    """A9: walker queries a session_id whose transcript lives in a subagent
    file (`subagents/agent-<session_id>.jsonl`), not a top-level parent file.

    Layout: scenario dir is the slug. Beacons-latest discovery is:
      <root>/*/<session_id>.jsonl                         (parent)
      <root>/*/*/subagents/agent-<session_id>.jsonl       (subagent)
    so the subagent file IS named for the session id. We supply
    `--session-id myagent` and place the beacon in
    `<scenario>/parent/subagents/agent-myagent.jsonl`.
    """
    sid = "myagent"  # the subagent's session id (= --session-id arg)
    parent_session = "parent"  # the enclosing parent transcript's stem
    t1, t2 = NOW_UNIX - 400, NOW_UNIX - 100
    parent_lines = [
        assistant_with_text(t1, "msg_sub_p001", "no beacon in the parent"),
    ]
    subagent_lines = [
        assistant_with_text(t2, "msg_sub_a001",
            beacon_text(beacon_json("end", 0, "subagent finished"))),
    ]
    files = {
        f"{parent_session}.jsonl": parent_lines,
        f"{parent_session}/subagents/agent-{sid}.jsonl": subagent_lines,
    }
    expected = {
        "session_id": sid,
        "beacon": {"kind": "end", "eta_seconds": 0,
                   "summary": "subagent finished", "drift": "nominal"},
        "emitted_at": t2,
        "age_seconds": NOW_UNIX - t2,
    }
    return "subagent_latest", files, expected


def scenario_int_numeric_forms():
    """A10: `eta_seconds` is a JSON integer (30, not 30.0), and `beats_left`
    is present. Walker must parse int-typed numeric fields without rejecting
    the beacon as malformed.
    """
    sid = "session"
    t1 = NOW_UNIX - 200
    raw_int = json.dumps({
        "kind": "begin",
        "eta_seconds": 30,           # int, not float
        "summary": "int eta",
        "drift": "nominal",
        "beats_left": 5,             # int
    })
    lines = [assistant_with_text(t1, "msg_int_001", beacon_text(raw_int))]
    expected = {
        "session_id": sid,
        "beacon": {"kind": "begin", "eta_seconds": 30,
                   "summary": "int eta", "drift": "nominal",
                   "beats_left": 5},
        "emitted_at": t1,
        "age_seconds": NOW_UNIX - t1,
    }
    return "int_numeric_forms", sid, lines, expected


def scenario_matcher_edges():
    """A12: `<progress-beacon>` open tag not followed by `{`, and a separate
    turn with `{...}` followed by trailing non-whitespace text before the
    `</progress-beacon>` close tag. Walker must NOT emit a spurious beacon —
    the latest result must be None for both shapes.

    Two turns so we exercise both shapes; the LATER turn is the trailing-text
    one, and walker walks backwards.
    """
    sid = "session"
    t1, t2 = NOW_UNIX - 300, NOW_UNIX - 200
    # Open tag not followed by `{`: tag present, body is plain text.
    lines = [
        assistant_with_text(t1, "msg_me_001",
            "Working... <progress-beacon>oops not JSON</progress-beacon> end"),
        # `{...}` present with trailing junk before close tag — should not match.
        assistant_with_text(t2, "msg_me_002",
            "Working... <progress-beacon>{\"kind\":\"begin\","
            "\"eta_seconds\":60,\"summary\":\"unterminated\"} <other> end"),
    ]
    expected = {
        "session_id": sid,
        "beacon": None,
        "emitted_at": None,
        "age_seconds": None,
    }
    return "matcher_edges", sid, lines, expected


def scenario_escape_chars_latest():
    """A13: beacon summary contains control chars and special punctuation that
    must be JSON-escaped on output. Walker must emit valid JSON.
    """
    sid = "session"
    t1 = NOW_UNIX - 100
    # Summary with quotes, backslash, and control bytes. Build via dict so
    # json.dumps does the escaping for us (matches what walker will emit).
    nasty = "quote\"slash\\back\nnewline\ttab\rret\bbs\fff\x01ctl"
    raw = json.dumps({
        "kind": "report",
        "eta_seconds": 42,
        "summary": nasty,
        "drift": "nominal",
    })
    lines = [assistant_with_text(t1, "msg_esc_001", beacon_text(raw))]
    expected = {
        "session_id": sid,
        "beacon": {"kind": "report", "eta_seconds": 42,
                   "summary": nasty, "drift": "nominal"},
        "emitted_at": t1,
        "age_seconds": NOW_UNIX - t1,
    }
    return "escape_chars", sid, lines, expected


def scenario_dirty_ladder_latest():
    """A8: dirty/skip-ladder transcript with one valid beacon at the end.
    Walker must skip every bad line and return the trailing beacon.
    """
    sid = "session"
    t1 = NOW_UNIX - 100
    valid = beacon_json("report", 99, "after the ladder")
    valid_line = assistant_with_text(t1, "msg_dl_good", beacon_text(valid))
    # Inject bad lines as raw strings; write_jsonl only json-dumps dicts,
    # so we need to write them manually below. To keep write_jsonl's contract
    # simple, just include them as additional valid-ish entries that get
    # filtered out by the line filter — except we DO want to exercise the
    # malformed-JSON / blank-line / role-mismatch ladder. Tactic: write the
    # mixed file manually below in main(), keyed by scenario name.
    return "dirty_ladder_latest", sid, valid_line, {
        "session_id": sid,
        "beacon": {"kind": "report", "eta_seconds": 99,
                   "summary": "after the ladder", "drift": "nominal"},
        "emitted_at": t1,
        "age_seconds": NOW_UNIX - t1,
    }


# --- A9 / A11 / A13 / A15 scenarios for beacons-history --------------------

def scenario_subagent_history():
    """A9: history mode walks subagent transcripts. Layout (per conformance
    L242-251): scenario dir is the projects-root → subdirs are slugs.
    Beacon pair lives in `<slug>/<sid>/subagents/agent-*.jsonl`.
    """
    begin_ts, end_ts = NOW_UNIX - 1000, NOW_UNIX - 700  # 300s actual; eta=200 → 1.5
    sub_lines = [
        assistant_with_text(begin_ts, "msg_sah_001",
            beacon_text(beacon_json("begin", 200, "subagent A start", drift=None))),
        assistant_with_text(end_ts, "msg_sah_002",
            beacon_text(beacon_json("end", 0, "subagent A done", drift=None))),
    ]
    files = {
        "slug/session/subagents/agent-acompact-x.jsonl": sub_lines,
    }
    return "subagent_history", files, history_expected(
        [(200.0, 300.0)], session_count=1)


def scenario_even_pair_count():
    """A11: even number of pairs → median is mean of the two middle ratios.
    Four pairs with ratios 0.5, 1.0, 2.0, 4.0 → median = (1.0 + 2.0) / 2 = 1.5.
    """
    # Pair 1: eta=200, actual=100 → 0.5
    a0, a1 = NOW_UNIX - 3000, NOW_UNIX - 2900
    # Pair 2: eta=100, actual=100 → 1.0
    b0, b1 = NOW_UNIX - 2800, NOW_UNIX - 2700
    # Pair 3: eta=100, actual=200 → 2.0
    c0, c1 = NOW_UNIX - 2600, NOW_UNIX - 2400
    # Pair 4: eta=100, actual=400 → 4.0
    d0, d1 = NOW_UNIX - 2300, NOW_UNIX - 1900
    lines = [
        assistant_with_text(a0, "msg_ev_a0", beacon_text(beacon_json("begin", 200, "p1", drift=None))),
        assistant_with_text(a1, "msg_ev_a1", beacon_text(beacon_json("end", 0, "p1 done", drift=None))),
        assistant_with_text(b0, "msg_ev_b0", beacon_text(beacon_json("begin", 100, "p2", drift=None))),
        assistant_with_text(b1, "msg_ev_b1", beacon_text(beacon_json("end", 0, "p2 done", drift=None))),
        assistant_with_text(c0, "msg_ev_c0", beacon_text(beacon_json("begin", 100, "p3", drift=None))),
        assistant_with_text(c1, "msg_ev_c1", beacon_text(beacon_json("end", 0, "p3 done", drift=None))),
        assistant_with_text(d0, "msg_ev_d0", beacon_text(beacon_json("begin", 100, "p4", drift=None))),
        assistant_with_text(d1, "msg_ev_d1", beacon_text(beacon_json("end", 0, "p4 done", drift=None))),
    ]
    return "even_pair_count", {"slug/session.jsonl": lines}, history_expected(
        [(200.0, 100.0), (100.0, 100.0), (100.0, 200.0), (100.0, 400.0)],
        session_count=1)


def scenario_all_zero_eta():
    """A11: every pair has begin_eta == 0 → all ratios filtered out →
    bias_factor must be null. Two pairs guarantee the all-zero branch fires
    even if an impl preserves at least one ratio.
    """
    a0, a1 = NOW_UNIX - 2000, NOW_UNIX - 1900
    b0, b1 = NOW_UNIX - 1800, NOW_UNIX - 1700
    lines = [
        assistant_with_text(a0, "msg_az_a0", beacon_text(beacon_json("begin", 0, "zero-eta a", drift=None))),
        assistant_with_text(a1, "msg_az_a1", beacon_text(beacon_json("end", 0, "a done", drift=None))),
        assistant_with_text(b0, "msg_az_b0", beacon_text(beacon_json("begin", 0, "zero-eta b", drift=None))),
        assistant_with_text(b1, "msg_az_b1", beacon_text(beacon_json("end", 0, "b done", drift=None))),
    ]
    return "all_zero_eta", {"slug/session.jsonl": lines}, history_expected(
        [(0.0, 100.0), (0.0, 100.0)], session_count=1)


def scenario_idle_gap_history():
    """A15: a real user prompt inside the begin→end span creates an idle gap
    that must be excluded from bias_factor. A separate `type:"user"` whose
    content is a tool_result array must NOT count as idle.

    Layout:
        begin (t0)
            ... agent work ...
            tool_use → tool_result(user, list-content) at t_tool_result
            ... more work ...
            REAL user prompt at t_user (gap of 100s before this)
            ... more work ...
        end (t1)

    Span = t1 - t0 = 1000s. Real-user-gap = (t_user - t_prev_assistant).
    With prev_assistant at t_user - 100, the idle clip is 100s.
    bias_factor = (actual - idle) / eta = (1000 - 100) / 500 = 1.8.
    """
    t0 = NOW_UNIX - 2000
    t_mid_1 = NOW_UNIX - 1800
    # Tool_result user entry — NOT counted as idle. Sits between two assistant turns.
    t_tool_result = NOW_UNIX - 1700
    t_mid_2 = NOW_UNIX - 1600
    # Real user prompt 100s after the previous assistant turn = NOW_UNIX-1500.
    t_user = NOW_UNIX - 1500
    t_mid_3 = NOW_UNIX - 1400
    t1 = NOW_UNIX - 1000
    lines = [
        assistant_with_text(t0, "msg_idle_begin",
            beacon_text(beacon_json("begin", 500, "idle gap test", drift=None))),
        assistant_with_text(t_mid_1, "msg_idle_m1", "agent working"),
        user_entry(t_tool_result, "msg_idle_tool", [
            {"type": "tool_result", "tool_use_id": "x", "content": "ok"}
        ]),
        assistant_with_text(t_mid_2, "msg_idle_m2", "agent continuing after tool"),
        # Real user prompt — gap (t_user - t_mid_2) = 100s should be excluded.
        user_entry(t_user, "msg_idle_user", "real user follow-up"),
        assistant_with_text(t_mid_3, "msg_idle_m3", "agent resuming"),
        assistant_with_text(t1, "msg_idle_end",
            beacon_text(beacon_json("end", 0, "idle gap done", drift=None))),
    ]
    return "idle_gap_history", {"slug/session.jsonl": lines}, history_expected_with_idle(
        [(500.0, 1000.0, 100.0)], session_count=1)


def scenario_usage_only_idle():
    """A16: contentless and text-less assistant entries vs idle detection.

    Between begin and the real user prompt sit two special assistant turns:
      - tool_use-only (content present, no text blocks)  -> IS an event
      - usage-only (no content key at all)               -> is NOT an event
    The idle gap before the real user prompt must therefore be measured from
    the tool_use-only turn. The three behaviors give three distinct biases:
      correct:                   idle = t_user - t_tool_use = 400 -> bias 1.2
      count usage-only as event: idle = t_user - t_usage    = 300 -> bias 1.4
      require text for events:   idle = t_user - t_begin    = 600 -> bias 0.8
    so both the known Zig divergence AND the tempting overcorrection fail.
    """
    t_begin = NOW_UNIX - 3000
    t_tool_use = NOW_UNIX - 2800
    t_usage = NOW_UNIX - 2700
    t_user = NOW_UNIX - 2400
    t_resume = NOW_UNIX - 2200
    t_end = NOW_UNIX - 2000
    lines = [
        assistant_with_text(t_begin, "msg_uo_begin",
            beacon_text(beacon_json("begin", 500, "usage-only idle test", drift=None))),
        assistant_tool_use_only(t_tool_use, "msg_uo_tool"),
        assistant_usage_only(t_usage, "msg_uo_usage"),
        user_entry(t_user, "msg_uo_user", "real user follow-up"),
        assistant_with_text(t_resume, "msg_uo_resume", "agent resuming"),
        assistant_with_text(t_end, "msg_uo_end",
            beacon_text(beacon_json("end", 0, "usage-only idle done", drift=None))),
    ]
    return "usage_only_idle", {"slug/session.jsonl": lines}, history_expected_with_idle(
        [(500.0, 1000.0, 400.0)], session_count=1)


def scenario_end_without_eta_history():
    """A lifecycle closed by an eta-less end must still pair (the end's own
    eta is never used in pairing; only its timestamp closes the span).
    begin eta=300 at t0, end (no eta_seconds) at t0+450 -> pair (300, 450)."""
    t0, t1 = NOW_UNIX - 1500, NOW_UNIX - 1050
    end_no_eta = json.dumps({"kind": "end", "summary": "closing without eta"})
    lines = [
        assistant_with_text(t0, "msg_eneh_001",
            beacon_text(beacon_json("begin", 300, "eta-less end pairing",
                                    drift=None))),
        assistant_with_text(t1, "msg_eneh_002", beacon_text(end_no_eta)),
    ]
    return "end_without_eta_history", {"slug/session.jsonl": lines}, history_expected(
        [(300.0, 450.0)], session_count=1)


def scenario_pre_window_begin():
    """A begin beacon OLDER than window_lo (= max(now - period, win_start);
    the conformance runner pins period to 7 days) followed by an in-window
    end: the begin is window-filtered out, so the end is an orphan and no
    pair forms."""
    t0 = NOW_UNIX - 700000  # ~8.1 days ago, before the 7-day window
    t1 = NOW_UNIX - 600
    lines = [
        assistant_with_text(t0, "msg_pwb_001",
            beacon_text(beacon_json("begin", 300, "pre-window begin",
                                    drift=None))),
        assistant_with_text(t1, "msg_pwb_002",
            beacon_text(beacon_json("end", 0, "in-window end", drift=None))),
    ]
    return "pre_window_begin", {"slug/session.jsonl": lines}, history_expected(
        [], session_count=1)


def scenario_dirty_ladder_history():
    """A8 for history: a dirty-line ladder in a session group that also
    contains one valid begin/end lifecycle. Walker must skip every bad line
    without crashing and emit exactly one pair.
    """
    t0, t1 = NOW_UNIX - 1200, NOW_UNIX - 900   # eta=300, actual=300 → 1.0
    valid = [
        assistant_with_text(t0, "msg_dlh_begin",
            beacon_text(beacon_json("begin", 300, "dirty history begin", drift=None))),
        assistant_with_text(t1, "msg_dlh_end",
            beacon_text(beacon_json("end", 0, "dirty history end", drift=None))),
    ]
    return "dirty_ladder_history", {"slug/session.jsonl": valid}, history_expected(
        [(300.0, 300.0)], session_count=1)


LATEST_SCENARIOS = (
    scenario_clean_lifecycle,
    scenario_malformed,
    scenario_missing_fields,
    scenario_optional_drift,
    scenario_multiple_in_turn,
    # New (Phase 3 batch B):
    scenario_int_numeric_forms,
    scenario_matcher_edges,
    scenario_escape_chars_latest,
    # Optional eta_seconds on end beacons (2026-06-10 nudge-bug fix):
    scenario_end_without_eta,
    scenario_report_without_eta,
    scenario_end_with_bad_eta,
    # Coverage-regression batch (2026-06-10):
    scenario_two_text_blocks,
    scenario_wrong_typed_fields,
    scenario_end_int_eta,
    scenario_malformed_bodies,
)

# scenario_subagent_latest is multi-file; scenario_dirty_ladder_latest needs
# raw-line injection. Both are handled out-of-band in main().
LATEST_SCENARIOS_MULTIFILE = (
    scenario_subagent_latest,
)
LATEST_SCENARIOS_WITH_DIRTY = (
    scenario_dirty_ladder_latest,
)

HISTORY_SCENARIOS = (
    scenario_cross_session_pairs,
    scenario_multi_lifecycle,
    scenario_orphan_begin,
    scenario_orphan_end,
    scenario_back_to_back,
    # New (Phase 3 batch B):
    scenario_subagent_history,
    scenario_even_pair_count,
    scenario_all_zero_eta,
    scenario_idle_gap_history,
    scenario_dirty_ladder_history,
    # A16 (Zig bias_factor divergence repro):
    scenario_usage_only_idle,
    # Optional eta_seconds on end beacons (2026-06-10 nudge-bug fix):
    scenario_end_without_eta_history,
    # Coverage-regression batch (2026-06-10):
    scenario_pre_window_begin,
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

    # Multi-file latest scenarios (e.g., subagent transcript discovery).
    for build in LATEST_SCENARIOS_MULTIFILE:
        scenario, files, expected = build()
        for rel, lines in files.items():
            write_jsonl(CORPUS_BEACONS / scenario / rel, lines)
        expected_latest[scenario] = expected

    # Dirty-ladder latest: bracket the valid beacon turn with malformed lines.
    for build in LATEST_SCENARIOS_WITH_DIRTY:
        scenario, sid, good_entry, expected = build()
        entries = dirty_ladder_lines(good_entry)
        write_jsonl_mixed(CORPUS_BEACONS / scenario / f"{sid}.jsonl", entries)
        expected_latest[scenario] = expected

    expected_history = {}
    for build in HISTORY_SCENARIOS:
        scenario, files, expected = build()
        # dirty_ladder_history: inject the ladder into the session file.
        if scenario == "dirty_ladder_history":
            for rel, lines in files.items():
                # Take the first lifecycle's begin/end and interleave with
                # bad lines so the walker has to filter them.
                good_begin = lines[0]
                good_end = lines[1]
                mixed = dirty_ladder_lines(good_begin) + [good_end]
                write_jsonl_mixed(CORPUS_BEACONS / scenario / rel, mixed)
        else:
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
