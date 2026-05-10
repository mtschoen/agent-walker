// Beacon-mode subcommands: beacons-latest and beacons-history.
// See ../SPEC.md "Subcommands" for the contract.

const std = @import("std");
const builtin = @import("builtin");
const Allocator = std.mem.Allocator;
const main = @import("main.zig");

const is_windows = main.is_windows;
const PATH_SEP = main.PATH_SEP;

// ─── Beacon shape ────────────────────────────────────────────────────────────

const Beacon = struct {
    kind: []const u8,
    eta_seconds: f64,
    summary: []const u8,
    drift: []const u8,
    beats_left: ?i64 = null,

    fn writeJson(self: Beacon, w: *Buf) !void {
        try w.appendStr("{\"kind\":");
        try writeJsonString(w, self.kind);
        try w.appendStr(",\"eta_seconds\":");
        try writeJsonNumber(w, self.eta_seconds);
        try w.appendStr(",\"summary\":");
        try writeJsonString(w, self.summary);
        try w.appendStr(",\"drift\":");
        try writeJsonString(w, self.drift);
        if (self.beats_left) |bl| {
            try w.appendFmt(",\"beats_left\":{d}", .{bl});
        }
        try w.appendStr("}");
    }
};

const Buf = struct {
    list: std.ArrayList(u8) = .empty,
    alloc: Allocator,

    fn appendStr(self: *Buf, s: []const u8) !void {
        try self.list.appendSlice(self.alloc, s);
    }
    fn appendByte(self: *Buf, b: u8) !void {
        try self.list.append(self.alloc, b);
    }
    fn appendFmt(self: *Buf, comptime fmt: []const u8, args: anytype) !void {
        var tmp: [128]u8 = undefined;
        const n = try std.fmt.bufPrint(&tmp, fmt, args);
        try self.list.appendSlice(self.alloc, n);
    }
    fn items(self: *const Buf) []const u8 {
        return self.list.items;
    }
};

fn writeJsonString(w: *Buf, s: []const u8) !void {
    try w.appendStr("\"");
    var i: usize = 0;
    while (i < s.len) : (i += 1) {
        const c = s[i];
        switch (c) {
            '"' => try w.appendStr("\\\""),
            '\\' => try w.appendStr("\\\\"),
            '\n' => try w.appendStr("\\n"),
            '\r' => try w.appendStr("\\r"),
            '\t' => try w.appendStr("\\t"),
            8 => try w.appendStr("\\b"),
            12 => try w.appendStr("\\f"),
            else => {
                if (c < 0x20) {
                    try w.appendFmt("\\u{x:0>4}", .{c});
                } else {
                    try w.appendByte(c);
                }
            },
        }
    }
    try w.appendStr("\"");
}

fn writeJsonNumber(w: *Buf, v: f64) !void {
    // Match Rust serde_json: integral floats print without decimal point.
    if (std.math.isFinite(v) and @floor(v) == v and @abs(v) < 1e16) {
        try w.appendFmt("{d}", .{@as(i64, @intFromFloat(v))});
    } else {
        try w.appendFmt("{d}", .{v});
    }
}

// ─── progress-beacon matcher (bespoke, regex-free) ───────────────────────────
//
// Pattern: <progress-beacon>\s*({...})\s*</progress-beacon>
// Returns the byte slice of the inner JSON (matched-brace based, NOT ".*?"
// because the JSON body itself can contain '}' inside string literals).
// The Rust impl uses a non-greedy `\{.*?\}` which works for current corpora
// because the JSON body is a single flat object with no nested '{' or
// inline-string-escaped '{'. We match that semantics.

const OPEN_TAG = "<progress-beacon>";
const CLOSE_TAG = "</progress-beacon>";

const BeaconMatch = struct {
    json: []const u8, // slice into the source text
};

