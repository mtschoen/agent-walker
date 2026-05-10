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

For each implementation we run:
    1. Aggregate: full corpus root, expect totals to match expected.json::_aggregate
    2. Per-fixture: each fixture isolated in a temp root, expect totals to match
       expected.json::fixtures[name]. Catches over/under-counting that cancels
       in the aggregate.

Tolerance: ±$0.01 on trailing_usd and window_usd per fixture and aggregate.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
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


def run_walker(binary: Path, meta: dict, projects_root: Path) -> dict:
    """Run the walker binary against `projects_root`, return parsed JSON output."""
    cmd = [
        str(binary),
        "--period", str(meta["period_seconds"]),
        "--win-start", repr(meta["win_start_unix"]),
        "--now", repr(meta["now_unix"]),
        "--projects-root", str(projects_root),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise RuntimeError(
            f"{binary.name} exited {result.returncode}\n"
            f"stderr:\n{result.stderr}"
        )
    line = result.stdout.strip().splitlines()[-1]
    return json.loads(line)


def within_tolerance(got: dict, target: dict) -> tuple[bool, float, float]:
    dt = got.get("trailing_usd", 0) - target["trailing_usd"]
    dw = got.get("window_usd", 0) - target["window_usd"]
    ok = abs(dt) <= TOLERANCE and abs(dw) <= TOLERANCE
    return ok, dt, dw


def check_aggregate(lang: str, binary: Path, expected: dict) -> bool:
    meta = expected["_meta"]
    target = expected["_aggregate"]
    try:
        got = run_walker(binary, meta, CORPUS)
    except Exception as e:
        print(f"  [{lang:>4s}] aggregate    FAIL  {e}")
        return False
    ok, dt, dw = within_tolerance(got, target)
    badge = " OK " if ok else "FAIL"
    print(
        f"  [{lang:>4s}] aggregate    {badge}  "
        f"trailing=${got.get('trailing_usd', 0):.6f} (d=${dt:+.6f})  "
        f"window=${got.get('window_usd', 0):.6f} (d=${dw:+.6f})  "
        f"{got.get('elapsed_ms', '?')}ms"
    )
    return ok


def check_fixture(
    lang: str, binary: Path, meta: dict, fixture_name: str, target: dict
) -> bool:
    """Run the binary against just one fixture in an isolated temp root."""
    with tempfile.TemporaryDirectory(prefix="walker-fixture-") as tmp:
        # Recreate <tmp>/<fixture_name>/... from <CORPUS>/<fixture_name>/...
        # Walker's discovery expects <root>/<slug>/<session>.jsonl, with the
        # fixture directory acting as the slug.
        shutil.copytree(CORPUS / fixture_name, Path(tmp) / fixture_name)
        try:
            got = run_walker(binary, meta, Path(tmp))
        except Exception as e:
            print(f"  [{lang:>4s}] {fixture_name:20s} FAIL  {e}")
            return False
    ok, dt, dw = within_tolerance(got, target)
    badge = " OK " if ok else "FAIL"
    print(
        f"  [{lang:>4s}] {fixture_name:20s} {badge}  "
        f"trailing=${got.get('trailing_usd', 0):.6f} (d=${dt:+.6f})  "
        f"window=${got.get('window_usd', 0):.6f} (d=${dw:+.6f})"
    )
    return ok


def check_implementation(lang: str, binary: Path, expected: dict) -> bool:
    aggregate_ok = check_aggregate(lang, binary, expected)
    fixtures_ok = True
    for name, target in expected["fixtures"].items():
        if not check_fixture(lang, binary, expected["_meta"], name, target):
            fixtures_ok = False
    return aggregate_ok and fixtures_ok


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
          f"win_start={expected['_meta']['win_start_unix']}")
    print(f"Fixtures: {len(expected['fixtures'])}\n")

    for lang in requested:
        binary = find_binary(lang)
        if binary is None:
            print(f"  [{lang:>4s}] SKIP  no built binary "
                  f"(checked {[str(p) for p in CANDIDATES.get(lang, [])]})")
            continue
        if not check_implementation(lang, binary, expected):
            overall_ok = False
        print()

    sys.exit(0 if overall_ok else 1)


if __name__ == "__main__":
    main()
