// Native pace-walker -- Zig implementation.
// See ../SPEC.md for the contract every implementation must honor.
//
// Zig 0.16 completely overhauled the I/O subsystem. std.io, std.fs,
// std.process.argsWithAllocator, and std.time.nanoTimestamp are all gone.
// To avoid threading an Io context everywhere we call Win32 APIs directly
// for file I/O, directory traversal, stdout, and timing.

const std = @import("std");
const Allocator = std.mem.Allocator;

const VERSION = "zig/0.1.0";

// ─── Win32 types ─────────────────────────────────────────────────────────────

const HANDLE = *anyopaque;
const DWORD = u32;
const BOOL = i32;
const WCHAR = u16;
const ULONG = u32;
const LARGE_INTEGER_QUAD = i64;
const LARGE_INTEGER = extern union { parts: extern struct { lo: u32, hi: i32 }, quad: i64 };

const INVALID_HANDLE_VALUE: HANDLE = @ptrFromInt(@as(usize, @bitCast(@as(isize, -1))));

const FILETIME = extern struct {
    lo: u32 = 0,
    hi: u32 = 0,

    // Windows epoch to Unix epoch: 11644473600 s × 10^7 = 116444736000000000 hundred-ns
    fn toUnix(ft: FILETIME) f64 {
        const hns: i64 = (@as(i64, ft.hi) << 32) | @as(i64, ft.lo);
        return @as(f64, @floatFromInt(hns - 116_444_736_000_000_000)) / 10_000_000.0;
    }
};

const WIN32_FILE_ATTRIBUTE_DATA = extern struct {
    dwFileAttributes: u32,
    ftCreationTime: FILETIME,
    ftLastAccessTime: FILETIME,
    ftLastWriteTime: FILETIME,
    nFileSizeHigh: u32,
    nFileSizeLow: u32,
};

