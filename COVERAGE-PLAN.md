# claude-walker — Plan for 100% test coverage

> Status: **proposed** (not yet started). Author: session 2026-05-27.
> Goal: every line of production code in **all four impls** exercised by a
> test, with a checked-in `TEST-REPORT.md` and a CI gate. See
> `~/.claude/skills/maintaining-full-coverage` for the governing discipline.

## 1. Where we are today (honest baseline)

| Fact | Value |
| ---- | ----- |
| Native unit tests | **0** — no `#[test]`, no `*_test.go`, no C++ test TU, no Zig `test` blocks |
| Only test mechanism | `shared/conformance.py` (806 lines) — **black-box** harness that runs the compiled binaries against fixtures and diffs output |
| Coverage tooling | **none** in any language |
| CI coverage gate | **none** (`.gitea/workflows/ci.yml` builds + runs conformance only) |
| `TEST-REPORT.md` | does not exist |
| Production LOC | Rust ~2,260 · C++ ~3,100 (excl. vendored simdjson) · Go ~2,635 · Zig ~4,354 · **~12,350 total** |

**Implication:** this is a greenfield coverage effort across four languages. "100%"
means standing up four independent coverage toolchains and writing the tests to
back them — weeks of work, not an afternoon. The plan is phased so each phase
ships standalone value.

## 2. The three structural challenges

1. **Black-box harness → line coverage.** Our tests drive *compiled binaries*
   as subprocesses. Plain test runners won't see inside them. Each binary must
   be **instrumented**, the harness's invocations routed through the language's
   coverage collector, and the per-run data merged. The good news: the existing
   conformance fixtures already exercise the main happy paths, so Phase 0 gives
   a real baseline % for free.
2. **The 4× multiplication.** The same logical gap (e.g. "malformed-usage line")
   exists in all four ports. Track gaps by *behavior*, not by file — a fixture
   added to `conformance.py` lifts coverage in all four at once, whereas a
   language-specific error path needs four unit tests. Prefer shared fixtures
   where the gap is shared.
3. **Platform-specific discovery branches.** Each impl has Linux / Windows /
   macOS code (`discoverDarwin`, `scanSlugDir*`, Windows `%USERPROFILE%` paths,
   the `mtimeOk` Darwin branch). CI runs only Linux + Windows; macOS is
   local-only (per CLAUDE.md). Those Darwin branches are **unreachable on the
   CI runners** and are the single hardest coverage problem here — addressed
   explicitly in §5.

## 3. Per-language coverage toolchain

All four collect coverage **from the existing conformance harness** by
instrumenting the binary and having the harness run it; native unit tests
(Phase 3) reuse the same instrumentation.

| Lang | Instrument | Collect from harness run | Report | Notes |
| ---- | ---------- | ------------------------ | ------ | ----- |
| **Rust** | `source <(cargo llvm-cov show-env --export-prefix)` then `cargo build` | binaries emit `*.profraw` via `LLVM_PROFILE_FILE` (set by `show-env`) | `cargo llvm-cov report --lcov` / `--summary-only` | wrap external harness per cargo-llvm-cov "external test" workflow; `cargo llvm-cov clean --workspace` first |
| **C++** | compile with `-fprofile-instr-generate -fcoverage-mapping` (Clang) | set `LLVM_PROFILE_FILE=cpp-%p.profraw` (the `%p` gives one file per process — the harness spawns the binary many times) | `llvm-profdata merge -sparse *.profraw -o cpp.profdata` → `llvm-cov report ./walker -instr-profile=cpp.profdata` | **exclude `build/_deps/` (simdjson)** from the denominator via `-ignore-filename-regex` |
| **Go** | `go build -cover -o walker .` | set `GOCOVERDIR=<dir>` before each invocation; each run drops `covcounter`/`covmeta` files | `go tool covdata textfmt -i=<dir> -o go.txt` → `go tool cover -func=go.txt` | Go 1.20+ integration-coverage feature; can also merge with `go test` unit coverage |
| **Zig** | debug build (`zig build`) — kcov uses DWARF, no compiler flag | run binary under `kcov --include-path=zig/src <out> ./zig-out/bin/walker …` | kcov writes HTML + cobertura XML to `<out>` | use the **roc-lang/zig-kcov** fork so `unreachable`/`@panic` auto-ignore; `zig test` blocks also coverable via `-Dtest-coverage` pattern |

