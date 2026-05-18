// Search subcommand: substring / minimal-regex match across transcript content.
// Mirrors rust/src/search.rs and go/search.go. Zig port keeps the
// "regex-free stdlib" style of beacons.zig — the matcher here implements only
// the regex surface the conformance fixtures exercise (`foo\d+` plus the
// common escape classes), not full PCRE.

const std = @import("std");
const builtin = @import("builtin");
const Allocator = std.mem.Allocator;
const main = @import("main.zig");

const is_windows = main.is_windows;
const PATH_SEP = main.PATH_SEP;

// ─── Args ────────────────────────────────────────────────────────────────────

const Role = enum { user, assistant, both };
const Format = enum { pretty, jsonl };

const SearchArgs = struct {
    pattern: []const u8,
    regex: bool,
    case_sensitive: bool,
    role: Role,
    since: ?f64,
    until: ?f64,
    cwd: ?[]const u8,
    context: u32,
    limit: u32,
    count_only: bool,
    include_tool_blocks: bool,
    format: Format,
    snippet_chars: u32,
    projects_root: []const u8,
    now_unix: f64,
};

fn parseArgs(alloc: Allocator, raw: [][]const u8) !SearchArgs {
    var pattern: ?[]const u8 = null;
    var regex = false;
    var case_sensitive = false;
    var role: Role = .both;
    var since_raw: ?[]const u8 = null;
    var until_raw: ?[]const u8 = null;
    var cwd: ?[]const u8 = null;
    var any_cwd_explicit = false;
    var context: u32 = 1;
    var limit: u32 = 50;
    var count_only = false;
    var include_tool_blocks = false;
    var format: Format = .pretty;
    var snippet_chars: u32 = 240;
    var projects_root: ?[]const u8 = null;
    var now_override: ?f64 = null;

    var i: usize = 0;
    while (i < raw.len) {
        const arg = raw[i];
        i += 1;
        if (std.mem.eql(u8, arg, "--regex")) {
            regex = true;
        } else if (std.mem.eql(u8, arg, "--case-sensitive")) {
            case_sensitive = true;
        } else if (std.mem.eql(u8, arg, "--role")) {
            const v = main.grab(raw, &i, "--role");
            if (std.mem.eql(u8, v, "user")) role = .user
            else if (std.mem.eql(u8, v, "assistant")) role = .assistant
            else if (std.mem.eql(u8, v, "both")) role = .both
            else {
                writeStderrFmt(alloc, "walker: search: --role: invalid value {s}; expected user|assistant|both\n", .{v});
                std.process.exit(2);
            }
        } else if (std.mem.eql(u8, arg, "--since")) {
            since_raw = main.grab(raw, &i, "--since");
        } else if (std.mem.eql(u8, arg, "--until")) {
            until_raw = main.grab(raw, &i, "--until");
        } else if (std.mem.eql(u8, arg, "--cwd")) {
            cwd = main.grab(raw, &i, "--cwd");
        } else if (std.mem.eql(u8, arg, "--any-cwd")) {
            any_cwd_explicit = true;
        } else if (std.mem.eql(u8, arg, "--context")) {
            context = std.fmt.parseInt(u32, main.grab(raw, &i, "--context"), 10) catch {
                writeStderrFmt(alloc, "walker: search: --context: invalid value\n", .{});
                std.process.exit(2);
            };
        } else if (std.mem.eql(u8, arg, "--limit")) {
            limit = std.fmt.parseInt(u32, main.grab(raw, &i, "--limit"), 10) catch {
                writeStderrFmt(alloc, "walker: search: --limit: invalid value\n", .{});
                std.process.exit(2);
            };
        } else if (std.mem.eql(u8, arg, "--count-only")) {
            count_only = true;
        } else if (std.mem.eql(u8, arg, "--include-tool-blocks")) {
            include_tool_blocks = true;
        } else if (std.mem.eql(u8, arg, "--format")) {
            const v = main.grab(raw, &i, "--format");
            if (std.mem.eql(u8, v, "pretty")) format = .pretty
            else if (std.mem.eql(u8, v, "jsonl")) format = .jsonl
            else {
                writeStderrFmt(alloc, "walker: search: --format: invalid value {s}; expected pretty|jsonl\n", .{v});
                std.process.exit(2);
            }
        } else if (std.mem.eql(u8, arg, "--snippet-chars")) {
            snippet_chars = std.fmt.parseInt(u32, main.grab(raw, &i, "--snippet-chars"), 10) catch {
                writeStderrFmt(alloc, "walker: search: --snippet-chars: invalid value\n", .{});
                std.process.exit(2);
            };
        } else if (std.mem.eql(u8, arg, "--projects-root")) {
            projects_root = main.grab(raw, &i, "--projects-root");
        } else if (std.mem.eql(u8, arg, "--now")) {
            now_override = std.fmt.parseFloat(f64, main.grab(raw, &i, "--now")) catch {
                writeStderrFmt(alloc, "walker: search: --now: invalid value\n", .{});
                std.process.exit(2);
            };
        } else if (std.mem.startsWith(u8, arg, "--")) {
            writeStderrFmt(alloc, "walker: search: unknown flag: {s}\n", .{arg});
            std.process.exit(2);
        } else {
            if (pattern != null) {
                writeStderrFmt(alloc, "walker: search: unexpected positional argument: {s}\n", .{arg});
                std.process.exit(2);
            }
            pattern = arg;
        }
    }

    const pat = pattern orelse {
        writeStderrFmt(alloc, "walker: search: pattern must be non-empty\n", .{});
        std.process.exit(2);
    };
    if (pat.len == 0) {
        writeStderrFmt(alloc, "walker: search: pattern must be non-empty\n", .{});
        std.process.exit(2);
    }
    if (cwd != null and any_cwd_explicit) {
        writeStderrFmt(alloc, "walker: search: --cwd and --any-cwd are mutually exclusive\n", .{});
        std.process.exit(2);
    }

    const now: f64 = now_override orelse main.nowUnix();

    var since: ?f64 = null;
    if (since_raw) |s| {
        since = parseTimeArg(s, now) catch {
            writeStderrFmt(alloc, "walker: search: bad time: --since={s}\n", .{s});
            std.process.exit(2);
        };
    }
    var until: ?f64 = null;
    if (until_raw) |s| {
        until = parseTimeArg(s, now) catch {
            writeStderrFmt(alloc, "walker: search: bad time: --until={s}\n", .{s});
            std.process.exit(2);
        };
    }

    return SearchArgs{
        .pattern = pat,
        .regex = regex,
        .case_sensitive = case_sensitive,
        .role = role,
        .since = since,
        .until = until,
        .cwd = cwd,
        .context = context,
        .limit = limit,
        .count_only = count_only,
        .include_tool_blocks = include_tool_blocks,
        .format = format,
        .snippet_chars = snippet_chars,
        .projects_root = projects_root orelse try main.defaultRoot(alloc),
        .now_unix = now,
    };
}