const WIN32_FIND_DATAW = extern struct {
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

// Win32 constants
const GENERIC_READ   = 0x80000000;
const FILE_SHARE_READ = 0x00000001;
const OPEN_EXISTING  = 3;
const FILE_ATTRIBUTE_NORMAL = 0x80;
const FILE_ATTRIBUTE_DIRECTORY = 0x10;
const STD_OUTPUT_HANDLE: u32 = @bitCast(@as(i32, -11));
const STD_ERROR_HANDLE:  u32 = @bitCast(@as(i32, -12));

extern "kernel32" fn GetSystemTimeAsFileTime(*FILETIME) callconv(.winapi) void;
extern "kernel32" fn QueryPerformanceCounter(*LARGE_INTEGER) callconv(.winapi) BOOL;
extern "kernel32" fn QueryPerformanceFrequency(*LARGE_INTEGER) callconv(.winapi) BOOL;
extern "kernel32" fn GetFileAttributesExW([*:0]const u16, u32, *WIN32_FILE_ATTRIBUTE_DATA) callconv(.winapi) BOOL;
extern "kernel32" fn GetCommandLineW() callconv(.winapi) [*:0]const u16;
extern "kernel32" fn GetEnvironmentVariableW([*:0]const u16, [*]u16, u32) callconv(.winapi) u32;
extern "kernel32" fn GetStdHandle(nStdHandle: u32) callconv(.winapi) ?HANDLE;
extern "kernel32" fn WriteFile(
    hFile: HANDLE, lpBuffer: *const anyopaque, nNumberOfBytesToWrite: u32,
    lpNumberOfBytesWritten: *u32, lpOverlapped: ?*anyopaque,
) callconv(.winapi) BOOL;
extern "kernel32" fn ReadFile(
    hFile: HANDLE, lpBuffer: *anyopaque, nNumberOfBytesToRead: u32,
    lpNumberOfBytesRead: *u32, lpOverlapped: ?*anyopaque,
) callconv(.winapi) BOOL;
extern "kernel32" fn CreateFileW(
    lpFileName: [*:0]const u16, dwDesiredAccess: u32, dwShareMode: u32,
    lpSecurityAttributes: ?*const SECURITY_ATTRIBUTES, dwCreationDisposition: u32,
    dwFlagsAndAttributes: u32, hTemplateFile: ?HANDLE,
) callconv(.winapi) ?HANDLE;
extern "kernel32" fn CloseHandle(hObject: HANDLE) callconv(.winapi) BOOL;
extern "kernel32" fn FindFirstFileW(lpFileName: [*:0]const u16, lpFindFileData: *WIN32_FIND_DATAW) callconv(.winapi) ?HANDLE;
extern "kernel32" fn FindNextFileW(hFindFile: HANDLE, lpFindFileData: *WIN32_FIND_DATAW) callconv(.winapi) BOOL;
extern "kernel32" fn FindClose(hFindFile: HANDLE) callconv(.winapi) BOOL;
extern "kernel32" fn GetLastError() callconv(.winapi) u32;

fn nowUnix() f64 {
    var ft: FILETIME = .{};
    GetSystemTimeAsFileTime(&ft);
    return ft.toUnix();
}

fn perfNow() i64 { var v: LARGE_INTEGER = undefined; _ = QueryPerformanceCounter(&v); return v.quad; }
fn perfFreq() i64 { var v: LARGE_INTEGER = undefined; _ = QueryPerformanceFrequency(&v); return v.quad; }

// ─── Stdout writer ────────────────────────────────────────────────────────────

const StdoutWriter = struct {
    handle: HANDLE,

    fn write(self: StdoutWriter, bytes: []const u8) void {
        var written: u32 = 0;
        var offset: usize = 0;
        while (offset < bytes.len) {
            const chunk: u32 = @intCast(@min(bytes.len - offset, 65536));
            _ = WriteFile(self.handle, bytes.ptr + offset, chunk, &written, null);
            offset += written;
            if (written == 0) break;
        }
    }
};

fn stdoutWriter() StdoutWriter {
    return .{ .handle = GetStdHandle(STD_OUTPUT_HANDLE).? };
}

/// Write all bytes to stdout handle.
fn writeStdout(handle: HANDLE, bytes: []const u8) void {
    var written: u32 = 0;
    var offset: usize = 0;
    while (offset < bytes.len) {
        const chunk: u32 = @intCast(@min(bytes.len - offset, 65536));
        const ptr: *const anyopaque = @ptrCast(bytes.ptr + offset);
        _ = WriteFile(handle, ptr, chunk, &written, null);
        if (written == 0) break;
        offset += written;
    }
}

// ─── Wide string helpers ──────────────────────────────────────────────────────

/// Convert UTF-8 path to null-terminated wide string for Win32 APIs.
fn toWide(alloc: Allocator, path: []const u8) ![*:0]const u16 {
    const w = try std.unicode.utf8ToUtf16LeAllocZ(alloc, path);
    return w.ptr;
}

/// Convert null-terminated wide string to UTF-8, owned by caller.
fn fromWideZ(alloc: Allocator, w: [*:0]const u16) ![]const u8 {
    return std.unicode.utf16LeToUtf8Alloc(alloc, std.mem.span(w));
}

// ─── Command-line parsing ─────────────────────────────────────────────────────

fn getArgs(alloc: Allocator) ![][]const u8 {
    const wcmd = GetCommandLineW();
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
            if (c == '"') { in_q = !in_q; i += 1; }
            else if (!in_q and (c == ' ' or c == '\t')) break
            else { try tok.append(alloc, c); i += 1; }
        }

        if (tok.items.len > 0 and !first)
            try out.append(alloc, try std.unicode.utf16LeToUtf8Alloc(alloc, tok.items));
        first = false;
        tok.deinit(alloc);
    }
    return out.toOwnedSlice(alloc);
}

fn getEnvVar(alloc: Allocator, name: []const u8) ?[]const u8 {
    var wn: [256:0]u16 = undefined;
    const n = std.unicode.utf8ToUtf16Le(wn[0..255], name) catch return null;
    wn[n] = 0;
    var wv: [32768]u16 = undefined;
    const len = GetEnvironmentVariableW(@ptrCast(&wn), &wv, @intCast(wv.len));
    if (len == 0) return null;
    return std.unicode.utf16LeToUtf8Alloc(alloc, wv[0..len]) catch null;
}

// ─── CLI ──────────────────────────────────────────────────────────────────────

const Cli = struct {
    period: u64 = 0,
    win_start: f64 = 0.0,
    now: ?f64 = null,
    root: ?[]const u8 = null,
};

