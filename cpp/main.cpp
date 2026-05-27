// Native pace-walker -- C++ implementation.
// See ../SPEC.md for the contract every implementation must honor.
//
// Entry point + cost-mode walker. Beacon-mode subcommands live in
// beacons.cpp and shared helpers (ISO 8601, default root) in common.hpp.

#include "beacons.hpp"
#include "events.hpp"
#include "search.hpp"
#include "common.hpp"
#include "pricing.hpp"
#include "walker_roots.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <optional>
#include <sstream>
#include <string>
#include <string_view>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <vector>

// simdjson on-demand (built from source by CMake)
#include <simdjson.h>

namespace fs = std::filesystem;
namespace sj = simdjson;

// ---------------------------------------------------------------------------
// Argument parsing
// ---------------------------------------------------------------------------

struct Args {
    uint64_t period_seconds = 0;
    double win_start_unix = 0.0;
    std::optional<double> now_unix;
    std::optional<fs::path> projects_root;
    std::vector<fs::path> extra_projects_roots;
    bool read_config = true;
};

static const char* const HELP = R"(claude-walker - fast cost & progress walker over Claude Code transcripts

USAGE:
    claude-walker [SUBCOMMAND] [OPTIONS]

With no subcommand it runs `cost` (back-compat for the status line).

SUBCOMMANDS:
    cost              Trailing + window USD over the transcript fleet (default)
    search <pattern>  Cross-root/-machine content search over transcripts
    events            One NDJSON line per assistant turn (ts, usd, model, session)
    beacons-latest    Most recent <progress-beacon> for a session
    beacons-history   Calibration bias_factor over begin/end beacon pairs

COST OPTIONS (default mode):
    --period <seconds>            Required. Trailing-window length.
    --win-start <unix>            Required. Cost-window start (unix epoch).
    --projects-root <path>        Transcript root (default: ~/.claude/projects).
    --extra-projects-root <path>  Additional root; repeatable.
    --no-config                   Skip ~/.claude/walker-roots.json extras.
    --now <unix>                  Pin "now" (default: wall clock; for tests).

GLOBAL:
    -h, --help     Show this help.
    --version      Print <lang>/<version>.

Full contract: SPEC.md in the source tree.
)";

static bool is_help_flag(const std::string& arg) {
    return arg == "-h" || arg == "--help";
}

// Help is shown when: no args, or the first arg is -h/--help, or the first
// arg is a known subcommand followed by -h/--help. See SPEC.md "Help & usage".
static bool wants_help(const std::vector<std::string>& raw) {
    if (raw.empty()) return true;
    const std::string& first = raw.front();
    if (is_help_flag(first)) return true;
    static const char* const subs[] = {
        "cost", "beacons-latest", "beacons-history", "search", "events"};
    for (const char* sub : subs) {
        if (first == sub) {
            return raw.size() > 1 && is_help_flag(raw[1]);
        }
    }
    return false;
}

[[noreturn]] static void die(std::string_view message) {
    std::cerr << "walker: " << message << "\n";
    std::cerr << "Run 'claude-walker --help' for usage.\n";
    std::exit(2);
}

static Args parse_args(const std::vector<std::string>& argv) {
    Args args;
    for (size_t i = 0; i < argv.size(); ++i) {
        const std::string& flag = argv[i];
        auto next = [&]() -> const std::string& {
            if (i + 1 >= argv.size()) die(flag + " needs a value");
            return argv[++i];
        };
        if (flag == "--period") {
            try { args.period_seconds = std::stoull(next()); }
            catch (...) { die("--period: invalid integer"); }
        } else if (flag == "--win-start") {
            try { args.win_start_unix = std::stod(next()); }
            catch (...) { die("--win-start: invalid number"); }
        } else if (flag == "--now") {
            try { args.now_unix = std::stod(next()); }
            catch (...) { die("--now: invalid number"); }
        } else if (flag == "--projects-root") {
            args.projects_root = fs::path(next());
        } else if (flag == "--version") {
            std::cout << walker::VERSION << "\n";
            std::exit(0);
        } else if (flag == "--extra-projects-root") {
            args.extra_projects_roots.emplace_back(next());
        } else if (flag == "--no-config") {
            args.read_config = false;
        } else {
            die(std::string("unknown flag: ") + flag);
        }
    }
    if (args.period_seconds == 0) {
        die("--period is required");
    }
    return args;
}