fn parseTimeArg(s: []const u8, now: f64) !f64 {
    const trimmed = std.mem.trim(u8, s, " \t\r\n");
    if (trimmed.len == 0) return error.Empty;
    const last = trimmed[trimmed.len - 1];
    if (last == 'd' or last == 'h' or last == 'm' or last == 's') {
        const head = trimmed[0 .. trimmed.len - 1];
        if (head.len > 0 and isNumeric(head)) {
            const n = try std.fmt.parseFloat(f64, head);
            const mult: f64 = switch (last) {
                'd' => 86400.0,
                'h' => 3600.0,
                'm' => 60.0,
                's' => 1.0,
                else => unreachable,
            };
            return now - n * mult;
        }
    }
    return main.parseTs(trimmed);
}

fn isNumeric(s: []const u8) bool {
    for (s) |c| {
        if (c != '.' and (c < '0' or c > '9')) return false;
    }
    return true;
}

fn writeStderrFmt(alloc: Allocator, comptime fmt: []const u8, args: anytype) void {
    const buf = std.fmt.allocPrint(alloc, fmt, args) catch return;
    defer alloc.free(buf);
    main.writeStderr(buf);
}

// ─── Minimal regex / literal matcher ─────────────────────────────────────────
//
// Compile pattern to a flat sequence of Items (Atom + Quantifier). Match is a
// simple backtracking walker. Sufficient for the conformance fixtures
// (`needle`, `foo\d+`) plus common escape classes; does NOT implement groups,
// alternation, anchors, lookaround. That matches the documented scope —
// production callers needing PCRE features should use the Rust or C++ binary.

const Class = enum { digit, non_digit, word, non_word, space, non_space };

const CharClass = struct {
    negated: bool,
    // Bitset of 256 bits indicating which bytes are in the class.
    bits: [32]u8 = [_]u8{0} ** 32,

    fn set(self: *CharClass, b: u8) void {
        self.bits[b / 8] |= @as(u8, 1) << @intCast(b % 8);
    }
    fn contains(self: CharClass, b: u8) bool {
        const inSet = (self.bits[b / 8] >> @intCast(b % 8)) & 1 != 0;
        return if (self.negated) !inSet else inSet;
    }
};

const Atom = union(enum) {
    char: u8,
    dot,
    class_escape: Class,
    class_set: CharClass,
};

const Quant = enum { one, opt, star, plus };

const Item = struct {
    atom: Atom,
    quant: Quant,
};

const Program = struct {
    items: []Item,
    alloc: Allocator,

    fn deinit(self: *Program) void {
        self.alloc.free(self.items);
    }
};

const Pattern = struct {
    // The literal mode keeps the lowercased needle for case-insensitive scan.
    mode: enum { literal, regex },
    literal_needle: []const u8, // owned when case_insensitive (lowercased)
    case_sensitive: bool,
    program: ?Program,
    alloc: Allocator,

    fn deinit(self: *Pattern) void {
        if (self.mode == .literal and !self.case_sensitive) {
            self.alloc.free(self.literal_needle);
        }
        if (self.program) |*p| p.deinit();
    }
};

fn compilePattern(alloc: Allocator, raw_pattern: []const u8, regex_mode: bool, case_sensitive: bool) !Pattern {
    if (!regex_mode) {
        if (case_sensitive) {
            return Pattern{
                .mode = .literal,
                .literal_needle = raw_pattern,
                .case_sensitive = true,
                .program = null,
                .alloc = alloc,
            };
        }
        const lower = try alloc.alloc(u8, raw_pattern.len);
        for (raw_pattern, 0..) |c, k| lower[k] = std.ascii.toLower(c);
        return Pattern{
            .mode = .literal,
            .literal_needle = lower,
            .case_sensitive = false,
            .program = null,
            .alloc = alloc,
        };
    }
    const items = try compileRegex(alloc, raw_pattern, case_sensitive);
    return Pattern{
        .mode = .regex,
        .literal_needle = &.{},
        .case_sensitive = case_sensitive,
        .program = .{ .items = items, .alloc = alloc },
        .alloc = alloc,
    };
}