fn parseCli(alloc: Allocator) !Cli {
    const argv = try getArgs(alloc);
    var cli = Cli{};
    var i: usize = 0;
    while (i < argv.len) {
        const flag = argv[i]; i += 1;
        if (std.mem.eql(u8, flag, "--period")) {
            cli.period = std.fmt.parseInt(u64, grab(argv, &i, "--period"), 10) catch die("--period: invalid");
        } else if (std.mem.eql(u8, flag, "--win-start")) {
            cli.win_start = std.fmt.parseFloat(f64, grab(argv, &i, "--win-start")) catch die("--win-start: invalid");
        } else if (std.mem.eql(u8, flag, "--now")) {
            cli.now = std.fmt.parseFloat(f64, grab(argv, &i, "--now")) catch die("--now: invalid");
        } else if (std.mem.eql(u8, flag, "--projects-root")) {
            cli.root = grab(argv, &i, "--projects-root");
        } else if (std.mem.eql(u8, flag, "--version")) {
            const h = GetStdHandle(STD_OUTPUT_HANDLE).?;
            writeStdout(h, VERSION ++ "\n");
            std.process.exit(0);
        } else { std.debug.print("walker: unknown flag: {s}\n", .{flag}); std.process.exit(2); }
    }
    if (cli.period == 0) die("--period is required");
    return cli;
}

fn grab(argv: [][]const u8, i: *usize, flag: []const u8) []const u8 {
    if (i.* >= argv.len) { std.debug.print("walker: {s} needs a value\n", .{flag}); std.process.exit(2); }
    defer i.* += 1;
    return argv[i.*];
}

fn die(msg: []const u8) noreturn { std.debug.print("walker: {s}\n", .{msg}); std.process.exit(2); }

// ─── Pricing ──────────────────────────────────────────────────────────────────

fn modelCost(inp: u64, out_: u64, cr: u64, cw: u64, model: []const u8) f64 {
    var buf: [256]u8 = undefined;
    const n = @min(model.len, buf.len);
    const lo = std.ascii.lowerString(buf[0..n], model[0..n]);
    const ir: f64 = if (std.mem.indexOf(u8, lo, "opus")  != null) 5.0
               else if (std.mem.indexOf(u8, lo, "haiku") != null) 1.0 else 3.0;
    const or_: f64 = if (std.mem.indexOf(u8, lo, "opus")  != null) 25.0
                else if (std.mem.indexOf(u8, lo, "haiku") != null) 5.0 else 15.0;
    return (@as(f64, @floatFromInt(inp)) * ir
          + @as(f64, @floatFromInt(cr))  * ir * 0.10
          + @as(f64, @floatFromInt(cw))  * ir * 1.25
          + @as(f64, @floatFromInt(out_)) * or_) / 1_000_000.0;
}

// ─── ISO 8601 ─────────────────────────────────────────────────────────────────

