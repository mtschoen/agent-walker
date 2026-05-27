// events subcommand: emit one NDJSON record per accepted assistant turn.
// Reuses cost-mode's parse/dedup/filter/pricing verbatim (the shared helpers
// in main.zig); only aggregation differs — per-turn output instead of
// accumulated totals. Mirrors rust/src/events.rs and the cost-mode walk in
// main.zig. See ../SPEC.md §events for the full contract.

const std = @import("std");
const Allocator = std.mem.Allocator;
const main = @import("main.zig");
const walker_roots = @import("walker_roots.zig");

// ─── Args ────────────────────────────────────────────────────────────────────

const EventsArgs = struct {
    period: u64,
    /// Defaults to `now - period` when not supplied by the caller.
    win_start: f64,
    now_unix: f64,
    projects_root: []const u8,
    extra_roots: [][]const u8,
    read_config: bool,
};

fn parseArgs(alloc: Allocator, raw: [][]const u8) !EventsArgs {
    var period: u64 = 0;
    var win_start_raw: ?f64 = null;
    var now_override: ?f64 = null;
    var projects_root: ?[]const u8 = null;
    var extra_roots: std.ArrayList([]const u8) = .empty;
    var read_config = true;

    var i: usize = 0;
    while (i < raw.len) {
        const flag = raw[i];
        i += 1;
        if (std.mem.eql(u8, flag, "--period")) {
            period = std.fmt.parseInt(u64, main.grab(raw, &i, "--period"), 10) catch main.die("events: --period: invalid");
        } else if (std.mem.eql(u8, flag, "--win-start")) {
            win_start_raw = std.fmt.parseFloat(f64, main.grab(raw, &i, "--win-start")) catch main.die("events: --win-start: invalid");
        } else if (std.mem.eql(u8, flag, "--now")) {
            now_override = std.fmt.parseFloat(f64, main.grab(raw, &i, "--now")) catch main.die("events: --now: invalid");
        } else if (std.mem.eql(u8, flag, "--projects-root")) {
            projects_root = main.grab(raw, &i, "--projects-root");
        } else if (std.mem.eql(u8, flag, "--extra-projects-root")) {
            try extra_roots.append(alloc, main.grab(raw, &i, "--extra-projects-root"));
        } else if (std.mem.eql(u8, flag, "--no-config")) {
            read_config = false;
        } else if (std.mem.eql(u8, flag, "--version")) {
            main.writeStdout(main.VERSION ++ "\n");
            std.process.exit(0);
        } else {
            std.debug.print("walker: events: unknown flag: {s}\n", .{flag});
            std.process.exit(2);
        }
    }

    if (period == 0) main.die("events: --period is required");

    const now: f64 = now_override orelse main.nowUnix();
    // When --win-start is omitted, default to now - period (simplifies the
    // predicate to ts >= now - period, per SPEC §events).
    const win_start = win_start_raw orelse (now - @as(f64, @floatFromInt(period)));

    return EventsArgs{
        .period = period,
        .win_start = win_start,
        .now_unix = now,
        .projects_root = projects_root orelse try main.defaultRoot(alloc),
        .extra_roots = try extra_roots.toOwnedSlice(alloc),
        .read_config = read_config,
    };
}

// ─── Output record ───────────────────────────────────────────────────────────

const EventRecord = struct {
    ts: f64,
    usd: f64,
    model: []const u8, // owned (lowercased), valid for arena lifetime
    session_id: []const u8, // borrowed from the discover key
    slug: []const u8, // borrowed from the discover key
};

fn recordLessThan(_: void, a: EventRecord, b: EventRecord) bool {
    // Sort by (ts, session_id, model), matching SPEC §events §Ordering and
    // the conformance multiset tiebreaker.
    if (a.ts != b.ts) return a.ts < b.ts;
    const sid_cmp = std.mem.order(u8, a.session_id, b.session_id);
    if (sid_cmp != .eq) return sid_cmp == .lt;
    return std.mem.order(u8, a.model, b.model) == .lt;
}

// ─── Per-group walker ────────────────────────────────────────────────────────

