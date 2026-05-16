// Native pace-walker -- Zig implementation (cross-platform: Linux + Windows).
// See ../SPEC.md for the contract every implementation must honor.

const std = @import("std");
const builtin = @import("builtin");
const Allocator = std.mem.Allocator;
const beacons = @import("beacons.zig");
const search = @import("search.zig");

const VERSION = "zig/0.1.1";
pub const is_windows = builtin.os.tag == .windows;

// ─── Platform abstraction ────────────────────────────────────────────────────

const PlatformFd = if (is_windows) *anyopaque else i32;

pub const platform = if (is_windows) struct {
    pub const HANDLE = *anyopaque;
    const DWORD = u32;
    const BOOL = i32;
    const WCHAR = u16;
    pub const LARGE_INTEGER = extern union { parts: extern struct { lo: u32, hi: i32 }, quad: i64 };
    pub const INVALID_HANDLE_VALUE: HANDLE = @ptrFromInt(@as(usize, @bitCast(@as(isize, -1))));
    pub const FILETIME = extern struct {
        lo: u32 = 0,
        hi: u32 = 0,
        fn toUnix(ft: FILETIME) f64 {
            const hns: i64 = (@as(i64, ft.hi) << 32) | @as(i64, ft.lo);
            return @as(f64, @floatFromInt(hns - 116_444_736_000_000_000)) / 10_000_000.0;
        }
    };
    pub const WIN32_FILE_ATTRIBUTE_DATA = extern struct {
        dwFileAttributes: u32,
        ftCreationTime: FILETIME,
        ftLastAccessTime: FILETIME,
        ftLastWriteTime: FILETIME,
        nFileSizeHigh: u32,
        nFileSizeLow: u32,
    };
    pub const WIN32_FIND_DATAW = extern struct {
        dwFileAttributes: u32,
        ftCreationTime: FILETIME,
        ftLastAccessTime: FILETIME,
        ftLastWriteTime: FILETIME,
        nFileSizeHigh: u32,
        nFileSizeLow: u32,
        dwReserved0: u32,
        dwReserved1: u32,
        cFileName: [260]u16,
        cAlternateFileName: [14]u16,
    };
    const SECURITY_ATTRIBUTES = extern struct {
        nLength: u32,
        lpSecurityDescriptor: ?*anyopaque,
        bInheritHandle: i32,
    };
    pub const GENERIC_READ = 0x80000000;
    pub const FILE_SHARE_READ = 0x00000001;
    pub const OPEN_EXISTING = 3;
    pub const FILE_ATTRIBUTE_NORMAL = 0x80;
    pub const FILE_ATTRIBUTE_DIRECTORY = 0x10;
    pub const STD_OUTPUT_HANDLE: u32 = @bitCast(@as(i32, -11));

    extern "kernel32" fn GetSystemTimeAsFileTime(*FILETIME) callconv(.winapi) void;
    extern "kernel32" fn QueryPerformanceCounter(*LARGE_INTEGER) callconv(.winapi) BOOL;
    extern "kernel32" fn QueryPerformanceFrequency(*LARGE_INTEGER) callconv(.winapi) BOOL;
    pub extern "kernel32" fn GetFileAttributesExW([*:0]const u16, u32, *WIN32_FILE_ATTRIBUTE_DATA) callconv(.winapi) BOOL;
    extern "kernel32" fn GetCommandLineW() callconv(.winapi) [*:0]const u16;
    extern "kernel32" fn GetEnvironmentVariableW([*:0]const u16, [*]u16, u32) callconv(.winapi) u32;
    extern "kernel32" fn GetStdHandle(nStdHandle: u32) callconv(.winapi) ?HANDLE;
    extern "kernel32" fn WriteFile(hFile: HANDLE, lpBuffer: *const anyopaque, nNumberOfBytesToWrite: u32, lpNumberOfBytesWritten: *u32, lpOverlapped: ?*anyopaque) callconv(.winapi) BOOL;
    extern "kernel32" fn ReadFile(hFile: HANDLE, lpBuffer: *anyopaque, nNumberOfBytesToRead: u32, lpNumberOfBytesRead: *u32, lpOverlapped: ?*anyopaque) callconv(.winapi) BOOL;
    extern "kernel32" fn CreateFileW(lpFileName: [*:0]const u16, dwDesiredAccess: u32, dwShareMode: u32, lpSecurityAttributes: ?*const SECURITY_ATTRIBUTES, dwCreationDisposition: u32, dwFlagsAndAttributes: u32, hTemplateFile: ?HANDLE) callconv(.winapi) ?HANDLE;
    extern "kernel32" fn CloseHandle(hObject: HANDLE) callconv(.winapi) BOOL;
    pub extern "kernel32" fn FindFirstFileW(lpFileName: [*:0]const u16, lpFindFileData: *WIN32_FIND_DATAW) callconv(.winapi) ?HANDLE;
    pub extern "kernel32" fn FindNextFileW(hFindFile: HANDLE, lpFindFileData: *WIN32_FIND_DATAW) callconv(.winapi) BOOL;
    pub extern "kernel32" fn FindClose(hFindFile: HANDLE) callconv(.winapi) BOOL;
} else struct {
    pub const linux = std.os.linux;
};

