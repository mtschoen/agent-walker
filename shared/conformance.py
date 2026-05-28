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
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "shared" / "corpus"
EXPECTED = ROOT / "shared" / "expected.json"
BEACON_CORPUS = ROOT / "shared" / "corpus" / "beacons"
MULTI_ROOT_CORPUS = ROOT / "shared" / "corpus" / "multi_root"
SEARCH_CORPUS = ROOT / "shared" / "corpus" / "search"
SEARCH_MULTI_ROOT_CORPUS = ROOT / "shared" / "corpus" / "search_multi_root"
EVENTS_CORPUS = ROOT / "shared" / "corpus" / "events"
EVENTS_EXPECTED = EVENTS_CORPUS / "expected_events.json"
EXPECTED_LATEST = BEACON_CORPUS / "expected_latest.json"
EXPECTED_HISTORY = BEACON_CORPUS / "expected_history.json"
TOLERANCE = 0.01  # $
BIAS_TOLERANCE = 0.001
EVENTS_USD_TOLERANCE = 1e-4
EVENTS_TS_TOLERANCE = 1e-6

# Hit fields that vary per run (absolute tempdir paths) — strip before compare.
SEARCH_HIT_STRIP_KEYS = {"host_root", "file_path"}
# Summary fields that vary per run — strip before compare.
SEARCH_SUMMARY_STRIP_KEYS = {"elapsed_ms", "files_walked"}

# Impls that have implemented the `search` subcommand. Add languages here as
# their search ports land. Until the set contains an impl, its search check
# is skipped (rather than reported as failure).
IMPLS_WITH_SEARCH: set[str] = {"rust", "cpp", "go", "zig"}

# Impls that have implemented the `events` subcommand. Add languages here as
# their events ports land. Until the set contains an impl, its events check
# is skipped (rather than reported as failure).
IMPLS_WITH_EVENTS: set[str] = {"rust", "cpp", "go", "zig"}

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
    # Coverage orchestrator (shared/coverage.py) sets WALKER_BIN_<LANG> to an
    # instrumented build so we can collect coverage without clobbering the
    # release binaries at the default discovery paths.
    override = os.environ.get(f"WALKER_BIN_{lang.upper()}")
    if override:
        path = Path(override)
        return path if path.is_file() else None
    for path in CANDIDATES.get(lang, []):
        if path.is_file():
            return path
    return None


def run_walker(lang: str, binary: Path, meta: dict, projects_root: Path, extras: list[Path] | None = None) -> dict:
    """Run the walker binary against `projects_root`, return parsed JSON output."""
    cmd = [
        str(binary),
        "--period", str(meta["period_seconds"]),
        "--win-start", repr(meta["win_start_unix"]),
        "--now", repr(meta["now_unix"]),
        "--projects-root", str(projects_root),
        "--no-config",
    ]
    for extra in extras or []:
        cmd.extend(["--extra-projects-root", str(extra)])
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=10)
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
        got = run_walker(lang, binary, meta, CORPUS)
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
            got = run_walker(lang, binary, meta, Path(tmp))
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


def run_walker_subcommand(lang: str, binary: Path, subcommand: str, args: list[str]) -> dict:
    """Invoke a walker subcommand, return parsed JSON of last stdout line."""
    cmd = [str(binary), subcommand, *args, "--no-config"]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=10)
    if result.returncode != 0:
        raise RuntimeError(
            f"{binary.name} {subcommand} exited {result.returncode}\n"
            f"stderr:\n{result.stderr}"
        )
    line = result.stdout.strip().splitlines()[-1]
    return json.loads(line)


