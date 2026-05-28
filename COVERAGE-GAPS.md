# claude-walker — Phase 2 coverage gap analysis

> Baseline (git `acbaa4a`, see `TEST-REPORT.md`): rust **81.75%** · cpp
> **89.13%** · go **76.92%** · zig **80.76%**.
> Method: per-language uncovered-line reports (`coverage/missing/*.txt`),
> classified by four parallel read-only agents and synthesized here.
> Regenerate the raw reports after any coverage run; re-derive this doc when
> the gap shape changes materially.

## How to read this

Gaps are keyed by **behavior**, not by line. Fix strategy per bucket:

| Bucket | Meaning | Fix |
| ------ | ------- | --- |
| **SHARED** | same behavior uncovered in all/most impls | one `conformance.py` fixture via a `generate_*.py` script lifts all four at once — **cheapest, do first** |
| **LOCAL** | language-specific error path (e.g. Go `if err != nil`, Zig allocator-failure) | native unit test in that impl |
| **PLATFORM** | OS-gated branch unreachable on the Linux/Windows CI | §5 of COVERAGE-PLAN: restructure for a platform seam, or multi-OS merge |
| **DEAD** | genuinely unreachable | delete (escalation-ladder preferred outcome) |
| **ASSERT** | Zig `unreachable`/`@panic` counted as uncovered by kcov | kcov-ignore annotation or restructure — *not* a test |

> **Caveat on Zig line attributions:** the Zig agent over-read several large
> uncovered ranges as "subcommand entirely untested" — but the conformance
> suite *does* run search/events/beacons fixtures for Zig (its per-file numbers
> are 68–92%, not ~0). The *behaviors* below are reliable; re-verify exact Zig
> line ranges against `coverage/missing/zig.txt` when writing each test.

---

## A. Shared behavior gaps — fixture-driven (highest value)

Rows are behaviors; ✓ = the gap is present in that impl's uncovered set
(blank = already covered or N/A). Nearly everything is uncovered in all four
because the gap is in the **fixture set**, not the code.

| # | Behavior | rust | cpp | go | zig | Fixture to add |
|---|----------|:----:|:---:|:--:|:---:|----------------|
| A1 | **`search --format pretty`** (the *default* format) — header/file/context-before/match-highlight `>>>[..]<<<`/no-match-offset/after-context/summary | ✓ | ✓ | ✓ | ✓ | search fixtures asserting **pretty** output (harness today checks only `jsonl`) |
| A2 | **Result truncation to `--limit`** + `truncated=true` summary + stderr warning | ✓ | ✓ | ✓ | ✓ | `search --limit 1` with ≥2 hits |
| A3 | **`--include-tool-blocks`**: `tool_use.input` (string + nested object/array/bool/null dump) and `tool_result` content (string + text-block-array) | ✓ | ✓ | ✓ | ✓ | search fixture w/ tool_use + tool_result blocks, both content shapes |
| A4 | **Relative + ISO `--since`/`--until`**: `3d`/`2h`/`30m`/`10s`, RFC3339, and malformed → exit 2 | ✓ | ✓ | ✓ | ✓ | search fixtures w/ each time form + a bad one |
| A5 | **Time-window filtering**: message past `--until`, message lacking a timestamp skipped under `--since` | ✓ | ✓ | ✓ | ✓ | search fixture w/ bounded window + a timestamp-less line |
| A6 | **Snippet whitespace + char-boundary nudging** on an interior (mid-paragraph) match, both directions | ✓ | ✓ | ✓ | ✓ | search fixture w/ a match deep inside a long unicode paragraph |
| A7 | **Regex engine** (Zig hand-rolled; others via lib): classes `\d\D\w\W\s\S`, dot, `[a-z]`/negation/ranges, quantifiers `*+?`, `BadEscape`/`UnclosedClass` → exit 2 | ✓ | ✓ | ✓ | ✓ | `search --regex` fixtures per class/quantifier + 2 malformed patterns |
| A8 | **JSONL skip-ladder** (shared by cost/events/beacons/search scanners): blank line, malformed JSON, non-assistant/nil message, empty content, missing timestamp, unparseable timestamp | ✓ | ✓ | ✓ | ✓ | one "dirty transcript" fixture reused across all four modes |
| A9 | **Subagent transcript discovery**: `<slug>/<sid>/subagents/agent-*.jsonl` in beacons-latest, beacons-history, events | ✓ | ✓ | ✓ | ✓ | one subagent-layout fixture per mode |
| A10 | **Beacon optional fields / numeric forms**: integer `eta_seconds` (not `30.0`), `beats_left` present, `eta_seconds`/`beats_left` as float-vs-int | ✓ | ✓ | ✓ | ✓ | beacon fixture w/ int eta + beats_left |
| A11 | **bias_factor pairing edges**: even-count median averaging, all-eta≤0 → null bias, no-pairs output | ✓ | ✓ | ✓ | ✓ | history fixtures: even # of pairs; all etas ≤0 |
| A12 | **Beacon matcher edges**: `<progress-beacon>` open tag not followed by `{`; `{…}` with no closing tag | ✓ | | | ✓ | assistant-text fixture w/ both malformed beacon shapes |
| A13 | **JSON output escaping**: `" \ \b \f \n \r \t` and control `<0x20` → `\uXXXX`, in beacon summary/kind, search snippet, slug/model/session | ✓ | ✓ | ✓ | ✓ | fixture w/ control chars + quotes in searchable text / slug / summary |
| A14 | **ISO-8601 variants**: non-`Z` numeric offset (`+05:30`), fractional seconds, malformed → skip | ✓ | ✓ | ✓ | ✓ | transcript w/ offset timestamp + a malformed one |
| A15 | **Idle-gap exclusion in beacons-history**: real-user vs `type:"user"` tool_result classification, gap clip-to-window accumulation | ✓ | ✓ | ✓ | ✓ | history fixture w/ a real user prompt creating an idle gap |
| A16 | **Same-timestamp sort tiebreaks**: events `model` tiebreak; search `session_id` then `line_number` | ✓ | ✓ | ✓ | ✓ | events: 2 records same ts/session, diff model; search: 2 hits same ts |
| A17 | **Empty / nonexistent `--projects-root`** → empty output, exit 0 | ✓ | ✓ | ✓ | ✓ | any mode pointed at a nonexistent root w/ `--no-config` |
| A18 | **Arg-validation → exit 2** across every subcommand: missing required flag, missing flag value, unparseable numeric, unknown flag, **unknown subcommand**, invalid `--role`/`--format` enum, `--cwd`+`--any-cwd` conflict, empty pattern, duplicate positional | ✓ | ✓ | ✓ | ✓ | a dedicated arg-error fixture/case matrix (exit code is the shared contract; stderr text is LOCAL) |
| A19 | **`--version`** (cost + events) → prints, exit 0 | ✓ | ✓ | ✓ | ✓ | `walker --version`, `walker events --version` |
| A20 | **Duplicate-root dedup**: same root via `--projects-root` and `--extra-projects-root` merges once | ✓ | | ✓ | | pass the same root twice |

