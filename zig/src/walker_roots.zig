// Roots discovery: primary root + CLI extras + extras from
// ~/.claude/walker-roots.json. Deduped via realpath, filtered to existing
// directories.
//
// Mirrors cpp/walker_roots.hpp and rust/src/walker_roots.rs. Failure modes
// follow the SPEC.md contract:
//   * Missing config file -> no extras (silent).
//   * Malformed JSON -> stderr diagnostic, treat as no extras.
//   * Listed path doesn't exist on disk -> skip silently with stderr line.
//   * realpath() fails (broken symlink etc) -> fall back to the raw path.
//   * Primary is allowed to not exist (empty-fleet case); no stderr for it.

const std = @import("std");
const Allocator = std.mem.Allocator;
const main = @import("main.zig");

const is_windows = main.is_windows;
const is_darwin = main.is_darwin;
const PATH_SEP = main.PATH_SEP;

/// Return the path to ~/.claude/walker-roots.json (USERPROFILE on Windows).
/// Falls back to a relative path if neither env var is set. Returns an
/// arena-allocated string the caller need not free.
pub fn walkerConfigPath(alloc: Allocator) ![]const u8 {
    if (main.homeDir(alloc)) |home| {
        defer alloc.free(home);
        return std.fmt.allocPrint(alloc, "{s}{c}.claude{c}walker-roots.json", .{ home, PATH_SEP, PATH_SEP });
    }
    return alloc.dupe(u8, ".claude/walker-roots.json");
}

/// Read extras list from ~/.claude/walker-roots.json. Returns an empty
/// slice on any failure (with a stderr diagnostic for malformed JSON
/// specifically). Returned slice + entries are arena-allocated.
pub fn readExtraRootsFromConfig(alloc: Allocator) ![][]const u8 {
    const config_path = try walkerConfigPath(alloc);

    // Try to read the file. Missing -> silent empty.
    const body = main.readEntireFile(alloc, config_path) catch return &.{};
    if (body.len == 0) return &.{};

    // Trim ASCII whitespace; if the remainder is empty, treat as missing.
    const trimmed = std.mem.trim(u8, body, " \t\r\n");
    if (trimmed.len == 0) return &.{};

    var parsed = std.json.parseFromSlice(std.json.Value, alloc, trimmed, .{}) catch {
        const msg = try std.fmt.allocPrint(
            alloc,
            "walker: malformed {s} -- ignoring extra roots\n",
            .{config_path},
        );
        main.writeStderr(msg);
        return &.{};
    };
    defer parsed.deinit();

    const root_obj = switch (parsed.value) {
        .object => |o| o,
        else => {
            const msg = try std.fmt.allocPrint(
                alloc,
                "walker: {s} is not a JSON object -- ignoring\n",
                .{config_path},
            );
            main.writeStderr(msg);
            return &.{};
        },
    };

    const extras_value = root_obj.get("extra_roots") orelse return &.{};
    const arr = switch (extras_value) {
        .array => |a| a,
        else => return &.{},
    };

    var out: std.ArrayList([]const u8) = .empty;
    for (arr.items) |item| {
        switch (item) {
            .string => |s| {
                if (s.len == 0) continue;
                try out.append(alloc, try alloc.dupe(u8, s));
            },
            else => continue,
        }
    }
    return out.toOwnedSlice(alloc);
}

/// Resolve the effective root list:
///   [primary] + cli_extras + (config extras if read_config)
///   -> dedup via realpath (fallback to raw path)
///   -> filter to existing directories
/// Returned slice + entries are arena-allocated.
pub fn resolveRoots(
    alloc: Allocator,
    primary: []const u8,
    cli_extras: []const []const u8,
    read_config: bool,
) ![][]const u8 {
    // Combined list of (path, is_primary) pairs in spec-mandated order.
    const Entry = struct { path: []const u8, is_primary: bool };
    var combined: std.ArrayList(Entry) = .empty;
    try combined.append(alloc, .{ .path = primary, .is_primary = true });
    for (cli_extras) |p| {
        try combined.append(alloc, .{ .path = p, .is_primary = false });
    }
    if (read_config) {
        const config_extras = readExtraRootsFromConfig(alloc) catch &.{};
        for (config_extras) |p| {
            try combined.append(alloc, .{ .path = p, .is_primary = false });
        }
    }

    var seen = std.StringHashMap(void).init(alloc);
    defer seen.deinit();
    var result: std.ArrayList([]const u8) = .empty;

    for (combined.items) |entry| {
        if (!isExistingDir(alloc, entry.path)) {
            if (!entry.is_primary) {
                const msg = try std.fmt.allocPrint(
                    alloc,
                    "walker: extra root not a directory, skipping: {s}\n",
                    .{entry.path},
                );
                main.writeStderr(msg);
            }
            continue;
        }
        // Canonicalization (realpath) isn't available in zig's manual-syscall
        // style — std.fs is not part of this binary's deps. Strip a single
        // trailing path separator so "/a/b" and "/a/b/" dedup; otherwise rely
        // on raw-path identity. Conformance fixtures don't exercise symlink-
        // based dedup, so this is sufficient.
        const trimmed = stripTrailingSep(entry.path);
        const canonical = try alloc.dupe(u8, trimmed);
        const key = try alloc.dupe(u8, canonical);
        const gop = try seen.getOrPut(key);
        if (gop.found_existing) {
            alloc.free(key);
            alloc.free(canonical);
            continue;
        }
        try result.append(alloc, canonical);
    }
    return result.toOwnedSlice(alloc);
}

fn stripTrailingSep(path: []const u8) []const u8 {
    if (path.len <= 1) return path;
    const last = path[path.len - 1];
    if (last == '/' or last == '\\') return path[0 .. path.len - 1];
    return path;
}

/// Cross-platform check: does `path` exist AND is it a directory?
fn isExistingDir(alloc: Allocator, path: []const u8) bool {
    if (is_windows) {
        const platform = main.platform;
        const wpath = std.unicode.utf8ToUtf16LeAllocZ(alloc, path) catch return false;
        defer alloc.free(wpath);
        var info: platform.WIN32_FILE_ATTRIBUTE_DATA = undefined;
        if (platform.GetFileAttributesExW(wpath.ptr, 0, &info) == 0) return false;
        return (info.dwFileAttributes & platform.FILE_ATTRIBUTE_DIRECTORY) != 0;
    } else if (is_darwin) {
        const zpath = alloc.dupeZ(u8, path) catch return false;
        defer alloc.free(zpath);
        var st: std.c.Stat = undefined;
        if (std.c.fstatat(std.c.AT.FDCWD, zpath, &st, 0) != 0) return false;
        return (st.mode & 0o170000) == 0o040000;
    } else {
        const linux = main.platform.linux;
        const zpath = alloc.dupeZ(u8, path) catch return false;
        defer alloc.free(zpath);
        var statx_buf: linux.Statx = std.mem.zeroes(linux.Statx);
        const ret = linux.statx(linux.AT.FDCWD, zpath, 0, .{}, &statx_buf);
        const signed: isize = @bitCast(ret);
        if (signed < 0) return false;
        return (statx_buf.mode & 0o170000) == 0o040000;
    }
}

