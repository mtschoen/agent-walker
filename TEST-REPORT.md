claude-walker test report — 2026-05-28T04:33:45+00:00
============================================================

Status:       BASELINE (Phase 4 baseline-gated — CI rejects regression vs ci.yml thresholds)
Conformance:  PASS (596 checks across measured impls)
Git:          e1f9af3 (coverage-pipeline)
Target:       100% line/statement coverage in all four implementations

Per-implementation coverage
------------------------------------------------------------
impl   metric         covered/total    cover   conformance
rust   lines              1385/1421   97.47%   PASS (149 ok)
cpp    lines              2154/2189   98.40%   PASS (149 ok)
go     statements         1184/1224   96.73%   PASS (149 ok)
zig    lines              1971/2105   93.63%   PASS (149 ok)

### rust — 97.47% (36 lines uncovered)
    beacons.rs               362/373    97.05%   <-- 11 uncovered
    content.rs                53/55     96.36%   <-- 2 uncovered
    events.rs                161/163    98.77%   <-- 2 uncovered
    main.rs                  192/195    98.46%   <-- 3 uncovered
    search.rs                476/485    98.14%   <-- 9 uncovered
    transcript.rs             64/72     88.89%   <-- 8 uncovered
    walker_roots.rs           77/78     98.72%   <-- 1 uncovered

### cpp — 98.40% (35 lines uncovered)
    beacons.cpp              770/791    97.35%   <-- 21 uncovered
    common.hpp                93/96     96.88%   <-- 3 uncovered
    events.cpp               303/304    99.67%   <-- 1 uncovered
    json_writer.hpp           20/23     86.96%   <-- 3 uncovered
    main.cpp                 310/310   100.00%
    pricing.hpp               34/34    100.00%
    search.cpp               558/560    99.64%   <-- 2 uncovered
    walker_roots.hpp          66/71     92.96%   <-- 5 uncovered

### go — 96.73% (40 statements uncovered)
    beacons.go               350/365    95.89%   <-- 15 uncovered
    events.go                131/131   100.00%
    main.go                  235/249    94.38%   <-- 14 uncovered
    search.go                416/425    97.88%   <-- 9 uncovered
    walker_roots.go           52/54     96.30%   <-- 2 uncovered

### zig — 93.63% (134 lines uncovered)
    beacons.zig              476/493    96.55%   <-- 17 uncovered
    events.zig               163/172    94.77%   <-- 9 uncovered
    main.zig                 524/548    95.62%   <-- 24 uncovered
    search.zig               735/817    89.96%   <-- 82 uncovered
    walker_roots.zig          73/75     97.33%   <-- 2 uncovered

Regenerate: `python shared/coverage.py`  (see CLAUDE.md)
