// Beacon-mode subcommands: beacons-latest and beacons-history.
// See ../SPEC.md "Subcommands" for the contract.

const std = @import("std");
const builtin = @import("builtin");
const Allocator = std.mem.Allocator;
const main = @import("main.zig");
const walker_roots = @import("walker_roots.zig");

const is_windows = main.is_windows;
const is_darwin = main.is_darwin;
const PATH_SEP = main.PATH_SEP;

// --- Beacon shape ----------------------------------------------------------

const Beacon = struct {
    kind: []const u8,
    eta_seconds: f64,
    summary: []const u8,
    // Optional per SPEC: null (and so omitted from output) when the source
    // beacon lacked drift; matches rust Option / cpp has_drift / go *string.
    drift: ?[]const u8 = null,
    beats_left: ?i64 = null,

    fn writeJson(self: Beacon, w: *Buf) !void {
        try w.appendStr("{\"kind\":");
        try writeJsonString(w, self.kind);
        try w.appendStr(",\"eta_seconds\":");
        try writeJsonNumber(w, self.eta_seconds);
        try w.appendStr(",\"summary\":");
        try writeJsonString(w, self.summary);
        if (self.drift) |d| {
            try w.appendStr(",\"drift\":");
            try writeJsonString(w, d);
        }
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

// --- progress-beacon matcher (bespoke, regex-free) -------------------------
//
// Pattern: <progress-beacon>\s*({...})\s*</progress-beacon>
// Matches the FIRST '}' followed (after optional whitespace) by the close
// tag -- the same semantics as the Rust regex `\{.*?\}` non-greedy match.

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
            while (inner_start < self.text.len and std.ascii.isWhitespace(self.text[inner_start])) inner_start += 1;
            if (inner_start >= self.text.len or self.text[inner_start] != '{') {
                self.pos = open_at + OPEN_TAG.len;
                continue;
            }
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
            return null;
        }
        return null;
    }
};

fn parseBeaconJson(alloc: Allocator, json_src: []const u8) ?Beacon {
    var scanner = std.json.Scanner.initCompleteInput(alloc, json_src);
    defer scanner.deinit();

    if (!(main.enterObject(&scanner) catch return null)) return null;

    var kind: ?[]const u8 = null;
    var eta: ?f64 = null;
    var summary: ?[]const u8 = null;
    var drift: ?[]const u8 = null;
    var beats_left: ?i64 = null;

    while (true) {
        const key = (main.parseObjectKey(&scanner, alloc) catch return null) orelse break;
        if (std.mem.eql(u8, key, "kind")) {
            kind = main.parseStringValue(&scanner, alloc) catch return null;
        } else if (std.mem.eql(u8, key, "eta_seconds")) {
            eta = parseF64Value(&scanner, alloc) catch return null;
        } else if (std.mem.eql(u8, key, "summary")) {
            summary = main.parseStringValue(&scanner, alloc) catch return null;
        } else if (std.mem.eql(u8, key, "drift")) {
            drift = main.parseStringValue(&scanner, alloc) catch return null;
        } else if (std.mem.eql(u8, key, "beats_left")) {
            beats_left = parseI64Value(&scanner, alloc) catch return null;
        } else {
            scanner.skipValue() catch return null;
        }
    }

    return Beacon{
        .kind = kind orelse return null,
        .eta_seconds = eta orelse return null,
        .summary = summary orelse return null,
        .drift = drift,
        .beats_left = beats_left,
    };
}

/// Read a numeric value as f64. Returns null when the value is non-numeric.
fn parseF64Value(scanner: *std.json.Scanner, alloc: Allocator) !?f64 {
    const peek = try scanner.peekNextTokenType();
    if (peek != .number) {
        try scanner.skipValue();
        return null;
    }
    const tok = try scanner.nextAlloc(alloc, .alloc_if_needed);
    const slice: []const u8 = switch (tok) {
        .number => |s| s,
        .allocated_number => |s| s,
        else => unreachable,
    };
    return std.fmt.parseFloat(f64, slice) catch null;
}