pub fn nowUnix() f64 {
    if (is_windows) {
        var ft: platform.FILETIME = .{};
        platform.GetSystemTimeAsFileTime(&ft);
        return ft.toUnix();
    } else {
        var ts: platform.linux.timespec = undefined;
        _ = platform.linux.clock_gettime(platform.linux.CLOCK.REALTIME, &ts);
        return @as(f64, @floatFromInt(ts.sec)) + @as(f64, @floatFromInt(ts.nsec)) / 1_000_000_000.0;
    }
}

pub fn perfNow() i64 {
    if (is_windows) {
        var v: platform.LARGE_INTEGER = undefined;
        _ = platform.QueryPerformanceCounter(&v);
        return v.quad;
    } else {
        var ts: platform.linux.timespec = undefined;
        _ = platform.linux.clock_gettime(platform.linux.CLOCK.MONOTONIC, &ts);
        return ts.sec * 1_000_000_000 + ts.nsec;
    }
}

pub fn perfFreq() i64 {
    if (is_windows) {
        var v: platform.LARGE_INTEGER = undefined;
        _ = platform.QueryPerformanceFrequency(&v);
        return v.quad;
    } else {
        return 1_000_000_000;
    }
}

pub fn writeStdout(bytes: []const u8) void {
    if (is_windows) {
        const h = platform.GetStdHandle(platform.STD_OUTPUT_HANDLE).?;
        var written: u32 = 0;
        var offset: usize = 0;
        while (offset < bytes.len) {
            const chunk: u32 = @intCast(@min(bytes.len - offset, 65536));
            const ptr: *const anyopaque = @ptrCast(bytes.ptr + offset);
            _ = platform.WriteFile(h, ptr, chunk, &written, null);
            if (written == 0) break;
            offset += written;
        }
    } else {
        var offset: usize = 0;
        while (offset < bytes.len) {
            const n = platform.linux.write(1, bytes.ptr + offset, bytes.len - offset);
            const signed: isize = @bitCast(n);
            if (signed <= 0) break;
            offset += @intCast(signed);
        }
    }
}

pub fn writeStderr(bytes: []const u8) void {
    if (is_windows) {
        const h = platform.GetStdHandle(@bitCast(@as(i32, -12))).?;
        var written: u32 = 0;
        var offset: usize = 0;
        while (offset < bytes.len) {
            const chunk: u32 = @intCast(@min(bytes.len - offset, 65536));
            const ptr: *const anyopaque = @ptrCast(bytes.ptr + offset);
            _ = platform.WriteFile(h, ptr, chunk, &written, null);
            if (written == 0) break;
            offset += written;
        }
    } else {
        var offset: usize = 0;
        while (offset < bytes.len) {
            const n = platform.linux.write(2, bytes.ptr + offset, bytes.len - offset);
            const signed: isize = @bitCast(n);
            if (signed <= 0) break;
            offset += @intCast(signed);
        }
    }
}

// ─── File I/O abstraction ────────────────────────────────────────────────────

fn fileOpen(alloc: Allocator, path: []const u8) !PlatformFd {
    if (is_windows) {
        const wpath = try std.unicode.utf8ToUtf16LeAllocZ(alloc, path);
        const h = platform.CreateFileW(wpath.ptr, platform.GENERIC_READ, platform.FILE_SHARE_READ, null, platform.OPEN_EXISTING, platform.FILE_ATTRIBUTE_NORMAL, null) orelse return error.OpenFailed;
        if (h == platform.INVALID_HANDLE_VALUE) return error.OpenFailed;
        return h;
    } else {
        const zpath = try alloc.dupeZ(u8, path);
        defer alloc.free(zpath);
        const ret = platform.linux.openat(platform.linux.AT.FDCWD, zpath, .{}, 0);
        const fd: i32 = @bitCast(@as(u32, @truncate(ret)));
        if (fd < 0) return error.OpenFailed;
        return fd;
    }
}

fn fileClose(fd: PlatformFd) void {
    if (is_windows) {
        _ = platform.CloseHandle(fd);
    } else {
        _ = platform.linux.close(fd);
    }
}

fn fileRead(fd: PlatformFd, buf: []u8) !usize {
    if (is_windows) {
        var n: u32 = 0;
        if (platform.ReadFile(fd, buf.ptr, @intCast(buf.len), &n, null) == 0) return error.ReadFailed;
        return n;
    } else {
        const n = platform.linux.read(fd, buf.ptr, buf.len);
        const signed: isize = @bitCast(n);
        if (signed < 0) return error.ReadFailed;
        return @intCast(signed);
    }
}

pub fn readEntireFile(alloc: Allocator, path: []const u8) ![]u8 {
    const fd = try fileOpen(alloc, path);
    defer fileClose(fd);
    var buf: std.ArrayList(u8) = .empty;
    var read_buf: [65536]u8 = undefined;
    while (true) {
        const n = fileRead(fd, &read_buf) catch break;
        if (n == 0) break;
        buf.appendSlice(alloc, read_buf[0..n]) catch break;
    }
    return buf.toOwnedSlice(alloc) catch buf.items;
}

