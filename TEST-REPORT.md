claude-walker test report — 2026-05-28T01:09:55+00:00
============================================================

Status:       BASELINE (Phase 0 baseline — coverage gate not yet met)
Conformance:  PASS (208 checks across measured impls)
Git:          04970a8 (main)
Target:       100% line/statement coverage in all four implementations

Per-implementation coverage
------------------------------------------------------------
impl   metric         covered/total    cover   conformance
rust   lines              1245/1523   81.75%   PASS (52 ok)
cpp    lines              1952/2190   89.13%   PASS (52 ok)
go     statements          950/1235   76.92%   PASS (52 ok)
zig    lines              1679/2079   80.76%   PASS (52 ok)

### rust — 81.75% (278 lines uncovered)
    beacons.rs               332/401    82.79%   <-- 69 uncovered
    content.rs                38/64     59.38%   <-- 26 uncovered
    events.rs                147/174    84.48%   <-- 27 uncovered
    main.rs                  183/208    87.98%   <-- 25 uncovered
    search.rs                407/512    79.49%   <-- 105 uncovered
    transcript.rs             71/81     87.65%   <-- 10 uncovered
    walker_roots.rs           67/83     80.72%   <-- 16 uncovered

### cpp — 89.13% (238 lines uncovered)
    beacons.cpp              712/791    90.01%   <-- 79 uncovered
    common.hpp                83/96     86.46%   <-- 13 uncovered
    events.cpp               275/304    90.46%   <-- 29 uncovered
    json_writer.hpp           13/23     56.52%   <-- 10 uncovered
    main.cpp                 302/311    97.11%   <-- 9 uncovered
    pricing.hpp               34/34    100.00%
    search.cpp               471/560    84.11%   <-- 89 uncovered
    walker_roots.hpp          62/71     87.32%   <-- 9 uncovered

### go — 76.92% (285 statements uncovered)
    beacons.go               274/367    74.66%   <-- 93 uncovered
    events.go                107/138    77.54%   <-- 31 uncovered
    main.go                  212/249    85.14%   <-- 37 uncovered
    search.go                315/427    73.77%   <-- 112 uncovered
    walker_roots.go           42/54     77.78%   <-- 12 uncovered

### zig — 80.76% (400 lines uncovered)
    beacons.zig              427/493    86.61%   <-- 66 uncovered
    events.zig               151/172    87.79%   <-- 21 uncovered
    main.zig                 481/523    91.97%   <-- 42 uncovered
    search.zig               555/816    68.01%   <-- 261 uncovered
    walker_roots.zig          65/75     86.67%   <-- 10 uncovered

Regenerate: `python shared/coverage.py`  (see CLAUDE.md)
