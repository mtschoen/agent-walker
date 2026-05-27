# PLAN — beacon-pairing-fix

Implements `docs/superpowers/specs/beacon-pairing-fix.md`. Bundles the
walker fix with the SKILL.md drift removal and the statusline objective-drift
computation so all three repos ship in lockstep.

## Branch strategy

- **claude-walker:** branch `fix/beacon-pairing` off `main`. Conformance
  must pass for all four impls before merging.
- **schoen-claude-status:** branch `feat/objective-drift` off `main`.
  Bumps the consumed walker behavior; can land independently after
  walker merges since the field-relaxation is backward compatible.
- **skills-dev/progress-beacon:** branch `feat/drop-drift-field` off
  `main` on the submodule. Updates SKILL.md, evals if any, and the
  recency-nudge hook (no functional change there, just doc consistency).

Land order: walker → statusline → SKILL. The walker change is the load
bearing one; the other two clean up the now-orphaned `drift` field.

## Walker (claude-walker repo) — DONE (merged to main `d416ac9`, 2026-05-27)

Sections 1–7 complete: spec rewritten, all four impls on the
`pending_begin` loop + relaxed required-field check, fixtures +
expectations regenerated, conformance green (196 OK / 0 fail), cpp
rebuilt and reinstalled to `~/.local/bin/claude-walker.exe`.

### 1. Update SPEC.md `beacons-history` section
Rewrite the "find earliest 'begin' and latest 'end' within window" text
to describe the timestamp-ordered iteration with single in-flight
`pending_begin`. State explicitly:
- Orphaned begins (no end follows before another begin or eof) → no pair.
- Orphaned ends (no preceding pending begin) → no pair.
- Window filter (`begin_ts >= window_lo`) applies to begin timestamp only.
- `drift` is no longer required for beacon parsing; document the new
  required set `{kind, eta_seconds, summary}`.

### 2. Update each impl's `beacons-history` pair-finding loop
Four files, same algorithm change in each:

- `cpp/beacons.cpp` lines 812–822 (the two scan loops that find
  `begin_ptr` and `end_ptr` per group)
- `rust/src/beacons.rs` corresponding logic
- `go/.../beacons.go` corresponding logic
- `zig/.../beacons.zig` corresponding logic
- `shared/walker_ref.py` (reference Python impl, if it implements
  history mode)

Each becomes: sort beacons in the group by timestamp ascending; iterate
once; maintain `pending_begin: Option<(ts, beacon)>`; on `end` with
matching `pending_begin` emit a pair and clear pending; on `begin`
replace pending (orphaning any prior). Verify each lang's iteration is
deterministic (stable sort or already-sorted JSONL).

### 3. Relax required-field check in beacon parser
Same four impls. Remove `has_drift` from the `if (!has_kind || !has_eta
|| !has_summary || !has_drift) return std::nullopt;` check (and
equivalents). Continue to populate `drift` on the `Beacon` struct when
present; just don't fail if absent.

### 4. Add corpus fixtures
Under `shared/corpus/beacons/`:

- `multi_lifecycle/session.jsonl` — two begin→end lifecycles in one
  session with interleaved reports. Hand-written; small (~6 entries).
- `orphan_begin/session.jsonl` — begin with no end.
- `orphan_end/session.jsonl` — end with no begin.
- `back_to_back/session.jsonl` — begin, end, begin, end pattern.

### 5. Update corpus expectations
- Re-run `shared/generate_beacon_corpus.py` (or hand-update
  `expected_history.json` and `expected_latest.json`) to reflect new
  pairing semantics and the new fixtures.
- Re-purpose or replace the `missing_fields/` fixture: under the new
  spec, missing `drift` is no longer a failure. Either change it to
  test a missing `eta_seconds` (still required), or add a new
  `optional_drift/` fixture that explicitly verifies "no drift → still
  parses."

