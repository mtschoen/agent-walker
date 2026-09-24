// Compressed archive support: zstd inflate at the transcript-read chokepoint,
// transcript file-name classification shared by every discovery path, and
// expansion of a claude-archive root into one claude-code layout root per
// immediate <archive>/<hostname> subdirectory.
//
// See ../SPEC.md sections "Roots", "Discovery", and "Filters".

const std = @import("std");
const Allocator = std.mem.Allocator;
const main = @import("main.zig");

pub const archive_directory_name = "claude-archive";
pub const transcript_suffix = ".jsonl";
pub const compressed_suffix = ".jsonl.zst";
pub const subagent_prefix = "agent-";

pub fn defaultArchiveRoot(allocator: Allocator) ![]const u8 {
    if (main.homeDir(allocator)) |home| {
        defer allocator.free(home);
        return std.fmt.allocPrint(allocator, "{s}{c}{s}", .{
            home, main.PATH_SEP, archive_directory_name,
        });
    }
    return allocator.dupe(u8, archive_directory_name);
}

pub fn parentSessionId(name: []const u8) ?[]const u8 {
    if (std.mem.endsWith(u8, name, compressed_suffix))
        return name[0 .. name.len - compressed_suffix.len];
    if (std.mem.endsWith(u8, name, transcript_suffix))
        return name[0 .. name.len - transcript_suffix.len];
    return null;
}

pub fn subagentAgentId(name: []const u8) ?[]const u8 {
    if (!std.mem.startsWith(u8, name, subagent_prefix)) return null;
    return parentSessionId(name[subagent_prefix.len..]);
}

pub fn isCompressedTranscript(path: []const u8) bool {
    return std.mem.endsWith(u8, path, compressed_suffix);
}

/// Inflate one zstd frame sequence whole. Returns null on any decode failure;
/// the caller emits the SPEC "Filters" stderr line and skips the file.
pub fn inflate(allocator: Allocator, compressed: []const u8) ?[]u8 {
    var input: std.Io.Reader = .fixed(compressed);
    var output: std.Io.Writer.Allocating = .init(allocator);
    defer output.deinit();
    var stream: std.compress.zstd.Decompress = .init(&input, &.{}, .{});
    _ = stream.reader.streamRemaining(&output.writer) catch return null;
    return output.toOwnedSlice() catch null;
}

/// Immediate subdirectories, sorted ascending by name so the effective root
/// order is deterministic. A missing or unreadable path yields a zero-length
/// slice; the caller emits the diagnostic. Uses the same per-platform
/// directory walk the discovery code uses, via main.listSubdirectories.
///
/// The empty case allocates a zero-length slice rather than returning `&.{}`:
/// `&.{}` is a `*const [0][]const u8`, which coerces to `[]const []const u8`
/// but NOT to the mutable `[][]const u8` std.mem.sort needs.
pub fn expandArchiveRoot(allocator: Allocator, path: []const u8) ![][]const u8 {
    const hosts = main.listSubdirectories(allocator, path) catch
        return allocator.alloc([]const u8, 0);
    std.mem.sort([]const u8, hosts, {}, struct {
        fn lessThan(_: void, left: []const u8, right: []const u8) bool {
            return std.mem.lessThan(u8, left, right);
        }
    }.lessThan);
    return hosts;
}

test "parentSessionId accepts both suffixes" {
    try std.testing.expectEqualStrings("abc", parentSessionId("abc.jsonl").?);
    try std.testing.expectEqualStrings("abc", parentSessionId("abc.jsonl.zst").?);
    try std.testing.expect(parentSessionId("abc.jsonl.gz") == null);
    try std.testing.expect(parentSessionId("abc.txt") == null);
}

test "subagentAgentId requires the prefix" {
    try std.testing.expectEqualStrings("aaa", subagentAgentId("agent-aaa.jsonl.zst").?);
    try std.testing.expect(subagentAgentId("aaa.jsonl") == null);
}

test "inflate round trips and rejects a corrupt frame" {
    const allocator = std.testing.allocator;
    // A minimal valid frame: magic number, single-segment header, one empty
    // raw block. Decodes to the empty string.
    const empty_frame = "\x28\xb5\x2f\xfd\x20\x00\x01\x00\x00";
    const decoded = inflate(allocator, empty_frame).?;
    defer allocator.free(decoded);
    try std.testing.expectEqual(@as(usize, 0), decoded.len);
    try std.testing.expect(inflate(allocator, "not a zstd frame") == null);
}
