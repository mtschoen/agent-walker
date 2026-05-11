"""Time each available implementation against the live ~/.claude/projects fleet.

Usage:
    python shared/bench.py [--runs 5] [--mode cost|beacons-history] [<lang> ...]

With no language args, runs every implementation it can find a binary for.
In `cost` mode (the default) also benchmarks the Python parallel walker in
schoen-claude-status (if found on PYTHONPATH or at the conventional path)
for reference. `beacons-history` mode skips the Python reference because no
pure-Python implementation exists — schoen-claude-status's statusline
shells out to walker for that subcommand.

Reports min / median / max wall-clock per implementation and a one-line
summary table.
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

# Mirrors shared/conformance.py: --no-config is cpp-only today, so other
# impls would error on it. Bench passes it for parity (every impl walks
# the same primary root, no walker-roots.json extras).
IMPLS_WITH_NO_CONFIG = {"cpp"}

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
    if lang in IMPLS_WITH_NO_CONFIG:
        cmd.append("--no-config")
    return cmd


def time_binary(lang: str, binary: Path, mode: str, period: int, win_start: float, now: float, runs: int):
    cmd = build_cmd(lang, binary, mode, period, win_start, now)
    elapsed = []
    last_output = None
    for _ in range(runs):
        t0 = time.perf_counter()
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        wall = time.perf_counter() - t0
        if result.returncode != 0:
            return None, None, result.stderr
        last_output = json.loads(result.stdout.strip().splitlines()[-1])
        elapsed.append(wall * 1000)
    return elapsed, last_output, None


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


def fmt_stats(label: str, elapsed: list, output: dict | None, mode: str):
    e = sorted(elapsed)
    median = e[len(e) // 2]
    line = (f"  {label:18s}  min={e[0]:>5.0f}ms  median={median:>5.0f}ms  "
            f"max={e[-1]:>5.0f}ms")
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
    parser.add_argument("langs", nargs="*", default=None)
    parser.add_argument("--no-python", action="store_true",
                        help="Skip the Python reference bench")
    args = parser.parse_args()

    now = time.time()
    win_start = now - PERIOD + 3600  # 1h into a fresh weekly window

    requested = args.langs or list(CANDIDATES.keys())
    print(f"Live fleet bench  --  mode={args.mode}  --  {args.runs} runs each  --  weekly window\n")

    medians: dict[str, float] = {}

    if args.mode == "cost" and not args.no_python:
        print("Python reference (orjson + 8-worker ProcessPool):")
        elapsed, output, err = time_python_walker(PERIOD, win_start, now, args.runs)
        if err:
            print(f"  python              SKIP  {err}")
        else:
            line, m = fmt_stats("python (parallel)", elapsed, output, args.mode)
            print(line)
            medians["python"] = m
        print()
    elif args.mode == "beacons-history" and not args.no_python:
        print("No pure-Python beacons-history implementation; "
              "schoen-claude-status shells out to walker. Skipping Python row.\n")

    print("Native implementations:")
    for lang in requested:
        binary = find_binary(lang)
        if binary is None:
            print(f"  {lang:18s}  SKIP  no binary")
            continue
        elapsed, output, err = time_binary(lang, binary, args.mode, PERIOD, win_start, now, args.runs)
        if err:
            print(f"  {lang:18s}  FAIL  {err}")
            continue
        line, m = fmt_stats(lang, elapsed, output, args.mode)
        print(line)
        medians[lang] = m

    if medians:
        baseline = max(medians.values())
        print("\nSpeedup vs slowest:")
        for label, m in sorted(medians.items(), key=lambda kv: kv[1]):
            print(f"  {label:18s}  {m:>5.0f}ms  ({baseline / m:.2f}x)")


if __name__ == "__main__":
    main()