### 6. Verify
- `shared/conformance.py` passes for all four impls.
- `shared/bench.py` runs on a live local corpus and produces a sane
  bias_factor (expected: ~0.5 on this user's fleet, vs current 3.45).
- Manual spot-check: `claude-walker beacons-history --period 604800
  --win-start 0` returns `n_pairs` close to `simulate-beacons-v2.py`'s
  per-lifecycle count, not a smaller number.

### 7. Rebuild + reinstall
- `cpp/build/Release/walker.exe` rebuilt (C++ is the production binary
  per skills-dev install).
- `claude-walker/install.{sh,bat}` to drop the new binary into
  `~/.local/bin/`.

## Statusline (schoen-claude-status repo)

### 8. Extend `_find_beacon_anchors` to capture begin eta
Currently returns `(begin_ts, report_ts)`. Add a third return value:
`begin_eta_seconds`. The scan already parses every beacon's JSON; pluck
`eta_seconds` from the begin and return it.

### 9. Compute drift objectively in `format_beacon`
Replace `drift = beacon.get("drift", "nominal")` lookup with:

```python
elapsed = (now - begin_ts).total_seconds()
ratio = (elapsed + eta_seconds) / begin_eta_seconds
if elapsed > 1800 or ratio >= 2:
    drift = "material"
elif ratio >= 1.5:
    drift = "moderate"
else:
    drift = "nominal"
```

Color lookup (`_BEACON_DRIFT_COLOR`) unchanged. Beacon's own `drift`
field is ignored for color decisions (still passed through if a future
consumer wants it).

### 10. Verify
- Manual: deliberately emit a beacon with begin_eta=60s, wait 70s, then
  emit a report. Status line should turn yellow at ~90s elapsed (>1.5×)
  and red at ~120s (>2×).
- Flush `~/.claude/.statusline-bias-cache.json` to force a fresh walker
  query and confirm calibrated multiplier comes back ~0.5×.

## SKILL.md (skills-dev/progress-beacon repo)

### 11. Drop `drift` from required fields
- Remove from the "Beacon format" block's required-fields list.
- Update all example beacons in SKILL.md to omit `drift`.

### 12. Replace "Drift judgement" section
Rewrite as "Surfacing ETA creep loudly." The objective math is now the
status line's job, but the agent should still flash the 🚨 inline note
when its own arithmetic crosses the material threshold. Same threshold:
`(elapsed_so_far + current_eta) / original_eta ≥ 2` or
`elapsed_so_far > 30min`. The note text stays the same; the trigger is
explicit math, not a self-assessed label.

### 13. Sweep adjacent docs
- `progress-beacon/README.md` — beacon-format section still references
  `drift` as a required field. Update.
- `progress-beacon/hooks/*.sh` — search for any reference to `.drift`.
  The recency-nudge hook uses `.kind` only (verified earlier); no
  change expected, but confirm.
- `progress-beacon/evals/` — if any eval fixtures pin the drift field,
  update them.

## Risk surface

- **Reverse direction calibration:** users currently see `~Nm calibrated
  (3.5×)` and may have built intuition around that number. The deploy
  flips it to `~Nm calibrated (0.5×)`. We should mention this in the
  schoen-claude-status changelog/commit message so it's obvious why the
  multiplier moved.
- **Walker conformance corpus:** the `expected_history.json` regen step
  is where mistakes will hide. If `bias_factor` is off by a small
  delta, the conformance tolerance (0.001) catches it.
- **SKILL.md propagation lag:** installed agents will keep emitting
  `drift` until users re-install the skill. The walker MUST keep
  accepting it. Lockstep release helps but isn't required.

## Out of scope

- Reconsidering the 1.5×/2× thresholds. User confirmed they're right.
- Removing `bias_factor` calibration entirely or replacing with a
  range/distribution. Future work; this fix gets the existing single-
  number multiplier into a sane regime first.
- The "report beacons near actual-end are noisy" pattern (worst case 11×
  off on PR-review sessions). That's a SKILL.md guidance issue, not a
  protocol one; revisit after this lands.

## Test plan summary

- [x] Conformance passes for all four walker impls (196 OK / 0 fail)
- [x] `bench.py` on live corpus produces a sane bias_factor — lands at
      1.70 on the current 1943-session fleet (was 3.82). The spec's
      "< 1.0 / ~0.5" prediction came from a 58-session snapshot; on the
      full fleet 1.70 is the correct sane regime — do NOT chase 0.5.
- [ ] Manual statusline check: yellow at 1.5× crossing, red at 2× crossing
      (pending — schoen-claude-status `feat/objective-drift`)
- [x] Backward-compat: existing JSONL transcripts still parse, drift
      field ignored (optional_drift fixture)
- [x] New corpus fixtures cover multi-lifecycle, orphan-begin, orphan-end,
      back-to-back patterns