def assert_beacons_latest(lang: str, binary: Path, expected: dict) -> bool:
    """Run beacons-latest against each scenario in isolation."""
    meta = expected["_meta"]
    overall = True
    for scenario, target in expected["fixtures"].items():
        with tempfile.TemporaryDirectory(prefix="walker-beacon-latest-") as tmp:
            shutil.copytree(BEACON_CORPUS / scenario, Path(tmp) / scenario)
            label = f"beacons-latest:{scenario}"
            try:
                got = run_walker_subcommand(lang, binary, "beacons-latest", [
                    "--session-id", target["session_id"],
                    "--projects-root", str(tmp),
                    "--now", repr(meta["now_unix"]),
                ])
            except Exception as e:
                print(f"  [{lang:>4s}] {label:38s} FAIL  {e}")
                overall = False
                continue
        got_cmp = {
            "beacon": got.get("beacon"),
            "emitted_at": got.get("emitted_at"),
            "age_seconds": got.get("age_seconds"),
        }
        target_cmp = {
            "beacon": target.get("beacon"),
            "emitted_at": target.get("emitted_at"),
            "age_seconds": target.get("age_seconds"),
        }
        ok = got_cmp == target_cmp
        badge = " OK " if ok else "FAIL"
        print(f"  [{lang:>4s}] {label:38s} {badge}")
        if not ok:
            print(f"        got:    {got_cmp}")
            print(f"        target: {target_cmp}")
            overall = False
    return overall


def assert_beacons_history(lang: str, binary: Path, expected: dict) -> bool:
    """Run beacons-history against each history scenario in isolation."""
    meta = expected["_meta"]
    overall = True

    def pairs_key(p):
        return (p["begin_eta"], p["actual_elapsed"])

    for scenario, target in expected["fixtures"].items():
        label = f"beacons-history:{scenario}"
        with tempfile.TemporaryDirectory(prefix="walker-beacon-history-") as tmp:
            # Each scenario dir is a projects-root containing slug subdirs.
            tree = Path(tmp) / "tree"
            shutil.copytree(BEACON_CORPUS / scenario, tree)
            try:
                got = run_walker_subcommand(lang, binary, "beacons-history", [
                    "--period", "604800",  # 7 days, generous
                    "--win-start", "0",
                    "--projects-root", str(tree),
                    "--now", repr(meta["now_unix"]),
                ])
            except Exception as e:
                print(f"  [{lang:>4s}] {label:38s} FAIL  {e}")
                overall = False
                continue

        pairs_ok = sorted(map(pairs_key, got.get("pairs", []))) == sorted(
            map(pairs_key, target["pairs"])
        )
        counts_ok = (
            got.get("session_count") == target["session_count"]
            and got.get("n_pairs") == target["n_pairs"]
        )
        bias_got = got.get("bias_factor")
        bias_tgt = target.get("bias_factor")
        if bias_got is None and bias_tgt is None:
            bias_ok = True
        elif bias_got is None or bias_tgt is None:
            bias_ok = False
        else:
            bias_ok = abs(bias_got - bias_tgt) <= BIAS_TOLERANCE
        ok = pairs_ok and counts_ok and bias_ok
        badge = " OK " if ok else "FAIL"
        print(
            f"  [{lang:>4s}] {label:38s} {badge}  "
            f"n_pairs={got.get('n_pairs')}  bias={bias_got}"
        )
        if not ok:
            print(f"        got:    {got}")
            print(f"        target: {target}")
            overall = False
    return overall


def check_beacons(lang: str, binary: Path) -> bool:
    """Run all beacon-mode assertions if expected files are present."""
    if not EXPECTED_LATEST.is_file() or not EXPECTED_HISTORY.is_file():
        print(f"  [{lang:>4s}] beacon expected files missing -- skipping beacon assertions")
        return True
    expected_latest = json.loads(EXPECTED_LATEST.read_text(encoding="utf-8"))
    expected_history = json.loads(EXPECTED_HISTORY.read_text(encoding="utf-8"))
    latest_ok = assert_beacons_latest(lang, binary, expected_latest)
    history_ok = assert_beacons_history(lang, binary, expected_history)
    return latest_ok and history_ok