fn parseTs(s: []const u8) !f64 {
    if (s.len < 20 or s[s.len - 1] != 'Z') return error.Bad;
    if (s[4] != '-' or s[7] != '-' or s[10] != 'T' or s[13] != ':' or s[16] != ':')
        return error.Bad;
    const yr: i32 = try std.fmt.parseInt(i32, s[0..4],   10);
    const mo: u32 = try std.fmt.parseInt(u32, s[5..7],   10);
    const dy: u32 = try std.fmt.parseInt(u32, s[8..10],  10);
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

fn leap(y: i32) bool { return (@rem(y, 4) == 0 and @rem(y, 100) != 0) or @rem(y, 400) == 0; }
fn dim(m: u32, y: i32) i64 { return switch (m) { 1,3,5,7,8,10,12 => 31, 4,6,9,11 => 30, 2 => if (leap(y)) 29 else 28, else => 0 }; }
fn calToUnix(yr: i32, mo: u32, dy: u32, hr: u32, mn: u32, sc: u32) i64 {
    var d: i64 = 0;
    var y: i32 = 1970;
    if (yr >= 1970) { while (y < yr) : (y += 1) d += if (leap(y)) 366 else 365; }
    else            { while (y > yr) : (y -= 1) d -= if (leap(y-1)) 366 else 365; }
    var m: u32 = 1;
    while (m < mo) : (m += 1) d += dim(m, yr);
    d += @as(i64, dy) - 1;
    return d * 86400 + @as(i64, hr) * 3600 + @as(i64, mn) * 60 + @as(i64, sc);
}

// ─── File reading via Win32 ───────────────────────────────────────────────────

const WinFile = struct {
    handle: HANDLE,

    fn open(alloc: Allocator, path: []const u8) !WinFile {
        const wpath = try toWide(alloc, path);
        const h = CreateFileW(wpath, GENERIC_READ, FILE_SHARE_READ, null, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, null)
            orelse return error.OpenFailed;
        if (h == INVALID_HANDLE_VALUE) return error.OpenFailed;
        return .{ .handle = h };
    }

    fn close(self: WinFile) void { _ = CloseHandle(self.handle); }

    fn read(self: WinFile, buf: []u8) !usize {
        var n: u32 = 0;
        if (ReadFile(self.handle, buf.ptr, @intCast(buf.len), &n, null) == 0) return error.ReadFailed;
        return n;
    }
};

// ─── mtime filter ─────────────────────────────────────────────────────────────

fn mtimeOk(alloc: Allocator, path: []const u8, earliest: f64) bool {
    const wpath = toWide(alloc, path) catch return true;
    var info: WIN32_FILE_ATTRIBUTE_DATA = undefined;
    if (GetFileAttributesExW(wpath, 0, &info) == 0) return true;
    return info.ftLastWriteTime.toUnix() >= earliest;
}

// ─── JSON helpers ─────────────────────────────────────────────────────────────

fn ju64(obj: std.json.ObjectMap, key: []const u8) u64 {
    return switch (obj.get(key) orelse return 0) {
        .integer => |n| if (n >= 0) @intCast(n) else 0,
        .float   => |f| if (f >= 0.0) @intFromFloat(f) else 0,
        else => 0,
    };
}

// ─── Group walking ────────────────────────────────────────────────────────────

const Pair = struct { trailing: f64, window: f64 };

fn walkGroup(alloc: Allocator, paths: []const []const u8, pc: f64, ws: f64) Pair {
    const earliest = @min(pc, ws);
    var trailing: f64 = 0.0;
    var window:   f64 = 0.0;
    var seen = std.StringHashMap(void).init(alloc);
    defer { var it = seen.keyIterator(); while (it.next()) |k| alloc.free(k.*); seen.deinit(); }

    for (paths) |path| {
        const f = WinFile.open(alloc, path) catch continue;
        defer f.close();

        // Read the whole file into memory (JSONL files are typically <10 MB)
        var file_buf: std.ArrayList(u8) = .empty;
        defer file_buf.deinit(alloc);
        var read_buf: [65536]u8 = undefined;
        while (true) {
            const n = f.read(&read_buf) catch break;
            if (n == 0) break;
            file_buf.appendSlice(alloc, read_buf[0..n]) catch break;
        }

        var iter = std.mem.splitScalar(u8, file_buf.items, '\n');
        while (iter.next()) |line| {
            processLine(alloc, line, &seen, earliest, pc, ws, &trailing, &window);
        }
    }
    return .{ .trailing = trailing, .window = window };
}

fn processLine(
    alloc: Allocator, raw: []const u8,
    seen: *std.StringHashMap(void),
    earliest: f64, pc: f64, ws: f64,
    trailing: *f64, window: *f64,
) void {
    const line = std.mem.trim(u8, raw, " \t\r\n");
    if (line.len == 0) return;

    const parsed = std.json.parseFromSlice(
        std.json.Value, alloc, line, .{ .ignore_unknown_fields = true },
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
            seen.put(k, {}) catch { alloc.free(k); return; };
        }
    }

    const ts_v = root.object.get("timestamp") orelse return;
    if (ts_v != .string or ts_v.string.len == 0) return;
    const ts = parseTs(ts_v.string) catch return;
    if (ts < earliest) return;

    const mdl: []const u8 = if (msg.get("model")) |mv| (if (mv == .string) mv.string else "") else "";
    var inp: u64 = 0; var out_: u64 = 0; var cr: u64 = 0; var cw: u64 = 0;
    if (msg.get("usage")) |uv| { if (uv == .object) {
        inp  = ju64(uv.object, "input_tokens");
        out_ = ju64(uv.object, "output_tokens");
        cr   = ju64(uv.object, "cache_read_input_tokens");
        cw   = ju64(uv.object, "cache_creation_input_tokens");
    }}

    const c = modelCost(inp, out_, cr, cw, mdl);
    if (ts >= pc) trailing.* += c;
    if (ts >= ws) window.*   += c;
}

