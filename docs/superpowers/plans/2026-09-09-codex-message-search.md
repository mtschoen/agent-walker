# Current Codex rollout search

Goal: Search current Codex user/assistant messages reliably while preserving legacy search and keeping analytics isolated.

Architecture: Extend search-only parsing to canonical response_item records whose payload.type is message. Extract user input_text and assistant output_text blocks, preserving context order and source metadata. Keep legacy event_msg user_message/agent_message support. Ignore system/developer roles and item_completed mirrors. Cost, events and beacons remain unchanged.

Tech stack: Existing Rust reference and C++/Go/Zig conformance implementations, generated shared search fixtures.

## Work

- [x] Update SPEC and generated Codex search fixtures for current messages, mixed roles/blocks, legacy events and duplicate item_completed mirrors. Verify new fixtures fail before parser changes.
- [x] Implement and verify Rust reference, then matching C++/Go/Zig search-only parsing. Use existing parsers and helpers. Do not change token accounting or provider discovery in this bugfix.
- [ ] Build available implementations and run full relevant conformance plus targeted tests/lint/coverage. Report any unavailable platform validation precisely.
- [x] Independent review, fix important findings, and smoke a real current rollout query with the resulting native binary.
- [x] Document supported schema and update verification evidence. Preserve the unrelated compressed-archive design branch and its untracked plan.

## MCP provenance follow-up

Native search now returns current Codex messages, but the existing MCP wrapper
labels all native hits Claude. Correct source selection before native limits.

- [x] Add search-only `--transcript-format claude-code|codex` in all four ports, default both. Filter resolved roots before discovery, scanning, counts and limits; do not filter already truncated hits.
- [x] Honor CODEX_HOME for the default implicit Codex sessions root, retaining existing home fallback and tagged extra-root behavior.
- [x] Add the Codex MCP provider and select each native format explicitly, preserving existing OpenCode behavior and global result merging/caps. No duplicate Python rollout parser or temporary HOME/config tricks.
- [ ] Cover provider isolation, count-only, per-provider/global limits, unknown filter values and CODEX_HOME in shared conformance and MCP tests; review and deploy native and MCP sides together.
- [x] Keep compressed/archived-session support and usage analytics explicitly separate.

## Windows verification (2026-10-11)

All four release builds passed. The shared conformance suite passed 1,156 checks
(289 per implementation), and the MCP tests passed 42 cases. Rust unit tests and
Clippy, C++ unit tests, Go tests and vet, and Zig formatting passed. A real current
rollout query using the worktree C++ binary found its canonical user message.
New malformed-block and null-text fixtures failed in Go before the parser fixes.
The original snapshot's red-first history was not available to verify.

Independent review led to a null-text regression fixture and an explicit tested
record-based policy for matching legacy/canonical text, preserving repeated turns.
Archive/provider isolation also has generated regression cases.

The build/coverage item remains open until the Linux coverage gate is verified.
Local Go coverage measures the changed parser and format handling at 100%; the
full Windows measurement retains pre-existing filesystem error-path gaps.
The provider-test/deployment item has passing conformance and MCP coverage and
an internal review, but deployment is intentionally pending owner integration.
No native binary or MCP registration was installed or changed.

## Bounds

- Work only in this isolated worktree, no changes to the source satellite or live installation by workers.
- No commits, pushes, remote calls, memory writes, or native binary installation by workers without parent direction.
- Do not falsely deduplicate repeated user turns by text alone. Canonical response_item and legacy event mirrors require an explicit tested policy if both appear in a rollout.
- No weakening gates, exclusions, canned empty results or changes to analytics semantics.
