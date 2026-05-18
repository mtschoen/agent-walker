# SPEC — beacons-history pairing fix + drift field removal

## Problem

`beacons-history` returns a `bias_factor` whose value is directionally wrong
and quantitatively far off. Over a 7-day window on a production-like fleet
(58 sessions touched), the walker emits `bias_factor = 3.45` (n = 31 pairs).
Independent per-lifecycle replay over the same window yields **78 pairs**
with median `active_elapsed / begin_eta = 0.54` — agents over-estimate by
roughly 2×, not under-estimate by 3.5×. The status line consumes this
bias as a calibration multiplier on the user-visible ETA, so the wrong
sign yields ETAs that are 5–10× too high (e.g. a 44-minute lifecycle was
displayed as "~310m calibrated" mid-run).

Repro / evidence: `skills-dev/.claude/scripts/simulate-beacons-v2.py` and
`replay-statusbar.py`. Memory: `feedback_beacon_calibration_walker_bug.md`,
`feedback_beacon_drift_uninformative.md`.

## Root cause

`cpp/beacons.cpp` lines 812–822 (mirrored in `rust/`, `go/`, `zig/`, and
`shared/` reference) implement the SPEC.md `beacons-history` algorithm
literally: per session group, pick the **earliest** `kind=begin` beacon
and the **latest** `kind=end` beacon, then emit one pair per group with
`active_elapsed = (latest_end_ts - earliest_begin_ts) - idle_in_window`.

For any session containing N > 1 begin→end lifecycles, this divides the
**whole-session wall-clock span** by the **first lifecycle's eta_seconds**.
For a session with lifecycles at 10:00→10:05, 11:00→11:10, and 14:00→14:15,
the walker reports `(14:15 − 10:00) / 5min = 51×`. Sessions with one
lifecycle pair correctly; sessions with many produce extreme ratios that
dominate the median.

This is a spec bug, faithfully implemented. The SPEC.md text under
`beacons-history` ("find earliest 'begin' and latest 'end' within window")
is the algorithm that needs to change.

## Companion problem — drift field is dead weight

Over the same 7 days, **0 of 291 beacons** carried `drift = "moderate"` or
`drift = "material"`. Meanwhile 22/139 begin-beacons (16%) had actual
ratios ≥ 1.5× and 14/139 (10%) had ratios ≥ 2× — exactly the cases drift
is supposed to flag.

The cause is structural: `drift` is the agent's self-assessment, computed
as `current_eta / original_eta`. Both come from the same agent. An agent
that lowballs at `begin` and keeps lowballing at every `report` will stay
forever in `nominal` even when reality has diverged 10×. The signal is
correlated noise.

The fix is to **drop the field from the protocol** and compute drift
objectively in the status line from `(elapsed_so_far + current_eta) /
original_eta`. The walker doesn't need to expose drift — the statusline
already has begin_ts (from `_find_beacon_anchors`) and current eta_seconds
(from `beacons-latest`); adding begin_eta to the anchor scan completes
the inputs.

## New algorithm (beacons-history)

For each session group:

1. Walk all beacons in the group in **timestamp order**.
2. Iterate, tracking a single in-flight `pending_begin` (a `(ts, beacon)`
   pair or `null`).
   - On `kind = "begin"`: if `pending_begin` is non-null, **orphan it**
     (no pair emitted for this lifecycle) and replace with the new one.
     Otherwise just set `pending_begin`.
   - On `kind = "end"`: if `pending_begin` is non-null and the end's
     `ts > pending_begin.ts`, emit a pair `(begin_eta, ts - pending_begin.ts)`,
     then set `pending_begin = null`. If no pending begin, the end is
     orphaned (no pair emitted).
   - On `kind = "report"`: ignored for pairing.
3. Compute `idle_excluded` per-pair, scoped to `[begin_ts, end_ts]`
   (unchanged from current logic).
4. `active_elapsed = max(0, raw_elapsed - idle_excluded)`.

`bias_factor = median(active_elapsed / begin_eta)` across **all emitted
pairs**, unchanged.

This produces one pair per properly-closed lifecycle, matching how
calibration consumers think about the data. Orphaned begins (no end
beacon, e.g. agent forgot) and orphaned ends (no preceding begin)
contribute nothing — they aren't usable signal anyway.

### Filtering pairs into the window