fn compileRegex(alloc: Allocator, src: []const u8, case_sensitive: bool) ![]Item {
    var items: std.ArrayList(Item) = .empty;
    errdefer items.deinit(alloc);

    var i: usize = 0;
    while (i < src.len) {
        var atom: Atom = undefined;
        const c = src[i];
        if (c == '\\') {
            if (i + 1 >= src.len) return error.BadEscape;
            const esc = src[i + 1];
            switch (esc) {
                'd' => atom = .{ .class_escape = .digit },
                'D' => atom = .{ .class_escape = .non_digit },
                'w' => atom = .{ .class_escape = .word },
                'W' => atom = .{ .class_escape = .non_word },
                's' => atom = .{ .class_escape = .space },
                'S' => atom = .{ .class_escape = .non_space },
                'n' => atom = .{ .char = '\n' },
                't' => atom = .{ .char = '\t' },
                'r' => atom = .{ .char = '\r' },
                else => atom = .{ .char = if (case_sensitive) esc else std.ascii.toLower(esc) },
            }
            i += 2;
        } else if (c == '.') {
            atom = .dot;
            i += 1;
        } else if (c == '[') {
            var cls = CharClass{ .negated = false };
            i += 1;
            if (i < src.len and src[i] == '^') {
                cls.negated = true;
                i += 1;
            }
            while (i < src.len and src[i] != ']') {
                var lo: u8 = src[i];
                if (lo == '\\' and i + 1 < src.len) {
                    lo = switch (src[i + 1]) {
                        'n' => '\n',
                        't' => '\t',
                        'r' => '\r',
                        else => src[i + 1],
                    };
                    i += 2;
                } else {
                    i += 1;
                }
                if (i + 1 < src.len and src[i] == '-' and src[i + 1] != ']') {
                    var hi: u8 = src[i + 1];
                    i += 2;
                    if (hi == '\\' and i < src.len) {
                        hi = src[i];
                        i += 1;
                    }
                    var b = lo;
                    while (true) : (b += 1) {
                        if (case_sensitive) {
                            cls.set(b);
                        } else {
                            cls.set(std.ascii.toLower(b));
                            cls.set(std.ascii.toUpper(b));
                        }
                        if (b == hi) break;
                    }
                } else {
                    if (case_sensitive) {
                        cls.set(lo);
                    } else {
                        cls.set(std.ascii.toLower(lo));
                        cls.set(std.ascii.toUpper(lo));
                    }
                }
            }
            if (i >= src.len) return error.UnclosedClass;
            i += 1; // skip ']'
            atom = .{ .class_set = cls };
        } else {
            atom = .{ .char = if (case_sensitive) c else std.ascii.toLower(c) };
            i += 1;
        }

        var q: Quant = .one;
        if (i < src.len) {
            switch (src[i]) {
                '*' => { q = .star; i += 1; },
                '+' => { q = .plus; i += 1; },
                '?' => { q = .opt; i += 1; },
                else => {},
            }
        }
        try items.append(alloc, .{ .atom = atom, .quant = q });
    }
    return items.toOwnedSlice(alloc);
}

fn atomMatch(atom: Atom, b: u8, case_sensitive: bool) bool {
    return switch (atom) {
        .char => |cc| if (case_sensitive) (b == cc) else (std.ascii.toLower(b) == cc),
        .dot => b != '\n',
        .class_escape => |cls| switch (cls) {
            .digit => b >= '0' and b <= '9',
            .non_digit => !(b >= '0' and b <= '9'),
            .word => (b >= 'a' and b <= 'z') or (b >= 'A' and b <= 'Z') or (b >= '0' and b <= '9') or b == '_',
            .non_word => !((b >= 'a' and b <= 'z') or (b >= 'A' and b <= 'Z') or (b >= '0' and b <= '9') or b == '_'),
            .space => b == ' ' or b == '\t' or b == '\n' or b == '\r' or b == 0x0B or b == 0x0C,
            .non_space => !(b == ' ' or b == '\t' or b == '\n' or b == '\r' or b == 0x0B or b == 0x0C),
        },
        .class_set => |cls| cls.contains(b),
    };
}

// Try to match items[idx..] at text[pos..]. Returns end position on success,
// null on failure. Greedy quantifiers backtrack one byte at a time.
fn matchHere(items: []const Item, idx: usize, text: []const u8, pos: usize, case_sensitive: bool) ?usize {
    if (idx == items.len) return pos;
    const it = items[idx];
    switch (it.quant) {
        .one => {
            if (pos >= text.len) return null;
            if (!atomMatch(it.atom, text[pos], case_sensitive)) return null;
            return matchHere(items, idx + 1, text, pos + 1, case_sensitive);
        },
        .opt => {
            if (pos < text.len and atomMatch(it.atom, text[pos], case_sensitive)) {
                if (matchHere(items, idx + 1, text, pos + 1, case_sensitive)) |e| return e;
            }
            return matchHere(items, idx + 1, text, pos, case_sensitive);
        },
        .star => {
            var end = pos;
            while (end < text.len and atomMatch(it.atom, text[end], case_sensitive)) end += 1;
            while (true) {
                if (matchHere(items, idx + 1, text, end, case_sensitive)) |e| return e;
                if (end == pos) return null;
                end -= 1;
            }
        },
        .plus => {
            var end = pos;
            while (end < text.len and atomMatch(it.atom, text[end], case_sensitive)) end += 1;
            if (end == pos) return null;
            while (true) {
                if (matchHere(items, idx + 1, text, end, case_sensitive)) |e| return e;
                if (end == pos + 1) return null;
                end -= 1;
            }
        },
    }
}