def check_multi_root(lang: str, binary: Path) -> bool:
    """Run each multi-root scenario; assert binary sums match expected.json."""
    if not MULTI_ROOT_CORPUS.is_dir():
        return True  # no scenarios — skip cleanly
    all_ok = True
    for scenario_dir in sorted(MULTI_ROOT_CORPUS.iterdir()):
        if not scenario_dir.is_dir():
            continue
        expected_file = scenario_dir / "expected.json"
        if not expected_file.is_file():
            continue
        data = json.loads(expected_file.read_text())
        meta = data["_meta"]
        primary = scenario_dir / data["primary_root"]
        extras = [scenario_dir / r for r in data["extra_roots"]]
        try:
            got = run_walker(lang, binary, meta, primary, extras=extras)
        except Exception as e:
            print(f"  [{lang:>4s}] {scenario_dir.name:<22s} FAIL  {e}")
            all_ok = False
            continue
        target = data["expected"]
        ok, dt, dw = within_tolerance(got, target)
        badge = " OK " if ok else "FAIL"
        print(
            f"  [{lang:>4s}] {scenario_dir.name:<22s} {badge}  "
            f"trailing=${got.get('trailing_usd', 0):.6f} (d=${dt:+.6f})  "
            f"window=${got.get('window_usd', 0):.6f} (d=${dw:+.6f})"
        )
        if not ok:
            all_ok = False
    return all_ok


def run_walker_search(
    lang: str,
    binary: Path,
    projects_root: Path,
    pattern: str,
    flags: list[str],
    now_unix: float,
    extras: list[Path] | None = None,
) -> tuple[list[dict], dict | None]:
    """Invoke `walker search <pattern> <flags> --format jsonl ...`.

    `extras` adds `--extra-projects-root <path>` flags (the multi-root case).
    Returns (hits, summary). Hits are stripped of per-run path fields
    (host_root, file_path); summary is stripped of elapsed_ms / files_walked.
    Raises RuntimeError on non-zero exit.
    """
    cmd = [
        str(binary), "search", pattern,
        "--projects-root", str(projects_root),
        "--now", repr(now_unix),
        "--format", "jsonl",
        "--no-config",
    ]
    for extra in extras or []:
        cmd.extend(["--extra-projects-root", str(extra)])
    cmd.extend(flags)
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=10)
    if result.returncode != 0:
        raise RuntimeError(
            f"{binary.name} search exited {result.returncode}\n"
            f"cmd: {' '.join(cmd)}\n"
            f"stderr:\n{result.stderr}"
        )
    hits: list[dict] = []
    summary: dict | None = None
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"non-JSON line on stdout: {line!r} ({e})")
        kind = obj.get("type")
        if kind == "hit":
            for key in SEARCH_HIT_STRIP_KEYS:
                obj.pop(key, None)
            hits.append(obj)
        elif kind == "summary":
            for key in SEARCH_SUMMARY_STRIP_KEYS:
                obj.pop(key, None)
            summary = obj
        else:
            raise RuntimeError(f"unknown record type on stdout: {obj!r}")
    return hits, summary


def assert_search_combo(
    lang: str,
    binary: Path,
    scenario_name: str,
    combo_name: str,
    combo: dict,
    now_unix: float,
) -> bool:
    """Run one (scenario, combo) and structurally compare to expected output."""
    label = f"search/{scenario_name}/{combo_name}"
    with tempfile.TemporaryDirectory(prefix=f"walker-search-{scenario_name}-") as tmp:
        shutil.copytree(SEARCH_CORPUS / scenario_name, Path(tmp) / scenario_name)
        try:
            got_hits, got_summary = run_walker_search(
                lang, binary, Path(tmp),
                combo["pattern"], combo["flags"], now_unix,
            )
        except Exception as e:
            print(f"  [{lang:>4s}] {label:48s} FAIL  {e}")
            return False

    exp_hits = combo["hits"]
    exp_summary = combo["summary"]
    hits_ok = got_hits == exp_hits
    summary_ok = got_summary == exp_summary
    ok = hits_ok and summary_ok
    badge = " OK " if ok else "FAIL"
    print(f"  [{lang:>4s}] {label:48s} {badge}  hits={len(got_hits)}")
    if not hits_ok:
        print(f"        hits mismatch: got {len(got_hits)}, expected {len(exp_hits)}")
        for i in range(max(len(got_hits), len(exp_hits))):
            g = got_hits[i] if i < len(got_hits) else None
            e = exp_hits[i] if i < len(exp_hits) else None
            if g != e:
                print(f"          hit[{i}] got:      {json.dumps(g, sort_keys=True)}")
                print(f"          hit[{i}] expected: {json.dumps(e, sort_keys=True)}")
                break
    if not summary_ok:
        print(f"        summary mismatch:")
        print(f"          got:      {json.dumps(got_summary, sort_keys=True)}")
        print(f"          expected: {json.dumps(exp_summary, sort_keys=True)}")
    return ok


