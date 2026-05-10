// Native pace-walker -- C++ implementation.
// See ../SPEC.md for the contract every implementation must honor.

#include <algorithm>
#include <atomic>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iomanip>
#include <iostream>
#include <mutex>
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
};

[[noreturn]] static void die(std::string_view message) {
    std::cerr << "walker: " << message << "\n";
    std::exit(2);
}

static Args parse_args(int argc, char* argv[]) {
    Args args;
    for (int i = 1; i < argc; ++i) {
        std::string_view flag = argv[i];
        auto next = [&]() -> std::string_view {
            if (i + 1 >= argc) die(std::string(flag) + " needs a value");
            return argv[++i];
        };
        if (flag == "--period") {
            try { args.period_seconds = std::stoull(std::string(next())); }
            catch (...) { die("--period: invalid integer"); }
        } else if (flag == "--win-start") {
            try { args.win_start_unix = std::stod(std::string(next())); }
            catch (...) { die("--win-start: invalid number"); }
        } else if (flag == "--now") {
            try { args.now_unix = std::stod(std::string(next())); }
            catch (...) { die("--now: invalid number"); }
        } else if (flag == "--projects-root") {
            args.projects_root = fs::path(next());
        } else if (flag == "--version") {
            std::cout << "cpp/0.1.0\n";
            std::exit(0);
        } else {
            die(std::string("unknown flag: ") + std::string(flag));
        }
    }
    if (args.period_seconds == 0) {
        die("--period is required");
    }
    return args;
}

static fs::path default_projects_root() {
    const char* home = std::getenv("HOME");
    if (!home) home = std::getenv("USERPROFILE");
    if (home) return fs::path(home) / ".claude" / "projects";
    return fs::path(".claude/projects");
}

// ---------------------------------------------------------------------------
// Pricing
// ---------------------------------------------------------------------------

struct Rates {
    double input;   // per MTok
    double output;  // per MTok
};

static Rates rates_for(const std::string& model) {
    // Lowercase the model string for matching
    std::string low = model;
    std::transform(low.begin(), low.end(), low.begin(), ::tolower);
    if (low.find("opus") != std::string::npos)   return {5.0, 25.0};
    if (low.find("haiku") != std::string::npos)  return {1.0, 5.0};
    // sonnet or unknown -> sonnet rates
    return {3.0, 15.0};
}

static double cost_for(
    uint64_t input_tokens,
    uint64_t output_tokens,
    uint64_t cache_read_tokens,
    uint64_t cache_write_tokens,
    const std::string& model)
{
    auto [i_rate, o_rate] = rates_for(model);
    return (
        static_cast<double>(input_tokens) * i_rate
        + static_cast<double>(cache_read_tokens) * i_rate * 0.10
        + static_cast<double>(cache_write_tokens) * i_rate * 1.25
        + static_cast<double>(output_tokens) * o_rate
    ) / 1'000'000.0;
}

// ---------------------------------------------------------------------------
// ISO 8601 timestamp parsing
// Accepts "YYYY-MM-DDTHH:MM:SS[.fff]Z" or "+HH:MM" offset.
// Returns seconds since Unix epoch, or nullopt on failure.
// ---------------------------------------------------------------------------