// Find all non-overlapping matches of `pattern` in `text`. Empty matches are
// handled by advancing one byte to avoid infinite loops.
fn findAllMatches(alloc: Allocator, pattern: *const Pattern, text: []const u8) ![][2]usize {
    var out: std.ArrayList([2]usize) = .empty;
    errdefer out.deinit(alloc);

    if (pattern.mode == .literal) {
        const needle = pattern.literal_needle;
        if (needle.len == 0) return out.toOwnedSlice(alloc);
        var i: usize = 0;
        while (i + needle.len <= text.len) {
            const ok = blk: {
                if (pattern.case_sensitive) {
                    break :blk std.mem.eql(u8, text[i .. i + needle.len], needle);
                } else {
                    var k: usize = 0;
                    while (k < needle.len) : (k += 1) {
                        if (std.ascii.toLower(text[i + k]) != needle[k]) break :blk false;
                    }
                    break :blk true;
                }
            };
            if (ok) {
                try out.append(alloc, .{ i, i + needle.len });
                i += needle.len;
            } else {
                i += 1;
            }
        }
        return out.toOwnedSlice(alloc);
    }

    const prog = pattern.program orelse unreachable;
    var pos: usize = 0;
    while (pos <= text.len) {
        if (matchHere(prog.items, 0, text, pos, pattern.case_sensitive)) |end_pos| {
            try out.append(alloc, .{ pos, end_pos });
            pos = if (end_pos == pos) end_pos + 1 else end_pos;
        } else {
            pos += 1;
        }
    }
    return out.toOwnedSlice(alloc);
}

// ─── JSON output buffer ──────────────────────────────────────────────────────

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

// Serialize a std.json.Value back to a JSON string. Used for tool_use.input
// which the reference impls dump verbatim with include-tool-blocks.
fn writeJsonValue(w: *Buf, v: std.json.Value) !void {
    switch (v) {
        .null => try w.appendStr("null"),
        .bool => |b| try w.appendStr(if (b) "true" else "false"),
        .integer => |n| try w.appendFmt("{d}", .{n}),
        .float => |f| try w.appendFmt("{d}", .{f}),
        .number_string => |s| try w.appendStr(s),
        .string => |s| try writeJsonString(w, s),
        .array => |arr| {
            try w.appendStr("[");
            for (arr.items, 0..) |item, k| {
                if (k > 0) try w.appendStr(",");
                try writeJsonValue(w, item);
            }
            try w.appendStr("]");
        },
        .object => |obj| {
            try w.appendStr("{");
            var first = true;
            var it = obj.iterator();
            while (it.next()) |kv| {
                if (!first) try w.appendStr(",");
                first = false;
                try writeJsonString(w, kv.key_ptr.*);
                try w.appendStr(":");
                try writeJsonValue(w, kv.value_ptr.*);
            }
            try w.appendStr("}");
        },
    }
}

// ─── Content extraction ──────────────────────────────────────────────────────

// Concatenate text-block content into a single string with "\n" between blocks.
// With `include_tool_blocks`, also pulls tool_use.input (JSON-stringified) and
// tool_result.content (string or text-block array).
fn extractText(alloc: Allocator, content: std.json.Value, include_tool_blocks: bool) ![]u8 {
    if (content == .string) {
        return alloc.dupe(u8, content.string);
    }
    if (content != .array) return alloc.dupe(u8, "");

    var buf: std.ArrayList(u8) = .empty;
    defer buf.deinit(alloc);
    var first = true;

    for (content.array.items) |block| {
        if (block != .object) continue;
        const tv = block.object.get("type") orelse continue;
        if (tv != .string) continue;
        const t = tv.string;
        if (std.mem.eql(u8, t, "text")) {
            const txt = block.object.get("text") orelse continue;
            if (txt != .string) continue;
            if (!first) try buf.append(alloc, '\n');
            first = false;
            try buf.appendSlice(alloc, txt.string);
        } else if (include_tool_blocks and std.mem.eql(u8, t, "tool_use")) {
            const input = block.object.get("input") orelse continue;
            var w = Buf{ .alloc = alloc };
            defer w.list.deinit(alloc);
            try writeJsonValue(&w, input);
            if (!first) try buf.append(alloc, '\n');
            first = false;
            try buf.appendSlice(alloc, w.items());
        } else if (include_tool_blocks and std.mem.eql(u8, t, "tool_result")) {
            const c = block.object.get("content") orelse continue;
            if (c == .string) {
                if (!first) try buf.append(alloc, '\n');
                first = false;
                try buf.appendSlice(alloc, c.string);
            } else if (c == .array) {
                for (c.array.items) |ib| {
                    if (ib != .object) continue;
                    const itv = ib.object.get("type") orelse continue;
                    if (itv != .string or !std.mem.eql(u8, itv.string, "text")) continue;
                    const itx = ib.object.get("text") orelse continue;
                    if (itx != .string) continue;
                    if (!first) try buf.append(alloc, '\n');
                    first = false;
                    try buf.appendSlice(alloc, itx.string);
                }
            }
        }
    }
    return buf.toOwnedSlice(alloc);
}