def check_search(lang: str, binary: Path) -> bool:
    """Run every scenario/combo in shared/corpus/search/."""
    if not SEARCH_CORPUS.is_dir():
        return True  # corpus missing -- nothing to test
    if lang not in IMPLS_WITH_SEARCH:
        print(f"  [{lang:>4s}] search subcommand -- skipping (not in IMPLS_WITH_SEARCH)")
        return True
    all_ok = True
    for scenario_dir in sorted(SEARCH_CORPUS.iterdir()):
        if not scenario_dir.is_dir():
            continue
        expected_file = scenario_dir / "expected.json"
        if not expected_file.is_file():
            continue
        data = json.loads(expected_file.read_text(encoding="utf-8"))
        now_unix = data["_meta"]["now_unix"]
        for combo_name, combo in data["combos"].items():
            if not assert_search_combo(
                lang, binary, scenario_dir.name, combo_name, combo, now_unix,
            ):
                all_ok = False
    return all_ok


def check_search_multi_root(lang: str, binary: Path) -> bool:
    """Multi-root search: assert --extra-projects-root reaches a second root,
    hits aggregate + sort across roots, and roots_walked reflects the count.

    Regression guard for the cpp perf-pass-2 search rewrite, which dropped root
    resolution (ignored --extra-projects-root / walker-roots.json, hardcoded
    roots_walked=1). rust/go/zig already pass this. Runs read-only directly
    against the corpus (search never writes), mirroring check_multi_root.
    """
    if not SEARCH_MULTI_ROOT_CORPUS.is_dir():
        return True  # corpus missing -- nothing to test
    if lang not in IMPLS_WITH_SEARCH:
        print(f"  [{lang:>4s}] search multi-root -- skipping (not in IMPLS_WITH_SEARCH)")
        return True
    all_ok = True
    for scenario_dir in sorted(SEARCH_MULTI_ROOT_CORPUS.iterdir()):
        if not scenario_dir.is_dir():
            continue
        expected_file = scenario_dir / "expected.json"
        if not expected_file.is_file():
            continue
        data = json.loads(expected_file.read_text(encoding="utf-8"))
        now_unix = data["_meta"]["now_unix"]
        primary = scenario_dir / data["primary_root"]
        extras = [scenario_dir / r for r in data["extra_roots"]]
        for combo_name, combo in data["combos"].items():
            label = f"search-mr/{scenario_dir.name}/{combo_name}"
            try:
                got_hits, got_summary = run_walker_search(
                    lang, binary, primary,
                    combo["pattern"], combo["flags"], now_unix, extras=extras,
                )
            except Exception as e:
                print(f"  [{lang:>4s}] {label:48s} FAIL  {e}")
                all_ok = False
                continue
            hits_ok = got_hits == combo["hits"]
            summary_ok = got_summary == combo["summary"]
            ok = hits_ok and summary_ok
            badge = " OK " if ok else "FAIL"
            walked = got_summary.get("roots_walked") if got_summary else "?"
            print(f"  [{lang:>4s}] {label:48s} {badge}  hits={len(got_hits)} roots_walked={walked}")
            if not hits_ok:
                print(f"        hits mismatch: got {len(got_hits)}, expected {len(combo['hits'])}")
                for i in range(max(len(got_hits), len(combo["hits"]))):
                    g = got_hits[i] if i < len(got_hits) else None
                    e = combo["hits"][i] if i < len(combo["hits"]) else None
                    if g != e:
                        print(f"          hit[{i}] got:      {json.dumps(g, sort_keys=True)}")
                        print(f"          hit[{i}] expected: {json.dumps(e, sort_keys=True)}")
                        break
            if not summary_ok:
                print(f"        summary got:      {json.dumps(got_summary, sort_keys=True)}")
                print(f"        summary expected: {json.dumps(combo['summary'], sort_keys=True)}")
            if not ok:
                all_ok = False
    return all_ok