const MatchIter = struct {
    text: []const u8,
    pos: usize = 0,

    fn next(self: *MatchIter) ?BeaconMatch {
        while (self.pos < self.text.len) {
            const open_rel = std.mem.indexOf(u8, self.text[self.pos..], OPEN_TAG) orelse return null;
            const open_at = self.pos + open_rel;
            var inner_start = open_at + OPEN_TAG.len;
            // Skip whitespace
            while (inner_start < self.text.len and std.ascii.isWhitespace(self.text[inner_start])) inner_start += 1;
            if (inner_start >= self.text.len or self.text[inner_start] != '{') {
                self.pos = open_at + OPEN_TAG.len;
                continue;
            }
            // Find first '}' that is followed (after optional whitespace) by CLOSE_TAG.
            // We use '}' not balanced-brace because the Rust regex `\{.*?\}` is
            // non-greedy on the FIRST closing brace. For current beacon shapes
            // (flat JSON, no nested objects, no string-escaped '}') this is OK.
            var scan: usize = inner_start + 1;
            var matched_close: ?usize = null;
            while (scan < self.text.len) : (scan += 1) {
                if (self.text[scan] == '}') {
                    var after = scan + 1;
                    while (after < self.text.len and std.ascii.isWhitespace(self.text[after])) after += 1;
                    if (std.mem.startsWith(u8, self.text[after..], CLOSE_TAG)) {
                        matched_close = scan;
                        self.pos = after + CLOSE_TAG.len;
                        break;
                    }
                }
            }
            if (matched_close) |c| {
                return .{ .json = self.text[inner_start .. c + 1] };
            }
            // No close found; bail.
            return null;
        }
        return null;
    }
};

fn parseBeaconJson(alloc: Allocator, json_src: []const u8) ?Beacon {
    const parsed = std.json.parseFromSlice(
        std.json.Value,
        alloc,
        json_src,
        .{ .ignore_unknown_fields = true },
    ) catch return null;
    defer parsed.deinit();
    const root = parsed.value;
    if (root != .object) return null;
    const obj = root.object;

    const kind_v = obj.get("kind") orelse return null;
    if (kind_v != .string) return null;

    const eta_v = obj.get("eta_seconds") orelse return null;
    const eta: f64 = switch (eta_v) {
        .integer => |n| @floatFromInt(n),
        .float => |f| f,
        .number_string => |s| std.fmt.parseFloat(f64, s) catch return null,
        else => return null,
    };

    const summary_v = obj.get("summary") orelse return null;
    if (summary_v != .string) return null;

    const drift_v = obj.get("drift") orelse return null;
    if (drift_v != .string) return null;

    var beats_left: ?i64 = null;
    if (obj.get("beats_left")) |bl_v| {
        switch (bl_v) {
            .integer => |n| beats_left = n,
            .float => |f| beats_left = @intFromFloat(f),
            else => {},
        }
    }

    return Beacon{
        .kind = alloc.dupe(u8, kind_v.string) catch return null,
        .eta_seconds = eta,
        .summary = alloc.dupe(u8, summary_v.string) catch return null,
        .drift = alloc.dupe(u8, drift_v.string) catch return null,
        .beats_left = beats_left,
    };
}

// ─── transcript scanning ─────────────────────────────────────────────────────

const Found = struct {
    beacon: Beacon,
    ts: f64,
};

/// Walk one transcript file. For each assistant entry, parse the LAST
/// well-formed beacon embedded in its text content. Track the entry with
/// the highest timestamp.
fn findLatestInPath(alloc: Allocator, path: []const u8) ?Found {
    const data = main.readEntireFile(alloc, path) catch return null;
    defer alloc.free(data);

    var latest: ?Found = null;
    var iter = std.mem.splitScalar(u8, data, '\n');
    while (iter.next()) |raw| {
        if (parseEntryBeacon(alloc, raw, .last)) |found| {
            if (latest == null or found.ts >= latest.?.ts) {
                latest = found;
            }
        }
    }
    return latest;
}

/// Walk one transcript file and append ALL well-formed beacons paired
/// with their entry timestamp.
fn findAllInPath(alloc: Allocator, path: []const u8, out: *std.ArrayList(Found)) void {
    const data = main.readEntireFile(alloc, path) catch return;
    defer alloc.free(data);

    var iter = std.mem.splitScalar(u8, data, '\n');
    while (iter.next()) |raw| {
        appendEntryBeacons(alloc, raw, out);
    }
}

const PickMode = enum { last, all };

/// Parse one JSONL line, extract assistant message timestamp + concatenated
/// text-block content, and return the LAST well-formed beacon (or null).
fn parseEntryBeacon(alloc: Allocator, raw: []const u8, mode: PickMode) ?Found {
    _ = mode;
    const line = std.mem.trim(u8, raw, " \t\r\n");
    if (line.len == 0) return null;

    const ts_text = extractAssistantTimestampAndText(alloc, line) orelse return null;
    defer alloc.free(ts_text.text);

    var it = MatchIter{ .text = ts_text.text };
    var last_ok: ?Beacon = null;
    while (it.next()) |m| {
        if (parseBeaconJson(alloc, m.json)) |b| {
            // Free previous if any (we replace).
            if (last_ok) |prev| freeBeacon(alloc, prev);
            last_ok = b;
        }
    }
    if (last_ok) |b| return Found{ .beacon = b, .ts = ts_text.ts };
    return null;
}

