claude-walker test report — 2026-05-28T02:55:44+00:00
============================================================

Status:       BASELINE (Phase 0 baseline — coverage gate not yet met)
Conformance:  PASS (444 checks across measured impls)
Git:          e833846 (coverage-pipeline)
Target:       100% line/statement coverage in all four implementations

Per-implementation coverage
------------------------------------------------------------
impl   metric         covered/total    cover   conformance
rust   lines              1362/1523   89.43%   PASS (111 ok)
cpp    lines              2079/2190   94.93%   PASS (111 ok)
go     statements         1045/1235   84.62%   PASS (111 ok)
zig    lines              1813/2105   86.13%   PASS (111 ok)

### rust — 89.43% (161 lines uncovered)
    beacons.rs               380/401    94.76%   <-- 21 uncovered
    content.rs                49/64     76.56%   <-- 15 uncovered
    events.rs                162/174    93.10%   <-- 12 uncovered
    main.rs                  201/208    96.63%   <-- 7 uncovered
    search.rs                424/512    82.81%   <-- 88 uncovered
    transcript.rs             71/81     87.65%   <-- 10 uncovered
    walker_roots.rs           75/83     90.36%   <-- 8 uncovered

### cpp — 94.93% (111 lines uncovered)
    beacons.cpp              770/791    97.35%   <-- 21 uncovered
    common.hpp                93/96     96.88%   <-- 3 uncovered
    events.cpp               303/304    99.67%   <-- 1 uncovered
    json_writer.hpp           20/23     86.96%   <-- 3 uncovered
    main.cpp                 310/311    99.68%   <-- 1 uncovered
    pricing.hpp               34/34    100.00%
    search.cpp               483/560    86.25%   <-- 77 uncovered
    walker_roots.hpp          66/71     92.96%   <-- 5 uncovered

### go — 84.62% (190 statements uncovered)
    beacons.go               323/367    88.01%   <-- 44 uncovered
    events.go                121/138    87.68%   <-- 17 uncovered
    main.go                  225/249    90.36%   <-- 24 uncovered
    search.go                329/427    77.05%   <-- 98 uncovered
    walker_roots.go           47/54     87.04%   <-- 7 uncovered

### zig — 86.13% (292 lines uncovered)
    beacons.zig              476/493    96.55%   <-- 17 uncovered
    events.zig               163/172    94.77%   <-- 9 uncovered
    main.zig                 522/549    95.08%   <-- 27 uncovered
    search.zig               582/816    71.32%   <-- 234 uncovered
    walker_roots.zig          70/75     93.33%   <-- 5 uncovered

Regenerate: `python shared/coverage.py`  (see CLAUDE.md)