/// Read a numeric value as i64. Falls back to truncating-from-float when
/// the source is a JSON float (matches the prior `.float => |f| @intFromFloat(f)` path).
fn parseI64Value(scanner: *std.json.Scanner, alloc: Allocator) !?i64 {
    const peek = try scanner.peekNextTokenType();
    if (peek != .number) {
        try scanner.skipValue();
        return null;
    }
    const tok = try scanner.nextAlloc(alloc, .alloc_if_needed);
    const slice: []const u8 = switch (tok) {
        .number => |s| s,
        .allocated_number => |s| s,
        else => unreachable,
    };
    if (std.fmt.parseInt(i64, slice, 10)) |n| return n else |_| {}
    if (std.fmt.parseFloat(f64, slice)) |f| return @intFromFloat(f) else |_| {}
    return null;
}

// --- transcript scanning ---------------------------------------------------

const Found = struct {
    beacon: Beacon,
    ts: f64,
};

/// `is_real_user` is true iff this entry is `type: "user"` AND its
/// `message.content` is NOT a tool_result array (bare-string content counts
/// as a real user prompt). Used to detect agent-waiting-on-user idle gaps
/// for bias-factor correction in beacons-history.
const Event = struct {
    ts: f64,
    is_real_user: bool,
};

fn findLatestInPath(alloc: Allocator, path: []const u8) ?Found {
    const data = main.readEntireFile(alloc, path) catch return null;
    defer alloc.free(data);

    var latest: ?Found = null;
    var iter = std.mem.splitScalar(u8, data, '\n');
    while (iter.next()) |raw| {
        if (parseEntryBeacon(alloc, raw)) |found| {
            if (latest == null or found.ts >= latest.?.ts) {
                latest = found;
            }
        }
    }
    return latest;
}

/// Walk one transcript file and append:
///   - ALL well-formed beacons paired with their assistant-entry timestamp
///   - one Event per entry (assistant OR user) with `is_real_user` flag
/// Events are appended in encounter order -- caller must sort by ts before
/// feeding to `computeIdleInWindow`.
fn collectSessionEventsInPath(
    alloc: Allocator,
    path: []const u8,
    beacons_out: *std.ArrayList(Found),
    events_out: *std.ArrayList(Event),
) void {
    const data = main.readEntireFile(alloc, path) catch return;
    defer alloc.free(data);

    var iter = std.mem.splitScalar(u8, data, '\n');
    while (iter.next()) |raw| {
        scanEntry(alloc, raw, beacons_out, events_out);
    }
}

fn parseEntryBeacon(alloc: Allocator, raw: []const u8) ?Found {
    const line = std.mem.trim(u8, raw, " \t\r\n");
    if (line.len == 0) return null;

    const cls = classifyEntry(alloc, line) orelse return null;
    if (cls.kind != .assistant) return null;
    const text = cls.text orelse return null;

    var it = MatchIter{ .text = text };
    var last_ok: ?Beacon = null;
    while (it.next()) |m| {
        if (parseBeaconJson(alloc, m.json)) |b| last_ok = b;
    }
    if (last_ok) |b| return Found{ .beacon = b, .ts = cls.ts };
    return null;
}

fn scanEntry(
    alloc: Allocator,
    raw: []const u8,
    beacons_out: *std.ArrayList(Found),
    events_out: *std.ArrayList(Event),
) void {
    const line = std.mem.trim(u8, raw, " \t\r\n");
    if (line.len == 0) return;

    const cls = classifyEntry(alloc, line) orelse return;
    switch (cls.kind) {
        .user_real => {
            events_out.append(alloc, .{ .ts = cls.ts, .is_real_user = true }) catch {};
        },
        .user_tool_result => {
            // Tool-result entries are tagged `type: "user"` in the JSONL but
            // represent agent-active time waiting on tools -- NOT user idle.
            events_out.append(alloc, .{ .ts = cls.ts, .is_real_user = false }) catch {};
        },
        .assistant => {
            events_out.append(alloc, .{ .ts = cls.ts, .is_real_user = false }) catch {};
            if (cls.text) |text| {
                var it = MatchIter{ .text = text };
                while (it.next()) |m| {
                    if (parseBeaconJson(alloc, m.json)) |b| {
                        beacons_out.append(alloc, .{ .beacon = b, .ts = cls.ts }) catch {};
                    }
                }
            }
        },
        .other => {},
    }
}