// ─── mtime filter ────────────────────────────────────────────────────────────

fn mtimeOk(alloc: Allocator, path: []const u8, earliest: f64) bool {
    if (is_windows) {
        const wpath = std.unicode.utf8ToUtf16LeAllocZ(alloc, path) catch return true;
        var info: platform.WIN32_FILE_ATTRIBUTE_DATA = undefined;
        if (platform.GetFileAttributesExW(wpath.ptr, 0, &info) == 0) return true;
        return info.ftLastWriteTime.toUnix() >= earliest;
    } else {
        const zpath = alloc.dupeZ(u8, path) catch return true;
        defer alloc.free(zpath);
        var statx_buf: platform.linux.Statx = std.mem.zeroes(platform.linux.Statx);
        const ret = platform.linux.statx(
            platform.linux.AT.FDCWD,
            zpath,
            0,
            .{ .MTIME = true },
            &statx_buf,
        );
        const signed: isize = @bitCast(ret);
        if (signed < 0) return true;
        const mtime = @as(f64, @floatFromInt(statx_buf.mtime.sec)) +
            @as(f64, @floatFromInt(statx_buf.mtime.nsec)) / 1_000_000_000.0;
        return mtime >= earliest;
    }
}

// ─── Command-line parsing ────────────────────────────────────────────────────

pub fn getArgs(alloc: Allocator) ![][]const u8 {
    if (is_windows) {
        return getArgsWindows(alloc);
    } else {
        return getArgsLinux(alloc);
    }
}

fn getArgsLinux(alloc: Allocator) ![][]const u8 {
    const fd: i32 = @bitCast(@as(u32, @truncate(platform.linux.openat(
        platform.linux.AT.FDCWD,
        "/proc/self/cmdline\x00",
        .{},
        0,
    ))));
    if (fd < 0) return error.OpenFailed;
    defer _ = platform.linux.close(fd);

    var buf: [65536]u8 = undefined;
    var total: usize = 0;
    while (total < buf.len) {
        const n = platform.linux.read(fd, buf[total..].ptr, buf.len - total);
        const signed: isize = @bitCast(n);
        if (signed <= 0) break;
        total += @intCast(signed);
    }

    var out: std.ArrayList([]const u8) = .empty;
    var i: usize = 0;
    var first = true;
    while (i < total) {
        var end = i;
        while (end < total and buf[end] != 0) end += 1;
        if (end > i) {
            if (!first)
                try out.append(alloc, try alloc.dupe(u8, buf[i..end]));
            first = false;
        }
        i = end + 1;
    }
    return out.toOwnedSlice(alloc);
}

fn getArgsWindows(alloc: Allocator) ![][]const u8 {
    if (!is_windows) unreachable;
    const wcmd = platform.GetCommandLineW();
    const wlen = std.mem.len(wcmd);
    const ws = wcmd[0..wlen];

    var out: std.ArrayList([]const u8) = .empty;
    var i: usize = 0;
    var first = true;

    while (i < wlen) {
        while (i < wlen and (ws[i] == ' ' or ws[i] == '\t')) i += 1;
        if (i >= wlen) break;

        var tok: std.ArrayList(u16) = .empty;
        var in_q = false;
        while (i < wlen) {
            const c = ws[i];
            if (c == '"') {
                in_q = !in_q;
                i += 1;
            } else if (!in_q and (c == ' ' or c == '\t')) break else {
                try tok.append(alloc, c);
                i += 1;
            }
        }

        if (tok.items.len > 0 and !first)
            try out.append(alloc, try std.unicode.utf16LeToUtf8Alloc(alloc, tok.items));
        first = false;
        tok.deinit(alloc);
    }
    return out.toOwnedSlice(alloc);
}

pub fn getEnvVar(alloc: Allocator, name: []const u8) ?[]const u8 {
    if (is_windows) {
        var wn: [256:0]u16 = undefined;
        const n = std.unicode.utf8ToUtf16Le(wn[0..255], name) catch return null;
        wn[n] = 0;
        var wv: [32768]u16 = undefined;
        const len = platform.GetEnvironmentVariableW(@ptrCast(&wn), &wv, @intCast(wv.len));
        if (len == 0) return null;
        return std.unicode.utf16LeToUtf8Alloc(alloc, wv[0..len]) catch null;
    } else {
        // Read /proc/self/environ to find the variable without libc
        const fd: i32 = @bitCast(@as(u32, @truncate(platform.linux.openat(
            platform.linux.AT.FDCWD,
            "/proc/self/environ\x00",
            .{},
            0,
        ))));
        if (fd < 0) return null;
        defer _ = platform.linux.close(fd);

        var buf: [65536]u8 = undefined;
        var total: usize = 0;
        while (total < buf.len) {
            const rd = platform.linux.read(fd, buf[total..].ptr, buf.len - total);
            const signed: isize = @bitCast(rd);
            if (signed <= 0) break;
            total += @intCast(signed);
        }

        // Entries are null-separated: NAME=VALUE\0NAME=VALUE\0...
        var i: usize = 0;
        while (i < total) {
            var end = i;
            while (end < total and buf[end] != 0) end += 1;
            const entry = buf[i..end];
            if (entry.len > name.len and entry[name.len] == '=' and
                std.mem.eql(u8, entry[0..name.len], name))
            {
                return alloc.dupe(u8, entry[name.len + 1 ..]) catch null;
            }
            i = end + 1;
        }
        return null;
    }
}