def _events_sort_key(record: dict) -> tuple:
    """Stable sort key for events-mode NDJSON records (multiset comparison)."""
    return (
        record.get("ts", 0.0),
        record.get("session_id", ""),
        record.get("model", ""),
    )


def check_events(lang: str, binary: Path) -> bool:
    """Run events subcommand against each fixture; compare NDJSON output to expected."""
    if not EVENTS_EXPECTED.is_file():
        print(f"  [{lang:>4s}] events expected file missing -- skipping events assertions")
        return True
    if lang not in IMPLS_WITH_EVENTS:
        print(f"  [{lang:>4s}] events subcommand -- skipping (not in IMPLS_WITH_EVENTS)")
        return True

    expected_data = json.loads(EVENTS_EXPECTED.read_text(encoding="utf-8"))
    pin_now = expected_data["pin_now"]
    pin_period = expected_data["pin_period"]
    pin_win_start = expected_data["pin_win_start"]
    all_ok = True

    for fixture_name, target_records in sorted(expected_data["fixtures"].items()):
        fixture_root = EVENTS_CORPUS / fixture_name
        if not fixture_root.is_dir():
            print(f"  [{lang:>4s}] events/{fixture_name:20s} FAIL  fixture dir missing")
            all_ok = False
            continue

        label = f"events/{fixture_name}"
        cmd = [
            str(binary), "events",
            "--period", repr(pin_period),
            "--win-start", repr(pin_win_start),
            "--projects-root", str(fixture_root),
            "--now", repr(pin_now),
            "--no-config",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=10)
        if result.returncode != 0:
            print(
                f"  [{lang:>4s}] {label:30s} FAIL  "
                f"exit {result.returncode}: {result.stderr.strip()[:120]}"
            )
            all_ok = False
            continue

        # Parse NDJSON output — one JSON object per non-blank line.
        got_records: list[dict] = []
        parse_error: str | None = None
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            try:
                got_records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                parse_error = f"non-JSON line: {line!r} ({exc})"
                break

        if parse_error:
            print(f"  [{lang:>4s}] {label:30s} FAIL  {parse_error}")
            all_ok = False
            continue

        # Sort both lists by (ts, session_id, model) for stable multiset comparison.
        got_sorted = sorted(got_records, key=_events_sort_key)
        target_sorted = sorted(target_records, key=_events_sort_key)

        if len(got_sorted) != len(target_sorted):
            print(
                f"  [{lang:>4s}] {label:30s} FAIL  "
                f"length mismatch: got {len(got_sorted)}, expected {len(target_sorted)}"
            )
            all_ok = False
            continue

        first_diff: str | None = None
        records_ok = True
        for index, (got, target) in enumerate(zip(got_sorted, target_sorted)):
            ts_ok = abs(got.get("ts", 0.0) - target.get("ts", 0.0)) <= EVENTS_TS_TOLERANCE
            usd_ok = abs(got.get("usd", 0.0) - target.get("usd", 0.0)) <= EVENTS_USD_TOLERANCE
            fields_ok = (
                got.get("model") == target.get("model")
                and got.get("session_id") == target.get("session_id")
                and got.get("slug") == target.get("slug")
            )
            if not (ts_ok and usd_ok and fields_ok):
                records_ok = False
                first_diff = (
                    f"record[{index}] mismatch: "
                    f"got {json.dumps(got, sort_keys=True)} "
                    f"expected {json.dumps(target, sort_keys=True)}"
                )
                break

        badge = " OK " if records_ok else "FAIL"
        print(
            f"  [{lang:>4s}] {label:30s} {badge}  "
            f"records={len(got_sorted)}"
        )
        if first_diff:
            print(f"        {first_diff}")
            all_ok = False

    return all_ok


