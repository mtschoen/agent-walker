claude-walker test report — 2026-06-10T11:23:46+00:00
============================================================

Status:       BASELINE (Phase 4 baseline-gated — CI rejects regression vs ci.yml thresholds)
Conformance:  PASS (944 checks across measured impls)
Cumulative:   98.22% line/statement coverage (7744/7884 pooled across 4 impls)
Git:          d905408 (remote-claude/coverage-fix-2026-06-10)
Target:       100% line/statement coverage in all four implementations

Per-implementation coverage
------------------------------------------------------------
impl   metric         covered/total    cover   conformance
rust   lines              1617/1650   98.00%   PASS (236 ok)
cpp    lines              2678/2713   98.71%   PASS (236 ok)
go     statements         1301/1329   97.89%   PASS (236 ok)
zig    lines              2148/2192   97.99%   PASS (236 ok)

### rust — 98.00% (33 lines uncovered)
    beacons.rs               375/379    98.94%   <-- 4 uncovered
    content.rs                66/66    100.00%
    events.rs                165/166    99.40%   <-- 1 uncovered
    main.rs                  204/206    99.03%   <-- 2 uncovered
    search.rs                656/673    97.47%   <-- 17 uncovered
    transcript.rs             78/86     90.70%   <-- 8 uncovered
    walker_roots.rs           73/74     98.65%   <-- 1 uncovered

### cpp — 98.71% (35 lines uncovered)
    beacons.cpp              862/869    99.19%   <-- 7 uncovered
    common.hpp                96/96    100.00%
    discovery.hpp             67/69     97.10%   <-- 2 uncovered
    events.cpp               342/354    96.61%   <-- 12 uncovered
    json_writer.hpp           46/46    100.00%
    main.cpp                 331/333    99.40%   <-- 2 uncovered
    pricing.hpp               34/34    100.00%
    search.cpp               829/841    98.57%   <-- 12 uncovered
    walker_roots.hpp          71/71    100.00%

### go — 97.89% (28 statements uncovered)
    beacons.go               378/389    97.17%   <-- 11 uncovered
    events.go                130/130   100.00%
    main.go                  246/251    98.01%   <-- 5 uncovered
    search.go                495/505    98.02%   <-- 10 uncovered
    walker_roots.go           52/54     96.30%   <-- 2 uncovered

### zig — 97.99% (44 lines uncovered)
    beacons.zig              488/498    97.99%   <-- 10 uncovered
    events.zig               195/198    98.48%   <-- 3 uncovered
    main.zig                 542/551    98.37%   <-- 9 uncovered
    search.zig               849/870    97.59%   <-- 21 uncovered
    walker_roots.zig          74/75     98.67%   <-- 1 uncovered

Regenerate: `python shared/coverage.py`  (see CLAUDE.md)