// ─── CLI ─────────────────────────────────────────────────────────────────────

const Cli = struct {
    period: u64 = 0,
    win_start: f64 = 0.0,
    now: ?f64 = null,
    root: ?[]const u8 = null,
};

fn parseCli(argv: [][]const u8) !Cli {
    var cli = Cli{};
    var i: usize = 0;
    while (i < argv.len) {
        const flag = argv[i];
        i += 1;
        if (std.mem.eql(u8, flag, "--period")) {
            cli.period = std.fmt.parseInt(u64, grab(argv, &i, "--period"), 10) catch die("--period: invalid");
        } else if (std.mem.eql(u8, flag, "--win-start")) {
            cli.win_start = std.fmt.parseFloat(f64, grab(argv, &i, "--win-start")) catch die("--win-start: invalid");
        } else if (std.mem.eql(u8, flag, "--now")) {
            cli.now = std.fmt.parseFloat(f64, grab(argv, &i, "--now")) catch die("--now: invalid");
        } else if (std.mem.eql(u8, flag, "--projects-root")) {
            cli.root = grab(argv, &i, "--projects-root");
        } else if (std.mem.eql(u8, flag, "--version")) {
            writeStdout(VERSION ++ "\n");
            std.process.exit(0);
        } else {
            std.debug.print("walker: unknown flag: {s}\n", .{flag});
            std.process.exit(2);
        }
    }
    if (cli.period == 0) die("--period is required");
    return cli;
}

pub fn grab(argv: [][]const u8, i: *usize, flag: []const u8) []const u8 {
    if (i.* >= argv.len) {
        std.debug.print("walker: {s} needs a value\n", .{flag});
        std.process.exit(2);
    }
    defer i.* += 1;
    return argv[i.*];
}

pub fn die(msg: []const u8) noreturn {
    std.debug.print("walker: {s}\n", .{msg});
    std.process.exit(2);
}

// ─── Pricing ─────────────────────────────────────────────────────────────────

fn modelCost(inp: u64, out_: u64, cr: u64, cw: u64, model: []const u8) f64 {
    var buf: [256]u8 = undefined;
    const n = @min(model.len, buf.len);
    const lo = std.ascii.lowerString(buf[0..n], model[0..n]);
    const ir: f64 = if (std.mem.indexOf(u8, lo, "opus") != null) 5.0
    else if (std.mem.indexOf(u8, lo, "haiku") != null) 1.0
    else 3.0;
    const or_: f64 = if (std.mem.indexOf(u8, lo, "opus") != null) 25.0
    else if (std.mem.indexOf(u8, lo, "haiku") != null) 5.0
    else 15.0;
    return (@as(f64, @floatFromInt(inp)) * ir +
        @as(f64, @floatFromInt(cr)) * ir * 0.10 +
        @as(f64, @floatFromInt(cw)) * ir * 1.25 +
        @as(f64, @floatFromInt(out_)) * or_) / 1_000_000.0;
}

// ─── ISO 8601 ────────────────────────────────────────────────────────────────

pub fn parseTs(s: []const u8) !f64 {
    if (s.len < 20 or s[s.len - 1] != 'Z') return error.Bad;
    if (s[4] != '-' or s[7] != '-' or s[10] != 'T' or s[13] != ':' or s[16] != ':')
        return error.Bad;
    const yr: i32 = try std.fmt.parseInt(i32, s[0..4], 10);
    const mo: u32 = try std.fmt.parseInt(u32, s[5..7], 10);
    const dy: u32 = try std.fmt.parseInt(u32, s[8..10], 10);
    const hr: u32 = try std.fmt.parseInt(u32, s[11..13], 10);
    const mn: u32 = try std.fmt.parseInt(u32, s[14..16], 10);
    const sc: u32 = try std.fmt.parseInt(u32, s[17..19], 10);
    var frac: f64 = 0.0;
    if (s.len > 21 and s[19] == '.') {
        const fs = s[20 .. s.len - 1];
        const fn_ = try std.fmt.parseInt(u64, fs, 10);
        frac = @as(f64, @floatFromInt(fn_)) / std.math.pow(f64, 10.0, @as(f64, @floatFromInt(fs.len)));
    }
    return @as(f64, @floatFromInt(calToUnix(yr, mo, dy, hr, mn, sc))) + frac;
}