const EntryKind = enum { assistant, user_real, user_tool_result, other };

const Classified = struct {
    ts: f64,
    kind: EntryKind,
    /// Owned, joined "\n" of text-block contents (assistant only). Caller frees.
    text: ?[]u8,
};

/// Scanner-streamed entry classification. Walks the line in one pass,
/// collecting timestamp/type/role plus a joined text buffer for assistant
/// entries and a `has_tool_result` flag for user entries. Key order is not
/// assumed -- both content and role are collected eagerly inside the
/// message walk so the final classify-and-dispatch step at the bottom can
/// decide regardless of which key came first.
fn classifyEntry(alloc: Allocator, line: []const u8) ?Classified {
    var scanner = std.json.Scanner.initCompleteInput(alloc, line);
    defer scanner.deinit();

    if (!(main.enterObject(&scanner) catch return null)) return null;

    var ts: ?f64 = null;
    var type_is_user = false;
    var role_is_assistant = false;
    var has_tool_result = false;
    var text_buf: std.ArrayList(u8) = .empty;
    var text_first = true;

    while (true) {
        const key = (main.parseObjectKey(&scanner, alloc) catch return null) orelse break;
        if (std.mem.eql(u8, key, "timestamp")) {
            const v = main.parseStringValue(&scanner, alloc) catch return null;
            if (v) |s| ts = main.parseTs(s) catch return null;
        } else if (std.mem.eql(u8, key, "type")) {
            const v = main.parseStringValue(&scanner, alloc) catch return null;
            if (v) |s| type_is_user = std.mem.eql(u8, s, "user");
        } else if (std.mem.eql(u8, key, "message")) {
            if (!(main.enterObject(&scanner) catch return null)) continue;
            while (true) {
                const mkey = (main.parseObjectKey(&scanner, alloc) catch return null) orelse break;
                if (std.mem.eql(u8, mkey, "role")) {
                    const v = main.parseStringValue(&scanner, alloc) catch return null;
                    if (v) |s| role_is_assistant = std.mem.eql(u8, s, "assistant");
                } else if (std.mem.eql(u8, mkey, "content")) {
                    walkContentForClassify(&scanner, alloc, &text_buf, &text_first, &has_tool_result) catch return null;
                } else {
                    scanner.skipValue() catch return null;
                }
            }
        } else {
            scanner.skipValue() catch return null;
        }
    }

    const t = ts orelse return null;

    if (type_is_user) {
        return .{
            .ts = t,
            .kind = if (has_tool_result) .user_tool_result else .user_real,
            .text = null,
        };
    }
    if (role_is_assistant) {
        const text: ?[]u8 = if (text_first) null else (text_buf.toOwnedSlice(alloc) catch return null);
        return .{ .ts = t, .kind = .assistant, .text = text };
    }
    return .{ .ts = t, .kind = .other, .text = null };
}

