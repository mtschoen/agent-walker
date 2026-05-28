claude-walker test report — 2026-05-28T03:09:32+00:00
============================================================

Status:       BASELINE (Phase 0 baseline — coverage gate not yet met)
Conformance:  PASS (460 checks across measured impls)
Git:          9528483 (coverage-phase3-search-output)
Target:       100% line/statement coverage in all four implementations

Per-implementation coverage
------------------------------------------------------------
impl   metric         covered/total    cover   conformance
rust   lines              1399/1523   91.86%   PASS (115 ok)
cpp    lines              2120/2190   96.80%   PASS (115 ok)
go     statements         1073/1235   86.88%   PASS (115 ok)
zig    lines              1840/2105   87.41%   PASS (115 ok)

### rust — 91.86% (124 lines uncovered)
    beacons.rs               380/401    94.76%   <-- 21 uncovered
    content.rs                49/64     76.56%   <-- 15 uncovered
    events.rs                162/174    93.10%   <-- 12 uncovered
    main.rs                  201/208    96.63%   <-- 7 uncovered
    search.rs                461/512    90.04%   <-- 51 uncovered
    transcript.rs             71/81     87.65%   <-- 10 uncovered
    walker_roots.rs           75/83     90.36%   <-- 8 uncovered

### cpp — 96.80% (70 lines uncovered)
    beacons.cpp              770/791    97.35%   <-- 21 uncovered
    common.hpp                93/96     96.88%   <-- 3 uncovered
    events.cpp               303/304    99.67%   <-- 1 uncovered
    json_writer.hpp           20/23     86.96%   <-- 3 uncovered
    main.cpp                 310/311    99.68%   <-- 1 uncovered
    pricing.hpp               34/34    100.00%
    search.cpp               524/560    93.57%   <-- 36 uncovered
    walker_roots.hpp          66/71     92.96%   <-- 5 uncovered

### go — 86.88% (162 statements uncovered)
    beacons.go               323/367    88.01%   <-- 44 uncovered
    events.go                121/138    87.68%   <-- 17 uncovered
    main.go                  225/249    90.36%   <-- 24 uncovered
    search.go                357/427    83.61%   <-- 70 uncovered
    walker_roots.go           47/54     87.04%   <-- 7 uncovered

### zig — 87.41% (265 lines uncovered)
    beacons.zig              476/493    96.55%   <-- 17 uncovered
    events.zig               163/172    94.77%   <-- 9 uncovered
    main.zig                 522/549    95.08%   <-- 27 uncovered
    search.zig               609/816    74.63%   <-- 207 uncovered
    walker_roots.zig          70/75     93.33%   <-- 5 uncovered

Regenerate: `python shared/coverage.py`  (see CLAUDE.md)
