"""Generate a large, deterministic synthetic perf corpus for benchmarking.

Distinct from the tiny conformance generators (`generate_corpus.py` et al),
which exist to pin *correctness*. This one exists to pin *performance*: a
fixed, reproducible transcript tree big enough to expose real per-impl speed
differences, so benchmarks don't run against the live `~/.claude/projects`
fleet (which changes constantly and isn't reproducible).

Output is written to `shared/corpus-perf/` by default and is **gitignored** --
never check it in. Re-run any time to recreate the identical tree (fixed seed).

Layout mirrors the real fleet so every walker subcommand does real work:
    <root>/<slug>/<sid>.jsonl                         (parent transcripts)
    <root>/<slug>/<sid>/subagents/agent-<id>.jsonl    (subagent transcripts)

Each file mixes content so all five modes are exercised:
  - cost / events  -- assistant turns with `usage` across model families,
                      cache tokens, and occasional web_search_requests.
  - beacons-*      -- a fraction of sessions carry a <progress-beacon> begin/
                      report*/end lifecycle with ETAs (so bias_factor has pairs).
  - search         -- prose, tool_use/tool_result blocks, and queue-operation
                      entries, with a known token seeded at a known hit rate.

A `manifest.json` records the seed, the pinned `now`/window, file counts,
total bytes, a sample beacon session-id (for `beacons-latest`), and the
search pattern -- so `bench.py` is self-describing and reproducible.

Usage:
    python shared/generate_perf_corpus.py [--target-mb 150] [--seed 1234]
                                          [--out shared/corpus-perf] [--force]
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_OUT = ROOT / "corpus-perf"

# Pinned reference clock. Timestamps span [NOW - SPAN, NOW]; bench reads these
# from the manifest so its window covers the whole corpus (maximal work).
NOW_UNIX = 1_780_000_000.0  # 2026-05-28T14:13:20Z, a fixed point
SPAN_SECONDS = 30 * 86400  # transcripts spread across ~30 days

# The token seeded into a fraction of text blocks; bench searches for it.
SEARCH_PATTERN = "ZEBRAFINCH"
SEARCH_HIT_RATE = 0.12  # ~12% of text blocks embed the pattern

MODELS = [
    ("claude-opus-4-8", 0.18),
    ("claude-opus-4-7", 0.12),
    ("claude-sonnet-4-6", 0.34),
    ("claude-sonnet-4-5", 0.14),
    ("claude-haiku-4-5", 0.18),
    ("some-unknown-model-x", 0.04),
]

# Word pool for bulking text blocks to realistic byte sizes.
WORDS = (
    "transcript walker corpus benchmark session assistant token usage cache "
    "creation input output model opus sonnet haiku beacon progress drift eta "
    "summary parser simdjson glob discovery filter window period trailing cost "
    "estimate pricing fleet status line subagent parent group dedup mtime root "
    "machine mount search pattern snippet context regex queue operation enqueue "
    "popall result block tool invoke render thread parallel allocation latency "
    "throughput profile hotspot optimization measure baseline conformance gate "
    "coverage report fixture deterministic reproducible synthetic distribution"
).split()


def weighted_choice(rng: random.Random, pairs: list[tuple[str, float]]) -> str:
    total = sum(w for _, w in pairs)
    r = rng.random() * total
    upto = 0.0
    for value, weight in pairs:
        upto += weight
        if r <= upto:
            return value
    return pairs[-1][0]


def iso(unix: float) -> str:
    return datetime.fromtimestamp(unix, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


def paragraph(
    rng: random.Random, min_words: int, max_words: int, *, needle: bool
) -> str:
    n = rng.randint(min_words, max_words)
    chosen = [rng.choice(WORDS) for _ in range(n)]
    if needle:
        chosen.insert(rng.randint(0, len(chosen)), SEARCH_PATTERN)
    return " ".join(chosen)


def make_usage(rng: random.Random) -> dict:
    """A usage block with realistic, varied token counts."""
    usage = {
        "input_tokens": rng.randint(50, 4000),
        "output_tokens": rng.randint(20, 2500),
        "cache_read_input_tokens": rng.choice([0, 0, rng.randint(1000, 60000)]),
        "cache_creation_input_tokens": rng.choice([0, 0, rng.randint(500, 8000)]),
    }
    if rng.random() < 0.06:  # occasional server-side web search
        usage["server_tool_use"] = {"web_search_requests": rng.randint(1, 4)}
    return usage


def beacon_block(kind: str, eta: int, summary: str, *, drift: str | None) -> str:
    payload: dict = {"kind": kind, "eta_seconds": eta, "summary": summary}
    if drift is not None:
        payload["drift"] = drift
    return (
        "Working on it.\n\n<progress-beacon>\n"
        + json.dumps(payload)
        + "\n</progress-beacon>"
    )


def assistant_line(
    rng: random.Random,
    ts: float,
    msg_id: str,
    *,
    text: str | None,
    tool_use: bool = False,
) -> dict:
    content = []
    if text is not None:
        content.append({"type": "text", "text": text})
    if tool_use:
        content.append(
            {
                "type": "tool_use",
                "id": f"toolu_{msg_id}",
                "name": "fake_tool",
                "input": {
                    "query": paragraph(
                        rng, 4, 12, needle=rng.random() < SEARCH_HIT_RATE
                    )
                },
            }
        )
    message: dict = {
        "id": msg_id,
        "role": "assistant",
        "model": weighted_choice(rng, MODELS),
        "usage": make_usage(rng),
    }
    if content:
        message["content"] = content
    return {"type": "assistant", "timestamp": iso(ts), "message": message}


def user_line(rng: random.Random, ts: float, *, tool_result: bool) -> dict:
    if tool_result:
        content = [
            {
                "type": "tool_result",
                "tool_use_id": f"toolu_{rng.randint(0, 1_000_000)}",
                "content": paragraph(rng, 6, 30, needle=rng.random() < SEARCH_HIT_RATE),
            }
        ]
        message = {"role": "user", "content": content}
    else:
        message = {
            "role": "user",
            "content": paragraph(rng, 8, 40, needle=rng.random() < SEARCH_HIT_RATE),
        }
    return {"type": "user", "timestamp": iso(ts), "message": message}


def queue_op_line(rng: random.Random, ts: float) -> dict:
    return {
        "type": "queue-operation",
        "operation": "enqueue",
        "timestamp": iso(ts),
        "content": paragraph(rng, 5, 20, needle=rng.random() < SEARCH_HIT_RATE),
    }


def emit_session(
    rng: random.Random, start: float, turns: int, id_prefix: str, *, with_beacons: bool
) -> list[dict]:
    """Build the ordered line list for one transcript file."""
    lines: list[dict] = []
    ts = start
    # If this is a beacon session, pre-pick the turn indices for the lifecycle.
    beacon_turns: dict[int, tuple[str, int]] = {}
    if with_beacons and turns >= 4:
        report_count = rng.randint(1, 3)
        slots = sorted(rng.sample(range(turns), report_count + 2))
        eta = rng.randint(300, 1800)
        beacon_turns[slots[0]] = ("begin", eta)
        for slot in slots[1:-1]:
            eta = max(0, eta - rng.randint(60, 400))
            beacon_turns[slot] = ("report", eta)
        beacon_turns[slots[-1]] = ("end", 0)

    for turn in range(turns):
        ts += rng.randint(15, 900)
        msg_id = f"{id_prefix}-m{turn:04d}"
        if turn in beacon_turns:
            kind, eta = beacon_turns[turn]
            drift = rng.choice([None, "nominal", "minor", "major"])
            text = beacon_block(
                kind, eta, paragraph(rng, 3, 8, needle=False), drift=drift
            )
            lines.append(assistant_line(rng, ts, msg_id, text=text))
            continue
        roll = rng.random()
        if roll < 0.30:  # usage-only assistant turn (cost/events heavy)
            lines.append(assistant_line(rng, ts, msg_id, text=None))
        elif roll < 0.72:  # assistant with prose (search + cost)
            text = paragraph(rng, 20, 120, needle=rng.random() < SEARCH_HIT_RATE)
            lines.append(assistant_line(rng, ts, msg_id, text=text))
        elif roll < 0.82:  # assistant with a tool_use block
            text = paragraph(rng, 4, 20, needle=rng.random() < SEARCH_HIT_RATE)
            lines.append(assistant_line(rng, ts, msg_id, text=text, tool_use=True))
        elif roll < 0.90:  # user prose message
            lines.append(user_line(rng, ts, tool_result=False))
        elif roll < 0.96:  # user tool_result message
            lines.append(user_line(rng, ts, tool_result=True))
        else:  # queue operation
            lines.append(queue_op_line(rng, ts))
    return lines


def emit_dense_beacon_session(
    rng: random.Random, start: float, target_bytes: int, id_prefix: str
) -> tuple[list[dict], int]:
    """One large transcript packed with many begin/report*/end lifecycles.

    Pinned as the beacons-latest sample so that mode parses a substantial
    file (a long-running "current session") instead of a tiny one. Without
    this, beacons-latest reads a single ~7 KB transcript and the timing is
    dominated by directory traversal + process startup, masking any parse
    optimization. Each lifecycle is interleaved with normal turns (prose,
    tool, real user prompts) so the file also exercises beacons-history's
    idle-gap detection and adds realistic parse volume.

    Returns (lines, lifecycle_count). Generation stops once the serialized
    size crosses target_bytes (estimated as it builds, so the result is
    deterministic for a fixed seed).

    Time advances in small (1-8 s) steps so the whole session fits in a few
    hours of wall-clock. The caller pins `start` BEFORE the beacons-history
    window, so this session contributes parse volume to history without
    feeding its bias_factor (its begin->end gaps are seconds, not the
    realistic minutes-to-hours of the ordinary sessions; letting them count
    would crush the median). beacons-latest has no window filter, so it still
    parses the whole file -- which is the point.
    """
    lines: list[dict] = []
    ts = start
    approx = 0
    turn = 0
    lifecycles = 0

    def push(line: dict) -> None:
        nonlocal approx
        lines.append(line)
        approx += len(json.dumps(line)) + 1  # +1 for the trailing newline

    while approx < target_bytes:
        # --- one begin -> report* -> end lifecycle ---
        eta = rng.randint(300, 1800)
        ts += rng.randint(1, 5)
        push(
            assistant_line(
                rng,
                ts,
                f"{id_prefix}-m{turn:05d}",
                text=beacon_block("begin", eta, paragraph(rng, 3, 8, needle=False), drift=None),
            )
        )
        turn += 1
        for _ in range(rng.randint(1, 3)):
            ts += rng.randint(1, 5)
            eta = max(0, eta - rng.randint(60, 400))
            push(
                assistant_line(
                    rng,
                    ts,
                    f"{id_prefix}-m{turn:05d}",
                    text=beacon_block(
                        "report",
                        eta,
                        paragraph(rng, 3, 8, needle=False),
                        drift=rng.choice([None, "nominal", "minor", "major"]),
                    ),
                )
            )
            turn += 1
        ts += rng.randint(15, 300)
        push(
            assistant_line(
                rng,
                ts,
                f"{id_prefix}-m{turn:05d}",
                text=beacon_block("end", 0, paragraph(rng, 3, 8, needle=False), drift="nominal"),
            )
        )
        turn += 1
        lifecycles += 1

        # --- filler turns between lifecycles (parse volume + real-user idle gaps) ---
        for _ in range(rng.randint(2, 6)):
            ts += rng.randint(1, 8)
            roll = rng.random()
            if roll < 0.40:
                push(
                    assistant_line(
                        rng,
                        ts,
                        f"{id_prefix}-m{turn:05d}",
                        text=paragraph(rng, 20, 120, needle=rng.random() < SEARCH_HIT_RATE),
                    )
                )
            elif roll < 0.55:
                push(
                    assistant_line(
                        rng,
                        ts,
                        f"{id_prefix}-m{turn:05d}",
                        text=paragraph(rng, 4, 20, needle=rng.random() < SEARCH_HIT_RATE),
                        tool_use=True,
                    )
                )
            elif roll < 0.80:
                push(user_line(rng, ts, tool_result=False))
            else:
                push(user_line(rng, ts, tool_result=True))
            turn += 1

    return lines, lifecycles


def write_jsonl(path: Path, lines: list[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = "".join(json.dumps(line) + "\n" for line in lines)
    encoded = blob.encode("utf-8")
    path.write_bytes(encoded)
    return len(encoded)


def generate(
    out_dir: Path,
    target_bytes: int,
    seed: int,
    *,
    beacon_rate: float = 0.18,
    beacon_session_bytes: int = 48 * 1024 * 1024,
) -> dict:
    rng = random.Random(seed)
    parent_count = subagent_count = total_bytes = 0
    beacon_session_ids: list[str] = []
    slug_idx = 0

    while total_bytes < target_bytes:
        slug = f"perf-project-{slug_idx:03d}"
        sessions = rng.randint(3, 18)
        for sess in range(sessions):
            if total_bytes >= target_bytes:
                break
            sid = f"sid-{slug_idx:03d}-{sess:03d}"
            # Heavy-tailed turn count -> varied file sizes (a few huge, many small).
            turns = max(5, int(rng.lognormvariate(4.2, 0.9)))
            start = NOW_UNIX - rng.uniform(86400, SPAN_SECONDS)
            with_beacons = rng.random() < beacon_rate
            lines = emit_session(rng, start, turns, sid, with_beacons=with_beacons)
            total_bytes += write_jsonl(out_dir / slug / f"{sid}.jsonl", lines)
            parent_count += 1
            if with_beacons:
                beacon_session_ids.append(sid)
            # ~20% of sessions spawn a subagent transcript.
            if rng.random() < 0.20:
                agent_turns = max(4, int(rng.lognormvariate(3.6, 0.8)))
                a_start = start + rng.uniform(0, 3600)
                a_lines = emit_session(
                    rng,
                    a_start,
                    agent_turns,
                    f"{sid}-agent",
                    with_beacons=rng.random() < 0.10,
                )
                a_path = out_dir / slug / sid / "subagents" / f"agent-{sid}-x.jsonl"
                total_bytes += write_jsonl(a_path, a_lines)
                subagent_count += 1
        slug_idx += 1

    # The dense beacon stress session for beacons-latest. It lives in its OWN
    # root (a sibling dir), NOT in the main corpus, and is passed to the walker
    # only via --extra-projects-root for the beacons-latest run. Keeping it out
    # of the main corpus means:
    #   - it is not a single-file straggler for the parallel full-fleet modes
    #     (cost/events/search/beacons-history parse every file in the main root;
    #     a 20-50 MB file would dominate the slow-parse impls);
    #   - it cannot pollute beacons-history's bias_factor (the mode never sees
    #     it), so its begin/end timing is irrelevant and placement is cosmetic;
    #   - it can be sized freely to lift beacons-latest out of the noisy
    #     sub-100ms range WITHOUT moving any other mode's numbers.
    # Generated AFTER the main fill loop so the --beacon-session-mb knob does not
    # perturb the main corpus RNG stream (the other modes' fixtures stay
    # identical as the dense size changes). The pinned id is a constant string,
    # so ordering does not affect it.
    dense_sid = "sid-beacon-stress"
    beacon_root = out_dir.with_name(out_dir.name + "-beacons")
    if beacon_root.exists():
        shutil.rmtree(beacon_root)  # sibling root isn't covered by main()'s wipe
    # Cosmetic placement: a recent "current session" ending shortly before NOW
    # (drives only the age_seconds field of beacons-latest output).
    dense_start = NOW_UNIX - (beacon_session_bytes * 0.05) - 3600
    dense_lines, dense_lifecycles = emit_dense_beacon_session(
        rng, dense_start, beacon_session_bytes, dense_sid
    )
    dense_bytes = write_jsonl(
        beacon_root / "perf-beacon-stress" / f"{dense_sid}.jsonl", dense_lines
    )

    manifest = {
        "seed": seed,
        "now_unix": NOW_UNIX,
        "span_seconds": SPAN_SECONDS,
        "period_seconds": SPAN_SECONDS
        + 86400,  # trailing cutoff covers the whole corpus
        # Window spans the whole corpus too, so the cost window pass and the
        # beacons-history pairing both do full work (begin/end pairs land in-window).
        "win_start_unix": NOW_UNIX - (SPAN_SECONDS + 86400),
        "slug_count": slug_idx,  # dense beacon session lives in a separate root
        "parent_count": parent_count,
        "subagent_count": subagent_count,
        "file_count": parent_count + subagent_count,
        "total_bytes": total_bytes,
        "beacon_rate": beacon_rate,
        "beacon_session_id": dense_sid,
        # Separate root holding ONLY the dense session; beacons-latest is run
        # with this as --extra-projects-root so no other mode parses it.
        "beacon_latest_root": str(beacon_root),
        "beacon_session_bytes": dense_bytes,
        "beacon_session_lifecycles": dense_lifecycles,
        # Count of ordinary sessions that also carry a beacon lifecycle (the
        # dense stress session is separate and always present).
        "beacon_session_count": len(beacon_session_ids),
        "search_pattern": SEARCH_PATTERN,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target-mb",
        type=float,
        default=150.0,
        help="Approximate corpus size in MB (default: 150)",
    )
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument(
        "--beacon-rate",
        type=float,
        default=0.18,
        help="Fraction of ordinary sessions carrying a beacon lifecycle "
        "(default: 0.18). Raise to make beacons-history do more "
        "beacon-specific work; affects cost/events/search distribution too.",
    )
    parser.add_argument(
        "--beacon-session-mb",
        type=float,
        default=48.0,
        help="Size of the dense beacon stress session pinned for "
        "beacons-latest (default: 48). It lives in a SEPARATE root parsed only "
        "by beacons-latest, so this lever lifts that mode above the noisy "
        "sub-100ms range without affecting any other mode's numbers.",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Delete an existing output tree before generating",
    )
    args = parser.parse_args()

    out_dir = args.out.resolve()
    if out_dir.exists():
        if not args.force:
            existing = out_dir / "manifest.json"
            if existing.is_file():
                print(
                    f"Corpus already present at {out_dir} "
                    f"(use --force to regenerate). Manifest:"
                )
                print(existing.read_text(encoding="utf-8"))
                return
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    target_bytes = int(args.target_mb * 1024 * 1024)
    print(
        f"Generating ~{args.target_mb:.0f} MB perf corpus (seed={args.seed}) "
        f"to {out_dir} ..."
    )
    manifest = generate(
        out_dir,
        target_bytes,
        args.seed,
        beacon_rate=args.beacon_rate,
        beacon_session_bytes=int(args.beacon_session_mb * 1024 * 1024),
    )
    print(
        f"Done: {manifest['file_count']} files "
        f"({manifest['parent_count']} parents + {manifest['subagent_count']} "
        f"subagents) across {manifest['slug_count']} slugs, "
        f"{manifest['total_bytes'] / 1024 / 1024:.1f} MB."
    )
    print(
        f"Dense beacon session: {manifest['beacon_session_id']!r} "
        f"({manifest['beacon_session_bytes'] / 1024 / 1024:.1f} MB, "
        f"{manifest['beacon_session_lifecycles']} lifecycles); "
        f"{manifest['beacon_session_count']} ordinary beacon sessions "
        f"(rate {manifest['beacon_rate']}); "
        f"search pattern: {manifest['search_pattern']!r}"
    )


if __name__ == "__main__":
    main()