/// Walk a content value (which may be a bare string, an array of content
/// blocks, or something else). Builds joined "\n"-separated text for
/// `type: "text"` blocks and sets `has_tool_result` when any `tool_result`
/// block is seen. Bare-string content is collected as text too -- harmless
/// for user_real entries (text is discarded) and matches the legacy code's
/// "extract text where present" semantics.
fn walkContentForClassify(
    scanner: *std.json.Scanner,
    alloc: Allocator,
    text_buf: *std.ArrayList(u8),
    text_first: *bool,
    has_tool_result: *bool,
) !void {
    const peek = try scanner.peekNextTokenType();
    switch (peek) {
        .string => {
            if (try main.parseStringValue(scanner, alloc)) |s| {
                if (!text_first.*) try text_buf.append(alloc, '\n');
                text_first.* = false;
                try text_buf.appendSlice(alloc, s);
            }
        },
        .array_begin => {
            _ = try scanner.next();
            while (true) {
                const block_peek = try scanner.peekNextTokenType();
                if (block_peek == .array_end) {
                    _ = try scanner.next();
                    break;
                }
                if (block_peek != .object_begin) {
                    try scanner.skipValue();
                    continue;
                }
                _ = try scanner.next();
                var block_type: ?[]const u8 = null;
                var block_text: ?[]const u8 = null;
                while (true) {
                    const bkey = (try main.parseObjectKey(scanner, alloc)) orelse break;
                    if (std.mem.eql(u8, bkey, "type")) {
                        block_type = try main.parseStringValue(scanner, alloc);
                    } else if (std.mem.eql(u8, bkey, "text")) {
                        block_text = try main.parseStringValue(scanner, alloc);
                    } else {
                        try scanner.skipValue();
                    }
                }
                if (block_type) |bt| {
                    if (std.mem.eql(u8, bt, "tool_result")) {
                        has_tool_result.* = true;
                    } else if (std.mem.eql(u8, bt, "text")) {
                        if (block_text) |txt| {
                            if (!text_first.*) try text_buf.append(alloc, '\n');
                            text_first.* = false;
                            try text_buf.appendSlice(alloc, txt);
                        }
                    }
                }
            }
        },
        else => try scanner.skipValue(),
    }
}

/// Sum the portion of [lo, hi] occupied by gaps that immediately precede a
/// real user entry. Iterates events[1..]; for each `events[i].is_real_user`
/// true, accumulates the slice of `(events[i-1].ts, events[i].ts)` that lies
/// inside `[lo, hi]`.
fn computeIdleInWindow(events: []const Event, lo: f64, hi: f64) f64 {
    if (events.len < 2) return 0.0;
    var idle: f64 = 0.0;
    var i: usize = 1;
    while (i < events.len) : (i += 1) {
        if (!events[i].is_real_user) continue;
        const prev_ts = events[i - 1].ts;
        const ts = events[i].ts;
        const gap_lo = @max(prev_ts, lo);
        const gap_hi = @min(ts, hi);
        if (gap_hi > gap_lo) idle += gap_hi - gap_lo;
    }
    return idle;
}

// --- beacons-latest --------------------------------------------------------

const LatestArgs = struct {
    session_id: []const u8 = "",
    projects_root: ?[]const u8 = null,
    extra_roots: [][]const u8 = &.{},
    read_config: bool = true,
    now_unix: ?f64 = null,
};

fn parseLatestArgs(alloc: Allocator, args: [][]const u8) !LatestArgs {
    var out = LatestArgs{};
    var extras: std.ArrayList([]const u8) = .empty;
    var i: usize = 0;
    while (i < args.len) {
        const flag = args[i];
        i += 1;
        if (std.mem.eql(u8, flag, "--session-id")) {
            out.session_id = main.grab(args, &i, "--session-id");
        } else if (std.mem.eql(u8, flag, "--projects-root")) {
            out.projects_root = main.grab(args, &i, "--projects-root");
        } else if (std.mem.eql(u8, flag, "--extra-projects-root")) {
            try extras.append(alloc, main.grab(args, &i, "--extra-projects-root"));
        } else if (std.mem.eql(u8, flag, "--no-config")) {
            out.read_config = false;
        } else if (std.mem.eql(u8, flag, "--now")) {
            out.now_unix = std.fmt.parseFloat(f64, main.grab(args, &i, "--now")) catch main.die("--now: invalid");
        } else {
            std.debug.print("walker: beacons-latest: unknown flag: {s}\n", .{flag});
            std.process.exit(2);
        }
    }
    if (out.session_id.len == 0) main.die("beacons-latest: --session-id is required");
    out.extra_roots = try extras.toOwnedSlice(alloc);
    return out;
}

