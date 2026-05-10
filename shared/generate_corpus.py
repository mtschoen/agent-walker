"""Synthesize the conformance corpus and compute expected outputs in one pass.

Run from anywhere; writes to `shared/corpus/` and `shared/expected.json`
relative to this file. Re-run any time the fixtures or pricing change.

Each fixture is a small JSONL file (or directory of files) under
`shared/corpus/`. The expected output for each fixture is computed by the
SAME logic the spec mandates, then locked into `shared/expected.json`.
Languages must reproduce these values to within $0.01.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CORPUS = ROOT / "corpus"
EXPECTED_PATH = ROOT / "expected.json"

# Pinned "now" for conformance: 2026-05-09 12:00:00 UTC.
NOW_UNIX = 1778414400.0
PERIOD_SECONDS = 86400  # 1 day -> period_cutoff = NOW - 1 day
WIN_START_UNIX = NOW_UNIX - 6 * 3600  # 6 hours ago

# Pricing: must match SPEC.md.
RATES = {
    "opus": (5.0, 25.0),
    "sonnet": (3.0, 15.0),
    "haiku": (1.0, 5.0),
}


def rates_for(model_id: str) -> tuple[float, float]:
    name = (model_id or "").lower()
    for family, r in RATES.items():
        if family in name:
            return r
    return RATES["sonnet"]


def cost_for(usage: dict, model_id: str) -> float:
    inp, out = rates_for(model_id)
    i = int(usage.get("input_tokens", 0) or 0)
    r = int(usage.get("cache_read_input_tokens", 0) or 0)
    w = int(usage.get("cache_creation_input_tokens", 0) or 0)
    o = int(usage.get("output_tokens", 0) or 0)
    return (i * inp + r * inp * 0.10 + w * inp * 1.25 + o * out) / 1_000_000


def iso(unix: float) -> str:
    return datetime.fromtimestamp(unix, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


def turn(model: str, ts_unix: float, *, msg_id: str | None,
         input_tokens: int = 0, output_tokens: int = 0,
         cache_read: int = 0, cache_write: int = 0) -> dict:
    return {
        "type": "assistant",
        "timestamp": iso(ts_unix),
        "message": {
            "role": "assistant",
            "id": msg_id,
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_write,
            },
        },
    }


def write_jsonl(path: Path, entries: list[dict | str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(entry if isinstance(entry, str) else json.dumps(entry))
            f.write("\n")


# Build fixtures and walk them inline to compute expected outputs.
# Each fixture entry: (group_key, files_dict). files_dict maps relative path
# (under CORPUS) to its list of JSONL entries (dicts or raw strings for
# malformed-line cases).

period_cutoff = NOW_UNIX - PERIOD_SECONDS

# Convenient time anchors for fixtures
FRESH = NOW_UNIX - 1800       # 30 min ago: in trailing AND in window
RECENT = NOW_UNIX - 5 * 3600  # 5h ago: in trailing AND in window
PRE_WIN = NOW_UNIX - 12 * 3600  # 12h ago: in trailing, NOT in window
OLD = NOW_UNIX - 2 * 86400    # 2 days ago: out of trailing AND window


def fixture_01_single_parent():
    """One parent with mixed-model turns; all in BOTH buckets."""
    slug = "01-single-parent"
    sid = "alpha"
    turns = [
        turn("claude-opus-4-7", FRESH, msg_id="m01",
             input_tokens=1000, output_tokens=500,
             cache_read=10000, cache_write=2000),
        turn("claude-sonnet-4-6", RECENT, msg_id="m02",
             input_tokens=500, output_tokens=200,
             cache_read=5000, cache_write=0),
        turn("claude-haiku-4-5", FRESH, msg_id="m03",
             input_tokens=100, output_tokens=50),
    ]
    return slug, {f"{sid}.jsonl": turns}


def fixture_02_parent_acompact():
    """Parent + acompact-subagent share msg ids -> dedup must apply."""
    slug = "02-parent-acompact"
    sid = "beta"
    parent_turns = [
        turn("claude-opus-4-7", FRESH, msg_id="shared-1",
             input_tokens=2000, output_tokens=1000),
        turn("claude-opus-4-7", RECENT, msg_id="shared-2",
             input_tokens=2000, output_tokens=1000),
        turn("claude-sonnet-4-6", FRESH, msg_id="parent-only",
             input_tokens=500, output_tokens=200),
    ]
    subagent_turns = [
        turn("claude-opus-4-7", FRESH, msg_id="shared-1",
             input_tokens=2000, output_tokens=1000),  # dup -> skip
        turn("claude-opus-4-7", RECENT, msg_id="shared-2",
             input_tokens=2000, output_tokens=1000),  # dup -> skip
        turn("claude-haiku-4-5", FRESH, msg_id="sub-only",
             input_tokens=300, output_tokens=100),
    ]
    return slug, {
        f"{sid}.jsonl": parent_turns,
        f"{sid}/subagents/agent-acompact-x.jsonl": subagent_turns,
    }


def fixture_03_malformed_lines():
    """Malformed JSON, missing fields, non-assistant role -- all skipped."""
    slug = "03-malformed-lines"
    sid = "gamma"
    entries = [
        "this is not JSON",
        '{"unclosed":',
        json.dumps({"type": "user", "message": {"role": "user"}}),
        json.dumps({"type": "assistant", "message": {"role": "user"}}),  # wrong role
        json.dumps({"type": "assistant", "message": {"role": "assistant", "id": "no-ts",
                    "model": "claude-opus-4-7", "usage": {"input_tokens": 100}}}),
        # missing timestamp -> skipped
        turn("claude-opus-4-7", FRESH, msg_id="ok",
             input_tokens=1000, output_tokens=500),
        json.dumps({"type": "assistant", "message": {"role": "assistant",
                    "model": "claude-opus-4-7", "usage": {}, "id": "bad-ts"},
                    "timestamp": "not-a-timestamp"}),
        # bad timestamp -> skipped
    ]
    return slug, {f"{sid}.jsonl": entries}


def fixture_04_empty():
    """Zero-byte file."""
    slug = "04-empty"
    return slug, {"empty.jsonl": []}


def fixture_05_out_of_range():
    """All turns predate period_cutoff -> contribute zero."""
    slug = "05-out-of-range"
    sid = "delta"
    turns = [
        turn("claude-opus-4-7", OLD, msg_id="old1",
             input_tokens=999999, output_tokens=999999),
        turn("claude-opus-4-7", OLD, msg_id="old2",
             input_tokens=999999, output_tokens=999999),
    ]
    return slug, {f"{sid}.jsonl": turns}


def fixture_06_period_only():
    """Turn is in the period (trailing) but predates win_start."""
    slug = "06-period-only"
    sid = "epsilon"
    turns = [
        turn("claude-opus-4-7", PRE_WIN, msg_id="p1",
             input_tokens=1000, output_tokens=500),
        # Above is between period_cutoff and win_start: in trailing only.
        turn("claude-opus-4-7", FRESH, msg_id="p2",
             input_tokens=1000, output_tokens=500),
        # Above is in BOTH buckets.
    ]
    return slug, {f"{sid}.jsonl": turns}


def fixture_07_unknown_model():
    """Unknown model family -> falls back to sonnet rates."""
    slug = "07-unknown-model"
    sid = "zeta"
    turns = [
        turn("claude-mystery-x", FRESH, msg_id="u1",
             input_tokens=1000, output_tokens=500),  # priced as sonnet
    ]
    return slug, {f"{sid}.jsonl": turns}


FIXTURES = [
    fixture_01_single_parent,
    fixture_02_parent_acompact,
    fixture_03_malformed_lines,
    fixture_04_empty,
    fixture_05_out_of_range,
    fixture_06_period_only,
    fixture_07_unknown_model,
]


def walk_group(files: dict[str, list]) -> tuple[float, float]:
    """Reference walk: matches the spec exactly."""
    trailing = window = 0.0
    seen_ids: set[str] = set()
    for entries in files.values():
        for entry in entries:
            if isinstance(entry, str):
                try:
                    entry = json.loads(entry)
                except Exception:
                    continue
            msg = entry.get("message") or {}
            if msg.get("role") != "assistant":
                continue
            mid = msg.get("id")
            if mid:
                if mid in seen_ids:
                    continue
                seen_ids.add(mid)
            ts_str = entry.get("timestamp")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(
                    ts_str.replace("Z", "+00:00")
                ).timestamp()
            except (ValueError, TypeError):
                continue
            earliest = min(period_cutoff, WIN_START_UNIX)
            if ts < earliest:
                continue
            c = cost_for(msg.get("usage") or {}, msg.get("model") or "")
            if ts >= period_cutoff:
                trailing += c
            if ts >= WIN_START_UNIX:
                window += c
    return trailing, window


def main():
    if CORPUS.exists():
        # Wipe and regenerate so deletions take effect.
        for p in sorted(CORPUS.rglob("*"), reverse=True):
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                p.rmdir()
        CORPUS.rmdir()
    CORPUS.mkdir(parents=True, exist_ok=True)

    expected = {
        "_meta": {
            "now_unix": NOW_UNIX,
            "period_seconds": PERIOD_SECONDS,
            "win_start_unix": WIN_START_UNIX,
            "note": "Generated by generate_corpus.py. Do not hand-edit.",
        },
        "_aggregate": None,  # filled in below
        "fixtures": {},
    }

    agg_t = agg_w = 0.0
    file_count = 0
    for build in FIXTURES:
        slug, files = build()
        slug_dir = CORPUS / slug
        for rel, entries in files.items():
            target = slug_dir / rel
            write_jsonl(target, entries)
            file_count += 1
        t, w = walk_group(files)
        expected["fixtures"][slug] = {
            "trailing_usd": round(t, 6),
            "window_usd": round(w, 6),
            "files": sorted(files.keys()),
        }
        agg_t += t
        agg_w += w

    expected["_aggregate"] = {
        "trailing_usd": round(agg_t, 6),
        "window_usd": round(agg_w, 6),
        "files_walked_min": file_count,  # may be lower if any are mtime-pruned
        "groups": len(FIXTURES),
    }

    with open(EXPECTED_PATH, "w", encoding="utf-8") as f:
        json.dump(expected, f, indent=2)
        f.write("\n")

    print(f"Wrote {file_count} fixture files under {CORPUS}")
    print(f"Wrote {EXPECTED_PATH}")
    print(f"Aggregate: trailing=${agg_t:.6f}  window=${agg_w:.6f}")


if __name__ == "__main__":
    main()
