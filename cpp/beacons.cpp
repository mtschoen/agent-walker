// beacons-latest and beacons-history subcommands. See ../SPEC.md
// "Subcommands" for the contract.
//
// Algorithm mirrors rust/src/beacons.rs:
//   - Beacon JSON extracted via regex from message.content[*].text where
//     type=="text". Pattern: <progress-beacon>\s*({...})\s*</progress-beacon>.
//     `[\s\S]` substitutes for Rust `(?s).` since std::regex's `.` does
//     not match newlines.
//   - Beacon must parse AND have all four required fields (kind,
//     eta_seconds, summary, drift); otherwise silently skip.
//   - beacons-latest: pick the entry with the highest timestamp; if multiple
//     beacons exist within one entry, pick the LAST regex match in that
//     entry's text.
//   - beacons-history: group by (slug, session_id), find earliest "begin"
//     and latest "end" within window. Emit pair when end_ts > begin_ts.
//     bias_factor = median over (active_elapsed/eta) ratios with eta > 0,
//     where active_elapsed = actual_elapsed - idle_excluded. idle_excluded
//     is the sum of gaps preceding REAL user prompts (type=="user" entries
//     whose message.content is NOT an array containing a tool_result block)
//     inside the window. Bare-string content counts as a real user prompt.

#include "beacons.hpp"
#include "common.hpp"
#include "walker_roots.hpp"

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <iomanip>
#include <iostream>
#include <optional>
#include <regex>
#include <sstream>
#include <string>
#include <string_view>
#include <tuple>
#include <unordered_map>
#include <vector>

#include <simdjson.h>