pub fn runLatest(gpa: Allocator, args: [][]const u8) !void {
    const t0 = main.perfNow();
    const frq = main.perfFreq();

    var arena = std.heap.ArenaAllocator.init(gpa);
    defer arena.deinit();
    const alloc = arena.allocator();

    const parsed = try parseLatestArgs(alloc, args);
    const primary = if (parsed.projects_root) |r| try alloc.dupe(u8, r) else try main.defaultRoot(alloc);
    const roots = try walker_roots.resolveRoots(alloc, primary, parsed.extra_roots, parsed.read_config);
    const now_unix: f64 = parsed.now_unix orelse main.nowUnix();

    var best: ?Found = null;
    for (roots) |root| {
        const paths = try findSessionPaths(alloc, root, parsed.session_id);
        for (paths.items) |p| {
            if (findLatestInPath(alloc, p)) |f| {
                if (best == null or f.ts >= best.?.ts) best = f;
            }
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

// --- beacons-history -------------------------------------------------------

const HistoryArgs = struct {
    period_seconds: u64 = 0,
    win_start_unix: f64 = 0.0,
    projects_root: ?[]const u8 = null,
    extra_roots: [][]const u8 = &.{},
    read_config: bool = true,
    now_unix: ?f64 = null,
};

fn parseHistoryArgs(alloc: Allocator, args: [][]const u8) !HistoryArgs {
    var out = HistoryArgs{};
    var extras: std.ArrayList([]const u8) = .empty;
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
        } else if (std.mem.eql(u8, flag, "--extra-projects-root")) {
            try extras.append(alloc, main.grab(args, &i, "--extra-projects-root"));
        } else if (std.mem.eql(u8, flag, "--no-config")) {
            out.read_config = false;
        } else if (std.mem.eql(u8, flag, "--now")) {
            out.now_unix = std.fmt.parseFloat(f64, main.grab(args, &i, "--now")) catch main.die("--now: invalid");
        } else {
            std.debug.print("walker: beacons-history: unknown flag: {s}\n", .{flag});
            std.process.exit(2);
        }
    }
    if (!got_period) main.die("beacons-history: --period is required");
    out.extra_roots = try extras.toOwnedSlice(alloc);
    return out;
}

fn cmpEventAsc(_: void, a: Event, b: Event) bool {
    return a.ts < b.ts;
}

pub fn runHistory(gpa: Allocator, args: [][]const u8) !void {
    const t0 = main.perfNow();
    const frq = main.perfFreq();

    var arena = std.heap.ArenaAllocator.init(gpa);
    defer arena.deinit();
    const alloc = arena.allocator();

    const parsed = try parseHistoryArgs(alloc, args);
    const primary = if (parsed.projects_root) |r| try alloc.dupe(u8, r) else try main.defaultRoot(alloc);
    const roots = try walker_roots.resolveRoots(alloc, primary, parsed.extra_roots, parsed.read_config);
    const now_unix: f64 = parsed.now_unix orelse main.nowUnix();
    const period_cutoff = now_unix - @as(f64, @floatFromInt(parsed.period_seconds));
    const window_lo = @max(period_cutoff, parsed.win_start_unix);

    var grp_map = try main.discover(alloc, roots, -std.math.inf(f64));
    const session_count = grp_map.count();

    var groups: std.ArrayList([]const []const u8) = .empty;
    var vi = grp_map.valueIterator();
    while (vi.next()) |list| {
        try groups.append(alloc, list.items);
    }

    // Worker pool: each worker holds its own arena (arena allocators are not
    // thread-safe) and its own pairs list. The arenas stay alive until after
    // the output JSON is written -- pair fields are plain f64 so the merge
    // below copies values into the main arena cleanly.
    const ncpu = std.Thread.getCpuCount() catch 4;
    const nw = @min(8, ncpu);
    const workers = try alloc.alloc(HistoryWorker, nw);
    for (workers) |*w| w.* = .{
        .arena = std.heap.ArenaAllocator.init(gpa),
        .pairs = .empty,
    };
    defer for (workers) |*w| w.arena.deinit();

    var queue_cur = std.atomic.Value(usize).init(0);
    const threads = try alloc.alloc(std.Thread, nw);
    for (workers, 0..) |_, i| {
        threads[i] = try std.Thread.spawn(
            .{},
            historyDoWork,
            .{ &workers[i], &queue_cur, groups.items, window_lo },
        );
    }
    for (threads) |th| th.join();

    var pairs: std.ArrayList(Pair) = .empty;
    for (workers) |*w| try pairs.appendSlice(alloc, w.pairs.items);

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
        try w.appendStr(",\"idle_excluded\":");
        try writeJsonNumber(&w, p.idle_excluded);
        try w.appendStr(",\"active_elapsed\":");
        try writeJsonNumber(&w, p.active_elapsed);
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

const Pair = struct {
    begin_eta: f64,
    actual_elapsed: f64,
    idle_excluded: f64,
    active_elapsed: f64,
};

const HistoryWorker = struct {
    arena: std.heap.ArenaAllocator,
    pairs: std.ArrayList(Pair),
};

fn cmpFoundAsc(_: void, a: Found, b: Found) bool {
    return a.ts < b.ts;
}

fn historyDoWork(
    worker: *HistoryWorker,
    queue_cur: *std.atomic.Value(usize),
    groups: []const []const []const u8,
    window_lo: f64,
) void {
    const wa = worker.arena.allocator();
    while (true) {
        const i = queue_cur.fetchAdd(1, .seq_cst);
        if (i >= groups.len) break;
        const paths = groups[i];

        var all_beacons: std.ArrayList(Found) = .empty;
        var all_events: std.ArrayList(Event) = .empty;
        for (paths) |p| {
            collectSessionEventsInPath(wa, p, &all_beacons, &all_events);
        }
        std.mem.sort(Event, all_events.items, {}, cmpEventAsc);

        // Sort beacons by ts (stable), then iterate tracking one in-flight
        // pending begin: emit one pair per properly-closed begin->end
        // lifecycle. Beacons before window_lo are skipped, so a pair needs its
        // begin (and end) inside the window. Replaces the earliest/latest rule.
        std.mem.sort(Found, all_beacons.items, {}, cmpFoundAsc);
        var pending_begin: ?Found = null;
        for (all_beacons.items) |f| {
            if (f.ts < window_lo) continue;
            if (std.mem.eql(u8, f.beacon.kind, "begin")) {
                pending_begin = f; // orphans any prior pending begin
            } else if (std.mem.eql(u8, f.beacon.kind, "end")) {
                if (pending_begin) |b| {
                    if (f.ts > b.ts) {
                        const wall = f.ts - b.ts;
                        const idle = computeIdleInWindow(all_events.items, b.ts, f.ts);
                        const active = @max(0.0, wall - idle);
                        worker.pairs.append(wa, .{
                            .begin_eta = b.beacon.eta_seconds,
                            .actual_elapsed = wall,
                            .idle_excluded = idle,
                            .active_elapsed = active,
                        }) catch {};
                        pending_begin = null;
                    }
                }
            }
        }
    }
}

fn biasFactor(alloc: Allocator, pairs: []const Pair) ?f64 {
    if (pairs.len == 0) return null;
    var ratios: std.ArrayList(f64) = .empty;
    defer ratios.deinit(alloc);
    for (pairs) |p| {
        if (p.begin_eta > 0) {
            ratios.append(alloc, p.active_elapsed / p.begin_eta) catch return null;
        }
    }
    if (ratios.items.len == 0) return null;
    std.mem.sort(f64, ratios.items, {}, std.sort.asc(f64));
    const n = ratios.items.len;
    if (n % 2 == 1) return ratios.items[n / 2];
    return (ratios.items[n / 2 - 1] + ratios.items[n / 2]) / 2.0;
}

// --- session-id path discovery (beacons-latest) ----------------------------

fn findSessionPaths(alloc: Allocator, root: []const u8, sid: []const u8) !std.ArrayList([]const u8) {
    var out: std.ArrayList([]const u8) = .empty;
    if (is_windows) {
        try findSessionPathsWindows(alloc, &out, root, sid);
    } else if (is_darwin) {
        try findSessionPathsDarwin(alloc, &out, root, sid);
    } else {
        try findSessionPathsLinux(alloc, &out, root, sid);
    }
    return out;
}

fn findSessionPathsDarwin(alloc: Allocator, out: *std.ArrayList([]const u8), root: []const u8, sid: []const u8) !void {
    const root_z = try alloc.dupeZ(u8, root);
    defer alloc.free(root_z);
    const root_dir = std.c.opendir(root_z) orelse return;
    defer _ = std.c.closedir(root_dir);

    while (std.c.readdir(root_dir)) |ent| {
        if (ent.type != std.c.DT.DIR) continue;
        const name_ptr: [*:0]const u8 = @ptrCast(&ent.name);
        const slug = std.mem.span(name_ptr);
        if (slug.len == 0) continue;
        if (slug[0] == '.' and (slug.len == 1 or (slug.len == 2 and slug[1] == '.'))) continue;

        const slug_dir = try std.fmt.allocPrint(alloc, "{s}/{s}", .{ root, slug });

        const parent_path = try std.fmt.allocPrint(alloc, "{s}/{s}.jsonl", .{ slug_dir, sid });
        if (fileExists(alloc, parent_path)) {
            try out.append(alloc, parent_path);
        } else alloc.free(parent_path);

        try findSubagentsForSidDarwin(alloc, out, slug_dir, sid);
    }
}

fn findSubagentsForSidDarwin(alloc: Allocator, out: *std.ArrayList([]const u8), slug_dir: []const u8, sid: []const u8) !void {
    const slug_z = try alloc.dupeZ(u8, slug_dir);
    defer alloc.free(slug_z);
    const dir = std.c.opendir(slug_z) orelse return;
    defer _ = std.c.closedir(dir);

    while (std.c.readdir(dir)) |ent| {
        if (ent.type != std.c.DT.DIR) continue;
        const name_ptr: [*:0]const u8 = @ptrCast(&ent.name);
        const sess = std.mem.span(name_ptr);
        if (sess.len == 0) continue;
        if (sess[0] == '.' and (sess.len == 1 or (sess.len == 2 and sess[1] == '.'))) continue;

        const candidate = try std.fmt.allocPrint(alloc, "{s}/{s}/subagents/agent-{s}.jsonl", .{ slug_dir, sess, sid });
        if (fileExists(alloc, candidate)) {
            try out.append(alloc, candidate);
        } else alloc.free(candidate);
    }
}

fn findSessionPathsWindows(alloc: Allocator, out: *std.ArrayList([]const u8), root: []const u8, sid: []const u8) !void {
    const platform = main.platform;
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

                const parent_path = try std.fmt.allocPrint(alloc, "{s}\\{s}.jsonl", .{ slug_dir, sid });
                if (fileExists(alloc, parent_path)) {
                    try out.append(alloc, parent_path);
                } else alloc.free(parent_path);

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
    } else if (is_darwin) {
        const zpath = alloc.dupeZ(u8, path) catch return false;
        defer alloc.free(zpath);
        var st: std.c.Stat = undefined;
        if (std.c.fstatat(std.c.AT.FDCWD, zpath, &st, 0) != 0) return false;
        return (st.mode & 0o170000) != 0o040000;
    } else {
        const linux = main.platform.linux;
        const zpath = alloc.dupeZ(u8, path) catch return false;
        defer alloc.free(zpath);
        var statx_buf: linux.Statx = std.mem.zeroes(linux.Statx);
        const ret = linux.statx(linux.AT.FDCWD, zpath, 0, .{}, &statx_buf);
        const signed: isize = @bitCast(ret);
        if (signed < 0) return false;
        return (statx_buf.mode & 0o170000) != 0o040000;
    }
}
