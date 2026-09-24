// Shared transcript discovery. One fused walk used by cost (main.cpp),
// events (events.cpp), beacons-history (beacons.cpp), and search
// (search.cpp). Header-only, like common.hpp.
//
// Why one copy matters: the four per-mode versions drifted twice - the
// shared-error_code truncation bug was fixed in search but not the other
// three, and search grew its own (different) mtime conversion. See
// SPEC.md "Discovery".

#ifndef WALKER_DISCOVERY_HPP
#define WALKER_DISCOVERY_HPP

#include <cstdint>
#include <filesystem>
#include <iostream>
#include <limits>
#include <string>
#include <system_error>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "archive.hpp"
#include "common.hpp"
#include "walker_roots.hpp"

namespace walker {

namespace fs = std::filesystem;

// on_file(root, slug, session_id, agent_id_or_empty, entry). agent_id is the
// empty string for a parent transcript.
template <typename OnFile>
inline void for_each_transcript(const std::vector<ResolvedRoot> &roots,
                                const std::string *cwd_slug, OnFile &&on_file) {
  for (const ResolvedRoot &resolved : roots) {
    const fs::path &root = resolved.path;
    // Counted always, reported only for archive-expanded roots: cost mode
    // runs on every status line tick and a stray file in the live tree must
    // not add stderr to every invocation. See SPEC "Discovery".
    uint64_t skipped_suffixes = 0;
    std::error_code slug_iter_ec;
    for (auto const &slug_entry : fs::directory_iterator(root, slug_iter_ec)) {
      std::error_code slug_type_ec;
      if (!slug_entry.is_directory(slug_type_ec))
        continue;
      std::string slug = slug_entry.path().filename().string();
      if (cwd_slug && slug != *cwd_slug)
        continue;

      std::error_code entry_iter_ec;
      for (auto const &entry :
           fs::directory_iterator(slug_entry.path(), entry_iter_ec)) {
        std::error_code type_ec;
        if (entry.is_regular_file(type_ec)) {
          auto session_id = parent_session_id(entry.path().filename().string());
          if (!session_id) {
            ++skipped_suffixes;
            continue;
          }
          on_file(root, slug, *session_id, std::string(), entry);
        } else if (entry.is_directory(type_ec)) {
          std::string session_id = entry.path().filename().string();
          fs::path subagents_dir = entry.path() / "subagents";
          std::error_code subdir_ec;
          if (!fs::is_directory(subagents_dir, subdir_ec))
            continue;

          std::error_code agent_iter_ec;
          for (auto const &agent_entry :
               fs::directory_iterator(subagents_dir, agent_iter_ec)) {
            std::error_code agent_type_ec;
            if (!agent_entry.is_regular_file(agent_type_ec))
              continue;
            auto agent_id =
                subagent_agent_id(agent_entry.path().filename().string());
            if (!agent_id) {
              ++skipped_suffixes;
              continue;
            }
            on_file(root, slug, session_id, *agent_id, agent_entry);
          }
        }
      }
    }
    if (resolved.from_archive && skipped_suffixes > 0) {
      std::cerr << "walker: " << root.string() << ": skipped "
                << skipped_suffixes << " files with an unrecognized suffix\n";
    }
  }
}

using GroupMap = std::unordered_map<std::string, std::vector<fs::path>>;

// True when the entry's mtime is readable and earlier than `earliest`.
// Unreadable mtimes err on the side of inclusion.
inline bool entry_mtime_before(const fs::directory_entry &entry,
                               double earliest) {
  std::error_code ec;
  auto mtime = entry.last_write_time(ec);
  if (ec)
    return false;
  return file_mtime_to_unix(mtime) < earliest;
}

// Group transcripts by group_key(slug, session_id), pruning files whose
// mtime is before `earliest`. Pass -infinity to disable the prune (skips
// the mtime fetch entirely - beacons-history must see every transcript).
inline GroupMap discover_groups(const std::vector<ResolvedRoot> &roots,
                                double earliest) {
  GroupMap groups;
  std::unordered_set<std::string> claimed;
  const bool prune = earliest > -std::numeric_limits<double>::infinity();
  for_each_transcript(
      roots, nullptr,
      [&](const fs::path &, const std::string &slug,
          const std::string &session_id, const std::string &agent_id,
          const fs::directory_entry &entry) {
        if (prune && entry_mtime_before(entry, earliest))
          return;
        std::string key = slug;
        key.push_back('\0');
        key.append(session_id);
        key.push_back('\0');
        key.append(agent_id);
        if (!claimed.insert(std::move(key)).second)
          return;
        groups[group_key(slug, session_id)].push_back(entry.path());
      });
  return groups;
}

} // namespace walker

#endif // WALKER_DISCOVERY_HPP