fn isOnlyToolBlocks(content: std.json.Value) bool {
    if (content != .array) return false;
    if (content.array.items.len == 0) return false;
    for (content.array.items) |block| {
        if (block != .object) return false;
        const tv = block.object.get("type") orelse return false;
        if (tv != .string) return false;
        const t = tv.string;
        if (!std.mem.eql(u8, t, "tool_use") and !std.mem.eql(u8, t, "tool_result")) return false;
    }
    return true;
}

// ─── Per-file scan ───────────────────────────────────────────────────────────

const ScanMessage = struct {
    line_number: u32,
    timestamp: ?f64,
    timestamp_str: []const u8, // owned
    role: []const u8, // owned
    text_default: []const u8, // owned
    text_with_tools: []const u8, // owned
    is_only_tool_blocks: bool,
};

fn scanFile(alloc: Allocator, path: []const u8) ![]ScanMessage {
    const data = main.readEntireFile(alloc, path) catch return alloc.alloc(ScanMessage, 0);
    defer alloc.free(data);

    var out: std.ArrayList(ScanMessage) = .empty;
    errdefer out.deinit(alloc);

    var idx: u32 = 0;
    var iter = std.mem.splitScalar(u8, data, '\n');
    while (iter.next()) |raw| {
        idx += 1;
        const line = std.mem.trim(u8, raw, " \t\r\n");
        if (line.len == 0) continue;

        const parsed = std.json.parseFromSlice(
            std.json.Value,
            alloc,
            line,
            .{ .ignore_unknown_fields = true },
        ) catch continue;
        defer parsed.deinit();

        const root = parsed.value;
        if (root != .object) continue;

        const msg_v = root.object.get("message") orelse continue;
        if (msg_v != .object) continue;
        const role_v = msg_v.object.get("role") orelse continue;
        if (role_v != .string or role_v.string.len == 0) continue;
        const content_v = msg_v.object.get("content") orelse continue;

        var ts_str_owned: []u8 = try alloc.dupe(u8, "");
        var ts: ?f64 = null;
        if (root.object.get("timestamp")) |t| {
            if (t == .string and t.string.len > 0) {
                alloc.free(ts_str_owned);
                ts_str_owned = try alloc.dupe(u8, t.string);
                ts = main.parseTs(t.string) catch null;
            }
        }

        const text_default = try extractText(alloc, content_v, false);
        const text_with_tools = try extractText(alloc, content_v, true);
        const only_tool = isOnlyToolBlocks(content_v);
        const role_owned = try alloc.dupe(u8, role_v.string);

        try out.append(alloc, .{
            .line_number = idx,
            .timestamp = ts,
            .timestamp_str = ts_str_owned,
            .role = role_owned,
            .text_default = text_default,
            .text_with_tools = text_with_tools,
            .is_only_tool_blocks = only_tool,
        });
    }
    return out.toOwnedSlice(alloc);
}

// ─── Discovery ───────────────────────────────────────────────────────────────

const DiscoveredFile = struct {
    path: []const u8,
    slug: []const u8,
    session_id: []const u8,
};

fn discoverFiles(alloc: Allocator, root: []const u8, since: ?f64, cwd_filter: ?[]const u8) ![]DiscoveredFile {
    var out: std.ArrayList(DiscoveredFile) = .empty;
    errdefer out.deinit(alloc);

    if (is_windows) {
        try discoverWindows(alloc, &out, root, since, cwd_filter);
    } else {
        try discoverLinux(alloc, &out, root, since, cwd_filter);
    }
    return out.toOwnedSlice(alloc);
}

fn discoverWindows(alloc: Allocator, out: *std.ArrayList(DiscoveredFile), root: []const u8, since: ?f64, cwd_filter: ?[]const u8) !void {
    const platform = main.platform;
    const slug_pattern = try std.fmt.allocPrint(alloc, "{s}\\*", .{root});
    defer alloc.free(slug_pattern);
    const wpat = try std.unicode.utf8ToUtf16LeAllocZ(alloc, slug_pattern);
    defer alloc.free(wpat);

    var fd: platform.WIN32_FIND_DATAW = undefined;
    const h = platform.FindFirstFileW(wpat.ptr, &fd) orelse return;
    if (h == platform.INVALID_HANDLE_VALUE) return;
    defer _ = platform.FindClose(h);

    while (true) {
        const is_dir = (fd.dwFileAttributes & platform.FILE_ATTRIBUTE_DIRECTORY) != 0;
        if (is_dir) {
            const name_w = std.mem.span(@as([*:0]const u16, @ptrCast(&fd.cFileName)));
            const skip = name_w.len == 0 or
                (name_w.len == 1 and name_w[0] == '.') or
                (name_w.len == 2 and name_w[0] == '.' and name_w[1] == '.');
            if (!skip) {
                const slug = try std.unicode.utf16LeToUtf8Alloc(alloc, name_w);
                const passes = if (cwd_filter) |f| std.mem.eql(u8, slug, f) else true;
                if (passes) {
                    try scanSlugJsonlWindows(alloc, out, root, slug, since);
                }
            }
        }
        if (platform.FindNextFileW(h, &fd) == 0) break;
    }
}

