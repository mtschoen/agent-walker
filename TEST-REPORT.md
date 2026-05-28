claude-walker test report — 2026-05-28T02:42:39+00:00
============================================================

Status:       BASELINE (Phase 0 baseline — coverage gate not yet met)
Conformance:  PASS (328 checks across measured impls)
Git:          67d1e65 (coverage-phase3-cli)
Target:       100% line/statement coverage in all four implementations

Per-implementation coverage
------------------------------------------------------------
impl   metric         covered/total    cover   conformance
rust   lines              1297/1523   85.16%   PASS (82 ok)
cpp    lines              1995/2190   91.10%   PASS (82 ok)
go     statements          995/1235   80.57%   PASS (82 ok)
zig    lines              1727/2079   83.07%   PASS (82 ok)

### rust — 85.16% (226 lines uncovered)
    beacons.rs               341/401    85.04%   <-- 60 uncovered
    content.rs                38/64     59.38%   <-- 26 uncovered
    events.rs                156/174    89.66%   <-- 18 uncovered
    main.rs                  200/208    96.15%   <-- 8 uncovered
    search.rs                424/512    82.81%   <-- 88 uncovered
    transcript.rs             71/81     87.65%   <-- 10 uncovered
    walker_roots.rs           67/83     80.72%   <-- 16 uncovered

### cpp — 91.10% (195 lines uncovered)
    beacons.cpp              724/791    91.53%   <-- 67 uncovered
    common.hpp                83/96     86.46%   <-- 13 uncovered
    events.cpp               286/304    94.08%   <-- 18 uncovered
    json_writer.hpp           13/23     56.52%   <-- 10 uncovered
    main.cpp                 310/311    99.68%   <-- 1 uncovered
    pricing.hpp               34/34    100.00%
    search.cpp               483/560    86.25%   <-- 77 uncovered
    walker_roots.hpp          62/71     87.32%   <-- 9 uncovered

### go — 80.57% (240 statements uncovered)
    beacons.go               285/367    77.66%   <-- 82 uncovered
    events.go                115/138    83.33%   <-- 23 uncovered
    main.go                  224/249    89.96%   <-- 25 uncovered
    search.go                329/427    77.05%   <-- 98 uncovered
    walker_roots.go           42/54     77.78%   <-- 12 uncovered

### zig — 83.07% (352 lines uncovered)
    beacons.zig              429/493    87.02%   <-- 64 uncovered
    events.zig               155/172    90.12%   <-- 17 uncovered
    main.zig                 496/523    94.84%   <-- 27 uncovered
    search.zig               582/816    71.32%   <-- 234 uncovered
    walker_roots.zig          65/75     86.67%   <-- 10 uncovered

Regenerate: `python shared/coverage.py`  (see CLAUDE.md)