fn appendEntryBeacons(alloc: Allocator, raw: []const u8, out: *std.ArrayList(Found)) void {
    const line = std.mem.trim(u8, raw, " \t\r\n");
    if (line.len == 0) return;

    const ts_text = extractAssistantTimestampAndText(alloc, line) orelse return;
    defer alloc.free(ts_text.text);

    var it = MatchIter{ .text = ts_text.text };
    while (it.next()) |m| {
        if (parseBeaconJson(alloc, m.json)) |b| {
            out.append(alloc, .{ .beacon = b, .ts = ts_text.ts }) catch {
                freeBeacon(alloc, b);
                return;
            };
        }
    }
}

const TsText = struct {
    ts: f64,
    text: []u8, // owned, joined "\n" of text-block contents
};

fn extractAssistantTimestampAndText(alloc: Allocator, line: []const u8) ?TsText {
    const parsed = std.json.parseFromSlice(
        std.json.Value,
        alloc,
        line,
        .{ .ignore_unknown_fields = true },
    ) catch return null;
    defer parsed.deinit();

    const root = parsed.value;
    if (root != .object) return null;

    const msg_v = root.object.get("message") orelse return null;
    if (msg_v != .object) return null;
    const msg = msg_v.object;

    const role = msg.get("role") orelse return null;
    if (role != .string or !std.mem.eql(u8, role.string, "assistant")) return null;

    const ts_v = root.object.get("timestamp") orelse return null;
    if (ts_v != .string or ts_v.string.len == 0) return null;
    const ts = main.parseTs(ts_v.string) catch return null;

    const content_v = msg.get("content") orelse return null;
    if (content_v != .array) return null;

    var buf: std.ArrayList(u8) = .empty;
    var first = true;
    for (content_v.array.items) |block| {
        if (block != .object) continue;
        const tv = block.object.get("type") orelse continue;
        if (tv != .string or !std.mem.eql(u8, tv.string, "text")) continue;
        const txt_v = block.object.get("text") orelse continue;
        if (txt_v != .string) continue;
        if (!first) buf.append(alloc, '\n') catch return null;
        first = false;
        buf.appendSlice(alloc, txt_v.string) catch return null;
    }
    const owned = buf.toOwnedSlice(alloc) catch return null;
    return TsText{ .ts = ts, .text = owned };
}

fn freeBeacon(alloc: Allocator, b: Beacon) void {
    alloc.free(b.kind);
    alloc.free(b.summary);
    alloc.free(b.drift);
}

// ─── beacons-latest ──────────────────────────────────────────────────────────

const LatestArgs = struct {
    session_id: []const u8 = "",
    projects_root: ?[]const u8 = null,
    now_unix: ?f64 = null,
};

fn parseLatestArgs(args: [][]const u8) !LatestArgs {
    var out = LatestArgs{};
    var i: usize = 0;
    while (i < args.len) {
        const flag = args[i];
        i += 1;
        if (std.mem.eql(u8, flag, "--session-id")) {
            out.session_id = main.grab(args, &i, "--session-id");
        } else if (std.mem.eql(u8, flag, "--projects-root")) {
            out.projects_root = main.grab(args, &i, "--projects-root");
        } else if (std.mem.eql(u8, flag, "--now")) {
            out.now_unix = std.fmt.parseFloat(f64, main.grab(args, &i, "--now")) catch main.die("--now: invalid");
        } else {
            std.debug.print("walker: beacons-latest: unknown flag: {s}\n", .{flag});
            std.process.exit(2);
        }
    }
    if (out.session_id.len == 0) main.die("beacons-latest: --session-id is required");
    return out;
}