Sources: [go.dev/doc/build-cover](https://go.dev/doc/build-cover) ·
[taiki-e/cargo-llvm-cov](https://github.com/taiki-e/cargo-llvm-cov) ·
[Clang Source-based Coverage](https://clang.llvm.org/docs/SourceBasedCodeCoverage.html) ·
[zig-kcov](https://github.com/roc-lang/zig-kcov) · [Ziggit kcov thread](https://ziggit.dev/t/using-kcov-with-zig-test/3421)

## 4. Phased roadmap

### Phase 0 — Baseline (no new tests)
Stand up the four toolchains above, instrument each binary, run the **existing**
`conformance.py` against the instrumented binaries, and record the starting
coverage % per language. Deliverable: first `TEST-REPORT.md` with four numbers
(expected: high-but-not-100, with the gaps concentrated in error paths, CLI
edge cases, and platform branches). This proves the collection pipeline before
any test-writing.

### Phase 1 — Coverage orchestrator
Add `shared/coverage.py` (sibling to `conformance.py`) that:
- builds each impl in its instrumented mode,
- runs the conformance fixtures with the right env (`GOCOVERDIR`,
  `LLVM_PROFILE_FILE`, kcov wrapper),
- merges per-language data and prints a unified summary,
- writes/updates `TEST-REPORT.md` (format per the coverage skill).
Add a `--coverage` flag to `conformance.py` or a thin wrapper so one command
does the whole thing. Document it in `CLAUDE.md`.

### Phase 2 — Gap analysis
For each language, read the uncovered-line report and classify every gap:
- **(a) reachable, untested** → Phase 3 test (most gaps),
- **(b) dead code** → delete it (the escalation ladder's preferred outcome),
- **(c) genuinely untestable platform glue** → candidate exclusion, but only
  after §5's restructure attempt and explicit human sign-off.
Produce a behavior-keyed gap matrix (rows = behaviors, columns = 4 langs) so
shared gaps get fixed once via fixtures.

### Phase 3 — Fill the gaps
- **Shared behavior gaps** → new `conformance.py` fixtures via the
  `generate_*.py` scripts (lifts all four at once). Likely: more malformed-JSON
  shapes, empty/again-empty files, unknown-subcommand routing, `--help`/bad-flag
  exits, time-window boundaries, web-search pricing edges.
- **Language-local gaps** → native unit tests:
  - Rust: `#[cfg(test)]` modules + `rust/tests/` integration tests,
  - Go: `*_test.go` next to each file,
  - C++: a lightweight assert-based test TU per module (or pull in `doctest` —
    single header, no build friction),
  - Zig: `test "…" { … }` blocks compiled by `zig build test`.
  Target: JSON parse error branches, I/O failures (unreadable file, missing
  dir), arithmetic/aggregation edge cases, beacon pairing orphan/back-to-back
  cases, ISO-8601 parse failures.

### Phase 4 — Gate it
- Commit `TEST-REPORT.md` at repo root (git hash above the numbers).
- Extend `.gitea/workflows/ci.yml`: after the existing "verify binaries built"
  step, run `shared/coverage.py` and **fail the job below 100%** (or below the
  documented baseline from §5).
- `CLAUDE.md`: document the coverage command + point at `TEST-REPORT.md`.

## 5. The hard part — platform branches (decide before Phase 3)

The Linux/Windows/macOS discovery code is the main place where naive coverage
stalls below 100%. Options, **in escalation order**:

1. **Restructure for testability (preferred).** Introduce a platform/filesystem
   seam (inject the home dir, the dir-lister, the clock) so the Linux test run
   can drive the Windows/macOS *logic* with fixture inputs. This follows the
   skill's "restructure over exclude" rule and removes the branch problem at the
   source. Cost: a refactor in each impl's discovery layer.
2. **Multi-OS coverage merge.** Collect coverage on Linux + Windows (CI) **and**
   macOS (local, per the CLAUDE.md macOS ritual) and merge. Closes the Darwin
   gap honestly but needs a macOS collection step + cross-OS lcov merge.
3. **Documented exclusion (last resort, human approval).** If a Darwin syscall
   wrapper genuinely cannot be driven from another OS, exclude *that specific
   binding* (never a whole module) and record it as a baseline exception in
   `TEST-REPORT.md`.

Recommendation: option 1 for the path-building/slug-resolution logic (it's pure
and seam-able), option 2 for the thin syscall wrappers (`opendir`/`fstatat`
families), reserving option 3 for anything that resists both.

## 6. Always exclude from the denominator
- C++: `build/_deps/**` (vendored simdjson) — already excluded from clang-tidy.
- Any future vendored/generated code.
Never exclude our own production logic.

## 7. Effort estimate (rough)
- Phase 0–1 (pipeline + baseline): ~1–2 days.
- Phase 2 (gap analysis): ~0.5 day.
- Phase 3 (tests to 100%): the bulk — **~1–2 weeks** across four languages,
  dominated by the platform-seam refactor (§5) and C++/Zig unit-test scaffolding.
- Phase 4 (gate): ~0.5 day.

## 8. First concrete step
Implement Phase 0 for **one** language end-to-end (suggest Rust — `cargo
llvm-cov` is the lowest-friction) to validate the harness-driven collection
pattern, then fan the same pattern out to C++/Go/Zig.
