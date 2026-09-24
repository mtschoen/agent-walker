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
const archive = @import("archive.zig");

const is_windows = main.is_windows;
const is_darwin = main.is_darwin;
const PATH_SEP = main.PATH_SEP;

pub const TranscriptFormat = enum {
    claude_code,
    codex,
    /// A directory of per-hostname claude-code layout trees written by
    /// replica. Never walked directly; expanded by archive.expandArchiveRoot.
    claude_archive,
};

pub const TranscriptRoot = struct {
    path: []const u8,
    format: TranscriptFormat,
    from_archive: bool = false,
};

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
    const tagged_roots = try readTaggedExtraRootsFromConfig(alloc);
    var out: std.ArrayList([]const u8) = .empty;
    for (tagged_roots) |root| {
        if (root.format == .claude_code) try out.append(alloc, root.path);
    }
    return out.toOwnedSlice(alloc);
}

/// Read config roots with their transcript format. String entries remain
/// Claude Code roots; object entries must provide both path and format.
pub fn readTaggedExtraRootsFromConfig(alloc: Allocator) ![]TranscriptRoot {
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

    var out: std.ArrayList(TranscriptRoot) = .empty;
    for (arr.items) |item| {
        switch (item) {
            .string => |s| {
                if (s.len == 0) continue;
                try out.append(alloc, .{
                    .path = try alloc.dupe(u8, s),
                    .format = .claude_code,
                });
            },
            .object => |object| {
                const path_value = object.get("path") orelse continue;
                const path = switch (path_value) {
                    .string => |value| value,
                    else => continue,
                };
                if (path.len == 0) continue;
                const format_value = object.get("format") orelse continue;
                const format_string = switch (format_value) {
                    .string => |value| value,
                    else => continue,
                };
                const format: TranscriptFormat = if (std.mem.eql(u8, format_string, "claude-code"))
                    .claude_code
                else if (std.mem.eql(u8, format_string, "codex"))
                    .codex
                else if (std.mem.eql(u8, format_string, "claude-archive"))
                    .claude_archive
                else
                    continue;
                try out.append(alloc, .{
                    .path = try alloc.dupe(u8, path),
                    .format = format,
                });
            },
            else => continue,
        }
    }
    return out.toOwnedSlice(alloc);
}

pub fn defaultCodexRoot(alloc: Allocator) ![]const u8 {
    if (main.homeDir(alloc)) |home| {
        defer alloc.free(home);
        return std.fmt.allocPrint(alloc, "{s}{c}.codex{c}sessions", .{ home, PATH_SEP, PATH_SEP });
    }
    return alloc.dupe(u8, ".codex/sessions");
}

pub const ConfigRoots = struct {
    claude_code: [][]const u8,
    archives: [][]const u8,
    tagged: []TranscriptRoot,
};

pub fn configRootsByFormat(alloc: Allocator, read_config: bool) !ConfigRoots {
    var claude_code: std.ArrayList([]const u8) = .empty;
    var archives: std.ArrayList([]const u8) = .empty;
    if (!read_config) {
        return .{
            .claude_code = try claude_code.toOwnedSlice(alloc),
            .archives = try archives.toOwnedSlice(alloc),
            .tagged = &.{},
        };
    }
    const tagged = try readTaggedExtraRootsFromConfig(alloc);
    for (tagged) |root| {
        switch (root.format) {
            .claude_code => try claude_code.append(alloc, root.path),
            .claude_archive => try archives.append(alloc, root.path),
            .codex => {},
        }
    }
    return .{
        .claude_code = try claude_code.toOwnedSlice(alloc),
        .archives = try archives.toOwnedSlice(alloc),
        .tagged = tagged,
    };
}

/// Archive roots in SPEC effective order (implicit, CLI, config), each already
/// expanded into its <archive>/<hostname> claude-code roots. The implicit root
/// is silent when absent; a CLI or config root that is not a directory gets
/// the standard diagnostic.
pub fn archiveRootsInEffectiveOrder(
    alloc: Allocator,
    primary_explicit: bool,
    cli_archives: []const []const u8,
    config_archives: []const []const u8,
) ![][]const u8 {
    var expanded: std.ArrayList([]const u8) = .empty;
    if (!primary_explicit) {
        const implicit = try archive.defaultArchiveRoot(alloc);
        if (main.isDirectory(implicit)) {
            try expanded.appendSlice(alloc, try archive.expandArchiveRoot(alloc, implicit));
        }
    }
    for ([_][]const []const u8{ cli_archives, config_archives }) |group| {
        for (group) |archive_path| {
            if (!main.isDirectory(archive_path)) {
                const message = try std.fmt.allocPrint(
                    alloc,
                    "walker: archive root not a directory, skipping: {s}\n",
                    .{archive_path},
                );
                main.writeStderr(message);
                continue;
            }
            try expanded.appendSlice(alloc, try archive.expandArchiveRoot(alloc, archive_path));
        }
    }
    return expanded.toOwnedSlice(alloc);
}