def check_empty_root(lang: str, binary: Path, expected: dict) -> bool:
    """A17: every mode pointed at a nonexistent --projects-root must exit 0
    with empty/zero output (no crash, no stderr noise that consumers
    misinterpret as a fault). Covers cost / events / beacons-latest /
    beacons-history / search.

    The harness already exercises the *existing-but-empty* root case (the
    `04-empty` cost fixture), but never the *nonexistent* path. The
    discovery layer must skip a non-existent root silently per SPEC.md
    §Roots: "Non-existent extras are skipped silently with a stderr
    diagnostic. ... walker must keep going."
    """
    meta = expected["_meta"]
    all_ok = True

    with tempfile.TemporaryDirectory(prefix="walker-empty-root-") as tmp:
        # A path that DOES NOT EXIST under tmp.
        missing = Path(tmp) / "does-not-exist"
        common_args = [
            "--projects-root", str(missing),
            "--no-config",
            "--now", repr(meta["now_unix"]),
        ]

        # 1. cost mode (bare flags) — JSON with zero totals, exit 0.
        cmd = [str(binary),
               "--period", str(meta["period_seconds"]),
               "--win-start", repr(meta["win_start_unix"]),
               *common_args]
        result = subprocess.run(cmd, capture_output=True, text=True,
                                encoding="utf-8", timeout=10)
        cost_ok = result.returncode == 0
        if cost_ok:
            try:
                line = result.stdout.strip().splitlines()[-1]
                payload = json.loads(line)
                cost_ok = (
                    abs(payload.get("trailing_usd", 1) - 0.0) < TOLERANCE
                    and abs(payload.get("window_usd", 1) - 0.0) < TOLERANCE
                )
            except Exception:
                cost_ok = False
        print(f"  [{lang:>4s}] {'empty-root: cost':38s} "
              f"{' OK ' if cost_ok else 'FAIL'}")
        if not cost_ok:
            print(f"        exit={result.returncode} stdout={result.stdout!r}")
            all_ok = False

        # 2. events — exit 0, no NDJSON records (empty stdout).
        cmd = [str(binary), "events",
               "--period", str(meta["period_seconds"]),
               "--win-start", repr(meta["win_start_unix"]),
               *common_args]
        result = subprocess.run(cmd, capture_output=True, text=True,
                                encoding="utf-8", timeout=10)
        # events allows empty stdout per SPEC §events ("Exit 0 even when
        # the output stream is empty.").
        events_ok = result.returncode == 0 and all(
            not line.strip() for line in result.stdout.splitlines()
        )
        print(f"  [{lang:>4s}] {'empty-root: events':38s} "
              f"{' OK ' if events_ok else 'FAIL'}")
        if not events_ok:
            print(f"        exit={result.returncode} stdout={result.stdout!r}")
            all_ok = False

        # 3. beacons-latest — exit 0, beacon = null.
        cmd = [str(binary), "beacons-latest",
               "--session-id", "no-such-session",
               *common_args]
        result = subprocess.run(cmd, capture_output=True, text=True,
                                encoding="utf-8", timeout=10)
        bl_ok = result.returncode == 0
        if bl_ok:
            try:
                line = result.stdout.strip().splitlines()[-1]
                payload = json.loads(line)
                bl_ok = payload.get("beacon") is None
            except Exception:
                bl_ok = False
        print(f"  [{lang:>4s}] {'empty-root: beacons-latest':38s} "
              f"{' OK ' if bl_ok else 'FAIL'}")
        if not bl_ok:
            print(f"        exit={result.returncode} stdout={result.stdout!r}")
            all_ok = False

        # 4. beacons-history — exit 0, n_pairs = 0, bias_factor = null.
        cmd = [str(binary), "beacons-history",
               "--period", str(meta["period_seconds"]),
               "--win-start", repr(meta["win_start_unix"]),
               *common_args]
        result = subprocess.run(cmd, capture_output=True, text=True,
                                encoding="utf-8", timeout=10)
        bh_ok = result.returncode == 0
        if bh_ok:
            try:
                line = result.stdout.strip().splitlines()[-1]
                payload = json.loads(line)
                bh_ok = (
                    payload.get("n_pairs") == 0
                    and payload.get("bias_factor") is None
                )
            except Exception:
                bh_ok = False
        print(f"  [{lang:>4s}] {'empty-root: beacons-history':38s} "
              f"{' OK ' if bh_ok else 'FAIL'}")
        if not bh_ok:
            print(f"        exit={result.returncode} stdout={result.stdout!r}")
            all_ok = False

        # 5. search — exit 0, zero hits in jsonl output.
        if lang in IMPLS_WITH_SEARCH:
            cmd = [str(binary), "search", "pattern",
                   "--format", "jsonl",
                   *common_args]
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    encoding="utf-8", timeout=10)
            search_ok = result.returncode == 0
            hits_count = 0
            summary_ok = False
            if search_ok:
                try:
                    for line in result.stdout.splitlines():
                        if not line.strip():
                            continue
                        obj = json.loads(line)
                        kind = obj.get("type")
                        if kind == "hit":
                            hits_count += 1
                        elif kind == "summary":
                            summary_ok = obj.get("hits", -1) == 0
                except Exception:
                    search_ok = False
            search_ok = search_ok and hits_count == 0 and summary_ok
            print(f"  [{lang:>4s}] {'empty-root: search':38s} "
                  f"{' OK ' if search_ok else 'FAIL'}")
            if not search_ok:
                print(f"        exit={result.returncode} stdout={result.stdout!r}")
                all_ok = False

    return all_ok