// Shared helpers from common.hpp + pricing.hpp. Pricing (rates_for/cost_for),
// group_key, and file_mtime_to_unix are shared verbatim with events.cpp.
using walker::default_projects_root;
using walker::parse_iso8601;
using walker::cost_for;
using walker::group_key;
using walker::file_mtime_to_unix;

// ---------------------------------------------------------------------------
// Group walking
// ---------------------------------------------------------------------------

struct GroupResult {
    double trailing = 0.0;
    double window = 0.0;
};

// Per-line walk via simdjson on-demand. We iterate the top-level object
// once and dispatch on key name — the on-demand API rewards forward-only
// access, so we extract every needed field in one pass without backing up.
//
// Why per-line iterate() and not iterate_many(): iterate_many bails the
// entire document_stream on the first malformed line and can't resume,
// so we'd lose every line after a bad one. Per-line iterate skips bad
// lines naturally. Zero per-line allocation: we hand simdjson a
// padded_string_view that points into the whole-file `data` buffer
// (padded_string::load guarantees SIMDJSON_PADDING bytes of tail zero
// padding), so the parser never sees a fresh heap allocation.
static GroupResult walk_group(
    const std::vector<fs::path>& paths,
    double period_cutoff,
    double win_start_unix)
{
    double earliest = std::min(period_cutoff, win_start_unix);
    GroupResult result;
    std::unordered_set<std::string> seen_ids;

    sj::ondemand::parser parser;

    for (const auto& path : paths) {
        sj::padded_string data;
        if (sj::padded_string::load(path.string()).get(data) != sj::SUCCESS) continue;

        std::string_view buffer(data);
        size_t pos = 0;
        while (pos < buffer.size()) {
            size_t newline = buffer.find('\n', pos);
            size_t end = (newline == std::string_view::npos) ? buffer.size() : newline;
            size_t line_end = end;
            if (line_end > pos && buffer[line_end - 1] == '\r') --line_end;
            std::string_view line = buffer.substr(pos, line_end - pos);
            pos = (newline == std::string_view::npos) ? buffer.size() : newline + 1;

            // Skip empty / whitespace-only lines
            bool blank = true;
            for (char c : line) {
                if (!std::isspace(static_cast<unsigned char>(c))) { blank = false; break; }
            }
            if (blank) continue;

            size_t line_off = static_cast<size_t>(line.data() - buffer.data());
            sj::padded_string_view view(
                line.data(), line.size(),
                buffer.size() - line_off + sj::SIMDJSON_PADDING);
            sj::ondemand::document doc;
            if (parser.iterate(view).get(doc) != sj::SUCCESS) continue;

            sj::ondemand::object root;
            if (doc.get_object().get(root) != sj::SUCCESS) continue;

            std::string_view timestamp_view;
            bool has_timestamp = false;

            bool is_assistant = false;
            std::string_view message_id_view;
            bool has_message_id = false;
            std::string model;
            uint64_t input_tokens = 0, output_tokens = 0,
                     cache_read_tokens = 0, cache_write_tokens = 0;
            bool message_seen = false;

            for (auto root_field : root) {
                std::string_view key;
                if (root_field.unescaped_key().get(key) != sj::SUCCESS) continue;

                if (key == "timestamp") {
                    if (root_field.value().get_string().get(timestamp_view) == sj::SUCCESS) {
                        has_timestamp = !timestamp_view.empty();
                    }
                } else if (key == "message") {
                    sj::ondemand::object msg_obj;
                    if (root_field.value().get_object().get(msg_obj) != sj::SUCCESS) continue;
                    message_seen = true;

                    for (auto msg_field : msg_obj) {
                        std::string_view msg_key;
                        if (msg_field.unescaped_key().get(msg_key) != sj::SUCCESS) continue;

                        if (msg_key == "role") {
                            std::string_view role_view;
                            if (msg_field.value().get_string().get(role_view) == sj::SUCCESS) {
                                is_assistant = (role_view == "assistant");
                            }
                        } else if (msg_key == "id") {
                            std::string_view id_view;
                            if (msg_field.value().get_string().get(id_view) == sj::SUCCESS) {
                                if (!id_view.empty()) {
                                    message_id_view = id_view;
                                    has_message_id = true;
                                }
                            }
                        } else if (msg_key == "model") {
                            std::string_view model_view;
                            if (msg_field.value().get_string().get(model_view) == sj::SUCCESS) {
                                model.assign(model_view.data(), model_view.size());
                            }
                        } else if (msg_key == "usage") {
                            sj::ondemand::object usage_obj;
                            if (msg_field.value().get_object().get(usage_obj) != sj::SUCCESS) continue;

                            for (auto usage_field : usage_obj) {
                                std::string_view usage_key;
                                if (usage_field.unescaped_key().get(usage_key) != sj::SUCCESS) continue;

                                uint64_t value = 0;
                                if (usage_field.value().get_uint64().get(value) != sj::SUCCESS) continue;

                                if (usage_key == "input_tokens") input_tokens = value;
                                else if (usage_key == "output_tokens") output_tokens = value;
                                else if (usage_key == "cache_read_input_tokens") cache_read_tokens = value;
                                else if (usage_key == "cache_creation_input_tokens") cache_write_tokens = value;
                            }
                        }
                    }
                }
            }

            if (!message_seen || !is_assistant) continue;

            if (has_message_id) {
                std::string mid(message_id_view);
                if (!seen_ids.insert(std::move(mid)).second) continue;
            }

            if (!has_timestamp) continue;
            auto ts_opt = parse_iso8601(timestamp_view);
            if (!ts_opt) continue;
            double ts = *ts_opt;
            if (ts < earliest) continue;

            double cost = cost_for(input_tokens, output_tokens,
                                   cache_read_tokens, cache_write_tokens, model);

            if (ts >= period_cutoff) result.trailing += cost;
            if (ts >= win_start_unix) result.window += cost;
        }
    }

    return result;
}