namespace walker::beacons {

namespace fs = std::filesystem;
namespace sj = simdjson;

namespace {

struct Beacon {
    std::string kind;
    double eta_seconds = 0.0;
    std::string summary;
    std::string drift;
    std::optional<int64_t> beats_left;
};

const std::regex& beacon_re() {
    // [\s\S] matches any char including newlines (std::regex's `.` does not).
    // Non-greedy {[\s\S]*?} so two beacons in one text don't merge.
    static const std::regex re(
        R"(<progress-beacon>\s*(\{[\s\S]*?\})\s*</progress-beacon>)");
    return re;
}

// Try to parse a JSON beacon body. All four required fields must be present
// and well-typed; otherwise return nullopt (silently skip, per spec).
std::optional<Beacon> parse_beacon_body(std::string_view body) {
    sj::ondemand::parser parser;
    sj::padded_string padded(body);
    sj::ondemand::document doc;
    if (parser.iterate(padded).get(doc) != sj::SUCCESS) return std::nullopt;
    sj::ondemand::object obj;
    if (doc.get_object().get(obj) != sj::SUCCESS) return std::nullopt;

    Beacon b;
    bool has_kind = false, has_eta = false, has_summary = false, has_drift = false;

    for (auto field : obj) {
        std::string_view key;
        if (field.unescaped_key().get(key) != sj::SUCCESS) continue;

        if (key == "kind") {
            std::string_view v;
            if (field.value().get_string().get(v) != sj::SUCCESS) return std::nullopt;
            b.kind.assign(v.data(), v.size());
            has_kind = true;
        } else if (key == "eta_seconds") {
            // Accept either int or double.
            double dv;
            auto val = field.value();
            if (val.get_double().get(dv) == sj::SUCCESS) {
                b.eta_seconds = dv;
                has_eta = true;
            } else {
                int64_t iv;
                if (val.get_int64().get(iv) == sj::SUCCESS) {
                    b.eta_seconds = static_cast<double>(iv);
                    has_eta = true;
                } else {
                    return std::nullopt;
                }
            }
        } else if (key == "summary") {
            std::string_view v;
            if (field.value().get_string().get(v) != sj::SUCCESS) return std::nullopt;
            b.summary.assign(v.data(), v.size());
            has_summary = true;
        } else if (key == "drift") {
            std::string_view v;
            if (field.value().get_string().get(v) != sj::SUCCESS) return std::nullopt;
            b.drift.assign(v.data(), v.size());
            has_drift = true;
        } else if (key == "beats_left") {
            int64_t iv;
            if (field.value().get_int64().get(iv) == sj::SUCCESS) {
                b.beats_left = iv;
            }
        }
    }

    if (!has_kind || !has_eta || !has_summary || !has_drift) return std::nullopt;
    return b;
}

// Walk one transcript file and call `cb` for each parseable assistant entry
// with its (combined_text, timestamp_string). Skips silently on JSON or
// other errors. Used by beacons-latest (no need for user events).
template <typename Callback>
void walk_assistant_entries(const fs::path& path, Callback&& cb) {
    sj::padded_string data;
    if (sj::padded_string::load(path.string()).get(data) != sj::SUCCESS) return;
    sj::ondemand::parser parser;

    std::string_view buffer(data);
    size_t pos = 0;
    while (pos < buffer.size()) {
        size_t newline = buffer.find('\n', pos);
        size_t end = (newline == std::string_view::npos) ? buffer.size() : newline;
        size_t line_end = end;
        if (line_end > pos && buffer[line_end - 1] == '\r') --line_end;
        std::string_view line = buffer.substr(pos, line_end - pos);
        pos = (newline == std::string_view::npos) ? buffer.size() : newline + 1;

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

        std::string ts_str;
        bool has_ts = false;
        bool is_assistant = false;
        bool has_content = false;
        std::string combined_text;

        for (auto root_field : root) {
            std::string_view key;
            if (root_field.unescaped_key().get(key) != sj::SUCCESS) continue;

            if (key == "timestamp") {
                std::string_view v;
                if (root_field.value().get_string().get(v) == sj::SUCCESS) {
                    if (!v.empty()) {
                        ts_str.assign(v.data(), v.size());
                        has_ts = true;
                    }
                }
            } else if (key == "message") {
                sj::ondemand::object msg_obj;
                if (root_field.value().get_object().get(msg_obj) != sj::SUCCESS) continue;

                for (auto msg_field : msg_obj) {
                    std::string_view msg_key;
                    if (msg_field.unescaped_key().get(msg_key) != sj::SUCCESS) continue;

                    if (msg_key == "role") {
                        std::string_view role_view;
                        if (msg_field.value().get_string().get(role_view) == sj::SUCCESS) {
                            is_assistant = (role_view == "assistant");
                        }
                    } else if (msg_key == "content") {
                        sj::ondemand::array arr;
                        if (msg_field.value().get_array().get(arr) != sj::SUCCESS) continue;
                        has_content = true;

                        bool first_text = true;
                        for (auto block_val : arr) {
                            sj::ondemand::object block;
                            if (block_val.get_object().get(block) != sj::SUCCESS) continue;

                            bool is_text = false;
                            std::string text_value;
                            bool has_text = false;
                            for (auto block_field : block) {
                                std::string_view bk;
                                if (block_field.unescaped_key().get(bk) != sj::SUCCESS) continue;
                                if (bk == "type") {
                                    std::string_view tv;
                                    if (block_field.value().get_string().get(tv) == sj::SUCCESS) {
                                        is_text = (tv == "text");
                                    }
                                } else if (bk == "text") {
                                    std::string_view tv;
                                    if (block_field.value().get_string().get(tv) == sj::SUCCESS) {
                                        text_value.assign(tv.data(), tv.size());
                                        has_text = true;
                                    }
                                }
                            }
                            if (is_text && has_text) {
                                if (!first_text) combined_text.push_back('\n');
                                combined_text.append(text_value);
                                first_text = false;
                            }
                        }
                    }
                }
            }
        }

        if (!is_assistant || !has_ts || !has_content) continue;
        cb(combined_text, ts_str);
    }
}

// === beacons-history walker ===
//
// History mode also needs USER events (for idle-gap detection), not just
// assistant beacons. This walker classifies every entry by its top-level
// `type` field and, for user entries, inspects message.content shape to
// distinguish real user prompts from tool_result entries (which are
// agent-active time, not idle).

struct EventRow {
    double timestamp = 0.0;
    bool is_real_user = false;  // only true for genuine user prompts
};

// Walk a transcript file for beacons-history. Emits:
//   - assistant_cb(combined_text, ts_str): called once per assistant entry
//     that has both a timestamp and content; combined_text is the joined
//     text-block payload (used to extract beacons).
//   - event_cb(EventRow): called once per entry with a parseable timestamp,
//     regardless of role. is_real_user = (top-level type == "user" AND
//     content is NOT an array containing a tool_result block).
//
// GOTCHA: message.content can be EITHER a JSON array of blocks OR a bare
// string (older user-prompt format). simdjson on-demand consumes a value
// once, so we peek .type() first then dispatch. Bare-string content counts
// as a real user prompt (no tool_result possible there).
template <typename AssistantCb, typename EventCb>
void walk_entries_for_history(const fs::path& path, AssistantCb&& assistant_cb, EventCb&& event_cb) {
    sj::padded_string data;
    if (sj::padded_string::load(path.string()).get(data) != sj::SUCCESS) return;
    sj::ondemand::parser parser;

    std::string_view buffer(data);
    size_t pos = 0;
    while (pos < buffer.size()) {
        size_t newline = buffer.find('\n', pos);
        size_t end = (newline == std::string_view::npos) ? buffer.size() : newline;
        size_t line_end = end;
        if (line_end > pos && buffer[line_end - 1] == '\r') --line_end;
        std::string_view line = buffer.substr(pos, line_end - pos);
        pos = (newline == std::string_view::npos) ? buffer.size() : newline + 1;

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

        std::string ts_str;
        bool has_ts = false;
        std::string entry_type;       // top-level "type" field
        bool is_assistant_role = false;
        bool has_content = false;
        bool content_was_array = false;
        bool content_was_tool_result = false;
        std::string combined_text;

        for (auto root_field : root) {
            std::string_view key;
            if (root_field.unescaped_key().get(key) != sj::SUCCESS) continue;

            if (key == "type") {
                std::string_view tv;
                if (root_field.value().get_string().get(tv) == sj::SUCCESS) {
                    entry_type.assign(tv.data(), tv.size());
                }
            } else if (key == "timestamp") {
                std::string_view v;
                if (root_field.value().get_string().get(v) == sj::SUCCESS) {
                    if (!v.empty()) {
                        ts_str.assign(v.data(), v.size());
                        has_ts = true;
                    }
                }
            } else if (key == "message") {
                sj::ondemand::object msg_obj;
                if (root_field.value().get_object().get(msg_obj) != sj::SUCCESS) continue;

                for (auto msg_field : msg_obj) {
                    std::string_view msg_key;
                    if (msg_field.unescaped_key().get(msg_key) != sj::SUCCESS) continue;

                    if (msg_key == "role") {
                        std::string_view role_view;
                        if (msg_field.value().get_string().get(role_view) == sj::SUCCESS) {
                            is_assistant_role = (role_view == "assistant");
                        }
                    } else if (msg_key == "content") {
                        auto val = msg_field.value();
                        sj::ondemand::json_type ct;
                        if (val.type().get(ct) != sj::SUCCESS) continue;
                        has_content = true;
                        if (ct == sj::ondemand::json_type::array) {
                            content_was_array = true;
                            sj::ondemand::array arr;
                            if (val.get_array().get(arr) != sj::SUCCESS) continue;
                            bool first_text = true;
                            for (auto block_val : arr) {
                                sj::ondemand::object block;
                                if (block_val.get_object().get(block) != sj::SUCCESS) continue;

                                bool is_text_block = false;
                                bool is_tool_result_block = false;
                                std::string text_value;
                                bool has_text = false;
                                for (auto block_field : block) {
                                    std::string_view bk;
                                    if (block_field.unescaped_key().get(bk) != sj::SUCCESS) continue;
                                    if (bk == "type") {
                                        std::string_view tv;
                                        if (block_field.value().get_string().get(tv) == sj::SUCCESS) {
                                            if (tv == "text") is_text_block = true;
                                            else if (tv == "tool_result") is_tool_result_block = true;
                                        }
                                    } else if (bk == "text") {
                                        std::string_view tv;
                                        if (block_field.value().get_string().get(tv) == sj::SUCCESS) {
                                            text_value.assign(tv.data(), tv.size());
                                            has_text = true;
                                        }
                                    }
                                }
                                if (is_tool_result_block) content_was_tool_result = true;
                                if (is_text_block && has_text) {
                                    if (!first_text) combined_text.push_back('\n');
                                    combined_text.append(text_value);
                                    first_text = false;
                                }
                            }
                        } else if (ct == sj::ondemand::json_type::string) {
                            // Bare-string content: real user prompt, no
                            // text blocks (assistant entries never use this
                            // shape). Consume so on-demand cursor advances.
                            content_was_array = false;
                            std::string_view sv;
                            (void)val.get_string().get(sv);
                        } else {
                            content_was_array = false;
                        }
                    }
                }
            }
        }

        if (!has_ts) continue;
        auto ts_opt = walker::parse_iso8601(ts_str);
        if (!ts_opt) continue;

        // Mirror rust collect_session_events_in_path: emit event ONLY for
        // user-type entries OR assistant-role entries with content. Other
        // entries (system, summary, etc.) are skipped so they don't
        // pollute idle-gap detection. Without this filter, filler entries
        // between an assistant turn and a real user prompt shrink the
        // prev_ts -> user_ts gap and under-count idle.
        if (entry_type == "user") {
            EventRow row;
            row.timestamp = *ts_opt;
            row.is_real_user = !(content_was_array && content_was_tool_result);
            event_cb(row);
        } else if (is_assistant_role && has_content) {
            EventRow row;
            row.timestamp = *ts_opt;
            row.is_real_user = false;
            event_cb(row);
            assistant_cb(combined_text, ts_str);
        }
    }
}

// Find the LAST well-formed beacon in `text`.
std::optional<Beacon> last_beacon_in(const std::string& text) {
    const std::regex& re = beacon_re();
    std::optional<Beacon> result;
    auto begin = std::sregex_iterator(text.begin(), text.end(), re);
    auto end = std::sregex_iterator();
    for (auto it = begin; it != end; ++it) {
        const std::smatch& m = *it;
        if (m.size() < 2) continue;
        std::string body = m[1].str();
        auto parsed = parse_beacon_body(body);
        if (parsed) result = std::move(parsed);
    }
    return result;
}

// All well-formed beacons in `text`, in order.
std::vector<Beacon> all_beacons_in(const std::string& text) {
    const std::regex& re = beacon_re();
    std::vector<Beacon> out;
    auto begin = std::sregex_iterator(text.begin(), text.end(), re);
    auto end = std::sregex_iterator();
    for (auto it = begin; it != end; ++it) {
        const std::smatch& m = *it;
        if (m.size() < 2) continue;
        std::string body = m[1].str();
        auto parsed = parse_beacon_body(body);
        if (parsed) out.push_back(std::move(*parsed));
    }
    return out;
}

// JSON serialization helpers — manual, matching the existing main.cpp style.

std::string json_escape(const std::string& s) {
    std::string out;
    out.reserve(s.size() + 2);
    for (char c : s) {
        switch (c) {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (static_cast<unsigned char>(c) < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x", c);
                    out += buf;
                } else {
                    out.push_back(c);
                }
        }
    }
    return out;
}

// Format a finite double. Integers are rendered without a decimal point;
// non-integers use as-short-as-possible representation. Matches what Python's
// json.loads produces for downstream comparison (numeric equality holds across
// int/float, so this is just for clean output).
std::string format_number(double v) {
    if (v == std::floor(v) && std::isfinite(v) &&
        v >= -1e15 && v <= 1e15) {
        std::ostringstream os;
        os << std::fixed << std::setprecision(1) << v;
        return os.str();
    }
    std::ostringstream os;
    os << std::setprecision(17) << v;
    return os.str();
}

std::string serialize_beacon(const Beacon& b) {
    std::ostringstream os;
    os << "{\"kind\":\"" << json_escape(b.kind) << "\""
       << ",\"eta_seconds\":" << format_number(b.eta_seconds)
       << ",\"summary\":\"" << json_escape(b.summary) << "\""
       << ",\"drift\":\"" << json_escape(b.drift) << "\"";
    if (b.beats_left.has_value()) {
        os << ",\"beats_left\":" << *b.beats_left;
    }
    os << "}";
    return os.str();
}

// === argument parsing ===

struct LatestArgs {
    std::string session_id;
    std::optional<fs::path> projects_root;
    std::vector<fs::path> extra_projects_roots;
    bool read_config = true;
    std::optional<double> now_unix;
};

std::optional<LatestArgs> parse_latest_args(const std::vector<std::string>& args, std::string& err) {
    LatestArgs out;
    bool have_session = false;
    for (size_t i = 0; i < args.size(); ++i) {
        const std::string& flag = args[i];
        auto need_value = [&](const std::string& f) -> std::optional<std::string> {
            if (i + 1 >= args.size()) {
                err = f + " needs a value";
                return std::nullopt;
            }
            return args[++i];
        };
        if (flag == "--session-id") {
            auto v = need_value(flag);
            if (!v) return std::nullopt;
            out.session_id = *v;
            have_session = true;
        } else if (flag == "--projects-root") {
            auto v = need_value(flag);
            if (!v) return std::nullopt;
            out.projects_root = fs::path(*v);
        } else if (flag == "--now") {
            auto v = need_value(flag);
            if (!v) return std::nullopt;
            try { out.now_unix = std::stod(*v); }
            catch (...) { err = "--now: invalid number"; return std::nullopt; }
        } else if (flag == "--extra-projects-root") {
            auto v = need_value(flag);
            if (!v) return std::nullopt;
            out.extra_projects_roots.emplace_back(*v);
        } else if (flag == "--no-config") {
            out.read_config = false;
        } else {
            err = "unknown flag: " + flag;
            return std::nullopt;
        }
    }
    if (!have_session) { err = "--session-id is required"; return std::nullopt; }
    return out;
}

struct HistoryArgs {
    uint64_t period_seconds = 0;
    bool have_period = false;
    double win_start_unix = 0.0;
    std::optional<fs::path> projects_root;
    std::vector<fs::path> extra_projects_roots;
    bool read_config = true;
    std::optional<double> now_unix;
};

std::optional<HistoryArgs> parse_history_args(const std::vector<std::string>& args, std::string& err) {
    HistoryArgs out;
    for (size_t i = 0; i < args.size(); ++i) {
        const std::string& flag = args[i];
        auto need_value = [&](const std::string& f) -> std::optional<std::string> {
            if (i + 1 >= args.size()) {
                err = f + " needs a value";
                return std::nullopt;
            }
            return args[++i];
        };
        if (flag == "--period") {
            auto v = need_value(flag);
            if (!v) return std::nullopt;
            try { out.period_seconds = std::stoull(*v); out.have_period = true; }
            catch (...) { err = "--period: invalid integer"; return std::nullopt; }
        } else if (flag == "--win-start") {
            auto v = need_value(flag);
            if (!v) return std::nullopt;
            try { out.win_start_unix = std::stod(*v); }
            catch (...) { err = "--win-start: invalid number"; return std::nullopt; }
        } else if (flag == "--projects-root") {
            auto v = need_value(flag);
            if (!v) return std::nullopt;
            out.projects_root = fs::path(*v);
        } else if (flag == "--now") {
            auto v = need_value(flag);
            if (!v) return std::nullopt;
            try { out.now_unix = std::stod(*v); }
            catch (...) { err = "--now: invalid number"; return std::nullopt; }
        } else if (flag == "--extra-projects-root") {
            auto v = need_value(flag);
            if (!v) return std::nullopt;
            out.extra_projects_roots.emplace_back(*v);
        } else if (flag == "--no-config") {
            out.read_config = false;
        } else {
            err = "unknown flag: " + flag;
            return std::nullopt;
        }
    }
    if (!out.have_period) { err = "--period is required"; return std::nullopt; }
    return out;
}

// === beacons-latest implementation ===

std::optional<std::pair<Beacon, double>> find_latest_in_path(const fs::path& path) {
    std::optional<std::pair<Beacon, double>> latest;
    walk_assistant_entries(path, [&](const std::string& text, const std::string& ts_str) {
        auto ts_opt = walker::parse_iso8601(ts_str);
        if (!ts_opt) return;
        double ts = *ts_opt;
        auto entry_beacon = last_beacon_in(text);
        if (!entry_beacon) return;
        if (!latest.has_value() || ts >= latest->second) {
            latest = std::make_pair(std::move(*entry_beacon), ts);
        }
    });
    return latest;
}

}  // namespace

int run_latest(const std::vector<std::string>& args) {
    auto started = std::chrono::steady_clock::now();
    std::string err;
    auto parsed_opt = parse_latest_args(args, err);
    if (!parsed_opt) {
        std::cerr << "walker: beacons-latest: " << err << "\n";
        return 2;
    }
    LatestArgs parsed = std::move(*parsed_opt);
    fs::path primary = parsed.projects_root.value_or(walker::default_projects_root());
    std::vector<fs::path> roots = walker::resolve_roots(
        primary, parsed.extra_projects_roots, parsed.read_config);
    double now_unix = parsed.now_unix.value_or(walker::current_unix());

    std::vector<fs::path> paths;
    std::string parent_filename = parsed.session_id + ".jsonl";
    std::string subagent_filename = "agent-" + parsed.session_id + ".jsonl";

    for (const fs::path& root : roots) {
        std::error_code ec;
        if (!fs::exists(root, ec)) continue;

        for (auto& slug_entry : fs::directory_iterator(root, ec)) {
            if (!slug_entry.is_directory()) continue;
            fs::path candidate = slug_entry.path() / parent_filename;
            if (fs::is_regular_file(candidate, ec)) paths.push_back(candidate);

            for (auto& session_entry : fs::directory_iterator(slug_entry.path(), ec)) {
                if (!session_entry.is_directory()) continue;
                fs::path subdir = session_entry.path() / "subagents";
                if (!fs::is_directory(subdir, ec)) continue;
                fs::path scan = subdir / subagent_filename;
                if (fs::is_regular_file(scan, ec)) paths.push_back(scan);
            }
        }
    }

    std::optional<std::pair<Beacon, double>> best;
    for (const auto& p : paths) {
        auto found = find_latest_in_path(p);
        if (!found) continue;
        if (!best.has_value() || found->second > best->second) {
            best = std::move(found);
        }
    }

    auto elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now() - started).count();

