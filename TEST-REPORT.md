claude-walker test report — 2026-05-28T03:41:41+00:00
============================================================

Status:       BASELINE (Phase 0 baseline — coverage gate not yet met)
Conformance:  PASS (592 checks across measured impls)
Git:          c14ee31 (coverage-phase3-r3-regex)
Target:       100% line/statement coverage in all four implementations

Per-implementation coverage
------------------------------------------------------------
impl   metric         covered/total    cover   conformance
rust   lines              1431/1523   93.96%   PASS (148 ok)
cpp    lines              2154/2190   98.36%   PASS (148 ok)
go     statements         1104/1235   89.39%   PASS (148 ok)
zig    lines              1972/2107   93.59%   PASS (148 ok)

### rust — 93.96% (92 lines uncovered)
    beacons.rs               380/401    94.76%   <-- 21 uncovered
    content.rs                59/64     92.19%   <-- 5 uncovered
    events.rs                162/174    93.10%   <-- 12 uncovered
    main.rs                  201/208    96.63%   <-- 7 uncovered
    search.rs                483/512    94.34%   <-- 29 uncovered
    transcript.rs             71/81     87.65%   <-- 10 uncovered
    walker_roots.rs           75/83     90.36%   <-- 8 uncovered

### cpp — 98.36% (36 lines uncovered)
    beacons.cpp              770/791    97.35%   <-- 21 uncovered
    common.hpp                93/96     96.88%   <-- 3 uncovered
    events.cpp               303/304    99.67%   <-- 1 uncovered
    json_writer.hpp           20/23     86.96%   <-- 3 uncovered
    main.cpp                 310/311    99.68%   <-- 1 uncovered
    pricing.hpp               34/34    100.00%
    search.cpp               558/560    99.64%   <-- 2 uncovered
    walker_roots.hpp          66/71     92.96%   <-- 5 uncovered

### go — 89.39% (131 statements uncovered)
    beacons.go               323/367    88.01%   <-- 44 uncovered
    events.go                121/138    87.68%   <-- 17 uncovered
    main.go                  225/249    90.36%   <-- 24 uncovered
    search.go                387/427    90.63%   <-- 40 uncovered
    walker_roots.go           48/54     88.89%   <-- 6 uncovered

### zig — 93.59% (135 lines uncovered)
    beacons.zig              476/493    96.55%   <-- 17 uncovered
    events.zig               163/172    94.77%   <-- 9 uncovered
    main.zig                 524/549    95.45%   <-- 25 uncovered
    search.zig               736/818    89.98%   <-- 82 uncovered
    walker_roots.zig          73/75     97.33%   <-- 2 uncovered

Regenerate: `python shared/coverage.py`  (see CLAUDE.md)
