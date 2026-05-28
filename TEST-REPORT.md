claude-walker test report — 2026-05-28T02:42:54+00:00
============================================================

Status:       BASELINE (Phase 0 baseline — coverage gate not yet met)
Conformance:  PASS (240 checks across measured impls)
Git:          67d1e65 (coverage-phase3-config)
Target:       100% line/statement coverage in all four implementations

Per-implementation coverage
------------------------------------------------------------
impl   metric         covered/total    cover   conformance
rust   lines              1253/1523   82.27%   PASS (60 ok)
cpp    lines              1956/2190   89.32%   PASS (60 ok)
go     statements          955/1235   77.33%   PASS (60 ok)
zig    lines              1683/2079   80.95%   PASS (60 ok)

### rust — 82.27% (270 lines uncovered)
    beacons.rs               332/401    82.79%   <-- 69 uncovered
    content.rs                38/64     59.38%   <-- 26 uncovered
    events.rs                147/174    84.48%   <-- 27 uncovered
    main.rs                  183/208    87.98%   <-- 25 uncovered
    search.rs                407/512    79.49%   <-- 105 uncovered
    transcript.rs             71/81     87.65%   <-- 10 uncovered
    walker_roots.rs           75/83     90.36%   <-- 8 uncovered

### cpp — 89.32% (234 lines uncovered)
    beacons.cpp              712/791    90.01%   <-- 79 uncovered
    common.hpp                83/96     86.46%   <-- 13 uncovered
    events.cpp               275/304    90.46%   <-- 29 uncovered
    json_writer.hpp           13/23     56.52%   <-- 10 uncovered
    main.cpp                 302/311    97.11%   <-- 9 uncovered
    pricing.hpp               34/34    100.00%
    search.cpp               471/560    84.11%   <-- 89 uncovered
    walker_roots.hpp          66/71     92.96%   <-- 5 uncovered

### go — 77.33% (280 statements uncovered)
    beacons.go               274/367    74.66%   <-- 93 uncovered
    events.go                107/138    77.54%   <-- 31 uncovered
    main.go                  212/249    85.14%   <-- 37 uncovered
    search.go                315/427    73.77%   <-- 112 uncovered
    walker_roots.go           47/54     87.04%   <-- 7 uncovered

### zig — 80.95% (396 lines uncovered)
    beacons.zig              427/493    86.61%   <-- 66 uncovered
    events.zig               151/172    87.79%   <-- 21 uncovered
    main.zig                 481/523    91.97%   <-- 42 uncovered
    search.zig               555/816    68.01%   <-- 261 uncovered
    walker_roots.zig          69/75     92.00%   <-- 6 uncovered

Regenerate: `python shared/coverage.py`  (see CLAUDE.md)