def check_help(lang: str, binary: Path) -> bool:
    """Parity guard for the friendly help / default output.

    Wording is per-impl, so we assert structure only: --help and no-args
    print an overview to stdout and exit 0; a subcommand + --help does too;
    a bogus flag is a usage error (exit 2). Catches an impl that forgot a
    subcommand in its overview without pinning exact text.
    """
    required_substrings = [
        "USAGE",
        "cost",
        "search",
        "events",
        "beacons-latest",
        "beacons-history",
        "--period",
    ]
    all_ok = True

    def run(args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(binary), *args],
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )

    # 1. --help: exit 0, overview on stdout with every subcommand + --period.
    result = run(["--help"])
    missing = [s for s in required_substrings if s not in result.stdout]
    ok = result.returncode == 0 and not missing
    print(f"  [{lang:>4s}] {'help: --help':38s} {' OK ' if ok else 'FAIL'}")
    if not ok:
        print(f"        exit={result.returncode} missing={missing}")
        all_ok = False

    # 2. No args: friendly overview on stdout, exit 0 (not the terse error).
    result = run([])
    ok = result.returncode == 0 and result.stdout.strip() != ""
    print(f"  [{lang:>4s}] {'help: no-args':38s} {' OK ' if ok else 'FAIL'}")
    if not ok:
        print(f"        exit={result.returncode} stdout_empty={result.stdout.strip() == ''}")
        all_ok = False

    # 3. Subcommand + --help: same overview, exit 0 (rule 3).
    result = run(["search", "--help"])
    ok = result.returncode == 0 and "USAGE" in result.stdout
    print(f"  [{lang:>4s}] {'help: search --help':38s} {' OK ' if ok else 'FAIL'}")
    if not ok:
        print(f"        exit={result.returncode}")
        all_ok = False

    # 4. Bogus flag: usage error, exit 2 (still an error, not help).
    result = run(["--bogus-flag"])
    ok = result.returncode == 2
    print(f"  [{lang:>4s}] {'help: bad flag -> exit 2':38s} {' OK ' if ok else 'FAIL'}")
    if not ok:
        print(f"        exit={result.returncode} (expected 2)")
        all_ok = False

    return all_ok


def run_walker_env(
    lang: str, binary: Path, meta: dict, env: dict, projects_root: Path | None = None
) -> dict:
    """Run cost mode WITHOUT --no-config (so walker-roots.json is read) under a
    custom environment (to control home-dir resolution). With projects_root
    omitted, the binary falls back to its default <home>/.claude/projects."""
    cmd = [
        str(binary),
        "--period", str(meta["period_seconds"]),
        "--win-start", repr(meta["win_start_unix"]),
        "--now", repr(meta["now_unix"]),
    ]
    if projects_root is not None:
        cmd.extend(["--projects-root", str(projects_root)])
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", timeout=10, env=env
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{binary.name} exited {result.returncode}\nstderr:\n{result.stderr}"
        )
    line = result.stdout.strip().splitlines()[-1]
    return json.loads(line)