pub fn runLatest(gpa: Allocator, args: [][]const u8) !void {
    const t0 = main.perfNow();
    const frq = main.perfFreq();

    var arena = std.heap.ArenaAllocator.init(gpa);
    defer arena.deinit();
    const alloc = arena.allocator();

    const parsed = try parseLatestArgs(args);
    const root = if (parsed.projects_root) |r| try alloc.dupe(u8, r) else try main.defaultRoot(alloc);
    const now_unix: f64 = parsed.now_unix orelse main.nowUnix();

    // Discover candidate transcript paths matching the session-id.
    const paths = try findSessionPaths(alloc, root, parsed.session_id);

    var best: ?Found = null;
    for (paths.items) |p| {
        if (findLatestInPath(alloc, p)) |f| {
            if (best == null or f.ts >= best.?.ts) best = f;
        }
    }

    const elapsed_ms: u64 = @intCast(@divTrunc((main.perfNow() - t0) * 1000, frq));

    var w = Buf{ .alloc = alloc };
    try w.appendStr("{\"beacon\":");
    if (best) |f| {
        try f.beacon.writeJson(&w);
        try w.appendStr(",\"emitted_at\":");
        try writeJsonNumber(&w, f.ts);
        try w.appendStr(",\"age_seconds\":");
        try writeJsonNumber(&w, now_unix - f.ts);
    } else {
        try w.appendStr("null,\"emitted_at\":null,\"age_seconds\":null");
    }
    try w.appendFmt(",\"elapsed_ms\":{d}", .{elapsed_ms});
    try w.appendStr("}\n");
    main.writeStdout(w.items());
}

// ─── beacons-history ─────────────────────────────────────────────────────────

const HistoryArgs = struct {
    period_seconds: u64 = 0,
    win_start_unix: f64 = 0.0,
    projects_root: ?[]const u8 = null,
    now_unix: ?f64 = null,
};

fn parseHistoryArgs(args: [][]const u8) !HistoryArgs {
    var out = HistoryArgs{};
    var got_period = false;
    var i: usize = 0;
    while (i < args.len) {
        const flag = args[i];
        i += 1;
        if (std.mem.eql(u8, flag, "--period")) {
            out.period_seconds = std.fmt.parseInt(u64, main.grab(args, &i, "--period"), 10) catch main.die("--period: invalid");
            got_period = true;
        } else if (std.mem.eql(u8, flag, "--win-start")) {
            out.win_start_unix = std.fmt.parseFloat(f64, main.grab(args, &i, "--win-start")) catch main.die("--win-start: invalid");
        } else if (std.mem.eql(u8, flag, "--projects-root")) {
            out.projects_root = main.grab(args, &i, "--projects-root");
        } else if (std.mem.eql(u8, flag, "--now")) {
            out.now_unix = std.fmt.parseFloat(f64, main.grab(args, &i, "--now")) catch main.die("--now: invalid");
        } else {
            std.debug.print("walker: beacons-history: unknown flag: {s}\n", .{flag});
            std.process.exit(2);
        }
    }
    if (!got_period) main.die("beacons-history: --period is required");
    return out;
}

pub fn runHistory(gpa: Allocator, args: [][]const u8) !void {
    const t0 = main.perfNow();
    const frq = main.perfFreq();

    var arena = std.heap.ArenaAllocator.init(gpa);
    defer arena.deinit();
    const alloc = arena.allocator();

    const parsed = try parseHistoryArgs(args);
    const root = if (parsed.projects_root) |r| try alloc.dupe(u8, r) else try main.defaultRoot(alloc);
    const now_unix: f64 = parsed.now_unix orelse main.nowUnix();
    const period_cutoff = now_unix - @as(f64, @floatFromInt(parsed.period_seconds));
    const window_lo = @max(period_cutoff, parsed.win_start_unix);

    // Discover ALL groups (no mtime filter — beacon timestamps drive inclusion).
    var grp_map = try main.discover(alloc, root, -std.math.inf(f64));

    const session_count = grp_map.count();

    var pairs: std.ArrayList(Pair) = .empty;
    var vi = grp_map.valueIterator();
    while (vi.next()) |list| {
        var all: std.ArrayList(Found) = .empty;
        for (list.items) |p| {
            findAllInPath(alloc, p, &all);
        }
        // Filter to beacons inside the window.
        var begin: ?Found = null;
        var end: ?Found = null;
        for (all.items) |f| {
            if (f.ts < window_lo) continue;
            if (std.mem.eql(u8, f.beacon.kind, "begin")) {
                if (begin == null or f.ts < begin.?.ts) begin = f;
            } else if (std.mem.eql(u8, f.beacon.kind, "end")) {
                if (end == null or f.ts > end.?.ts) end = f;
            }
        }
        if (begin != null and end != null) {
            const b = begin.?;
            const e = end.?;
            if (e.ts > b.ts) {
                try pairs.append(alloc, .{ .begin_eta = b.beacon.eta_seconds, .actual_elapsed = e.ts - b.ts });
            }
        }
    }

    const bias = biasFactor(alloc, pairs.items);

    const elapsed_ms: u64 = @intCast(@divTrunc((main.perfNow() - t0) * 1000, frq));

    var w = Buf{ .alloc = alloc };
    try w.appendStr("{\"pairs\":[");
    for (pairs.items, 0..) |p, idx| {
        if (idx > 0) try w.appendStr(",");
        try w.appendStr("{\"begin_eta\":");
        try writeJsonNumber(&w, p.begin_eta);
        try w.appendStr(",\"actual_elapsed\":");
        try writeJsonNumber(&w, p.actual_elapsed);
        try w.appendStr("}");
    }
    try w.appendFmt("],\"session_count\":{d},\"n_pairs\":{d},\"bias_factor\":", .{ session_count, pairs.items.len });
    if (bias) |b| {
        try writeJsonNumber(&w, b);
    } else {
        try w.appendStr("null");
    }
    try w.appendFmt(",\"elapsed_ms\":{d}", .{elapsed_ms});
    try w.appendStr("}\n");
    main.writeStdout(w.items());
}

