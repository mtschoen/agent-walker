# agent-walker - Test Report

`2026-10-11` (Linux)

| Field | Value |
| --- | --- |
| Status | PARTIAL: required behavioral checks pass; full multi-language coverage unmeasured |
| Mode | best-effort |
| Git | `e9db042` plus Codex empty-block and conformance-environment fixes |
| Conformance | 1,168 passed, 0 failed, 0 skipped; 292 per implementation |
| Codex environment matrix | 36 passed with CODEX_HOME unset; 36 passed with CODEX_HOME pointing at an empty worktree directory |
| MCP | NOT RUN: mcp/ unchanged |
| Rust unit tests | 128 passed, 0 failed, 0 ignored |
| C++ unit tests | 59 assertions passed, 0 failed |
| Go unit tests | 107 top-level tests passed, 0 failed; 6 subtests passed |
| Coverage | Go native/conformance block union: 1,959/1,959 statements (100%); 0 uncovered |
| Other coverage | Rust, C++, Zig: NOT RUN; full Linux coverage gate unverified |
| Exclusions | No exclusions or thresholds changed |

## Lint

| Tool | Result |
| --- | --- |
| `aislop scan .` | Initial scan: 137 warnings including 1 fixable formatting warning; formatting corrected with ruff |
| `aislop ci .` | PASS: score 57, configured floor 20; 0 errors, 136 warnings, 527 informational findings; 0 fixable findings |
| `cargo clippy --all-targets` | PASS: 0 findings |
| `go vet ./...` | PASS: 0 findings |
| `staticcheck ./...` | FAIL: 4 import errors; installed analyzer cannot decode Go 1.27 export data |
| `zig fmt --check src/` | PASS |
| `clang-tidy` | Exit 0; 9 emitted warnings: 7 project findings outside changed parser logic, 2 vendored simdjson findings |
| golangci-lint via aislop | SKIPPED: analyzer built with Go 1.26 cannot analyze targeted Go 1.27.1 |
| `aislop scan --staged` | PASS: score 70; 0 errors, 71 warnings, 0 fixable findings; formatting/security: 0 issues |

## Commands

```bash
# rust/
cargo build --release
cargo test
cargo clippy --all-targets

# repository root
python shared/generate_search_corpus.py
cmake -S cpp -B cpp/build -DCMAKE_BUILD_TYPE=Release -DWALKER_BUILD_TESTS=ON -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
cmake --build cpp/build --config Release -j 4
TMPDIR="$PWD/.claude" cpp/build/walker_unit_tests
clang-tidy -p cpp/build --header-filter='\.hpp$' '-checks=-*,bugprone-*,performance-*,clang-analyzer-*' cpp/main.cpp cpp/beacons.cpp cpp/events.cpp cpp/search.cpp
ruff format shared/generate_search_corpus.py
aislop scan .
aislop ci .
aislop scan --staged

# go/
go build -o walker .
go test -count=1 -v ./...
go vet ./...
staticcheck ./...
go build -cover -o walker-cov .
TMPDIR="$PWD/../.claude" go test -count=1 -coverprofile=../.claude/lane/go-native-coverage.txt ./...
go tool covdata textfmt -i=../.claude/go-coverage -o=../.claude/lane/go-conformance-coverage.txt

# zig/
zig build
zig fmt --check src/

# repository root
TMPDIR="$PWD/.claude" CODEX_HOME="$PWD/.claude/empty-codex-home" GOCOVERDIR="$PWD/.claude/go-coverage" WALKER_BIN_GO="$PWD/go/walker-cov" python shared/conformance.py rust cpp go zig
env -u CODEX_HOME TMPDIR="$PWD/.claude" PYTHONPATH=shared python -c "import conformance; results = [conformance.check_search_codex(language, conformance.find_binary(language)) for language in ['rust', 'cpp', 'go', 'zig']]; raise SystemExit(not all(results))"
CODEX_HOME="$PWD/.claude/empty-codex-home" TMPDIR="$PWD/.claude" PYTHONPATH=shared python -c "import conformance; results = [conformance.check_search_codex(language, conformance.find_binary(language)) for language in ['rust', 'cpp', 'go', 'zig']]; raise SystemExit(not all(results))"
jq -R -s '[split("\n")[] | select(length > 0 and (startswith("mode:") | not)) | split(" ") | {position: .[0], statements: (.[1] | tonumber), count: (.[2] | tonumber)}] | group_by(.position) | map(.[0] + {count: (map(.count) | max)}) | {total: (map(.statements) | add), covered: (map(select(.count > 0) | .statements) | add), uncovered: map(select(.count == 0))}' .claude/lane/go-native-coverage.txt .claude/lane/go-conformance-coverage.txt
```