    std::ostringstream os;
    os << "{\"beacon\":";
    if (best) {
        os << serialize_beacon(best->first);
        os << ",\"emitted_at\":" << format_number(best->second);
        os << ",\"age_seconds\":" << format_number(now_unix - best->second);
    } else {
        os << "null,\"emitted_at\":null,\"age_seconds\":null";
    }
    os << ",\"elapsed_ms\":" << elapsed_ms << "}";
    std::cout << os.str() << "\n";
    return 0;
}

// === beacons-history implementation ===

namespace {

using GroupKey = std::pair<std::string, std::string>;

struct GroupKeyHash {
    size_t operator()(const GroupKey& k) const noexcept {
        std::hash<std::string> h;
        return h(k.first) ^ (h(k.second) << 1);
    }
};

using HistoryGroups = std::unordered_map<GroupKey, std::vector<fs::path>, GroupKeyHash>;

HistoryGroups discover_history_groups(const std::vector<fs::path>& roots) {
    HistoryGroups groups;
    std::error_code ec;

    for (const fs::path& root : roots) {
        if (!fs::exists(root, ec)) continue;

        for (auto& slug_entry : fs::directory_iterator(root, ec)) {
            if (!slug_entry.is_directory()) continue;
            std::string slug = slug_entry.path().filename().string();

            for (auto& file_entry : fs::directory_iterator(slug_entry.path(), ec)) {
                if (!file_entry.is_regular_file()) continue;
                const auto& path = file_entry.path();
                if (path.extension() != ".jsonl") continue;
                std::string sid = path.stem().string();
                groups[{slug, sid}].push_back(path);
            }

            for (auto& session_entry : fs::directory_iterator(slug_entry.path(), ec)) {
                if (!session_entry.is_directory()) continue;
                std::string sid = session_entry.path().filename().string();

                fs::path subdir = session_entry.path() / "subagents";
                if (!fs::is_directory(subdir, ec)) continue;
                for (auto& agent_entry : fs::directory_iterator(subdir, ec)) {
                    if (!agent_entry.is_regular_file()) continue;
                    const auto& apath = agent_entry.path();
                    if (apath.extension() != ".jsonl") continue;
                    std::string fname = apath.filename().string();
                    if (fname.substr(0, 6) != "agent-") continue;
                    groups[{slug, sid}].push_back(apath);
                }
            }
        }
    }
    return groups;
}

double compute_idle_in_window(const std::vector<EventRow>& events, double lo, double hi) {
    if (events.size() < 2) return 0.0;
    double idle = 0.0;
    for (size_t i = 1; i < events.size(); ++i) {
        if (!events[i].is_real_user) continue;
        double prev_ts = events[i - 1].timestamp;
        double ts = events[i].timestamp;
        double gap_lo = std::max(prev_ts, lo);
        double gap_hi = std::min(ts, hi);
        if (gap_hi > gap_lo) idle += gap_hi - gap_lo;
    }
    return idle;
}

std::optional<double> compute_bias_factor(const std::vector<std::pair<double, double>>& pairs) {
    if (pairs.empty()) return std::nullopt;
    std::vector<double> ratios;
    ratios.reserve(pairs.size());
    for (const auto& [eta, active] : pairs) {
        if (eta > 0.0) ratios.push_back(active / eta);
    }
    if (ratios.empty()) return std::nullopt;
    std::sort(ratios.begin(), ratios.end());
    size_t n = ratios.size();
    if (n % 2 == 1) return ratios[n / 2];
    return (ratios[n / 2 - 1] + ratios[n / 2]) / 2.0;
}

}  // namespace