const Pair = struct { begin_eta: f64, actual_elapsed: f64 };

fn biasFactor(alloc: Allocator, pairs: []const Pair) ?f64 {
    if (pairs.len == 0) return null;
    var ratios: std.ArrayList(f64) = .empty;
    defer ratios.deinit(alloc);
    for (pairs) |p| {
        if (p.begin_eta > 0) {
            ratios.append(alloc, p.actual_elapsed / p.begin_eta) catch return null;
        }
    }
    if (ratios.items.len == 0) return null;
    std.mem.sort(f64, ratios.items, {}, std.sort.asc(f64));
    const n = ratios.items.len;
    if (n % 2 == 1) return ratios.items[n / 2];
    return (ratios.items[n / 2 - 1] + ratios.items[n / 2]) / 2.0;
}

// ─── session-id path discovery (beacons-latest) ──────────────────────────────
//
// Find files matching either:
//   <root>/<slug>/<sid>.jsonl                           (parent transcript)
//   <root>/<slug>/<sess>/subagents/agent-<sid>.jsonl    (subagent transcript)

fn findSessionPaths(alloc: Allocator, root: []const u8, sid: []const u8) !std.ArrayList([]const u8) {
    var out: std.ArrayList([]const u8) = .empty;
    if (is_windows) {
        try findSessionPathsWindows(alloc, &out, root, sid);
    } else {
        try findSessionPathsLinux(alloc, &out, root, sid);
    }
    return out;
}

fn findSessionPathsWindows(alloc: Allocator, out: *std.ArrayList([]const u8), root: []const u8, sid: []const u8) !void {
    const platform = main.platform;
    // Iterate slug dirs under root.
    const slug_pattern = try std.fmt.allocPrint(alloc, "{s}\\*", .{root});
    var fd: platform.WIN32_FIND_DATAW = undefined;
    const wpat = try std.unicode.utf8ToUtf16LeAllocZ(alloc, slug_pattern);
    const h = platform.FindFirstFileW(wpat.ptr, &fd) orelse return;
    if (h == platform.INVALID_HANDLE_VALUE) return;
    defer _ = platform.FindClose(h);

    while (true) {
        const is_dir = (fd.dwFileAttributes & platform.FILE_ATTRIBUTE_DIRECTORY) != 0;
        if (is_dir) {
            const name_w = std.mem.span(@as([*:0]const u16, @ptrCast(&fd.cFileName)));
            if (!(name_w.len == 0 or
                (name_w.len == 1 and name_w[0] == '.') or
                (name_w.len == 2 and name_w[0] == '.' and name_w[1] == '.')))
            {
                const slug = try std.unicode.utf16LeToUtf8Alloc(alloc, name_w);
                const slug_dir = try std.fmt.allocPrint(alloc, "{s}\\{s}", .{ root, slug });

                // Parent: <slug_dir>/<sid>.jsonl
                const parent_path = try std.fmt.allocPrint(alloc, "{s}\\{s}.jsonl", .{ slug_dir, sid });
                if (fileExists(alloc, parent_path)) {
                    try out.append(alloc, parent_path);
                } else alloc.free(parent_path);

                // Subagents: enumerate session dirs and look for agent-<sid>.jsonl
                try findSubagentsForSidWindows(alloc, out, slug_dir, sid);
            }
        }
        if (platform.FindNextFileW(h, &fd) == 0) break;
    }
}