fn scanSlugJsonlWindows(alloc: Allocator, out: *std.ArrayList(DiscoveredFile), root: []const u8, slug: []const u8, since: ?f64) !void {
    const platform = main.platform;
    const slug_dir = try std.fmt.allocPrint(alloc, "{s}\\{s}", .{ root, slug });
    const pat = try std.fmt.allocPrint(alloc, "{s}\\*.jsonl", .{slug_dir});
    defer alloc.free(pat);

    const wpat = try std.unicode.utf8ToUtf16LeAllocZ(alloc, pat);
    defer alloc.free(wpat);

    var fd: platform.WIN32_FIND_DATAW = undefined;
    const h = platform.FindFirstFileW(wpat.ptr, &fd) orelse {
        alloc.free(slug_dir);
        return;
    };
    if (h == platform.INVALID_HANDLE_VALUE) {
        alloc.free(slug_dir);
        return;
    }
    defer _ = platform.FindClose(h);

    while (true) {
        const is_dir = (fd.dwFileAttributes & platform.FILE_ATTRIBUTE_DIRECTORY) != 0;
        if (!is_dir) {
            const name_w = std.mem.span(@as([*:0]const u16, @ptrCast(&fd.cFileName)));
            const name = try std.unicode.utf16LeToUtf8Alloc(alloc, name_w);
            if (std.mem.endsWith(u8, name, ".jsonl")) {
                if (since) |cutoff| {
                    const mtime = fd.ftLastWriteTime.toUnix();
                    if (mtime < cutoff) {
                        alloc.free(name);
                        if (platform.FindNextFileW(h, &fd) == 0) break;
                        continue;
                    }
                }
                const sid = name[0 .. name.len - 6];
                const sid_owned = try alloc.dupe(u8, sid);
                const path = try std.fmt.allocPrint(alloc, "{s}\\{s}", .{ slug_dir, name });
                try out.append(alloc, .{
                    .path = path,
                    .slug = slug,
                    .session_id = sid_owned,
                });
            }
            alloc.free(name);
        }
        if (platform.FindNextFileW(h, &fd) == 0) break;
    }
}

fn discoverLinux(alloc: Allocator, out: *std.ArrayList(DiscoveredFile), root: []const u8, since: ?f64, cwd_filter: ?[]const u8) !void {
    const linux = main.platform.linux;
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

            if (cwd_filter) |f| {
                if (!std.mem.eql(u8, slug, f)) continue;
            }
            const slug_owned = try alloc.dupe(u8, slug);
            try scanSlugJsonlLinux(alloc, out, root, slug_owned, since);
        }
    }
}

fn scanSlugJsonlLinux(alloc: Allocator, out: *std.ArrayList(DiscoveredFile), root: []const u8, slug: []const u8, since: ?f64) !void {
    const linux = main.platform.linux;
    const slug_dir = try std.fmt.allocPrint(alloc, "{s}/{s}", .{ root, slug });
    const slug_z = try alloc.dupeZ(u8, slug_dir);
    defer alloc.free(slug_z);
    const slug_fd: i32 = @bitCast(@as(u32, @truncate(linux.openat(linux.AT.FDCWD, slug_z, .{ .DIRECTORY = true }, 0))));
    if (slug_fd < 0) {
        alloc.free(slug_dir);
        return;
    }
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
            if (!std.mem.endsWith(u8, name, ".jsonl")) continue;
            if (entry.type != linux.DT.REG and entry.type != linux.DT.UNKNOWN) continue;

            const path = try std.fmt.allocPrint(alloc, "{s}/{s}", .{ slug_dir, name });
            if (since) |cutoff| {
                if (!mtimeOkLinux(alloc, path, cutoff)) {
                    alloc.free(path);
                    continue;
                }
            }
            const sid = try alloc.dupe(u8, name[0 .. name.len - 6]);
            try out.append(alloc, .{
                .path = path,
                .slug = slug,
                .session_id = sid,
            });
        }
    }
}

fn mtimeOkLinux(alloc: Allocator, path: []const u8, earliest: f64) bool {
    const linux = main.platform.linux;
    const zpath = alloc.dupeZ(u8, path) catch return true;
    defer alloc.free(zpath);
    var statx_buf: linux.Statx = std.mem.zeroes(linux.Statx);
    const ret = linux.statx(linux.AT.FDCWD, zpath, 0, .{ .MTIME = true }, &statx_buf);
    const signed: isize = @bitCast(ret);
    if (signed < 0) return true;
    const mtime = @as(f64, @floatFromInt(statx_buf.mtime.sec)) +
        @as(f64, @floatFromInt(statx_buf.mtime.nsec)) / 1_000_000_000.0;
    return mtime >= earliest;
}

// ─── Snippet ─────────────────────────────────────────────────────────────────

fn nudgeWhitespace(text: []const u8, cut: usize, direction: i32, max_nudge: usize) usize {
    if (cut == 0 or cut == text.len) return cut;
    if (direction < 0) {
        const lo = if (cut > max_nudge) cut - max_nudge else 0;
        var i = cut;
        while (i > lo) : (i -= 1) {
            const b = text[i - 1];
            if (b == ' ' or b == '\t' or b == '\n' or b == '\r') return i;
        }
    } else {
        const hi = @min(cut + max_nudge, text.len);
        var i = cut;
        while (i < hi) : (i += 1) {
            const b = text[i];
            if (b == ' ' or b == '\t' or b == '\n' or b == '\r') return i;
        }
    }
    return cut;
}

fn makeSnippet(text: []const u8, first_match: [2]usize, snippet_chars: u32) []const u8 {
    const half: usize = @intCast(snippet_chars / 2);
    const mstart = first_match[0];
    const mend = first_match[1];
    var lo: usize = if (mstart > half) mstart - half else 0;
    var hi: usize = @min(mend + half, text.len);
    if (lo > 0) lo = nudgeWhitespace(text, lo, -1, 20);
    if (hi < text.len) hi = nudgeWhitespace(text, hi, 1, 20);
    return text[lo..hi];
}

