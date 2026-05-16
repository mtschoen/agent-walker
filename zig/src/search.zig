// Search subcommand: substring/regex match across transcript content.
// See ../SPEC.md "Subcommands" for the contract.
//
// TODO: Full implementation needed. Currently a stub that exits with error 2.
// The conformance tests will skip this language until a working implementation
// is provided. See go/search.go and cpp/search.cpp for reference implementations.

const std = @import("std");
const main = @import("main.zig");
const Allocator = std.mem.Allocator;

pub fn run(alloc: Allocator, argv: [][]const u8) !void {
    _ = alloc;
    _ = argv;
    // TODO: Implement search subcommand
    // Reference implementations:
    // - Go: go/search.go (passes all 18 conformance tests)
    // - C++: cpp/search.cpp (passes all 18 conformance tests)
    // - Rust: rust/src/search.rs (reference implementation)
    std.debug.print("walker: search: not yet implemented in Zig\n", .{});
    std.process.exit(2);
}