fn findSubagentsForSidWindows(alloc: Allocator, out: *std.ArrayList([]const u8), slug_dir: []const u8, sid: []const u8) !void {
    const platform = main.platform;
    const sess_pat = try std.fmt.allocPrint(alloc, "{s}\\*", .{slug_dir});
    var fd: platform.WIN32_FIND_DATAW = undefined;
    const wpat = try std.unicode.utf8ToUtf16LeAllocZ(alloc, sess_pat);
    const h = platform.FindFirstFileW(wpat.ptr, &fd) orelse return;
    if (h == platform.INVALID_HANDLE_VALUE) return;
    defer _ = platform.FindClose(h);

    while (true) {
        const is_dir = (fd.dwFileAttributes & platform.FILE_ATTRIBUTE_DIRECTORY) != 0;
        if (is_dir) {
            const name_w = std.mem.span(@as([*:0]const u16, @ptrCast(&fd.cFileName)));
            if (!(name_w.len == 0 or
                (name_w.len == 1 and name_w[0] == '.') or
                (name_w.len == 2 and name_w[0] == '.' and name_w[1] == '.')))
            {
                const sess = try std.unicode.utf16LeToUtf8Alloc(alloc, name_w);
                const candidate = try std.fmt.allocPrint(alloc, "{s}\\{s}\\subagents\\agent-{s}.jsonl", .{ slug_dir, sess, sid });
                if (fileExists(alloc, candidate)) {
                    try out.append(alloc, candidate);
                } else alloc.free(candidate);
            }
        }
        if (platform.FindNextFileW(h, &fd) == 0) break;
    }
}

fn findSessionPathsLinux(alloc: Allocator, out: *std.ArrayList([]const u8), root: []const u8, sid: []const u8) !void {
    const platform = main.platform;
    const linux = platform.linux;
    const root_z = try alloc.dupeZ(u8, root);
    defer alloc.free(root_z);
    const root_fd_ret = linux.openat(linux.AT.FDCWD, root_z, .{ .DIRECTORY = true }, 0);
    const root_fd: i32 = @bitCast(@as(u32, @truncate(root_fd_ret)));
    if (root_fd < 0) return;
    defer _ = linux.close(root_fd);

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

            const slug_dir = try std.fmt.allocPrint(alloc, "{s}/{s}", .{ root, slug });

            const parent_path = try std.fmt.allocPrint(alloc, "{s}/{s}.jsonl", .{ slug_dir, sid });
            if (fileExists(alloc, parent_path)) {
                try out.append(alloc, parent_path);
            } else alloc.free(parent_path);

            try findSubagentsForSidLinux(alloc, out, slug_dir, sid);
        }
    }
}

fn findSubagentsForSidLinux(alloc: Allocator, out: *std.ArrayList([]const u8), slug_dir: []const u8, sid: []const u8) !void {
    const platform = main.platform;
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
            if (entry.type != linux.DT.DIR) continue;
            const name_ptr: [*:0]const u8 = @ptrCast(&entry.name);
            const sess = std.mem.span(name_ptr);
            if (sess.len == 0) continue;
            if (sess[0] == '.' and (sess.len == 1 or (sess.len == 2 and sess[1] == '.'))) continue;

            const candidate = try std.fmt.allocPrint(alloc, "{s}/{s}/subagents/agent-{s}.jsonl", .{ slug_dir, sess, sid });
            if (fileExists(alloc, candidate)) {
                try out.append(alloc, candidate);
            } else alloc.free(candidate);
        }
    }
}

fn fileExists(alloc: Allocator, path: []const u8) bool {
    if (is_windows) {
        const platform = main.platform;
        const wpath = std.unicode.utf8ToUtf16LeAllocZ(alloc, path) catch return false;
        defer alloc.free(wpath);
        var info: platform.WIN32_FILE_ATTRIBUTE_DATA = undefined;
        if (platform.GetFileAttributesExW(wpath.ptr, 0, &info) == 0) return false;
        return (info.dwFileAttributes & platform.FILE_ATTRIBUTE_DIRECTORY) == 0;
    } else {
        const linux = main.platform.linux;
        const zpath = alloc.dupeZ(u8, path) catch return false;
        defer alloc.free(zpath);
        var statx_buf: linux.Statx = std.mem.zeroes(linux.Statx);
        const ret = linux.statx(linux.AT.FDCWD, zpath, 0, .{}, &statx_buf);
        const signed: isize = @bitCast(ret);
        if (signed < 0) return false;
        // Reject directories.
        return (statx_buf.mode & 0o170000) != 0o040000;
    }
}
