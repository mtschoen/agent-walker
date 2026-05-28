claude-walker test report — 2026-05-28T03:19:26+00:00
============================================================

Status:       BASELINE (Phase 0 baseline — coverage gate not yet met)
Conformance:  PASS (500 checks across measured impls)
Git:          9528483 (coverage-phase3-search-content)
Target:       100% line/statement coverage in all four implementations

Per-implementation coverage
------------------------------------------------------------
impl   metric         covered/total    cover   conformance
rust   lines              1392/1523   91.40%   PASS (125 ok)
cpp    lines              2110/2190   96.35%   PASS (125 ok)
go     statements         1072/1235   86.80%   PASS (125 ok)
zig    lines              1880/2105   89.31%   PASS (125 ok)

### rust — 91.40% (131 lines uncovered)
    beacons.rs               380/401    94.76%   <-- 21 uncovered
    content.rs                59/64     92.19%   <-- 5 uncovered
    events.rs                162/174    93.10%   <-- 12 uncovered
    main.rs                  201/208    96.63%   <-- 7 uncovered
    search.rs                444/512    86.72%   <-- 68 uncovered
    transcript.rs             71/81     87.65%   <-- 10 uncovered
    walker_roots.rs           75/83     90.36%   <-- 8 uncovered

### cpp — 96.35% (80 lines uncovered)
    beacons.cpp              770/791    97.35%   <-- 21 uncovered
    common.hpp                93/96     96.88%   <-- 3 uncovered
    events.cpp               303/304    99.67%   <-- 1 uncovered
    json_writer.hpp           20/23     86.96%   <-- 3 uncovered
    main.cpp                 310/311    99.68%   <-- 1 uncovered
    pricing.hpp               34/34    100.00%
    search.cpp               514/560    91.79%   <-- 46 uncovered
    walker_roots.hpp          66/71     92.96%   <-- 5 uncovered

### go — 86.80% (163 statements uncovered)
    beacons.go               323/367    88.01%   <-- 44 uncovered
    events.go                121/138    87.68%   <-- 17 uncovered
    main.go                  225/249    90.36%   <-- 24 uncovered
    search.go                356/427    83.37%   <-- 71 uncovered
    walker_roots.go           47/54     87.04%   <-- 7 uncovered

### zig — 89.31% (225 lines uncovered)
    beacons.zig              476/493    96.55%   <-- 17 uncovered
    events.zig               163/172    94.77%   <-- 9 uncovered
    main.zig                 524/549    95.45%   <-- 25 uncovered
    search.zig               647/816    79.29%   <-- 169 uncovered
    walker_roots.zig          70/75     93.33%   <-- 5 uncovered

Regenerate: `python shared/coverage.py`  (see CLAUDE.md)
