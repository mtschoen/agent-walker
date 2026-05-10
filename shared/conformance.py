"""Run each implementation against the conformance corpus, assert agreement.

Usage:
    python shared/conformance.py [<lang> ...]

With no arguments, runs every implementation it can find a binary for.
With explicit args, runs only those (e.g. `python shared/conformance.py rust`).

Discovers binaries via:
    rust  -> rust/target/release/walker(.exe)
    go    -> go/walker(.exe)
    cpp   -> cpp/build/walker(.exe)  or  cpp/build/Release/walker.exe
    zig   -> zig/zig-out/bin/walker(.exe)

Tolerance: ±$0.01 on trailing_usd and window_usd per fixture and aggregate.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "shared" / "corpus"
EXPECTED = ROOT / "shared" / "expected.json"
TOLERANCE = 0.01  # $

CANDIDATES = {
    "rust": [
        ROOT / "rust" / "target" / "release" / "walker.exe",
        ROOT / "rust" / "target" / "release" / "walker",
    ],
    "go": [
        ROOT / "go" / "walker.exe",
        ROOT / "go" / "walker",
    ],
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


def find_binary(lang: str) -> Path | None:
    for path in CANDIDATES.get(lang, []):
        if path.is_file():
            return path
    return None


def run_aggregate(binary: Path, meta: dict) -> dict:
    """Run binary against the whole corpus, return parsed JSON output."""
    cmd = [
        str(binary),
        "--period", str(meta["period_seconds"]),
        "--win-start", repr(meta["win_start_unix"]),
        "--now", repr(meta["now_unix"]),
        "--projects-root", str(CORPUS),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise RuntimeError(
            f"{binary.name} exited {result.returncode}\n"
            f"stderr:\n{result.stderr}"
        )
    line = result.stdout.strip().splitlines()[-1]  # last non-empty line
    return json.loads(line)


def check_aggregate(lang: str, binary: Path, expected: dict) -> bool:
    meta = expected["_meta"]
    target = expected["_aggregate"]
    try:
        got = run_aggregate(binary, meta)
    except Exception as e:
        print(f"  [{lang:>4s}] FAIL  {e}")
        return False
    dt = got.get("trailing_usd", 0) - target["trailing_usd"]
    dw = got.get("window_usd", 0) - target["window_usd"]
    ok = abs(dt) <= TOLERANCE and abs(dw) <= TOLERANCE
    badge = " OK " if ok else "FAIL"
    print(
        f"  [{lang:>4s}] {badge}  "
        f"trailing=${got.get('trailing_usd', 0):.6f} (target ${target['trailing_usd']:.6f}, "
        f"d=${dt:+.6f})  "
        f"window=${got.get('window_usd', 0):.6f} (target ${target['window_usd']:.6f}, "
        f"d=${dw:+.6f})  "
        f"{got.get('elapsed_ms', '?')}ms"
    )
    return ok


def main():
    if not EXPECTED.is_file():
        print(f"missing {EXPECTED} -- run shared/generate_corpus.py first")
        sys.exit(2)
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))

    requested = sys.argv[1:] or list(CANDIDATES.keys())
    overall_ok = True
    print(f"Conformance corpus: {CORPUS}")
    print(f"Pinned now={expected['_meta']['now_unix']}  "
          f"period={expected['_meta']['period_seconds']}  "
          f"win_start={expected['_meta']['win_start_unix']}\n")

    for lang in requested:
        binary = find_binary(lang)
        if binary is None:
            print(f"  [{lang:>4s}] SKIP  no built binary "
                  f"(checked {[str(p) for p in CANDIDATES.get(lang, [])]})")
            continue
        if not check_aggregate(lang, binary, expected):
            overall_ok = False

    sys.exit(0 if overall_ok else 1)


if __name__ == "__main__":
    main()