def check_config_resolution(lang: str, binary: Path, expected: dict) -> bool:
    """Exercise home-dir + walker-roots.json resolution.

    The --no-config runners above never touch either, so a wrong HOME-vs-
    USERPROFILE precedence or a dropped config root sails through. Lay out a
    fake home:

        <home>/.claude/projects/<fixtureA>/...         -> default primary root
        <home>/.claude/walker-roots.json -> [<extra>]  -> config extra
        <extra>/<fixtureB>/...                         -> extra root data

    Point the canonical home var (USERPROFILE on Windows, else HOME) at <home>
    and the *other* var at a config-less dir, per the precedence contract in
    SPEC.md::Roots. Run with NO --projects-root and NO --no-config, so passing
    requires the binary to resolve BOTH the default root and the config file
    from the canonical home var. Expect the fixtureA+fixtureB sum.
    """
    meta = expected["_meta"]
    names = list(expected["fixtures"].keys())
    label = "config-resolution"
    if len(names) < 2:
        print(f"  [{lang:>4s}] {label:20s} SKIP  need >=2 fixtures")
        return True
    # Pick the two priciest fixtures so the sum is discriminating (a dropped
    # config root or a 0-cost primary can't masquerade as a pass).
    ranked = sorted(names, key=lambda n: expected["fixtures"][n]["trailing_usd"], reverse=True)
    fixture_a, fixture_b = ranked[0], ranked[1]
    target = {
        "trailing_usd": expected["fixtures"][fixture_a]["trailing_usd"]
        + expected["fixtures"][fixture_b]["trailing_usd"],
        "window_usd": expected["fixtures"][fixture_a]["window_usd"]
        + expected["fixtures"][fixture_b]["window_usd"],
    }
    with tempfile.TemporaryDirectory(prefix="walker-cfg-home-") as home, \
         tempfile.TemporaryDirectory(prefix="walker-cfg-extra-") as extra, \
         tempfile.TemporaryDirectory(prefix="walker-cfg-bogus-") as bogus:
        home_p, extra_p = Path(home), Path(extra)
        shutil.copytree(CORPUS / fixture_a, home_p / ".claude" / "projects" / fixture_a)
        shutil.copytree(CORPUS / fixture_b, extra_p / fixture_b)
        (home_p / ".claude" / "walker-roots.json").write_text(
            json.dumps({"extra_roots": [str(extra_p)]}), encoding="utf-8"
        )
        env = dict(os.environ)
        if sys.platform == "win32":
            env["USERPROFILE"], env["HOME"] = str(home_p), str(bogus)
        else:
            env["HOME"], env["USERPROFILE"] = str(home_p), str(bogus)
        try:
            got = run_walker_env(lang, binary, meta, env)
        except Exception as e:
            print(f"  [{lang:>4s}] {label:20s} FAIL  {e}")
            return False
    ok, dt, dw = within_tolerance(got, target)
    badge = " OK " if ok else "FAIL"
    print(
        f"  [{lang:>4s}] {label:20s} {badge}  "
        f"trailing=${got.get('trailing_usd', 0):.6f} (d=${dt:+.6f})  "
        f"window=${got.get('window_usd', 0):.6f} (d=${dw:+.6f})"
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
        if not check_beacons(lang, binary):
            overall_ok = False
        if not check_multi_root(lang, binary):
            overall_ok = False
        if not check_config_resolution(lang, binary, expected):
            overall_ok = False
        if not check_search(lang, binary):
            overall_ok = False
        if not check_search_multi_root(lang, binary):
            overall_ok = False
        if not check_events(lang, binary):
            overall_ok = False
        if not check_help(lang, binary):
            overall_ok = False
        if not check_empty_root(lang, binary, expected):
            overall_ok = False
        print()

    sys.exit(0 if overall_ok else 1)


if __name__ == "__main__":
    main()
