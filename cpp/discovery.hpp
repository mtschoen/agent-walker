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

#include <filesystem>
#include <limits>
#include <string>
#include <system_error>
#include <unordered_map>
#include <vector>

#include "common.hpp"

namespace walker {

namespace fs = std::filesystem;

// Visit every transcript file under `roots` in ONE pass per slug directory:
// each slug-dir entry is classified as a parent (`<slug>/<sid>.jsonl`,
// session_id = file stem) or a session dir probed for subagents
// (`<slug>/<session>/subagents/agent-*.jsonl`, session_id = session dir
// name). The prior per-mode copies iterated each slug dir twice - two
// FindFirstFile round-trips per slug on Windows.
//
// `cwd_slug` (nullable) restricts to one slug. `on_file(root, slug,
// session_id, entry)` decides mtime pruning itself via the cached
// directory_entry (entry.last_write_time avoids a fresh per-file stat on
// Windows).
//
// error_code discipline: every fallible call gets its OWN error_code. A
// single shared one accumulates failure state, making a later
// `if (!exists(root, ec))` see a stale error and silently skip the root
// (bug previously fixed in search.cpp only).
template <typename OnFile>
inline void for_each_transcript(const std::vector<fs::path> &roots,
                                const std::string *cwd_slug, OnFile &&on_file) {
  for (const fs::path &root : roots) {
    std::error_code root_ec;
    if (!fs::is_directory(root, root_ec))
      continue;

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
          // Parent: <root>/<slug>/<session_id>.jsonl
          const auto &path = entry.path();
          if (path.extension() != ".jsonl")
            continue;
          on_file(root, slug, path.stem().string(), entry);
        } else if (entry.is_directory(type_ec)) {
          // Subagents: <root>/<slug>/<session>/subagents/agent-*.jsonl
          std::string sid = entry.path().filename().string();
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
            const auto &apath = agent_entry.path();
            if (apath.extension() != ".jsonl")
              continue;
            std::string fname = apath.filename().string();
            // compare() instead of substr(): no temporary string.
            if (fname.size() < 6 || fname.compare(0, 6, "agent-") != 0)
              continue;
            on_file(root, slug, sid, agent_entry);
          }
        }
      }
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
inline GroupMap discover_groups(const std::vector<fs::path> &roots,
                                double earliest) {
  GroupMap groups;
  const bool prune = earliest > -std::numeric_limits<double>::infinity();
  for_each_transcript(roots, nullptr,
                      [&](const fs::path &, const std::string &slug,
                          const std::string &sid,
                          const fs::directory_entry &entry) {
                        if (prune && entry_mtime_before(entry, earliest))
                          return;
                        groups[group_key(slug, sid)].push_back(entry.path());
                      });
  return groups;
}

} // namespace walker

#endif // WALKER_DISCOVERY_HPP
