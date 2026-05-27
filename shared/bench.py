"""Time each available implementation against the live ~/.claude/projects fleet.

Usage:
    python shared/bench.py [--runs 5] [--mode cost|beacons-history]
                           [--interleave] [--no-python] [<lang> ...]

With no language args, runs every implementation it can find a binary for.
In `cost` mode (the default) also benchmarks the Python parallel walker in
schoen-claude-status (if found on PYTHONPATH or at the conventional path)
for reference. `beacons-history` mode skips the Python reference because no
pure-Python implementation exists — schoen-claude-status's statusline
shells out to walker for that subcommand.

By default the impls run sequentially: rust x N runs back-to-back, then cpp
x N, etc. (only ever one process at a time — each walker already fans out
across cores internally). `--interleave` instead round-robins them — rust,
cpp, go, zig, rust, cpp, ... — after an untimed warm-up round, so a
transient background-noise spike smears across every impl's samples roughly
equally instead of penalising whichever one happened to be running. Still
one process at a time; interleaving is about sample *ordering*, not
concurrency. The Python reference is timed in-process and is unaffected by
the flag.

Reports min / median / max wall-clock per implementation, the median
walker-reported `elapsed_ms` (in-binary work, exposing per-process startup
overhead), and a one-line speedup summary.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME = Path(os.path.expanduser("~"))

CANDIDATES = {
    "rust": [
        ROOT / "rust" / "target" / "release" / "walker.exe",
        ROOT / "rust" / "target" / "release" / "walker",
    ],
    "go": [ROOT / "go" / "walker.exe", ROOT / "go" / "walker"],
    "cpp": [
        ROOT / "cpp" / "build" / "Release" / "walker.exe",
        ROOT / "cpp" / "build" / "walker.exe",
        ROOT / "cpp" / "build" / "walker",
    ],
    "zig": [
        ROOT / "zig" / "zig-out" / "bin" / "walker.exe",
        ROOT / "zig" / "zig-out" / "bin" / "walker",
    ],
}

PERIOD = 7 * 86400  # weekly window

# Every impl supports --no-config; the bench passes it to ALL of them so each
# walks the identical primary root. Without it the impls diverge on the live
# fleet (some fold in walker-roots.json extras such as a mounted second
# machine, some don't), which skews the timing with unequal work and hides
# real per-impl speed behind SMB latency. The production status-line path DOES
# read the config; this flag only keeps the benchmark apples-to-apples.

MODES = ("cost", "beacons-history")


def find_binary(lang: str):
    for path in CANDIDATES.get(lang, []):
        if path.is_file():
            return path
    return None


def build_cmd(lang: str, binary: Path, mode: str, period: int, win_start: float, now: float):
    if mode == "cost":
        cmd = [
            str(binary),
            "--period", str(period),
            "--win-start", repr(win_start),
            "--now", repr(now),
        ]
    elif mode == "beacons-history":
        cmd = [
            str(binary),
            "beacons-history",
            "--period", str(period),
            "--win-start", repr(win_start),
            "--now", repr(now),
        ]
    else:
        raise ValueError(f"unknown mode: {mode}")
    cmd.append("--no-config")
    return cmd


def run_once(cmd: list):
    """One timed invocation. Returns (wall_ms, output, walker_ms, err).

    `output` is the parsed last JSON line; `walker_ms` is its `elapsed_ms`
    diagnostic (in-binary wall-clock) if present. On non-zero exit, the
    first three are None and `err` carries stderr.
    """
    t0 = time.perf_counter()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    wall = (time.perf_counter() - t0) * 1000
    if result.returncode != 0:
        return None, None, None, result.stderr
    output = json.loads(result.stdout.strip().splitlines()[-1])
    return wall, output, output.get("elapsed_ms"), None


def time_binary(lang: str, binary: Path, mode: str, period: int, win_start: float, now: float, runs: int):
    """Sequential timing: run this one impl `runs` times back-to-back."""
    cmd = build_cmd(lang, binary, mode, period, win_start, now)
    elapsed, walker = [], []
    last_output = None
    for _ in range(runs):
        wall, output, walker_ms, err = run_once(cmd)
        if err is not None:
            return None, None, None, err
        elapsed.append(wall)
        if walker_ms is not None:
            walker.append(walker_ms)
        last_output = output
    return elapsed, walker, last_output, None


def time_binaries_interleaved(specs: list, mode: str, period: int, win_start: float, now: float, runs: int):
    """Round-robin timing across `specs` (list of (lang, binary)).

    Runs an untimed warm-up of every impl, then loops `runs` rounds, doing
    one invocation per impl per round. Returns per-lang dicts:
    (elapsed_lists, walker_lists, last_outputs, errors). A lang that errors
    is recorded in `errors` and skipped for the rest of the run.
    """
    cmds = {lang: build_cmd(lang, binary, mode, period, win_start, now) for lang, binary in specs}
    elapsed = {lang: [] for lang, _ in specs}
    walker = {lang: [] for lang, _ in specs}
    outputs = {lang: None for lang, _ in specs}
    errors = {lang: None for lang, _ in specs}

    # Warm-up round (untimed) — touches the disk cache for every impl so the
    # first timed round isn't penalised by cold-cache reads.
    for lang, _ in specs:
        _, _, _, err = run_once(cmds[lang])
        if err is not None:
            errors[lang] = err

    for _ in range(runs):
        for lang, _ in specs:
            if errors[lang] is not None:
                continue
            wall, output, walker_ms, err = run_once(cmds[lang])
            if err is not None:
                errors[lang] = err
                continue
            elapsed[lang].append(wall)
            if walker_ms is not None:
                walker[lang].append(walker_ms)
            outputs[lang] = output
    return elapsed, walker, outputs, errors


def time_python_walker(period: int, win_start: float, now: float, runs: int):
    """Bench the schoen-claude-status parallel Python walker for reference."""
    sys.path.insert(0, str(HOME / "schoen-claude-status"))
    try:
        from statusline_lib import _walk_pace_buckets, _PACE_CACHE_PATH
    except ImportError as e:
        return None, None, f"could not import statusline_lib: {e}"
    if _PACE_CACHE_PATH and os.path.exists(_PACE_CACHE_PATH):
        os.remove(_PACE_CACHE_PATH)
    elapsed = []
    trailing = window = 0.0
    for _ in range(runs):
        t0 = time.perf_counter()
        trailing, window = _walk_pace_buckets(period, win_start)
        elapsed.append((time.perf_counter() - t0) * 1000)
    return elapsed, {"trailing_usd": trailing, "window_usd": window}, None


def fmt_stats(label: str, elapsed: list, walker: list, output: dict | None, mode: str):
    e = sorted(elapsed)
    median = e[len(e) // 2]
    line = (f"  {label:18s}  min={e[0]:>5.0f}ms  median={median:>5.0f}ms  "
            f"max={e[-1]:>5.0f}ms")
    if walker:
        w = sorted(walker)
        line += f"  walker={w[len(w) // 2]:>4.0f}ms"
    if output:
        if mode == "cost":
            line += (f"  trailing=${output.get('trailing_usd', 0):.2f}  "
                     f"window=${output.get('window_usd', 0):.2f}")
        elif mode == "beacons-history":
            bias = output.get("bias_factor")
            bias_str = f"{bias:.4f}" if bias is not None else "n/a"
            line += f"  n_pairs={output.get('n_pairs', 0)}  bias={bias_str}"
    return line, median


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--mode", choices=MODES, default="cost",
                        help="Which subcommand to bench (default: cost)")
    parser.add_argument("--interleave", action="store_true",
                        help="Round-robin the native impls each run (still one "
                             "process at a time) after an untimed warm-up, so "
                             "background noise hits every impl equally. Does not "
                             "affect the in-process Python reference.")
    parser.add_argument("langs", nargs="*", default=None)
    parser.add_argument("--no-python", action="store_true",
                        help="Skip the Python reference bench")
    args = parser.parse_args()

    now = time.time()
    win_start = now - PERIOD + 3600  # 1h into a fresh weekly window

    requested = args.langs or list(CANDIDATES.keys())
    order = "interleaved" if args.interleave else "sequential"
    print(f"Live fleet bench  --  mode={args.mode}  --  {args.runs} runs each "
          f"({order})  --  weekly window\n")

    medians: dict[str, float] = {}

    if args.mode == "cost" and not args.no_python:
        print("Python reference (orjson + 8-worker ProcessPool):")
        elapsed, output, err = time_python_walker(PERIOD, win_start, now, args.runs)
        if err:
            print(f"  python              SKIP  {err}")
        else:
            line, m = fmt_stats("python (parallel)", elapsed, [], output, args.mode)
            print(line)
            medians["python"] = m
        print()
    elif args.mode == "beacons-history" and not args.no_python:
        print("No pure-Python beacons-history implementation; "
              "schoen-claude-status shells out to walker. Skipping Python row.\n")

    print("Native implementations:")
    specs = []
    for lang in requested:
        binary = find_binary(lang)
        if binary is None:
            print(f"  {lang:18s}  SKIP  no binary")
            continue
        specs.append((lang, binary))

    if args.interleave:
        elapsed_map, walker_map, output_map, error_map = time_binaries_interleaved(
            specs, args.mode, PERIOD, win_start, now, args.runs)
        for lang, _ in specs:
            if error_map[lang] is not None:
                print(f"  {lang:18s}  FAIL  {error_map[lang]}")
                continue
            line, m = fmt_stats(lang, elapsed_map[lang], walker_map[lang], output_map[lang], args.mode)
            print(line)
            medians[lang] = m
    else:
        for lang, binary in specs:
            elapsed, walker, output, err = time_binary(lang, binary, args.mode, PERIOD, win_start, now, args.runs)
            if err:
                print(f"  {lang:18s}  FAIL  {err}")
                continue
            line, m = fmt_stats(lang, elapsed, walker, output, args.mode)
            print(line)
            medians[lang] = m

    if medians:
        baseline = max(medians.values())
        print("\nSpeedup vs slowest:")
        for label, m in sorted(medians.items(), key=lambda kv: kv[1]):
            print(f"  {label:18s}  {m:>5.0f}ms  ({baseline / m:.2f}x)")


if __name__ == "__main__":
    main()