/// Walk one (slug, session_id) group and append an EventRecord for every
/// accepted assistant turn. Dedup is per-group via a local seen_ids set,
/// exactly matching cost-mode's walk_group contract in main.zig.
fn walkGroupEvents(
    alloc: Allocator,
    paths: []const []const u8,
    slug: []const u8,
    session_id: []const u8,
    cutoff: f64,
    out: *std.ArrayList(EventRecord),
) void {
    var seen = std.StringHashMap(void).init(alloc);
    defer {
        var it = seen.keyIterator();
        while (it.next()) |k| alloc.free(k.*);
        seen.deinit();
    }

    for (paths) |path| {
        const data = main.readEntireFile(alloc, path) catch continue;
        defer alloc.free(data);

        var iter = std.mem.splitScalar(u8, data, '\n');
        while (iter.next()) |line| {
            processLine(alloc, line, &seen, slug, session_id, cutoff, out);
        }
    }
}

fn processLine(
    alloc: Allocator,
    raw: []const u8,
    seen: *std.StringHashMap(void),
    slug: []const u8,
    session_id: []const u8,
    cutoff: f64,
    out: *std.ArrayList(EventRecord),
) void {
    const line = std.mem.trim(u8, raw, " \t\r\n");
    if (line.len == 0) return;

    var scanner = std.json.Scanner.initCompleteInput(alloc, line);
    defer scanner.deinit();

    if (!(main.enterObject(&scanner) catch return)) return;

    var role_assistant = false;
    var id_str: ?[]const u8 = null;
    var ts_value: ?f64 = null;
    var model: []const u8 = "";
    var inp: u64 = 0;
    var out_: u64 = 0;
    var cr: u64 = 0;
    var cw: u64 = 0;
    var web_searches: u64 = 0;

    while (true) {
        const key = (main.parseObjectKey(&scanner, alloc) catch return) orelse break;
        if (std.mem.eql(u8, key, "message")) {
            if (!(main.enterObject(&scanner) catch return)) continue;
            while (true) {
                const mkey = (main.parseObjectKey(&scanner, alloc) catch return) orelse break;
                if (std.mem.eql(u8, mkey, "role")) {
                    const v = main.parseStringValue(&scanner, alloc) catch return;
                    if (v) |s| role_assistant = std.mem.eql(u8, s, "assistant");
                } else if (std.mem.eql(u8, mkey, "id")) {
                    id_str = main.parseStringValue(&scanner, alloc) catch return;
                } else if (std.mem.eql(u8, mkey, "model")) {
                    const v = main.parseStringValue(&scanner, alloc) catch return;
                    if (v) |s| model = s;
                } else if (std.mem.eql(u8, mkey, "usage")) {
                    if (!(main.enterObject(&scanner) catch return)) continue;
                    while (true) {
                        const ukey = (main.parseObjectKey(&scanner, alloc) catch return) orelse break;
                        if (std.mem.eql(u8, ukey, "input_tokens")) {
                            inp = main.parseU64Value(&scanner, alloc) catch return;
                        } else if (std.mem.eql(u8, ukey, "output_tokens")) {
                            out_ = main.parseU64Value(&scanner, alloc) catch return;
                        } else if (std.mem.eql(u8, ukey, "cache_read_input_tokens")) {
                            cr = main.parseU64Value(&scanner, alloc) catch return;
                        } else if (std.mem.eql(u8, ukey, "cache_creation_input_tokens")) {
                            cw = main.parseU64Value(&scanner, alloc) catch return;
                        } else if (std.mem.eql(u8, ukey, "server_tool_use")) {
                            // Nested object: descend for web_search_requests.
                            if (main.enterObject(&scanner) catch return) {
                                while (true) {
                                    const skey = (main.parseObjectKey(&scanner, alloc) catch return) orelse break;
                                    if (std.mem.eql(u8, skey, "web_search_requests")) {
                                        web_searches = main.parseU64Value(&scanner, alloc) catch return;
                                    } else {
                                        scanner.skipValue() catch return;
                                    }
                                }
                            }
                        } else {
                            scanner.skipValue() catch return;
                        }
                    }
                } else {
                    scanner.skipValue() catch return;
                }
            }
        } else if (std.mem.eql(u8, key, "timestamp")) {
            const v = main.parseStringValue(&scanner, alloc) catch return;
            if (v) |s| ts_value = main.parseTs(s) catch null;
        } else {
            scanner.skipValue() catch return;
        }
    }

    // Filter 1: assistant role only.
    if (!role_assistant) return;

    // Filter 2: dedup by message.id within this group.
    if (id_str) |id| {
        if (id.len > 0) {
            if (seen.contains(id)) return;
            const k = alloc.dupe(u8, id) catch return;
            seen.put(k, {}) catch {
                alloc.free(k);
                return;
            };
        }
    }

    // Filter 3 + 4: timestamp must parse and pass the window predicate.
    const ts = ts_value orelse return;
    if (ts < cutoff) return;

    const usd = main.modelCost(inp, out_, cr, cw, web_searches, model);

    // model is emitted lowercased (SPEC: "Lowercased model id ...").
    const model_lower = alloc.alloc(u8, model.len) catch return;
    _ = std.ascii.lowerString(model_lower, model);

    out.append(alloc, .{
        .ts = ts,
        .usd = usd,
        .model = model_lower,
        .session_id = session_id,
        .slug = slug,
    }) catch return;
}