static std::optional<double> parse_iso8601(std::string_view ts_view) {
    // Work with a local copy so we can mangle it
    std::string ts(ts_view);

    // Replace trailing Z with +00:00 to unify handling
    bool had_z = (!ts.empty() && ts.back() == 'Z');
    if (had_z) {
        ts.back() = '+';
        ts += "00:00";
    }

    // Expected minimum: "YYYY-MM-DDTHH:MM:SS" = 19 chars
    if (ts.size() < 19) return std::nullopt;

    // Parse components
    auto parse_int = [](const char* p, int len) -> int {
        int v = 0;
        for (int i = 0; i < len; ++i) {
            if (p[i] < '0' || p[i] > '9') return -1;
            v = v * 10 + (p[i] - '0');
        }
        return v;
    };

    int year   = parse_int(ts.c_str() + 0, 4);
    int month  = parse_int(ts.c_str() + 5, 2);
    int day    = parse_int(ts.c_str() + 8, 2);
    int hour   = parse_int(ts.c_str() + 11, 2);
    int minute = parse_int(ts.c_str() + 14, 2);
    int sec    = parse_int(ts.c_str() + 17, 2);

    if (year < 0 || month < 0 || day < 0 || hour < 0 || minute < 0 || sec < 0)
        return std::nullopt;
    if (month < 1 || month > 12 || day < 1 || day > 31) return std::nullopt;
    if (hour > 23 || minute > 59 || sec > 60) return std::nullopt;

    // Fractional seconds
    double frac = 0.0;
    size_t pos = 19;
    if (pos < ts.size() && ts[pos] == '.') {
        ++pos;
        double mult = 0.1;
        while (pos < ts.size() && ts[pos] >= '0' && ts[pos] <= '9') {
            frac += (ts[pos] - '0') * mult;
            mult *= 0.1;
            ++pos;
        }
    }

    // Timezone offset
    int tz_offset_sec = 0;
    if (pos < ts.size()) {
        char sign = ts[pos];
        if (sign == '+' || sign == '-') {
            if (pos + 5 < ts.size() + 1) {
                int tz_h = parse_int(ts.c_str() + pos + 1, 2);
                int tz_m = parse_int(ts.c_str() + pos + 4, 2);
                if (tz_h < 0 || tz_m < 0) return std::nullopt;
                tz_offset_sec = (tz_h * 3600 + tz_m * 60) * (sign == '-' ? -1 : 1);
            }
        }
    }

    // Convert to Unix epoch using Julian Day / proleptic Gregorian formula
    // Days from epoch 1970-01-01 using a well-known formula:
    //   JDN for date (y, m, d):
    int a = (14 - month) / 12;
    int y = year + 4800 - a;
    int m = month + 12 * a - 3;
    int jdn = day + (153 * m + 2) / 5 + 365 * y + y / 4 - y / 100 + y / 400 - 32045;
    // Unix epoch JDN
    static const int unix_epoch_jdn = 2440588; // 1970-01-01
    int64_t day_diff = static_cast<int64_t>(jdn) - unix_epoch_jdn;

    int64_t epoch_sec = day_diff * 86400LL
        + static_cast<int64_t>(hour) * 3600
        + static_cast<int64_t>(minute) * 60
        + static_cast<int64_t>(sec)
        - tz_offset_sec;

    return static_cast<double>(epoch_sec) + frac;
}

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
// lines naturally. Cost: one padded_string allocation per line — same
// shape as the previous std::getline + std::string_view code path.
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

            sj::padded_string padded(line);
            sj::ondemand::document doc;
            if (parser.iterate(padded).get(doc) != sj::SUCCESS) continue;

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

// Create a key string for (slug, session_id)
static std::string group_key(const std::string& slug, const std::string& sid) {
    return slug + '\0' + sid;
}

static GroupMap discover_groups(const fs::path& root, double earliest) {
    GroupMap groups;

    std::error_code ec;
    if (!fs::exists(root, ec)) return groups;

    // Parents: <root>/<slug>/<session_id>.jsonl
    for (auto& slug_entry : fs::directory_iterator(root, ec)) {
        if (!slug_entry.is_directory()) continue;
        std::string slug = slug_entry.path().filename().string();

        for (auto& file_entry : fs::directory_iterator(slug_entry.path(), ec)) {
            const auto& path = file_entry.path();

            if (!file_entry.is_regular_file()) continue;
            if (path.extension() != ".jsonl") continue;

            // Check mtime
            auto mtime = fs::last_write_time(path, ec);
            if (!ec) {
                // Convert file_time_type to unix epoch
                auto sys_time = std::chrono::time_point_cast<std::chrono::seconds>(
                    std::chrono::clock_cast<std::chrono::system_clock>(mtime));
                double mtime_unix = static_cast<double>(sys_time.time_since_epoch().count());
                if (mtime_unix < earliest) continue;
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

                // Check filename starts with "agent-"
                std::string fname = apath.filename().string();
                if (fname.substr(0, 6) != "agent-") continue;

                // Check mtime
                auto mtime = fs::last_write_time(apath, ec);
                if (!ec) {
                    auto sys_time = std::chrono::time_point_cast<std::chrono::seconds>(
                        std::chrono::clock_cast<std::chrono::system_clock>(mtime));
                    double mtime_unix = static_cast<double>(sys_time.time_since_epoch().count());
                    if (mtime_unix < earliest) continue;
                }

                groups[group_key(slug, sid)].push_back(apath);
            }
        }
    }

    return groups;
}