int run_history(const std::vector<std::string>& args) {
    auto started = std::chrono::steady_clock::now();
    std::string err;
    auto parsed_opt = parse_history_args(args, err);
    if (!parsed_opt) {
        std::cerr << "walker: beacons-history: " << err << "\n";
        return 2;
    }
    HistoryArgs parsed = std::move(*parsed_opt);
    double now_unix = parsed.now_unix.value_or(walker::current_unix());
    double period_cutoff = now_unix - static_cast<double>(parsed.period_seconds);
    double window_lo = std::max(period_cutoff, parsed.win_start_unix);
    fs::path primary = parsed.projects_root.value_or(walker::default_projects_root());
    std::vector<fs::path> roots = walker::resolve_roots(
        primary, parsed.extra_projects_roots, parsed.read_config);
    HistoryGroups groups = discover_history_groups(roots);
    size_t session_count = groups.size();

    std::vector<std::pair<double, double>> pairs;
    std::vector<std::tuple<double, double, double>> pair_meta;

    for (const auto& [key, paths] : groups) {
        std::vector<std::pair<Beacon, double>> all_beacons;
        std::vector<EventRow> events;
        for (const auto& path : paths) {
            walk_entries_for_history(
                path,
                [&](const std::string& text, const std::string& ts_str) {
                    auto ts_opt = walker::parse_iso8601(ts_str);
                    if (!ts_opt) return;
                    double ts = *ts_opt;
                    if (ts < window_lo) return;
                    for (auto& b : all_beacons_in(text)) {
                        all_beacons.emplace_back(std::move(b), ts);
                    }
                },
                [&](const EventRow& row) {
                    events.push_back(row);
                }
            );
        }
        std::sort(events.begin(), events.end(),
                  [](const EventRow& a, const EventRow& b) {
                      return a.timestamp < b.timestamp;
                  });

        const std::pair<Beacon, double>* begin_ptr = nullptr;
        for (const auto& bt : all_beacons) {
            if (bt.first.kind != "begin") continue;
            if (!begin_ptr || bt.second < begin_ptr->second) begin_ptr = &bt;
        }
        const std::pair<Beacon, double>* end_ptr = nullptr;
        for (const auto& bt : all_beacons) {
            if (bt.first.kind != "end") continue;
            if (!end_ptr || bt.second > end_ptr->second) end_ptr = &bt;
        }
        if (!begin_ptr || !end_ptr) continue;
        if (end_ptr->second <= begin_ptr->second) continue;

        double wall = end_ptr->second - begin_ptr->second;
        double idle = compute_idle_in_window(events, begin_ptr->second, end_ptr->second);
        double active = wall - idle;
        if (active < 0.0) active = 0.0;
        pairs.emplace_back(begin_ptr->first.eta_seconds, active);
        pair_meta.emplace_back(wall, idle, active);
    }

    auto bias = compute_bias_factor(pairs);
    auto elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now() - started).count();

    std::ostringstream os;
    os << "{\"pairs\":[";
    for (size_t i = 0; i < pairs.size(); ++i) {
        if (i > 0) os << ",";
        const auto& [wall, idle, active] = pair_meta[i];
        os << "{\"begin_eta\":" << format_number(pairs[i].first)
           << ",\"actual_elapsed\":" << format_number(wall)
           << ",\"idle_excluded\":" << format_number(idle)
           << ",\"active_elapsed\":" << format_number(active)
           << "}";
    }
    os << "],\"session_count\":" << session_count
       << ",\"n_pairs\":" << pairs.size()
       << ",\"bias_factor\":";
    if (bias) {
        os << format_number(*bias);
    } else {
        os << "null";
    }
    os << ",\"elapsed_ms\":" << elapsed_ms << "}";
    std::cout << os.str() << "\n";
    return 0;
}

}  // namespace walker::beacons