// ─── Discovery ────────────────────────────────────────────────────────────────

const FileMap = std.StringHashMap(std.ArrayList([]const u8));

fn addFile(alloc: Allocator, map: *FileMap, slug: []const u8, sid: []const u8, path: []const u8) !void {
    const key = try std.fmt.allocPrint(alloc, "{s}\x00{s}", .{ slug, sid });
    const gop = try map.getOrPut(key);
    if (!gop.found_existing) { gop.value_ptr.* = .empty; } else alloc.free(key);
    try gop.value_ptr.*.append(alloc, path);
}

fn defaultRoot(alloc: Allocator) ![]const u8 {
    if (getEnvVar(alloc, "USERPROFILE")) |up| {
        defer alloc.free(up);
        return std.fs.path.join(alloc, &.{ up, ".claude", "projects" });
    }
    if (getEnvVar(alloc, "HOME")) |home| {
        defer alloc.free(home);
        return std.fs.path.join(alloc, &.{ home, ".claude", "projects" });
    }
    return alloc.dupe(u8, ".claude/projects");
}

/// List directory entries matching a pattern via FindFirstFileW/FindNextFileW.
/// pattern = absolute path with wildcard at the end, e.g. "C:\foo\*"
fn findFiles(alloc: Allocator, pattern: []const u8) !std.ArrayList(WIN32_FIND_DATAW) {
    var list: std.ArrayList(WIN32_FIND_DATAW) = .empty;
    const wpat = try toWide(alloc, pattern);
    var fd: WIN32_FIND_DATAW = undefined;
    const h = FindFirstFileW(wpat, &fd) orelse return list;
    if (h == INVALID_HANDLE_VALUE) return list;
    defer _ = FindClose(h);

    while (true) {
        try list.append(alloc, fd);
        if (FindNextFileW(h, &fd) == 0) break;
    }
    return list;
}

fn discover(alloc: Allocator, root_path: []const u8, earliest: f64) !FileMap {
    var map = FileMap.init(alloc);

    // List slug directories
    const slug_pattern = try std.fmt.allocPrint(alloc, "{s}\\*", .{root_path});
    const slug_entries = try findFiles(alloc, slug_pattern);

    for (slug_entries.items) |se| {
        if (se.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY == 0) continue;
        const slug_w = std.mem.span(@as([*:0]const u16, @ptrCast(&se.cFileName)));
        // Skip . and ..
        if (slug_w.len == 0) continue;
        if (slug_w.len == 1 and slug_w[0] == '.') continue;
        if (slug_w.len == 2 and slug_w[0] == '.' and slug_w[1] == '.') continue;
        const slug = try std.unicode.utf16LeToUtf8Alloc(alloc, slug_w);

        const slug_dir = try std.fmt.allocPrint(alloc, "{s}\\{s}", .{ root_path, slug });

        // Parents: slug_dir\*.jsonl
        {
            const pat = try std.fmt.allocPrint(alloc, "{s}\\*.jsonl", .{slug_dir});
            const entries = try findFiles(alloc, pat);
            for (entries.items) |e| {
                if (e.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY != 0) continue;
                const name_w = std.mem.span(@as([*:0]const u16, @ptrCast(&e.cFileName)));
                const name = try std.unicode.utf16LeToUtf8Alloc(alloc, name_w);
                // name ends with .jsonl (FindFirstFileW with *.jsonl guarantees this)
                const sid = name[0 .. name.len - 6];
                const path = try std.fmt.allocPrint(alloc, "{s}\\{s}", .{ slug_dir, name });
                if (!mtimeOk(alloc, path, earliest)) { alloc.free(path); continue; }
                try addFile(alloc, &map, slug, sid, path);
            }
        }

        // Session dirs for subagents
        {
            const sess_pat = try std.fmt.allocPrint(alloc, "{s}\\*", .{slug_dir});
            const sess_entries = try findFiles(alloc, sess_pat);
            for (sess_entries.items) |se2| {
                if (se2.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY == 0) continue;
                const sid_w = std.mem.span(@as([*:0]const u16, @ptrCast(&se2.cFileName)));
                if (sid_w.len == 0) continue;
                if (sid_w.len == 1 and sid_w[0] == '.') continue;
                if (sid_w.len == 2 and sid_w[0] == '.' and sid_w[1] == '.') continue;
                const sid = try std.unicode.utf16LeToUtf8Alloc(alloc, sid_w);
                const sub_pat = try std.fmt.allocPrint(alloc, "{s}\\{s}\\subagents\\agent-*.jsonl", .{ slug_dir, sid });
                const sub_entries = try findFiles(alloc, sub_pat);
                for (sub_entries.items) |ae| {
                    if (ae.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY != 0) continue;
                    const aname_w = std.mem.span(@as([*:0]const u16, @ptrCast(&ae.cFileName)));
                    const aname = try std.unicode.utf16LeToUtf8Alloc(alloc, aname_w);
                    const path = try std.fmt.allocPrint(alloc, "{s}\\{s}\\subagents\\{s}", .{ slug_dir, sid, aname });
                    if (!mtimeOk(alloc, path, earliest)) { alloc.free(path); continue; }
                    try addFile(alloc, &map, slug, sid, path);
                }
            }
        }
    }
    return map;
}

