claude-walker test report — 2026-05-28T02:50:46+00:00
============================================================

Status:       BASELINE (Phase 0 baseline — coverage gate not yet met)
Conformance:  PASS (292 checks across measured impls)
Git:          67d1e65 (coverage-phase3-scanner)
Target:       100% line/statement coverage in all four implementations

Per-implementation coverage
------------------------------------------------------------
impl   metric         covered/total    cover   conformance
rust   lines              1302/1523   85.49%   PASS (73 ok)
cpp    lines              2032/2190   92.79%   PASS (73 ok)
go     statements          995/1235   80.57%   PASS (73 ok)
zig    lines              1771/2105   84.13%   PASS (73 ok)

### rust — 85.49% (221 lines uncovered)
    beacons.rs               371/401    92.52%   <-- 30 uncovered
    content.rs                49/64     76.56%   <-- 15 uncovered
    events.rs                153/174    87.93%   <-- 21 uncovered
    main.rs                  184/208    88.46%   <-- 24 uncovered
    search.rs                407/512    79.49%   <-- 105 uncovered
    transcript.rs             71/81     87.65%   <-- 10 uncovered
    walker_roots.rs           67/83     80.72%   <-- 16 uncovered

### cpp — 92.79% (158 lines uncovered)
    beacons.cpp              758/791    95.83%   <-- 33 uncovered
    common.hpp                93/96     96.88%   <-- 3 uncovered
    events.cpp               292/304    96.05%   <-- 12 uncovered
    json_writer.hpp           20/23     86.96%   <-- 3 uncovered
    main.cpp                 302/311    97.11%   <-- 9 uncovered
    pricing.hpp               34/34    100.00%
    search.cpp               471/560    84.11%   <-- 89 uncovered
    walker_roots.hpp          62/71     87.32%   <-- 9 uncovered

### go — 80.57% (240 statements uncovered)
    beacons.go               312/367    85.01%   <-- 55 uncovered
    events.go                113/138    81.88%   <-- 25 uncovered
    main.go                  213/249    85.54%   <-- 36 uncovered
    search.go                315/427    73.77%   <-- 112 uncovered
    walker_roots.go           42/54     77.78%   <-- 12 uncovered

### zig — 84.13% (334 lines uncovered)
    beacons.zig              474/493    96.15%   <-- 19 uncovered
    events.zig               159/172    92.44%   <-- 13 uncovered
    main.zig                 509/549    92.71%   <-- 40 uncovered
    search.zig               563/816    69.00%   <-- 253 uncovered
    walker_roots.zig          66/75     88.00%   <-- 9 uncovered

Regenerate: `python shared/coverage.py`  (see CLAUDE.md)