// ---------------------------------------------------------------------------
// Discovery
// ---------------------------------------------------------------------------

using GroupMap = std::unordered_map<std::string, std::vector<fs::path>>;

static GroupMap discover_groups(
    const std::vector<fs::path>& roots,
    double earliest)
{
    GroupMap groups;

    for (const fs::path& root : roots) {
        std::error_code ec;
        if (!fs::exists(root, ec)) continue;

        // Parents: <root>/<slug>/<session_id>.jsonl
        for (auto& slug_entry : fs::directory_iterator(root, ec)) {
            if (!slug_entry.is_directory()) continue;
            std::string slug = slug_entry.path().filename().string();

            for (auto& file_entry : fs::directory_iterator(slug_entry.path(), ec)) {
                const auto& path = file_entry.path();

                if (!file_entry.is_regular_file()) continue;
                if (path.extension() != ".jsonl") continue;

                auto mtime = fs::last_write_time(path, ec);
                if (!ec) {
                    if (file_mtime_to_unix(mtime) < earliest) continue;
                }

                std::string sid = path.stem().string();
                groups[group_key(slug, sid)].push_back(path);
            }

            // Subagents: <root>/<slug>/<session_id>/subagents/agent-*.jsonl
            for (auto& session_entry : fs::directory_iterator(slug_entry.path(), ec)) {
                if (!session_entry.is_directory()) continue;
                std::string sid = session_entry.path().filename().string();

                fs::path subagents_dir = session_entry.path() / "subagents";
                if (!fs::is_directory(subagents_dir, ec)) continue;

                for (auto& agent_entry : fs::directory_iterator(subagents_dir, ec)) {
                    const auto& apath = agent_entry.path();
                    if (!agent_entry.is_regular_file()) continue;
                    if (apath.extension() != ".jsonl") continue;

                    std::string fname = apath.filename().string();
                    if (fname.substr(0, 6) != "agent-") continue;

                    auto mtime = fs::last_write_time(apath, ec);
                    if (!ec) {
                        if (file_mtime_to_unix(mtime) < earliest) continue;
                    }

                    groups[group_key(slug, sid)].push_back(apath);
                }
            }
        }
    }

    return groups;
}