fn leap(y: i32) bool {
    return (@rem(y, 4) == 0 and @rem(y, 100) != 0) or @rem(y, 400) == 0;
}
fn dim(m: u32, y: i32) i64 {
    return switch (m) {
        1, 3, 5, 7, 8, 10, 12 => 31,
        4, 6, 9, 11 => 30,
        2 => if (leap(y)) 29 else 28,
        else => 0,
    };
}
fn calToUnix(yr: i32, mo: u32, dy: u32, hr: u32, mn: u32, sc: u32) i64 {
    var d: i64 = 0;
    var y: i32 = 1970;
    if (yr >= 1970) {
        while (y < yr) : (y += 1) d += if (leap(y)) 366 else 365;
    } else {
        while (y > yr) : (y -= 1) d -= if (leap(y - 1)) 366 else 365;
    }
    var m: u32 = 1;
    while (m < mo) : (m += 1) d += dim(m, yr);
    d += @as(i64, dy) - 1;
    return d * 86400 + @as(i64, hr) * 3600 + @as(i64, mn) * 60 + @as(i64, sc);
}

// ─── JSON helpers ────────────────────────────────────────────────────────────

fn ju64(obj: std.json.ObjectMap, key: []const u8) u64 {
    return switch (obj.get(key) orelse return 0) {
        .integer => |n| if (n >= 0) @intCast(n) else 0,
        .float => |f| if (f >= 0.0) @intFromFloat(f) else 0,
        else => 0,
    };
}

// ─── Group walking ───────────────────────────────────────────────────────────

const Pair = struct { trailing: f64, window: f64 };

fn walkGroup(alloc: Allocator, paths: []const []const u8, pc: f64, ws: f64) Pair {
    const earliest = @min(pc, ws);
    var trailing: f64 = 0.0;
    var window: f64 = 0.0;
    var seen = std.StringHashMap(void).init(alloc);
    defer {
        var it = seen.keyIterator();
        while (it.next()) |k| alloc.free(k.*);
        seen.deinit();
    }

    for (paths) |path| {
        const data = readEntireFile(alloc, path) catch continue;
        defer alloc.free(data);

        var iter = std.mem.splitScalar(u8, data, '\n');
        while (iter.next()) |line| {
            processLine(alloc, line, &seen, earliest, pc, ws, &trailing, &window);
        }
    }
    return .{ .trailing = trailing, .window = window };
}

fn processLine(
    alloc: Allocator,
    raw: []const u8,
    seen: *std.StringHashMap(void),
    earliest: f64,
    pc: f64,
    ws: f64,
    trailing: *f64,
    window: *f64,
) void {
    const line = std.mem.trim(u8, raw, " \t\r\n");
    if (line.len == 0) return;

    const parsed = std.json.parseFromSlice(
        std.json.Value,
        alloc,
        line,
        .{ .ignore_unknown_fields = true },
    ) catch return;
    defer parsed.deinit();

    const root = parsed.value;
    if (root != .object) return;
    const msg_v = root.object.get("message") orelse return;
    if (msg_v != .object) return;
    const msg = msg_v.object;

    const role = msg.get("role") orelse return;
    if (role != .string or !std.mem.eql(u8, role.string, "assistant")) return;

    if (msg.get("id")) |id_v| {
        if (id_v == .string and id_v.string.len > 0) {
            if (seen.contains(id_v.string)) return;
            const k = alloc.dupe(u8, id_v.string) catch return;
            seen.put(k, {}) catch {
                alloc.free(k);
                return;
            };
        }
    }

    const ts_v = root.object.get("timestamp") orelse return;
    if (ts_v != .string or ts_v.string.len == 0) return;
    const ts = parseTs(ts_v.string) catch return;
    if (ts < earliest) return;

    const mdl: []const u8 = if (msg.get("model")) |mv| (if (mv == .string) mv.string else "") else "";
    var inp: u64 = 0;
    var out_: u64 = 0;
    var cr: u64 = 0;
    var cw: u64 = 0;
    if (msg.get("usage")) |uv| {
        if (uv == .object) {
            inp = ju64(uv.object, "input_tokens");
            out_ = ju64(uv.object, "output_tokens");
            cr = ju64(uv.object, "cache_read_input_tokens");
            cw = ju64(uv.object, "cache_creation_input_tokens");
        }
    }

    const c = modelCost(inp, out_, cr, cw, mdl);
    if (ts >= pc) trailing.* += c;
    if (ts >= ws) window.* += c;
}

// ─── Directory discovery ─────────────────────────────────────────────────────

pub const FileMap = std.StringHashMap(std.ArrayList([]const u8));
pub const PATH_SEP = if (is_windows) '\\' else '/';

fn addFile(alloc: Allocator, map: *FileMap, slug: []const u8, sid: []const u8, path: []const u8) !void {
    const key = try std.fmt.allocPrint(alloc, "{s}\x00{s}", .{ slug, sid });
    const gop = try map.getOrPut(key);
    if (!gop.found_existing) {
        gop.value_ptr.* = .empty;
    } else alloc.free(key);
    try gop.value_ptr.*.append(alloc, path);
}

pub fn defaultRoot(alloc: Allocator) ![]const u8 {
    const home_var = if (is_windows) "USERPROFILE" else "HOME";
    if (getEnvVar(alloc, home_var)) |home| {
        defer alloc.free(home);
        return std.fmt.allocPrint(alloc, "{s}{c}.claude{c}projects", .{ home, PATH_SEP, PATH_SEP });
    }
    return alloc.dupe(u8, ".claude/projects");
}