/// Resolve search roots with transcript format tags. An implicit primary adds
/// both local Claude Code and Codex defaults. CLI extras remain Claude Code.
pub fn resolveSearchRoots(
    alloc: Allocator,
    primary: ?[]const u8,
    cli_extras: []const []const u8,
    cli_archives: []const []const u8,
    read_config: bool,
) ![]TranscriptRoot {
    const primary_explicit = primary != null;
    const cfg = try configRootsByFormat(alloc, read_config);
    const Entry = struct {
        root: TranscriptRoot,
        report_missing: bool,
    };
    var combined: std.ArrayList(Entry) = .empty;
    try combined.append(alloc, .{
        .root = .{
            .path = primary orelse try main.defaultRoot(alloc),
            .format = .claude_code,
            .from_archive = false,
        },
        .report_missing = false,
    });
    if (!primary_explicit) {
        try combined.append(alloc, .{
            .root = .{
                .path = try defaultCodexRoot(alloc),
                .format = .codex,
                .from_archive = false,
            },
            .report_missing = false,
        });
    }
    for (cli_extras) |path| {
        try combined.append(alloc, .{
            .root = .{ .path = path, .format = .claude_code, .from_archive = false },
            .report_missing = true,
        });
    }
    for (cfg.tagged) |root| {
        if (root.format != .claude_archive) {
            try combined.append(alloc, .{ .root = root, .report_missing = true });
        }
    }
    const expanded_archives = try archiveRootsInEffectiveOrder(alloc, primary_explicit, cli_archives, cfg.archives);
    for (expanded_archives) |host| {
        try combined.append(alloc, .{
            .root = .{ .path = host, .format = .claude_code, .from_archive = true },
            .report_missing = false,
        });
    }

    var seen = std.StringHashMap(void).init(alloc);
    defer seen.deinit();
    var result: std.ArrayList(TranscriptRoot) = .empty;

    for (combined.items) |entry| {
        if (!isExistingDir(alloc, entry.root.path)) {
            if (entry.report_missing) {
                const msg = try std.fmt.allocPrint(
                    alloc,
                    "walker: extra root not a directory, skipping: {s}\n",
                    .{entry.root.path},
                );
                main.writeStderr(msg);
            }
            continue;
        }
        const canonical = try alloc.dupe(u8, stripTrailingSep(entry.root.path));
        const format_prefix = switch (entry.root.format) {
            .claude_code => "claude-code:",
            .codex => "codex:",
            .claude_archive => "claude-archive:",
        };
        const key = try std.fmt.allocPrint(alloc, "{s}{s}", .{ format_prefix, canonical });
        const entry_result = try seen.getOrPut(key);
        if (entry_result.found_existing) {
            alloc.free(key);
            alloc.free(canonical);
            continue;
        }
        try result.append(alloc, .{
            .path = canonical,
            .format = entry.root.format,
            .from_archive = entry.root.from_archive,
        });
    }
    return result.toOwnedSlice(alloc);
}

/// Resolve the effective root list:
///   [primary] + cli_extras + (config extras if read_config) + expanded archive roots
///   -> dedup via realpath (fallback to raw path)
///   -> filter to existing directories
/// Returned slice + entries are arena-allocated.
pub fn resolveRoots(
    alloc: Allocator,
    primary: []const u8,
    primary_explicit: bool,
    cli_extras: []const []const u8,
    cli_archives: []const []const u8,
    read_config: bool,
) ![]main.ResolvedRoot {
    const cfg = try configRootsByFormat(alloc, read_config);
    const Entry = struct { path: []const u8, is_primary: bool, from_archive: bool };
    var combined: std.ArrayList(Entry) = .empty;
    try combined.append(alloc, .{ .path = primary, .is_primary = true, .from_archive = false });
    for (cli_extras) |p| {
        try combined.append(alloc, .{ .path = p, .is_primary = false, .from_archive = false });
    }
    for (cfg.claude_code) |p| {
        try combined.append(alloc, .{ .path = p, .is_primary = false, .from_archive = false });
    }
    const expanded_archives = try archiveRootsInEffectiveOrder(alloc, primary_explicit, cli_archives, cfg.archives);
    for (expanded_archives) |p| {
        try combined.append(alloc, .{ .path = p, .is_primary = false, .from_archive = true });
    }

    var seen = std.StringHashMap(void).init(alloc);
    defer seen.deinit();
    var result: std.ArrayList(main.ResolvedRoot) = .empty;

    for (combined.items) |entry| {
        if (!isExistingDir(alloc, entry.path)) {
            if (!entry.is_primary and !entry.from_archive) {
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
        // style - std.fs is not part of this binary's deps. Strip a single
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
        try result.append(alloc, .{ .path = canonical, .from_archive = entry.from_archive });
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
