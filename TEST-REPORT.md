claude-walker test report — 2026-05-28T03:50:00+00:00
============================================================

Status:       BASELINE (Phase 0 baseline — coverage gate not yet met)
Conformance:  PASS (520 checks across measured impls)
Git:          c14ee31 (coverage-phase3-r3-unittests)
Target:       100% line/statement coverage in all four implementations

Per-implementation coverage
------------------------------------------------------------
impl   metric         covered/total    cover   conformance
rust   lines              1346/1388   96.97%   PASS (130 ok)
cpp    lines              2151/2190   98.22%   PASS (130 ok)
go     statements         1184/1235   95.87%   PASS (130 ok)
zig    lines              1909/2107   90.60%   PASS (130 ok)

### rust — 96.97% (42 lines uncovered)
    beacons.rs               362/373    97.05%   <-- 11 uncovered
    content.rs                53/55     96.36%   <-- 2 uncovered
    events.rs                158/164    96.34%   <-- 6 uncovered
    main.rs                  192/196    97.96%   <-- 4 uncovered
    search.rs                475/485    97.94%   <-- 10 uncovered
    transcript.rs             64/72     88.89%   <-- 8 uncovered
    walker_roots.rs           42/43     97.67%   <-- 1 uncovered

### cpp — 98.22% (39 lines uncovered)
    beacons.cpp              770/791    97.35%   <-- 21 uncovered
    common.hpp                93/96     96.88%   <-- 3 uncovered
    events.cpp               303/304    99.67%   <-- 1 uncovered
    json_writer.hpp           20/23     86.96%   <-- 3 uncovered
    main.cpp                 310/311    99.68%   <-- 1 uncovered
    pricing.hpp               34/34    100.00%
    search.cpp               555/560    99.11%   <-- 5 uncovered
    walker_roots.hpp          66/71     92.96%   <-- 5 uncovered

### go — 95.87% (51 statements uncovered)
    beacons.go               351/367    95.64%   <-- 16 uncovered
    events.go                134/138    97.10%   <-- 4 uncovered
    main.go                  235/249    94.38%   <-- 14 uncovered
    search.go                414/427    96.96%   <-- 13 uncovered
    walker_roots.go           50/54     92.59%   <-- 4 uncovered

### zig — 90.60% (198 lines uncovered)
    beacons.zig              476/493    96.55%   <-- 17 uncovered
    events.zig               163/172    94.77%   <-- 9 uncovered
    main.zig                 524/549    95.45%   <-- 25 uncovered
    search.zig               676/818    82.64%   <-- 142 uncovered
    walker_roots.zig          70/75     93.33%   <-- 5 uncovered

Regenerate: `python shared/coverage.py`  (see CLAUDE.md)