pub fn discover(alloc: Allocator, root_path: []const u8, earliest: f64) !FileMap {
    if (is_windows) {
        return discoverWindows(alloc, root_path, earliest);
    } else {
        return discoverLinux(alloc, root_path, earliest);
    }
}

fn discoverLinux(alloc: Allocator, root_path: []const u8, earliest: f64) !FileMap {
    const linux = platform.linux;
    var map = FileMap.init(alloc);

    const root_z = try alloc.dupeZ(u8, root_path);
    defer alloc.free(root_z);
    const root_fd_ret = linux.openat(linux.AT.FDCWD, root_z, .{ .DIRECTORY = true }, 0);
    const root_fd: i32 = @bitCast(@as(u32, @truncate(root_fd_ret)));
    if (root_fd < 0) return map;
    defer _ = linux.close(root_fd);

    // Iterate slug directories
    var dent_buf: [8192]u8 = undefined;
    while (true) {
        const n = linux.getdents64(root_fd, &dent_buf, dent_buf.len);
        const signed: isize = @bitCast(n);
        if (signed <= 0) break;

        var offset: usize = 0;
        while (offset < n) {
            const entry = @as(*align(1) const linux.dirent64, @ptrCast(&dent_buf[offset]));
            offset += entry.reclen;

            if (entry.type != linux.DT.DIR) continue;
            const name_ptr: [*:0]const u8 = @ptrCast(&entry.name);
            const slug = std.mem.span(name_ptr);
            if (slug.len == 0) continue;
            if (slug[0] == '.' and (slug.len == 1 or (slug.len == 2 and slug[1] == '.'))) continue;

            const slug_dir = try std.fmt.allocPrint(alloc, "{s}/{s}", .{ root_path, slug });

            // Scan for parent .jsonl files and session dirs
            try scanSlugDir(alloc, &map, slug, slug_dir, earliest);
        }
    }
    return map;
}

fn scanSlugDir(alloc: Allocator, map: *FileMap, slug: []const u8, slug_dir: []const u8, earliest: f64) !void {
    const linux = platform.linux;
    const slug_z = try alloc.dupeZ(u8, slug_dir);
    defer alloc.free(slug_z);
    const slug_fd: i32 = @bitCast(@as(u32, @truncate(linux.openat(linux.AT.FDCWD, slug_z, .{ .DIRECTORY = true }, 0))));
    if (slug_fd < 0) return;
    defer _ = linux.close(slug_fd);

    var dent_buf: [8192]u8 = undefined;
    while (true) {
        const n = linux.getdents64(slug_fd, &dent_buf, dent_buf.len);
        const signed: isize = @bitCast(n);
        if (signed <= 0) break;

        var offset: usize = 0;
        while (offset < n) {
            const entry = @as(*align(1) const linux.dirent64, @ptrCast(&dent_buf[offset]));
            offset += entry.reclen;

            const name_ptr: [*:0]const u8 = @ptrCast(&entry.name);
            const name = std.mem.span(name_ptr);
            if (name.len == 0) continue;
            if (name[0] == '.' and (name.len == 1 or (name.len == 2 and name[1] == '.'))) continue;

            if (entry.type == linux.DT.REG or entry.type == linux.DT.UNKNOWN) {
                if (std.mem.endsWith(u8, name, ".jsonl")) {
                    const sid = name[0 .. name.len - 6];
                    const path = try std.fmt.allocPrint(alloc, "{s}/{s}", .{ slug_dir, name });
                    if (!mtimeOk(alloc, path, earliest)) {
                        alloc.free(path);
                        continue;
                    }
                    try addFile(alloc, map, slug, sid, path);
                }
            } else if (entry.type == linux.DT.DIR) {
                const sid = name;
                const subagents_dir = try std.fmt.allocPrint(alloc, "{s}/{s}/subagents", .{ slug_dir, sid });
                try scanSubagents(alloc, map, slug, sid, subagents_dir, earliest);
            }
        }
    }
}

fn scanSubagents(alloc: Allocator, map: *FileMap, slug: []const u8, sid: []const u8, subagents_dir: []const u8, earliest: f64) !void {
    const linux = platform.linux;
    const sub_z = try alloc.dupeZ(u8, subagents_dir);
    defer alloc.free(sub_z);
    const sub_fd: i32 = @bitCast(@as(u32, @truncate(linux.openat(linux.AT.FDCWD, sub_z, .{ .DIRECTORY = true }, 0))));
    if (sub_fd < 0) return;
    defer _ = linux.close(sub_fd);

    var dent_buf: [8192]u8 = undefined;
    while (true) {
        const n = linux.getdents64(sub_fd, &dent_buf, dent_buf.len);
        const signed: isize = @bitCast(n);
        if (signed <= 0) break;

        var offset: usize = 0;
        while (offset < n) {
            const entry = @as(*align(1) const linux.dirent64, @ptrCast(&dent_buf[offset]));
            offset += entry.reclen;

            if (entry.type != linux.DT.REG and entry.type != linux.DT.UNKNOWN) {
                continue;
            }
            const name_ptr: [*:0]const u8 = @ptrCast(&entry.name);
            const aname = std.mem.span(name_ptr);
            if (!std.mem.startsWith(u8, aname, "agent-")) continue;
            if (!std.mem.endsWith(u8, aname, ".jsonl")) continue;

            const path = try std.fmt.allocPrint(alloc, "{s}/{s}", .{ subagents_dir, aname });
            if (!mtimeOk(alloc, path, earliest)) {
                alloc.free(path);
                continue;
            }
            try addFile(alloc, map, slug, sid, path);
        }
    }
}