// ---------------------------------------------------------------------------
// Thread pool (simple work-stealing style)
// ---------------------------------------------------------------------------

class ThreadPool {
public:
    explicit ThreadPool(size_t num_threads)
        : stop_(false), task_index_(0)
    {
        threads_.reserve(num_threads);
        for (size_t i = 0; i < num_threads; ++i) {
            threads_.emplace_back([this] { worker(); });
        }
    }

    ~ThreadPool() {
        {
            std::unique_lock<std::mutex> lock(mutex_);
            stop_ = true;
        }
        cv_.notify_all();
        for (auto& t : threads_) t.join();
    }

    // Run tasks in parallel, return when all done
    void run(std::vector<std::function<void()>>& tasks) {
        {
            std::unique_lock<std::mutex> lock(mutex_);
            tasks_ = &tasks;
            task_index_ = 0;
            done_count_ = 0;
        }
        cv_.notify_all();

        // Also let the calling thread do work
        while (true) {
            size_t idx;
            {
                std::unique_lock<std::mutex> lock(mutex_);
                if (task_index_ >= tasks.size()) break;
                idx = task_index_++;
            }
            tasks[idx]();
            {
                std::unique_lock<std::mutex> lock(mutex_);
                ++done_count_;
            }
            done_cv_.notify_one();
        }

        // Wait for all workers to finish
        {
            std::unique_lock<std::mutex> lock(mutex_);
            done_cv_.wait(lock, [&] { return done_count_ >= tasks.size(); });
        }
    }

private:
    void worker() {
        while (true) {
            std::unique_lock<std::mutex> lock(mutex_);
            cv_.wait(lock, [this] { return stop_ || (tasks_ && task_index_ < tasks_->size()); });
            if (stop_ && (!tasks_ || task_index_ >= tasks_->size())) break;

            if (!tasks_ || task_index_ >= tasks_->size()) continue;
            size_t idx = task_index_++;
            lock.unlock();

            (*tasks_)[idx]();

            lock.lock();
            ++done_count_;
            lock.unlock();
            done_cv_.notify_one();
        }
    }

    std::vector<std::thread> threads_;
    std::mutex mutex_;
    std::condition_variable cv_, done_cv_;
    bool stop_;
    std::vector<std::function<void()>>* tasks_ = nullptr;
    size_t task_index_;
    size_t done_count_ = 0;
};

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

int main(int argc, char* argv[]) {
    auto started = std::chrono::steady_clock::now();

    Args args = parse_args(argc, argv);

    double now_unix;
    if (args.now_unix) {
        now_unix = *args.now_unix;
    } else {
        auto tp = std::chrono::system_clock::now();
        now_unix = static_cast<double>(
            std::chrono::duration_cast<std::chrono::milliseconds>(tp.time_since_epoch()).count()
        ) / 1000.0;
    }

    double period_cutoff = now_unix - static_cast<double>(args.period_seconds);
    double earliest = std::min(period_cutoff, args.win_start_unix);

    fs::path root = args.projects_root.value_or(default_projects_root());

    GroupMap groups = discover_groups(root, earliest);

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