// ─── Worker pool ──────────────────────────────────────────────────────────────

// Simple spinlock since std.Thread.Mutex was removed in Zig 0.16
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
    window:   f64 = 0.0,
    spin: Spinlock = .{},
    fn add(self: *Accum, p: Pair) void { self.spin.lock(); defer self.spin.unlock(); self.trailing += p.trailing; self.window += p.window; }
};

const Queue = struct {
    items: []const []const []const u8,
    cur:   std.atomic.Value(usize),
    fn init(items: []const []const []const u8) Queue { return .{ .items = items, .cur = .init(0) }; }
    fn pop(self: *Queue) ?[]const []const u8 { const i = self.cur.fetchAdd(1, .seq_cst); return if (i < self.items.len) self.items[i] else null; }
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

// ─── main ─────────────────────────────────────────────────────────────────────

pub fn main() !void {
    const t0  = perfNow();
    const frq = perfFreq();

    const gpa = std.heap.smp_allocator;
    var arena = std.heap.ArenaAllocator.init(gpa);
    defer arena.deinit();
    const alloc = arena.allocator();

    const cli = try parseCli(alloc);
    const now: f64 = cli.now orelse nowUnix();
    const pc = now - @as(f64, @floatFromInt(cli.period));
    const earliest = @min(pc, cli.win_start);

    const root = if (cli.root) |r| try alloc.dupe(u8, r) else try defaultRoot(alloc);

    var grp_map = try discover(alloc, root, earliest);

    const ngroups = grp_map.count();
    var nfiles: usize = 0;
    var grp_list: std.ArrayList([]const []const u8) = .empty;
    var vi = grp_map.valueIterator();
    while (vi.next()) |list| { nfiles += list.items.len; try grp_list.append(alloc, list.items); }

    var queue = Queue.init(grp_list.items);
    var accum = Accum{};

    const ncpu = std.Thread.getCpuCount() catch 4;
    const nw   = @min(8, ncpu);
    var thr: std.ArrayList(std.Thread) = .empty;
    const wctx = Wctx{ .q = &queue, .acc = &accum, .pc = pc, .ws = cli.win_start };
    var k: usize = 0;
    while (k < nw) : (k += 1) try thr.append(alloc, try std.Thread.spawn(.{}, doWork, .{wctx}));
    for (thr.items) |th| th.join();

    const elapsed_ms: u64 = @intCast(@divTrunc((perfNow() - t0) * 1000, frq));

    var out_buf: [256]u8 = undefined;
    const out_str = std.fmt.bufPrint(&out_buf,
        "{{\"trailing_usd\":{d:.6},\"window_usd\":{d:.6},\"files_walked\":{d},\"groups\":{d},\"elapsed_ms\":{d}}}\n",
        .{ accum.trailing, accum.window, nfiles, ngroups, elapsed_ms },
    ) catch "{}";
    const stdout_h = GetStdHandle(STD_OUTPUT_HANDLE).?;
    writeStdout(stdout_h, out_str);
}