// ─── Hit + context ───────────────────────────────────────────────────────────

const ContextTurn = struct {
    role: []const u8,
    text: []const u8,
    timestamp: []const u8,
};

const Hit = struct {
    timestamp: f64,
    timestamp_str: []const u8,
    session_id: []const u8,
    cwd_slug: []const u8,
    file_path: []const u8,
    line_number: u32,
    role: []const u8,
    snippet: []const u8,
    match_offsets: [][2]usize,
    context_before: []ContextTurn,
    context_after: []ContextTurn,
};

fn roleMatches(filter: Role, role: []const u8) bool {
    return switch (filter) {
        .both => true,
        .user => std.mem.eql(u8, role, "user"),
        .assistant => std.mem.eql(u8, role, "assistant"),
    };
}

fn buildContext(alloc: Allocator, msgs: []const ScanMessage, hit_idx: usize, n: u32) !struct { before: []ContextTurn, after: []ContextTurn } {
    if (n == 0) return .{ .before = &.{}, .after = &.{} };
    const nn: usize = n;
    const start: usize = if (hit_idx > nn) hit_idx - nn else 0;
    var before: std.ArrayList(ContextTurn) = .empty;
    var i: usize = start;
    while (i < hit_idx) : (i += 1) {
        try before.append(alloc, .{
            .role = msgs[i].role,
            .text = msgs[i].text_default,
            .timestamp = msgs[i].timestamp_str,
        });
    }
    var after: std.ArrayList(ContextTurn) = .empty;
    const end: usize = @min(hit_idx + 1 + nn, msgs.len);
    i = hit_idx + 1;
    while (i < end) : (i += 1) {
        try after.append(alloc, .{
            .role = msgs[i].role,
            .text = msgs[i].text_default,
            .timestamp = msgs[i].timestamp_str,
        });
    }
    return .{
        .before = try before.toOwnedSlice(alloc),
        .after = try after.toOwnedSlice(alloc),
    };
}

fn processFile(
    alloc: Allocator,
    file: DiscoveredFile,
    args: SearchArgs,
    pattern: *const Pattern,
    out: *std.ArrayList(Hit),
) !void {
    const msgs = try scanFile(alloc, file.path);
    for (msgs, 0..) |m, idx| {
        if (!roleMatches(args.role, m.role)) continue;
        if (!args.include_tool_blocks and m.is_only_tool_blocks) continue;
        if (m.timestamp) |ts| {
            if (args.since) |s| if (ts < s) continue;
            if (args.until) |u| if (ts > u) continue;
        } else if (args.since != null or args.until != null) {
            continue;
        }
        const searchable = if (args.include_tool_blocks) m.text_with_tools else m.text_default;
        if (searchable.len == 0) continue;

        const matches = try findAllMatches(alloc, pattern, searchable);
        defer alloc.free(matches);
        if (matches.len == 0) continue;

        const snippet_slice = makeSnippet(searchable, matches[0], args.snippet_chars);
        const snippet_owned = try alloc.dupe(u8, snippet_slice);
        const snippet_matches = try findAllMatches(alloc, pattern, snippet_owned);
        const ctx = try buildContext(alloc, msgs, idx, args.context);

        try out.append(alloc, .{
            .timestamp = m.timestamp orelse 0.0,
            .timestamp_str = m.timestamp_str,
            .session_id = file.session_id,
            .cwd_slug = file.slug,
            .file_path = file.path,
            .line_number = m.line_number,
            .role = m.role,
            .snippet = snippet_owned,
            .match_offsets = snippet_matches,
            .context_before = ctx.before,
            .context_after = ctx.after,
        });
    }
}

// ─── Sort + dedup ────────────────────────────────────────────────────────────

fn hitLessThan(_: void, a: Hit, b: Hit) bool {
    if (a.timestamp != b.timestamp) return a.timestamp > b.timestamp;
    const ord = std.mem.order(u8, a.session_id, b.session_id);
    if (ord != .eq) return ord == .lt;
    return a.line_number < b.line_number;
}

// ─── Output ──────────────────────────────────────────────────────────────────

fn writeHitJson(w: *Buf, h: Hit, host_root: []const u8) !void {
    try w.appendStr("{\"type\":\"hit\",\"session_id\":");
    try writeJsonString(w, h.session_id);
    try w.appendStr(",\"cwd_slug\":");
    try writeJsonString(w, h.cwd_slug);
    try w.appendStr(",\"host_root\":");
    try writeJsonString(w, host_root);
    try w.appendStr(",\"file_path\":");
    try writeJsonString(w, h.file_path);
    try w.appendFmt(",\"line_number\":{d}", .{h.line_number});
    try w.appendStr(",\"timestamp\":");
    try writeJsonString(w, h.timestamp_str);
    try w.appendStr(",\"role\":");
    try writeJsonString(w, h.role);
    try w.appendStr(",\"snippet\":");
    try writeJsonString(w, h.snippet);
    try w.appendStr(",\"match_offsets\":[");
    for (h.match_offsets, 0..) |m, k| {
        if (k > 0) try w.appendStr(",");
        try w.appendFmt("[{d},{d}]", .{ m[0], m[1] });
    }
    try w.appendStr("],\"context_before\":[");
    for (h.context_before, 0..) |t, k| {
        if (k > 0) try w.appendStr(",");
        try writeCtxTurn(w, t);
    }
    try w.appendStr("],\"context_after\":[");
    for (h.context_after, 0..) |t, k| {
        if (k > 0) try w.appendStr(",");
        try writeCtxTurn(w, t);
    }
    try w.appendStr("]}");
}