// ─── JSON string escaping ────────────────────────────────────────────────────

fn appendJsonString(buf: *std.ArrayList(u8), alloc: Allocator, s: []const u8) !void {
    try buf.append(alloc, '"');
    for (s) |c| {
        switch (c) {
            '"' => try buf.appendSlice(alloc, "\\\""),
            '\\' => try buf.appendSlice(alloc, "\\\\"),
            '\n' => try buf.appendSlice(alloc, "\\n"),
            '\r' => try buf.appendSlice(alloc, "\\r"),
            '\t' => try buf.appendSlice(alloc, "\\t"),
            8 => try buf.appendSlice(alloc, "\\b"),
            12 => try buf.appendSlice(alloc, "\\f"),
            else => {
                if (c < 0x20) {
                    const escaped = try std.fmt.allocPrint(alloc, "\\u{x:0>4}", .{c});
                    try buf.appendSlice(alloc, escaped);
                } else {
                    try buf.append(alloc, c);
                }
            },
        }
    }
    try buf.append(alloc, '"');
}

// ─── Entry point ─────────────────────────────────────────────────────────────

pub fn run(gpa: Allocator, argv: [][]const u8) !void {
    var arena = std.heap.ArenaAllocator.init(gpa);
    defer arena.deinit();
    const alloc = arena.allocator();

    const args = try parseArgs(alloc, argv);

    // Effective cutoff = min(now - period, win_start), per SPEC §events.
    const period_cutoff = args.now_unix - @as(f64, @floatFromInt(args.period));
    const cutoff = @min(period_cutoff, args.win_start);

    const roots = try walker_roots.resolveRoots(alloc, args.projects_root, args.extra_roots, args.read_config);
    if (roots.len == 0) {
        // Primary root doesn't exist — emit nothing, exit 0 (consistent with
        // cost-mode's empty-fleet case).
        return;
    }

    // discover applies the mtime prune using the same cutoff.
    var grp_map = try main.discover(alloc, roots, cutoff);

    var records: std.ArrayList(EventRecord) = .empty;
    var it = grp_map.iterator();
    while (it.next()) |kv| {
        // Keys are "{slug}\x00{sid}" (see main.addFile). Split to recover the
        // slug and session_id for the output records.
        const key = kv.key_ptr.*;
        const sep = std.mem.indexOfScalar(u8, key, 0) orelse continue;
        const slug = key[0..sep];
        const session_id = key[sep + 1 ..];
        walkGroupEvents(alloc, kv.value_ptr.*.items, slug, session_id, cutoff, &records);
    }

    // Sort for deterministic output: (ts, session_id, model).
    std.mem.sort(EventRecord, records.items, {}, recordLessThan);

    // Emit NDJSON — one line per record, field order fixed per SPEC:
    // ts, usd, model, session_id, slug.
    var buf: std.ArrayList(u8) = .empty;
    for (records.items) |rec| {
        try buf.appendSlice(alloc, "{\"ts\":");
        try buf.appendSlice(alloc, try std.fmt.allocPrint(alloc, "{d}", .{rec.ts}));
        try buf.appendSlice(alloc, ",\"usd\":");
        try buf.appendSlice(alloc, try std.fmt.allocPrint(alloc, "{d}", .{rec.usd}));
        try buf.appendSlice(alloc, ",\"model\":");
        try appendJsonString(&buf, alloc, rec.model);
        try buf.appendSlice(alloc, ",\"session_id\":");
        try appendJsonString(&buf, alloc, rec.session_id);
        try buf.appendSlice(alloc, ",\"slug\":");
        try appendJsonString(&buf, alloc, rec.slug);
        try buf.appendSlice(alloc, "}\n");
    }
    main.writeStdout(buf.items);
}