fn discoverWindows(alloc: Allocator, root_path: []const u8, earliest: f64) !FileMap {
    var map = FileMap.init(alloc);

    const slug_pattern = try std.fmt.allocPrint(alloc, "{s}\\*", .{root_path});
    const slug_entries = try findFilesWin(alloc, slug_pattern);

    for (slug_entries.items) |se| {
        if (se.dwFileAttributes & platform.FILE_ATTRIBUTE_DIRECTORY == 0) continue;
        const slug_w = std.mem.span(@as([*:0]const u16, @ptrCast(&se.cFileName)));
        if (slug_w.len == 0) continue;
        if (slug_w.len == 1 and slug_w[0] == '.') continue;
        if (slug_w.len == 2 and slug_w[0] == '.' and slug_w[1] == '.') continue;
        const slug = try std.unicode.utf16LeToUtf8Alloc(alloc, slug_w);
        const slug_dir = try std.fmt.allocPrint(alloc, "{s}\\{s}", .{ root_path, slug });

        {
            const pat = try std.fmt.allocPrint(alloc, "{s}\\*.jsonl", .{slug_dir});
            const entries = try findFilesWin(alloc, pat);
            for (entries.items) |e| {
                if (e.dwFileAttributes & platform.FILE_ATTRIBUTE_DIRECTORY != 0) continue;
                const name_w = std.mem.span(@as([*:0]const u16, @ptrCast(&e.cFileName)));
                const name = try std.unicode.utf16LeToUtf8Alloc(alloc, name_w);
                const sid = name[0 .. name.len - 6];
                const path = try std.fmt.allocPrint(alloc, "{s}\\{s}", .{ slug_dir, name });
                if (!mtimeOk(alloc, path, earliest)) {
                    alloc.free(path);
                    continue;
                }
                try addFile(alloc, &map, slug, sid, path);
            }
        }
        {
            const sess_pat = try std.fmt.allocPrint(alloc, "{s}\\*", .{slug_dir});
            const sess_entries = try findFilesWin(alloc, sess_pat);
            for (sess_entries.items) |se2| {
                if (se2.dwFileAttributes & platform.FILE_ATTRIBUTE_DIRECTORY == 0) continue;
                const sid_w = std.mem.span(@as([*:0]const u16, @ptrCast(&se2.cFileName)));
                if (sid_w.len == 0) continue;
                if (sid_w.len == 1 and sid_w[0] == '.') continue;
                if (sid_w.len == 2 and sid_w[0] == '.' and sid_w[1] == '.') continue;
                const sid = try std.unicode.utf16LeToUtf8Alloc(alloc, sid_w);
                const sub_pat = try std.fmt.allocPrint(alloc, "{s}\\{s}\\subagents\\agent-*.jsonl", .{ slug_dir, sid });
                const sub_entries = try findFilesWin(alloc, sub_pat);
                for (sub_entries.items) |ae| {
                    if (ae.dwFileAttributes & platform.FILE_ATTRIBUTE_DIRECTORY != 0) continue;
                    const aname_w = std.mem.span(@as([*:0]const u16, @ptrCast(&ae.cFileName)));
                    const aname = try std.unicode.utf16LeToUtf8Alloc(alloc, aname_w);
                    const path = try std.fmt.allocPrint(alloc, "{s}\\{s}\\subagents\\{s}", .{ slug_dir, sid, aname });
                    if (!mtimeOk(alloc, path, earliest)) {
                        alloc.free(path);
                        continue;
                    }
                    try addFile(alloc, &map, slug, sid, path);
                }
            }
        }
    }
    return map;
}

fn findFilesWin(alloc: Allocator, pat: []const u8) !std.ArrayList(platform.WIN32_FIND_DATAW) {
    var list: std.ArrayList(platform.WIN32_FIND_DATAW) = .empty;
    if (!is_windows) return list;
    const wpat = try std.unicode.utf8ToUtf16LeAllocZ(alloc, pat);
    var fd: platform.WIN32_FIND_DATAW = undefined;
    const h = platform.FindFirstFileW(wpat.ptr, &fd) orelse return list;
    if (h == platform.INVALID_HANDLE_VALUE) return list;
    defer _ = platform.FindClose(h);
    while (true) {
        try list.append(alloc, fd);
        if (platform.FindNextFileW(h, &fd) == 0) break;
    }
    return list;
}

// ─── Worker pool ─────────────────────────────────────────────────────────────

const Spinlock = struct {
    state: std.atomic.Value(u32) = .init(0),

    fn lock(self: *Spinlock) void {
        while (self.state.cmpxchgWeak(0, 1, .acquire, .monotonic) != null) {
            std.atomic.spinLoopHint();
        }
    }
    fn unlock(self: *Spinlock) void {
        self.state.store(0, .release);
    }
};