// ---------------------------------------------------------------------------
// Subcommand: cost (the original, default-shape walker behavior)
// ---------------------------------------------------------------------------

static int run_cost(const std::vector<std::string>& argv) {
    auto started = std::chrono::steady_clock::now();

    Args args = parse_args(argv);

    double now_unix = args.now_unix.value_or(walker::current_unix());

    double period_cutoff = now_unix - static_cast<double>(args.period_seconds);
    double earliest = std::min(period_cutoff, args.win_start_unix);

    fs::path primary = args.projects_root.value_or(default_projects_root());
    std::vector<fs::path> roots = walker::resolve_roots(
        primary, args.extra_projects_roots, args.read_config);

    GroupMap groups = discover_groups(roots, earliest);

    size_t total_files = 0;
    for (auto& [key, paths] : groups) total_files += paths.size();
    size_t total_groups = groups.size();

    // Collect group paths into a vector for parallel dispatch
    std::vector<std::vector<fs::path>> group_list;
    group_list.reserve(total_groups);
    for (auto& [key, paths] : groups) {
        group_list.push_back(std::move(paths));
    }

    // Parallel walk
    size_t num_workers = std::min<size_t>(8, std::thread::hardware_concurrency());
    if (num_workers == 0) num_workers = 4;

    std::vector<GroupResult> results(total_groups);
    std::atomic<size_t> task_index(0);

    auto run_tasks = [&]() {
        while (true) {
            size_t idx = task_index.fetch_add(1, std::memory_order_relaxed);
            if (idx >= group_list.size()) break;
            results[idx] = walk_group(group_list[idx], period_cutoff, args.win_start_unix);
        }
    };

    std::vector<std::thread> threads;
    // Use num_workers - 1 background threads; main thread also works
    size_t bg_threads = (num_workers > 1) ? num_workers - 1 : 0;
    threads.reserve(bg_threads);
    for (size_t i = 0; i < bg_threads; ++i) {
        threads.emplace_back(run_tasks);
    }
    run_tasks(); // main thread participates
    for (auto& t : threads) t.join();

    // Aggregate results
    double trailing = 0.0, window = 0.0;
    for (const auto& r : results) {
        trailing += r.trailing;
        window += r.window;
    }

    auto elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now() - started).count();

    // Output one JSON line
    std::cout
        << "{\"trailing_usd\":" << std::fixed << std::setprecision(6) << trailing
        << ",\"window_usd\":" << window
        << ",\"files_walked\":" << total_files
        << ",\"groups\":" << total_groups
        << ",\"elapsed_ms\":" << elapsed_ms
        << "}\n";

    return 0;
}

// ---------------------------------------------------------------------------
// main — subcommand dispatch
// ---------------------------------------------------------------------------

int main(int argc, char* argv[]) {
    std::vector<std::string> raw;
    raw.reserve(argc > 1 ? argc - 1 : 0);
    for (int i = 1; i < argc; ++i) raw.emplace_back(argv[i]);

    if (wants_help(raw)) {
        std::cout << HELP;
        return 0;
    }

    // Subcommand routing. Bare flag invocations (first arg starts with '-')
    // route to cost mode for back-compat.
    std::string subcommand = "cost";
    std::vector<std::string> rest;
    if (!raw.empty()) {
        const std::string& first = raw.front();
        if (first == "cost" || first == "beacons-latest" || first == "beacons-history" || first == "search" || first == "events") {
            subcommand = first;
            rest.assign(raw.begin() + 1, raw.end());
        } else if (!first.empty() && first.front() == '-') {
            rest = raw;  // bare-flag -> cost mode
        } else {
            std::cerr << "walker: unknown subcommand: " << first << "\n";
            std::cerr << "Run 'claude-walker --help' for usage.\n";
            return 2;
        }
    }

    if (subcommand == "cost") return run_cost(rest);
    if (subcommand == "beacons-latest") return walker::beacons::run_latest(rest);
    if (subcommand == "beacons-history") return walker::beacons::run_history(rest);
    if (subcommand == "search") return walker::search::run(rest);
    if (subcommand == "events") return walker::events::run(rest);
    return 2;  // unreachable
}
