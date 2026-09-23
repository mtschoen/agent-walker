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

#include "archive.hpp"
#include "common.hpp"

#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <optional>
#include <sstream>
#include <string>
#include <unordered_set>
#include <vector>

#include <simdjson.h>

namespace walker {

namespace sj = simdjson;

enum class TranscriptFormat : uint8_t { ClaudeCode, Codex, ClaudeArchive };

struct TranscriptRoot {
  fs::path path;
  TranscriptFormat format = TranscriptFormat::ClaudeCode;
  // True when this root is one <archive>/<hostname> directory produced by
  // expanding a claude-archive root. Only these report the
  // unrecognized-suffix count. Not part of any dedup key.
  bool from_archive = false;
};

// A resolved root for the non-search modes, which have no format to carry.
struct ResolvedRoot {
  fs::path path;
  bool from_archive = false;
};

inline fs::path walker_config_path() {
  if (auto home = home_directory())
    return fs::path(*home) / ".claude" / "walker-roots.json";
  return fs::path(".claude/walker-roots.json");
}

// Parse extras from `~/.claude/walker-roots.json`. Returns empty vector on
// any failure (with a stderr diagnostic for malformed JSON specifically).
inline std::vector<TranscriptRoot> read_tagged_extra_roots_from_config() {
  fs::path config = walker_config_path();
  std::error_code ec;
  if (!fs::exists(config, ec))
    return {};

  std::ifstream in(config);
  if (!in)
    return {};
  std::ostringstream buf;
  buf << in.rdbuf();
  std::string body = buf.str();
  if (body.empty())
    return {};

  sj::dom::parser parser;
  sj::padded_string padded(body);
  sj::dom::element doc;
  if (parser.parse(padded).get(doc) != sj::SUCCESS) {
    std::cerr << "walker: malformed " << config.string()
              << " -- ignoring extra roots\n";
    return {};
  }
  sj::dom::object root;
  if (doc.get_object().get(root) != sj::SUCCESS) {
    std::cerr << "walker: " << config.string()
              << " is not a JSON object -- ignoring\n";
    return {};
  }

  sj::dom::array arr;
  if (root["extra_roots"].get_array().get(arr) != sj::SUCCESS)
    return {};

  std::vector<TranscriptRoot> extras;
  for (auto element : arr) {
    std::string_view path_view;
    if (element.get_string().get(path_view) == sj::SUCCESS) {
      if (!path_view.empty())
        extras.push_back(
            {fs::path(std::string(path_view)), TranscriptFormat::ClaudeCode});
      continue;
    }

    sj::dom::object tagged;
    if (element.get_object().get(tagged) != sj::SUCCESS)
      continue;
    if (tagged["path"].get_string().get(path_view) != sj::SUCCESS ||
        path_view.empty())
      continue;
    std::string_view format_view;
    if (tagged["format"].get_string().get(format_view) != sj::SUCCESS)
      continue;
    TranscriptFormat format;
    if (format_view == "claude-code")
      format = TranscriptFormat::ClaudeCode;
    else if (format_view == "codex")
      format = TranscriptFormat::Codex;
    else if (format_view == "claude-archive")
      format = TranscriptFormat::ClaudeArchive;
    else
      continue;
    extras.push_back({fs::path(std::string(path_view)), format});
  }
  return extras;
}

struct ConfigRoots {
  std::vector<fs::path> claude_code;
  std::vector<fs::path> archives;
  std::vector<TranscriptRoot> tagged;
};

inline ConfigRoots config_roots_by_format(bool read_config) {
  ConfigRoots split;
  if (!read_config)
    return split;
  for (auto &root : read_tagged_extra_roots_from_config()) {
    if (root.format == TranscriptFormat::ClaudeCode)
      split.claude_code.push_back(root.path);
    else if (root.format == TranscriptFormat::ClaudeArchive)
      split.archives.push_back(root.path);
    split.tagged.push_back(std::move(root));
  }
  return split;
}

// Archive roots in SPEC effective order (implicit, CLI, config), each already
// expanded into its <archive>/<hostname> claude-code roots. The implicit root
// is silent when absent; a CLI or config root that is not a directory gets the
// standard diagnostic.
inline std::vector<fs::path>
archive_roots_in_effective_order(bool primary_explicit,
                                 const std::vector<fs::path> &cli_archives,
                                 const std::vector<fs::path> &config_archives) {
  std::vector<fs::path> expanded;
  if (!primary_explicit) {
    fs::path implicit = default_archive_root();
    std::error_code implicit_ec;
    if (fs::is_directory(implicit, implicit_ec)) {
      auto hosts = expand_archive_root(implicit);
      expanded.insert(expanded.end(), hosts.begin(), hosts.end());
    }
  }
  auto append = [&](const std::vector<fs::path> &sources) {
    for (const auto &archive_path : sources) {
      std::error_code directory_ec;
      if (!fs::is_directory(archive_path, directory_ec)) {
        std::cerr << "walker: archive root not a directory, skipping: "
                  << archive_path.string() << "\n";
        continue;
      }
      auto hosts = expand_archive_root(archive_path);
      expanded.insert(expanded.end(), hosts.begin(), hosts.end());
    }
  };
  append(cli_archives);
  append(config_archives);
  return expanded;
}

inline fs::path default_codex_root() {
  if (auto home = home_directory())
    return fs::path(*home) / ".codex" / "sessions";
  return fs::path(".codex/sessions");
}

inline std::vector<TranscriptRoot>
resolve_search_roots(const std::optional<fs::path> &primary,
                     const std::vector<fs::path> &cli_extras,
                     const std::vector<fs::path> &cli_archives,
                     bool read_config) {
  const bool primary_explicit = primary.has_value();
  ConfigRoots config = config_roots_by_format(read_config);

  std::vector<TranscriptRoot> all;
  all.push_back({primary.value_or(default_projects_root()),
                 TranscriptFormat::ClaudeCode, false});
  if (!primary_explicit)
    all.push_back({default_codex_root(), TranscriptFormat::Codex, false});
  for (const auto &path : cli_extras)
    all.push_back({path, TranscriptFormat::ClaudeCode, false});
  for (auto &root : config.tagged) {
    if (root.format != TranscriptFormat::ClaudeArchive)
      all.push_back(std::move(root));
  }
  for (auto &path : archive_roots_in_effective_order(primary_explicit, cli_archives,
                                                     config.archives)) {
    all.push_back({std::move(path), TranscriptFormat::ClaudeCode, true});
  }

  std::vector<TranscriptRoot> result;
  std::unordered_set<std::string> seen;
  for (size_t index = 0; index < all.size(); ++index) {
    auto &root = all[index];
    std::error_code ec;
    if (!fs::is_directory(root.path, ec)) {
      std::error_code exists_ec;
      if (index > 0 && fs::exists(root.path, exists_ec)) {
        std::cerr << "walker: extra root not a directory, skipping: "
                  << root.path.string() << "\n";
      }
      continue;
    }
    fs::path canonical = fs::canonical(root.path, ec);
    if (ec)
      canonical = root.path.lexically_normal();
    std::string key(1, root.format == TranscriptFormat::Codex ? 'X' : 'C');
    key.push_back('\0');
    key.append(canonical.string());
    if (seen.insert(key).second)
      result.push_back(std::move(root));
  }
  return result;
}

// Resolve the effective root list:
//   [primary] + cli_extras + (config extras if read_config) + archives
//   -> dedup via canonical
//   -> filter to existing directories
inline std::vector<ResolvedRoot>
resolve_roots(const std::optional<fs::path> &primary,
              const std::vector<fs::path> &cli_extras,
              const std::vector<fs::path> &cli_archives, bool read_config) {
  const bool primary_explicit = primary.has_value();
  ConfigRoots config = config_roots_by_format(read_config);

  std::vector<ResolvedRoot> all;
  all.push_back({primary.value_or(default_projects_root()), false});
  for (const auto &path : cli_extras)
    all.push_back({path, false});
  for (const auto &path : config.claude_code)
    all.push_back({path, false});
  for (auto &path : archive_roots_in_effective_order(primary_explicit, cli_archives,
                                                     config.archives))
    all.push_back({std::move(path), true});

  std::vector<ResolvedRoot> result;
  std::unordered_set<std::string> seen;
  for (size_t index = 0; index < all.size(); ++index) {
    const auto &root = all[index];
    std::error_code ec;
    if (!fs::exists(root.path, ec) || !fs::is_directory(root.path, ec)) {
      if (index != 0) {
        std::cerr << "walker: extra root not a directory, skipping: "
                  << root.path.string() << "\n";
      }
      continue;
    }
    // from_archive is deliberately absent from the dedup key: two roots at
    // the same place collapse to the first one seen, live or archived.
    fs::path canonical = fs::canonical(root.path, ec);
    if (ec)
      canonical = root.path.lexically_normal();
    if (seen.insert(canonical.string()).second)
      result.push_back(root);
  }
  return result;
}

} // namespace walker

#endif // WALKER_ROOTS_HPP
