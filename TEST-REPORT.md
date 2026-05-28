claude-walker test report — 2026-05-28T03:57:34+00:00
============================================================

Status:       BASELINE (Phase 0 baseline — coverage gate not yet met)
Conformance:  PASS (592 checks across measured impls)
Git:          291e3bb (coverage-pipeline)
Target:       100% line/statement coverage in all four implementations

Per-implementation coverage
------------------------------------------------------------
impl   metric         covered/total    cover   conformance
rust   lines              1381/1418   97.39%   PASS (148 ok)
cpp    lines              2154/2189   98.40%   PASS (148 ok)
go     statements         1182/1224   96.57%   PASS (148 ok)
zig    lines              1972/2107   93.59%   PASS (148 ok)

### rust — 97.39% (37 lines uncovered)
    beacons.rs               362/373    97.05%   <-- 11 uncovered
    content.rs                53/55     96.36%   <-- 2 uncovered
    events.rs                157/160    98.12%   <-- 3 uncovered
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

### go — 96.57% (42 statements uncovered)
    beacons.go               350/365    95.89%   <-- 15 uncovered
    events.go                131/131   100.00%
    main.go                  235/249    94.38%   <-- 14 uncovered
    search.go                416/425    97.88%   <-- 9 uncovered
    walker_roots.go           50/54     92.59%   <-- 4 uncovered

### zig — 93.59% (135 lines uncovered)
    beacons.zig              476/493    96.55%   <-- 17 uncovered
    events.zig               163/172    94.77%   <-- 9 uncovered
    main.zig                 524/549    95.45%   <-- 25 uncovered
    search.zig               736/818    89.98%   <-- 82 uncovered
    walker_roots.zig          73/75     97.33%   <-- 2 uncovered

Regenerate: `python shared/coverage.py`  (see CLAUDE.md)