A pair is included in the window iff `begin_ts >= window_lo`. End
timestamp can fall after `now` (still pending) — but since `end` ts
defines completion, only completed pairs (`end` exists) qualify, and
in practice `end_ts <= now`. The `--win-start` and `--period` filters
apply to `begin_ts`, not to a derived per-pair span.

## Required field change

`drift` is removed from the **required-field set** for beacon parsing.
The parser MUST still accept beacons that carry a `drift` field (for
backward compatibility with existing transcripts), but MUST NOT reject
beacons whose only missing required field is `drift`.

Updated required set: `kind`, `eta_seconds`, `summary`. (Was: same plus
`drift`.)

This is a strict relaxation. Every previously-parseable beacon remains
parseable. Output JSON from `beacons-latest` no longer needs to include
the `drift` field; consumers should treat absence as the new normal.

## Output JSON

Unchanged shape, but:

- `beacons-latest` MAY omit `drift` from the returned `beacon` object
  when the source beacon lacked it. When present, the field is passed
  through unchanged (kept as informational).
- `beacons-history` `pairs` array entries are unchanged: `begin_eta`,
  `actual_elapsed`, `idle_excluded`, `active_elapsed`. `n_pairs` and
  `session_count` semantics unchanged. `bias_factor` semantics
  unchanged (now correctly computed because `pairs` is correctly built).

## Conformance corpus changes

Required additions:

1. **`shared/corpus/beacons/multi_lifecycle/`** — a single session JSONL
   with two complete begin→end lifecycles plus interleaved reports.
   Verifies the pairing iterates correctly:
   - Lifecycle A: begin@T0 (eta=300), report@T0+60, end@T0+200
   - Lifecycle B: begin@T0+1800 (eta=600), report@T0+2100, end@T0+2400
   - Expected: 2 pairs, `[{begin_eta:300, active:200}, {begin_eta:600, active:600}]`
   - Expected `bias_factor = median([200/300, 600/600]) = median([0.667, 1.0]) = 0.833`

2. **`shared/corpus/beacons/orphan_begin/`** — a session with begin but
   no end. Expected: 0 pairs for this fixture.

3. **`shared/corpus/beacons/orphan_end/`** — a session with end but no
   preceding begin. Expected: 0 pairs.

4. **`shared/corpus/beacons/back_to_back/`** — begin, end, begin, end.
   Expected: 2 pairs from one group.

5. **Drop `drift` from `missing_fields`** — the existing fixture removes
   `drift` and expects parse failure. Under the new spec this beacon
   should parse successfully. Replace the fixture's expected outcome
   to reflect the new required-field set, OR re-purpose the fixture
   to test a different missing field (e.g. `eta_seconds`) and add a
   new `optional_drift` fixture that explicitly tests "missing drift
   is OK."

`expected_history.json` at the corpus root gets regenerated by
`generate_beacon_corpus.py` after these additions. `bias_factor` will
change — that's expected and confirms the fix.

## Statusline changes (out of scope for this spec, listed for context)

These ride on the walker fix but live in `schoen-claude-status`:

- `format_beacon` in `statusline_lib.py` computes drift from
  `(elapsed_seconds + current_eta_seconds) / original_eta_seconds`
  rather than reading `beacon["drift"]`. Thresholds unchanged:
  `< 1.5× → green`, `1.5–2× → yellow`, `≥ 2× OR elapsed > 30min → red`.
- `_find_beacon_anchors` extends to also capture the original begin's
  `eta_seconds` so `format_beacon` can compute the ratio.
- Stale `~/.claude/.statusline-bias-cache.json` invalidated on first
  render after deploy (existing 60s TTL handles this automatically).

## SKILL.md changes (out of scope for this spec, listed for context)

These live in `skills-dev/progress-beacon/SKILL.md`:

- Drop the `drift` field from required-fields list.
- Drop the "Drift judgement" section.
- Keep the loud 🚨 inline note system, but trigger it on the same
  objective math the status line uses (the agent computes
  `(elapsed + new_eta) / original_eta ≥ 2` or `elapsed > 30min` and
  flashes the note when crossing from below).

## Backward compatibility

Existing JSONL transcripts carry the `drift` field. The parser MUST
keep accepting them. New transcripts produced after the SKILL change
won't carry `drift`. Both shapes parse cleanly.

Calibration numbers change at deploy time — bias_factor flips from
~3.5× to ~0.5× on existing transcripts because the **pairing** changes,
not the data. This is the intended fix.