**Mechanics:** A1–A7, A16, A20 extend `generate_search_corpus.py` + `conformance.py`'s search asserts. A8/A14 extend the cost/events/beacon corpora. A9–A12, A15 extend `generate_beacon_corpus.py`. A18/A19 are new CLI-exit assertions in `conformance.py`.

---

## B. Harness-shape gaps (verified against conformance.py)

Three gaps come from *how* the harness invokes the binary, not the code:

1. **`walker-roots.json` malformed-variant parsing.** The main runners pass
   `--no-config`, but `conformance.py` **already** has `run_walker_env` +
   `check_config_resolution` (≈L684–730) that omit `--no-config`, pin `HOME` to
   a fake home with a **valid** `walker-roots.json`, and assert the canonical
   HOME-vs-USERPROFILE precedence. So the config happy path *is* covered — only
   the malformed variants (empty body / malformed JSON / non-object / missing
   `extra_roots`) are uncovered, and they just need **more fixtures in that
   existing env-runner style** (no harness change). Treat as a SHARED fixture
   gap, not a blocker.
2. **`--now` is always passed** (cost L105, subcommands L205/250/350, config
   runner L694) → the "current time when `--now` omitted" default
   (`current_unix`/`nowUnix`/…) is never exercised. *Fix:* one smoke invocation
   per mode that omits `--now` and asserts only exit 0 (the value is
   nondeterministic, so don't assert output).
3. **Search asserted only as `--format jsonl`** (CONFIRMED — `conformance.py:351`
   hard-codes `jsonl`). The default **pretty** renderer is therefore uncovered
   in all four (gap A1). *Fix:* a parallel pretty-format search assertion.

---

## C. Language-local gaps (native unit tests)

- **Go** — `if err != nil` branches after `os.Open`/`os.Stat`/`os.ReadDir`/
  `filepath.EvalSymlinks` (beacons 146/217, events 140, search 139/220/233,
  main 377/393/421, walker_roots 120). Filesystem-race-only; cover with unit
  tests using an unreadable path / broken symlink, or accept as documented.
- **Rust** — mostly covered by shared fixtures; residual: `walker_roots.rs`
  home-fallback (HOME+USERPROFILE unset) needs a unit test that clears env.
- **C++** — `home_directory`/`default_projects_root` HOME-unset fallback
  (`common.hpp:55,61`; `walker_roots.hpp:32`) → unit test clearing `HOME`.
  `die()` message *text* is local (exit code is shared, A18).
- **Zig** — allocator-failure cleanup (e.g. `events.zig:214` dedup put-fail) →
  failing-allocator unit test; env-resolution fallbacks (`homeDir`/`defaultRoot`/
  `getEnvVar`) → unit test.

---

## D. Platform branches (COVERAGE-PLAN §5 — needs a decision)

Present in all four impls; unreachable on the current Linux runner:

- **Windows** `%USERPROFILE%`-first home resolution + Windows discovery/env/IO
  (`_dupenv_s`, `discoverWindows`, etc.). Coverable on the **Windows CI runner**
  (multi-OS merge) — but the coverage step doesn't run there yet.
- **macOS Darwin** discovery family (`discoverDarwin`, `scanSlugDir*Darwin`,
  `mtimeOk` Darwin, `getArgsDarwin`). **No macOS CI** (per CLAUDE.md) → local
  macOS coverage merge or documented exclusion.
- **No-home fallback** (HOME *and* USERPROFILE unset → `./.claude/projects`).
  Not OS-gated but unreachable while the runner always has HOME — unit test
  with cleared env (preferred, §5 option 1: inject the home dir).

Recommendation (per §5): introduce a **platform/home/dir-lister/clock seam** so
the Linux run drives the path-building logic with fixture inputs; reserve
multi-OS merge for the thin syscall wrappers and documented exclusion as last
resort with sign-off.

---

## E. Dead code (delete — escalation-ladder preferred)

- **Rust** — `main.rs:254` `_ => unreachable!()` (closed subcommand set);
  `search.rs:187` `_ => unreachable!()` (guarded by preceding `matches!`);
  `events.rs:274/277-279` broken-pipe + serialize-error (serde can't fail on a
  fixed struct).
- **C++** — `main.cpp:500` `return 2; // unreachable` after the subcommand chain.
- **Go** — `numWorkers < 1` clamps (`beacons.go:631`, `events.go:236`,
  `search.go:762`) — `runtime.NumCPU()` is always ≥1; `json.Marshal` error
  branches on fixed-shape structs; `walker_roots.go:71` typed unmarshal after
  the object-probe already passed.

Each deletion is per-impl; verify the twin in the other impls before removing
to keep parity (a `numWorkers<1` clamp likely exists in all four).

---

## F. Zig unreachable-asserts (kcov-ignore or restructure)

kcov-master (unlike the dead liyu1981 fork) does **not** auto-ignore these:
`beacons.zig:202,212` (`parseF64/I64Value` token `else => unreachable`),
`search.zig:197` (multiplier `else => unreachable`), `search.zig:503`
(`prog orelse unreachable`), `main.zig:667` (`parseStringValue` token arm), and
the comptime `if (!is_*) unreachable` asserts (`main.zig:307,355`, surface on
their native OS). *Options:* (a) restructure — replace the peek-guaranteed
`else => unreachable` with returning an error/null the peek proves can't
happen; (b) a kcov exclude-pattern for `unreachable`/`@panic` lines (needs
sign-off, documented in TEST-REPORT). Prefer (a) where it reads cleanly.

---

## Prioritized Phase 3 sequence

1. **Confirm harness assumptions** — read the search + arg asserts in
   `conformance.py`; verify pretty-format is untested and `--no-config`/`--now`
   are always passed (§B). Cheap, de-risks everything below.
2. **Shared search fixtures** (A1–A7, A16, A20) — biggest single lift; search
   is the largest module and lowest-covered everywhere.
3. **Shared scanner/beacon fixtures** (A8–A15, A17–A19) — dirty-transcript,
   subagent-layout, beacon-field, idle-gap, arg-error, `--version`.
4. **Harness-blocker work** (§B) — config fixtures / unit tests + `--now`-omit
   smokes.
5. **Language-local unit tests** (§C) and **dead-code deletion** (§E) — per impl.
6. **Zig asserts** (§F) — restructure preferred.
7. **Platform seam** (§D) — the §5 decision; the long pole.
8. Re-run `python shared/coverage.py`; iterate until 100%; then Phase 4 gate.

After each batch, regenerate fixtures via the matching `generate_*.py`, re-run
`shared/coverage.py`, and watch all four numbers move together — shared fixtures
should lift them in lockstep, surfacing any impl that has drifted.