fn writeCtxTurn(w: *Buf, t: ContextTurn) !void {
    try w.appendStr("{\"role\":");
    try writeJsonString(w, t.role);
    try w.appendStr(",\"text\":");
    try writeJsonString(w, t.text);
    try w.appendStr(",\"timestamp\":");
    try writeJsonString(w, t.timestamp);
    try w.appendStr("}");
}

fn writeSummaryJson(w: *Buf, hits: u64, sessions: u64, roots: u64, files: u64, truncated: bool, elapsed_ms: u64) !void {
    try w.appendFmt(
        "{{\"type\":\"summary\",\"hits\":{d},\"sessions_matched\":{d},\"roots_walked\":{d},\"files_walked\":{d},\"truncated\":{s},\"elapsed_ms\":{d}}}",
        .{ hits, sessions, roots, files, if (truncated) "true" else "false", elapsed_ms },
    );
}

// ─── Top-level run ───────────────────────────────────────────────────────────

pub fn run(gpa: Allocator, argv: [][]const u8) !void {
    const t0 = main.perfNow();
    const frq = main.perfFreq();

    var arena = std.heap.ArenaAllocator.init(gpa);
    defer arena.deinit();
    const alloc = arena.allocator();

    const args = try parseArgs(alloc, argv);

    var pattern = compilePattern(alloc, args.pattern, args.regex, args.case_sensitive) catch {
        writeStderrFmt(alloc, "walker: search: bad pattern\n", .{});
        std.process.exit(2);
    };
    defer pattern.deinit();

    const files = try discoverFiles(alloc, args.projects_root, args.since, args.cwd);
    const files_walked: u64 = files.len;
    const host_root = args.projects_root;
    const roots_walked: u64 = 1;

    var hits: std.ArrayList(Hit) = .empty;
    for (files) |f| {
        try processFile(alloc, f, args, &pattern, &hits);
    }

    std.mem.sort(Hit, hits.items, {}, hitLessThan);

    // sessions_matched counted BEFORE truncation
    var session_set = std.StringHashMap(void).init(alloc);
    defer session_set.deinit();
    for (hits.items) |h| {
        const key = try std.fmt.allocPrint(alloc, "{s}/{s}", .{ h.cwd_slug, h.session_id });
        try session_set.put(key, {});
    }
    const sessions_matched: u64 = session_set.count();

    const total_unfiltered: u64 = hits.items.len;
    const truncated = total_unfiltered > args.limit;
    const limit_us: usize = args.limit;
    if (truncated) {
        hits.shrinkRetainingCapacity(limit_us);
    }

    const elapsed_ms: u64 = @intCast(@divTrunc((main.perfNow() - t0) * 1000, frq));
    const hits_output: u64 = if (args.count_only) total_unfiltered else hits.items.len;

    var w = Buf{ .alloc = alloc };
    switch (args.format) {
        .jsonl => {
            if (!args.count_only) {
                for (hits.items) |h| {
                    try writeHitJson(&w, h, host_root);
                    try w.appendStr("\n");
                }
            }
            try writeSummaryJson(&w, hits_output, sessions_matched, roots_walked, files_walked, truncated, elapsed_ms);
            try w.appendStr("\n");
            main.writeStdout(w.items());
        },
        .pretty => {
            if (!args.count_only) {
                for (hits.items) |h| {
                    w.appendFmt("[{s}] cwd={s} role={s} session={s}\n", .{ h.timestamp_str, h.cwd_slug, h.role, h.session_id }) catch {};
                    w.appendFmt("  {s}:{d}\n", .{ h.file_path, h.line_number }) catch {};
                    for (h.context_before) |t| {
                        w.appendFmt("  before: {s}\n", .{truncateForDisplay(t.text)}) catch {};
                    }
                    if (h.match_offsets.len > 0) {
                        const mo = h.match_offsets[0];
                        const ms = @min(mo[0], h.snippet.len);
                        const me = @min(mo[1], h.snippet.len);
                        w.appendFmt("  >>> {s}[{s}]{s} <<<\n", .{ h.snippet[0..ms], h.snippet[ms..me], h.snippet[me..] }) catch {};
                    } else {
                        w.appendFmt("  {s}\n", .{h.snippet}) catch {};
                    }
                    for (h.context_after) |t| {
                        w.appendFmt("  after:  {s}\n", .{truncateForDisplay(t.text)}) catch {};
                    }
                    w.appendStr("\n") catch {};
                }
            }
            w.appendFmt("{d} hits in {d} sessions across {d} roots ({d} files). truncated={s} elapsed {d}ms.\n", .{
                hits_output, sessions_matched, roots_walked, files_walked,
                if (truncated) "true" else "false", elapsed_ms,
            }) catch {};
            main.writeStdout(w.items());
        },
    }

    if (truncated) {
        writeStderrFmt(alloc, "walker: search: truncated to --limit={d} (had {d} total); narrow with --since\n", .{ args.limit, total_unfiltered });
    }
}

fn truncateForDisplay(s: []const u8) []const u8 {
    return if (s.len <= 120) s else s[0..120];
}
