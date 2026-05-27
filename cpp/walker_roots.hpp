// Roots discovery: default root + extras from ~/.claude/walker-roots.json
// + extras from CLI flags. Deduped via fs::canonical, filtered to
// existing directories.
//
// Failure modes follow the SPEC contract:
//   * Missing config file -> no extras (silent).
//   * Malformed JSON -> stderr diagnostic, treat as no extras.
//   * Listed path doesn't exist on disk -> skip silently (stderr).
//   * canonical() fails (broken symlink etc) -> fall back to lexically_normal.

#ifndef WALKER_ROOTS_HPP
#define WALKER_ROOTS_HPP

#include "common.hpp"

#include <filesystem>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <unordered_set>
#include <vector>

#include <simdjson.h>

namespace walker {

namespace sj = simdjson;

inline fs::path walker_config_path() {
    if (auto home = home_directory()) return fs::path(*home) / ".claude" / "walker-roots.json";
    return fs::path(".claude/walker-roots.json");
}

// Parse extras from `~/.claude/walker-roots.json`. Returns empty vector on
// any failure (with a stderr diagnostic for malformed JSON specifically).
inline std::vector<fs::path> read_extra_roots_from_config() {
    fs::path config = walker_config_path();
    std::error_code ec;
    if (!fs::exists(config, ec)) return {};

    std::ifstream in(config);
    if (!in) return {};
    std::ostringstream buf;
    buf << in.rdbuf();
    std::string body = buf.str();
    if (body.empty()) return {};

    sj::ondemand::parser parser;
    sj::padded_string padded(body);
    sj::ondemand::document doc;
    if (parser.iterate(padded).get(doc) != sj::SUCCESS) {
        std::cerr << "walker: malformed " << config.string()
                  << " -- ignoring extra roots\n";
        return {};
    }
    sj::ondemand::object root;
    if (doc.get_object().get(root) != sj::SUCCESS) {
        std::cerr << "walker: " << config.string()
                  << " is not a JSON object -- ignoring\n";
        return {};
    }

    std::vector<fs::path> extras;
    for (auto field : root) {
        std::string_view key;
        if (field.unescaped_key().get(key) != sj::SUCCESS) continue;
        if (key != "extra_roots") continue;

        sj::ondemand::array arr;
        if (field.value().get_array().get(arr) != sj::SUCCESS) continue;

        for (auto element : arr) {
            std::string_view path_view;
            if (element.get_string().get(path_view) != sj::SUCCESS) continue;
            if (path_view.empty()) continue;
            extras.emplace_back(std::string(path_view));
        }
    }
    return extras;
}

// Resolve the effective root list:
//   [primary] + cli_extras + (config extras if read_config)
//   -> dedup via canonical
//   -> filter to existing directories
inline std::vector<fs::path> resolve_roots(
    const fs::path& primary,
    const std::vector<fs::path>& cli_extras,
    bool read_config)
{
    std::vector<fs::path> all;
    all.push_back(primary);
    for (const auto& p : cli_extras) all.push_back(p);
    if (read_config) {
        for (const auto& p : read_extra_roots_from_config()) all.push_back(p);
    }

    std::vector<fs::path> result;
    std::unordered_set<std::string> seen;
    for (const auto& p : all) {
        std::error_code ec;
        if (!fs::exists(p, ec) || !fs::is_directory(p, ec)) {
            if (&p != &all[0]) {  // primary is allowed to not exist; that's the empty-fleet case
                std::cerr << "walker: extra root not a directory, skipping: "
                          << p.string() << "\n";
            }
            continue;
        }
        fs::path canon = fs::canonical(p, ec);
        if (ec) canon = p.lexically_normal();
        std::string key = canon.string();
        if (seen.insert(key).second) {
            result.push_back(canon);
        }
    }
    return result;
}

}  // namespace walker

#endif  // WALKER_ROOTS_HPP