const Accum = struct {
    trailing: f64 = 0.0,
    window: f64 = 0.0,
    spin: Spinlock = .{},
    fn add(self: *Accum, p: Pair) void {
        self.spin.lock();
        defer self.spin.unlock();
        self.trailing += p.trailing;
        self.window += p.window;
    }
};

const Queue = struct {
    items: []const []const []const u8,
    cur: std.atomic.Value(usize),
    fn init(items: []const []const []const u8) Queue {
        return .{ .items = items, .cur = .init(0) };
    }
    fn pop(self: *Queue) ?[]const []const u8 {
        const i = self.cur.fetchAdd(1, .seq_cst);
        return if (i < self.items.len) self.items[i] else null;
    }
};

const Wctx = struct { q: *Queue, acc: *Accum, pc: f64, ws: f64 };

fn doWork(ctx: Wctx) void {
    var arena = std.heap.ArenaAllocator.init(std.heap.page_allocator);
    defer arena.deinit();
    while (ctx.q.pop()) |paths| {
        _ = arena.reset(.retain_capacity);
        ctx.acc.add(walkGroup(arena.allocator(), paths, ctx.pc, ctx.ws));
    }
}

// ─── main ────────────────────────────────────────────────────────────────────

pub fn main() !void {
    const gpa = std.heap.smp_allocator;
    var dispatch_arena = std.heap.ArenaAllocator.init(gpa);
    defer dispatch_arena.deinit();
    const dispatch_alloc = dispatch_arena.allocator();

    const argv = try getArgs(dispatch_alloc);
    // Subcommand routing: "cost", "beacons-latest", "beacons-history" route
    // to the matching impl. Bare flag invocation (first arg starts with "-")
    // or no args at all routes to cost mode for back-compat.
    const subcommand: []const u8 = blk: {
        if (argv.len == 0) break :blk "cost";
        const first = argv[0];
        if (std.mem.eql(u8, first, "cost")) break :blk "cost";
        if (std.mem.eql(u8, first, "beacons-latest")) break :blk "beacons-latest";
        if (std.mem.eql(u8, first, "beacons-history")) break :blk "beacons-history";
        if (std.mem.eql(u8, first, "search")) break :blk "search";
        if (first.len > 0 and first[0] == '-') break :blk "cost";
        std.debug.print("walker: unknown subcommand: {s}\n", .{first});
        std.process.exit(2);
    };
    const rest: [][]const u8 = if (std.mem.eql(u8, subcommand, "cost") and (argv.len == 0 or argv[0].len == 0 or argv[0][0] == '-'))
        argv
    else
        argv[1..];

    if (std.mem.eql(u8, subcommand, "beacons-latest")) {
        return beacons.runLatest(gpa, rest);
    }
    if (std.mem.eql(u8, subcommand, "beacons-history")) {
        return beacons.runHistory(gpa, rest);
    }
    if (std.mem.eql(u8, subcommand, "search")) {
        return search.run(gpa, rest);
    }
    return runCost(gpa, rest);
}

fn runCost(gpa: Allocator, args: [][]const u8) !void {
    const t0 = perfNow();
    const frq = perfFreq();

    var arena = std.heap.ArenaAllocator.init(gpa);
    defer arena.deinit();
    const alloc = arena.allocator();

    const cli = try parseCli(args);
    const now: f64 = cli.now orelse nowUnix();
    const pc = now - @as(f64, @floatFromInt(cli.period));
    const earliest = @min(pc, cli.win_start);

    const root = if (cli.root) |r| try alloc.dupe(u8, r) else try defaultRoot(alloc);

    var grp_map = try discover(alloc, root, earliest);

    const ngroups = grp_map.count();
    var nfiles: usize = 0;
    var grp_list: std.ArrayList([]const []const u8) = .empty;
    var vi = grp_map.valueIterator();
    while (vi.next()) |list| {
        nfiles += list.items.len;
        try grp_list.append(alloc, list.items);
    }

    var queue = Queue.init(grp_list.items);
    var accum = Accum{};

    const ncpu = std.Thread.getCpuCount() catch 4;
    const nw = @min(8, ncpu);
    var thr: std.ArrayList(std.Thread) = .empty;
    const wctx = Wctx{ .q = &queue, .acc = &accum, .pc = pc, .ws = cli.win_start };
    var k: usize = 0;
    while (k < nw) : (k += 1) try thr.append(alloc, try std.Thread.spawn(.{}, doWork, .{wctx}));
    for (thr.items) |th| th.join();

    const elapsed_ms: u64 = @intCast(@divTrunc((perfNow() - t0) * 1000, frq));

    var out_buf: [256]u8 = undefined;
    const out_str = std.fmt.bufPrint(&out_buf, "{{\"trailing_usd\":{d:.6},\"window_usd\":{d:.6},\"files_walked\":{d},\"groups\":{d},\"elapsed_ms\":{d}}}\n", .{ accum.trailing, accum.window, nfiles, ngroups, elapsed_ms }) catch "{}";
    writeStdout(out_str);
}
