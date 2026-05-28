claude-walker test report — 2026-05-28T03:24:39+00:00
============================================================

Status:       BASELINE (Phase 0 baseline — coverage gate not yet met)
Conformance:  PASS (520 checks across measured impls)
Git:          366b13b (coverage-pipeline)
Target:       100% line/statement coverage in all four implementations

Per-implementation coverage
------------------------------------------------------------
impl   metric         covered/total    cover   conformance
rust   lines              1429/1523   93.83%   PASS (130 ok)
cpp    lines              2151/2190   98.22%   PASS (130 ok)
go     statements         1100/1235   89.07%   PASS (130 ok)
zig    lines              1909/2107   90.60%   PASS (130 ok)

### rust — 93.83% (94 lines uncovered)
    beacons.rs               380/401    94.76%   <-- 21 uncovered
    content.rs                59/64     92.19%   <-- 5 uncovered
    events.rs                162/174    93.10%   <-- 12 uncovered
    main.rs                  201/208    96.63%   <-- 7 uncovered
    search.rs                481/512    93.95%   <-- 31 uncovered
    transcript.rs             71/81     87.65%   <-- 10 uncovered
    walker_roots.rs           75/83     90.36%   <-- 8 uncovered

### cpp — 98.22% (39 lines uncovered)
    beacons.cpp              770/791    97.35%   <-- 21 uncovered
    common.hpp                93/96     96.88%   <-- 3 uncovered
    events.cpp               303/304    99.67%   <-- 1 uncovered
    json_writer.hpp           20/23     86.96%   <-- 3 uncovered
    main.cpp                 310/311    99.68%   <-- 1 uncovered
    pricing.hpp               34/34    100.00%
    search.cpp               555/560    99.11%   <-- 5 uncovered
    walker_roots.hpp          66/71     92.96%   <-- 5 uncovered

### go — 89.07% (135 statements uncovered)
    beacons.go               323/367    88.01%   <-- 44 uncovered
    events.go                121/138    87.68%   <-- 17 uncovered
    main.go                  225/249    90.36%   <-- 24 uncovered
    search.go                384/427    89.93%   <-- 43 uncovered
    walker_roots.go           47/54     87.04%   <-- 7 uncovered

### zig — 90.60% (198 lines uncovered)
    beacons.zig              476/493    96.55%   <-- 17 uncovered
    events.zig               163/172    94.77%   <-- 9 uncovered
    main.zig                 524/549    95.45%   <-- 25 uncovered
    search.zig               676/818    82.64%   <-- 142 uncovered
    walker_roots.zig          70/75     93.33%   <-- 5 uncovered

Regenerate: `python shared/coverage.py`  (see CLAUDE.md)
