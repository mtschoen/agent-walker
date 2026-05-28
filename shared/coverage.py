#!/usr/bin/env python3
"""Coverage orchestrator for claude-walker's four implementations.

Builds each implementation in an *instrumented* mode, drives the existing
``conformance.py`` harness against the instrumented binary (so every walker
subprocess emits coverage data), then collects per-language line coverage and
writes ``TEST-REPORT.md`` at the repo root.

The conformance harness runs the binary as a subprocess inheriting our
environment, so we hook coverage in by (a) setting the right env vars
(``LLVM_PROFILE_FILE``, ``GOCOVERDIR``, …) and (b) pointing the harness at the
instrumented build via ``WALKER_BIN_<LANG>`` (see ``conformance.find_binary``).

Per-language mechanism:

    rust -> cargo-llvm-cov, "external test" show-env workflow. Binary lands at
            the normal rust/target/release/walker path.
    cpp  -> clang -fprofile-instr-generate -fcoverage-mapping (WALKER_COVERAGE
            CMake option), separate cpp/build-cov tree; llvm-profdata + llvm-cov.
            Vendored simdjson (build/_deps) excluded from the denominator.
    go   -> `go build -cover` into go/walker-cov; GOCOVERDIR per run; go tool
            covdata textfmt -> statement counts.
    zig  -> LLVM-backend Debug build (`-Dcoverage=true`; the default self-hosted
            backend emits DWARF kcov cannot parse), run under a DWARF5-capable
            kcov (see find_kcov). Each invocation accumulates into one outdir.

Usage:
    python shared/coverage.py [rust cpp go zig]   # default: all available
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUST_DIR = ROOT / "rust"
CPP_DIR = ROOT / "cpp"
GO_DIR = ROOT / "go"
ZIG_DIR = ROOT / "zig"
COVERAGE_DIR = ROOT / "coverage"
CONFORMANCE = ROOT / "shared" / "conformance.py"
REPORT = ROOT / "TEST-REPORT.md"

ALL_LANGS = ["rust", "cpp", "go", "zig"]


@dataclass
class Result:
    lang: str
    metric: str                       # "lines" | "statements"
    covered: int
    total: int
    files: list[tuple[str, int, int]] = field(default_factory=list)  # (name, covered, total)
    conformance_ok: bool = False
    conformance_passed: int = 0
    conformance_failed: int = 0
    note: str = ""

    @property
    def percent(self) -> float:
        return 100.0 * self.covered / self.total if self.total else 0.0

    @property
    def uncovered(self) -> int:
        return self.total - self.covered


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def run(cmd, *, cwd=None, env=None, check=True, capture=True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=cwd, env=env, check=check,
        capture_output=capture, text=True, encoding="utf-8",
    )


def run_conformance(lang: str, env: dict) -> tuple[bool, int, int]:
    """Run conformance.py for one language with `env`; return (ok, passed, failed)."""
    proc = run(
        [sys.executable, str(CONFORMANCE), lang],
        cwd=ROOT, env=env, check=False,
    )
    out = proc.stdout + proc.stderr
    passed = out.count(" OK ") + out.count(" OK\n")
    failed = sum(out.count(tok) for tok in ("FAIL", "MISMATCH"))
    ok = proc.returncode == 0
    if not ok:
        sys.stderr.write(f"\n[coverage] conformance({lang}) FAILED (exit {proc.returncode}):\n")
        sys.stderr.write("\n".join(out.splitlines()[-25:]) + "\n")
    return ok, passed, failed


def git_describe() -> tuple[str, str]:
    short = run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, check=False).stdout.strip()
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ROOT, check=False).stdout.strip()
    return short or "unknown", branch or "unknown"


def _have(tool: str) -> bool:
    return shutil.which(tool) is not None


def find_kcov() -> str | None:
    """Locate a DWARF5-capable kcov.

    Stock kcov releases (and the liyu1981/zig-kcov fork) cannot parse the DWARF
    that modern Zig/Clang emit — they crash or silently report zero lines. The
    upstream master build does work, so we look for it explicitly first.
    Override with the KCOV env var.
    """
    explicit = os.environ.get("KCOV")
    if explicit and Path(explicit).is_file():
        return explicit
    candidates = [
        Path.home() / ".local/src/kcov-master/build/src/kcov",
        Path.home() / ".local/bin/kcov-master",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return shutil.which("kcov")


def _llvm_lines_from_export(export_json: str) -> tuple[int, int, list[tuple[str, int, int]]]:
    """Parse line totals + per-file from an `llvm-cov export -summary-only` blob."""
    data = json.loads(export_json)
    block = data["data"][0]
    totals = block["totals"]["lines"]
    files = [
        (Path(f["filename"]).name, f["summary"]["lines"]["covered"], f["summary"]["lines"]["count"])
        for f in block["files"]
    ]
    files.sort()
    return totals["covered"], totals["count"], files


# --------------------------------------------------------------------------- #
# Rust — cargo-llvm-cov external-test workflow
# --------------------------------------------------------------------------- #
def _rust_env() -> dict:
    env = dict(os.environ)
    out = run([_CARGO, "llvm-cov", "show-env", "--sh"], cwd=RUST_DIR).stdout
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("export ") or "=" not in line:
            continue
        key, value = line[len("export "):].split("=", 1)
        env[key.strip()] = value.strip().strip("'\"")
    # The release profile strips symbols and uses fat LTO; both corrupt coverage
    # mapping. Override via env so we never have to touch Cargo.toml.
    env["CARGO_PROFILE_RELEASE_STRIP"] = "false"
    env["CARGO_PROFILE_RELEASE_LTO"] = "false"
    return env


_CARGO = "cargo"


def cov_rust() -> Result | None:
    if not _have(_CARGO) or not (RUST_DIR / "Cargo.toml").exists():
        return None
    if run([_CARGO, "llvm-cov", "--version"], check=False).returncode != 0:
        return Result("rust", "lines", 0, 0, note="cargo-llvm-cov not installed (cargo install cargo-llvm-cov)")
    print("[coverage] rust: build instrumented + run conformance …")
    env = _rust_env()
    run([_CARGO, "llvm-cov", "clean", "--workspace"], cwd=RUST_DIR, env=env)
    run([_CARGO, "build", "--release"], cwd=RUST_DIR, env=env)
    ok, passed, failed = run_conformance("rust", env)
    export_json = run(
        [_CARGO, "llvm-cov", "report", "--release", "--json", "--summary-only"],
        cwd=RUST_DIR, env=env,
    ).stdout
    covered, total, files = _llvm_lines_from_export(export_json)
    return Result("rust", "lines", covered, total, files, ok, passed, failed)


# --------------------------------------------------------------------------- #
# C++ — clang source-based coverage
# --------------------------------------------------------------------------- #
def cov_cpp() -> Result | None:
    if not (_have("clang++") and _have("llvm-profdata") and _have("llvm-cov") and _have("cmake")):
        return Result("cpp", "lines", 0, 0, note="need clang++, cmake, llvm-profdata, llvm-cov")
    print("[coverage] cpp: configure WALKER_COVERAGE build + run conformance …")
    build = CPP_DIR / "build-cov"
    profraw_dir = build / "profraw"
    profraw_dir.mkdir(parents=True, exist_ok=True)
    for stale in profraw_dir.glob("*.profraw"):
        stale.unlink()
    cfg_env = dict(os.environ, CC="clang", CXX="clang++")
    cmake_args = [
        "cmake", "-S", str(CPP_DIR), "-B", str(build),
        "-DCMAKE_BUILD_TYPE=Debug", "-DWALKER_COVERAGE=ON",
    ]
    # Reuse already-fetched simdjson if a normal build tree has it.
    fetched = CPP_DIR / "build" / "_deps"
    if fetched.is_dir():
        cmake_args.append(f"-DFETCHCONTENT_BASE_DIR={fetched}")
    run(cmake_args, env=cfg_env)
    run(["cmake", "--build", str(build), "-j"], env=cfg_env)
    binary = build / "walker"
    env = dict(os.environ)
    env["LLVM_PROFILE_FILE"] = str(profraw_dir / "cpp-%p.profraw")
    env["WALKER_BIN_CPP"] = str(binary)
    ok, passed, failed = run_conformance("cpp", env)
    profdata = build / "cpp.profdata"
    raws = [str(p) for p in profraw_dir.glob("*.profraw")]
    run(["llvm-profdata", "merge", "-sparse", *raws, "-o", str(profdata)])
    export_json = run([
        "llvm-cov", "export", str(binary),
        f"-instr-profile={profdata}", "-summary-only",
        "-ignore-filename-regex=_deps/",
    ]).stdout
    covered, total, files = _llvm_lines_from_export(export_json)
    return Result("cpp", "lines", covered, total, files, ok, passed, failed)


# --------------------------------------------------------------------------- #
# Go — integration coverage (go build -cover)
# --------------------------------------------------------------------------- #
def cov_go() -> Result | None:
    if not _have("go") or not (GO_DIR / "go.mod").exists():
        return None
    print("[coverage] go: build -cover + run conformance …")
    binary = GO_DIR / "walker-cov"
    run(["go", "build", "-cover", "-o", str(binary), "."], cwd=GO_DIR)
    covdir = GO_DIR / "covdata"
    if covdir.exists():
        shutil.rmtree(covdir)
    covdir.mkdir()
    env = dict(os.environ, GOCOVERDIR=str(covdir), WALKER_BIN_GO=str(binary))
    ok, passed, failed = run_conformance("go", env)
    txt = GO_DIR / "go-cov.txt"
    run(["go", "tool", "covdata", "textfmt", f"-i={covdir}", f"-o={txt}"], cwd=GO_DIR)
    covered, total, per_file = _go_counts(txt)
    files = sorted((Path(f).name, c, t) for f, (c, t) in per_file.items())
    return Result("go", "statements", covered, total, files, ok, passed, failed)


def _go_counts(txt_path: Path) -> tuple[int, int, dict[str, tuple[int, int]]]:
    total = covered = 0
    per_file: dict[str, list[int]] = {}
    for line in txt_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("mode:"):
            continue
        # "walker/main.go:120.13,134.2 4 1"  ->  loc  numstmt  count
        loc, numstmt, count = line.rsplit(" ", 2)
        fname = loc.split(":", 1)[0]
        statements, hits = int(numstmt), int(count)
        total += statements
        slot = per_file.setdefault(fname, [0, 0])
        slot[1] += statements
        if hits > 0:
            covered += statements
            slot[0] += statements
    return covered, total, {f: (c, t) for f, (c, t) in per_file.items()}


# --------------------------------------------------------------------------- #
# Zig — LLVM-backend build under kcov
# --------------------------------------------------------------------------- #
def cov_zig() -> Result | None:
    if not _have("zig") or not (ZIG_DIR / "build.zig").exists():
        return None
    kcov = find_kcov()
    if not kcov:
        return Result("zig", "lines", 0, 0,
                      note="no DWARF5-capable kcov found (build SimonKagstrom/kcov master; set $KCOV)")
    print("[coverage] zig: LLVM-backend build + run conformance under kcov …")
    for stale in (ZIG_DIR / ".zig-cache", ZIG_DIR / "zig-out"):
        if stale.exists():
            shutil.rmtree(stale)
    run(["zig", "build", "-Dcoverage=true", "-Doptimize=Debug"], cwd=ZIG_DIR)
    real_bin = ZIG_DIR / "zig-out" / "bin" / "walker"
    kcov_out = COVERAGE_DIR / "zig"
    if kcov_out.exists():
        shutil.rmtree(kcov_out)
    kcov_out.mkdir(parents=True)
    # Wrapper the harness invokes in place of the binary: kcov accumulates all
    # runs into one outdir (verified: sequential runs merge).
    wrapper = COVERAGE_DIR / "walker-kcov.sh"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'exec "{kcov}" --include-path="{ZIG_DIR / "src"}" "{kcov_out}" "{real_bin}" "$@"\n'
    )
    wrapper.chmod(0o755)
    env = dict(os.environ, WALKER_BIN_ZIG=str(wrapper))
    ok, passed, failed = run_conformance("zig", env)
    cov_json = _find_kcov_json(kcov_out)
    if not cov_json:
        return Result("zig", "lines", 0, 0, conformance_ok=ok,
                      conformance_passed=passed, conformance_failed=failed,
                      note="kcov produced no coverage.json")
    data = json.loads(cov_json.read_text())
    covered = int(data["covered_lines"])
    total = int(data["total_lines"])
    files = sorted(
        (Path(f["file"]).name, int(f["covered_lines"]), int(f["total_lines"]))
        for f in data["files"]
    )
    return Result("zig", "lines", covered, total, files, ok, passed, failed)


def _find_kcov_json(outdir: Path) -> Path | None:
    candidates = [p for p in outdir.rglob("coverage.json")]
    # Prefer a merged report with actual files over an empty per-run stub.
    best = None
    for path in candidates:
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        if int(data.get("total_lines", 0)) > 0:
            if best is None or int(data["total_lines"]) >= int(json.loads(best.read_text())["total_lines"]):
                best = path
    return best


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
COLLECTORS = {"rust": cov_rust, "cpp": cov_cpp, "go": cov_go, "zig": cov_zig}


def write_report(results: list[Result]) -> None:
    short, branch = git_describe()
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    measured = [r for r in results if r.total > 0]
    all_100 = bool(measured) and all(r.covered == r.total for r in measured)
    conformance_ok = all(r.conformance_ok for r in results if r.note == "")
    total_tests = sum(r.conformance_passed for r in results)

    lines: list[str] = []
    lines.append(f"claude-walker test report — {ts}")
    lines.append("=" * 60)
    lines.append("")
    lines.append(f"Status:       {'PASS' if all_100 else 'BASELINE'} "
                 f"({'100% all impls' if all_100 else 'Phase 0 baseline — coverage gate not yet met'})")
    lines.append(f"Conformance:  {'PASS' if conformance_ok else 'FAIL'} ({total_tests} checks across measured impls)")
    lines.append(f"Git:          {short} ({branch})")
    lines.append(f"Target:       100% line/statement coverage in all four implementations")
    lines.append("")
    lines.append("Per-implementation coverage")
    lines.append("-" * 60)
    lines.append(f"{'impl':<6} {'metric':<11} {'covered/total':>16} {'cover':>8}   conformance")
    for r in results:
        if r.note and r.total == 0:
            lines.append(f"{r.lang:<6} {'—':<11} {'(skipped)':>16} {'—':>8}   {r.note}")
            continue
        conf = "PASS" if r.conformance_ok else "FAIL"
        lines.append(
            f"{r.lang:<6} {r.metric:<11} {f'{r.covered}/{r.total}':>16} "
            f"{r.percent:>7.2f}%   {conf} ({r.conformance_passed} ok)"
        )
    lines.append("")
    for r in results:
        if r.total == 0:
            continue
        lines.append(f"### {r.lang} — {r.percent:.2f}% ({r.uncovered} {r.metric} uncovered)")
        for name, c, t in r.files:
            pct = 100.0 * c / t if t else 0.0
            flag = "" if c == t else f"   <-- {t - c} uncovered"
            lines.append(f"    {name:<22} {c:>5}/{t:<5} {pct:6.2f}%{flag}")
        lines.append("")
    lines.append("Regenerate: `python shared/coverage.py`  (see CLAUDE.md)")
    lines.append("")
    REPORT.write_text("\n".join(lines))
    print(f"\n[coverage] wrote {REPORT}")


def main() -> int:
    langs = [a for a in sys.argv[1:] if a in ALL_LANGS] or ALL_LANGS
    COVERAGE_DIR.mkdir(exist_ok=True)
    results: list[Result] = []
    for lang in langs:
        try:
            r = COLLECTORS[lang]()
        except subprocess.CalledProcessError as exc:
            sys.stderr.write(f"[coverage] {lang}: command failed: {exc}\n")
            r = Result(lang, "lines", 0, 0, note=f"collection error: {exc}")
        if r is not None:
            results.append(r)
            if r.total:
                print(f"[coverage] {lang}: {r.percent:.2f}% ({r.covered}/{r.total} {r.metric})")

    print("\n" + "=" * 60)
    for r in results:
        if r.total:
            print(f"  {r.lang:<6} {r.percent:6.2f}%  ({r.covered}/{r.total} {r.metric})")
        else:
            print(f"  {r.lang:<6}  skipped — {r.note}")
    print("=" * 60)

    write_report(results)
    measured = [r for r in results if r.total > 0]
    all_100 = bool(measured) and all(r.covered == r.total for r in measured)
    return 0 if all_100 else 1


if __name__ == "__main__":
    sys.exit(main())
