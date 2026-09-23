// Compressed archive support: libzstd inflate at the transcript-load
// chokepoint, transcript file-name classification shared by every discovery
// path, and expansion of a claude-archive root into one claude-code layout
// root per immediate <archive>/<hostname> subdirectory.
//
// Header-only, like common.hpp and discovery.hpp. See ../SPEC.md sections
// "Roots", "Discovery", and "Filters".

#ifndef WALKER_ARCHIVE_HPP
#define WALKER_ARCHIVE_HPP

#include "common.hpp"

#include <algorithm>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <optional>
#include <string>
#include <string_view>
#include <system_error>
#include <vector>

#include <simdjson.h>
#include <zstd.h>

namespace walker {

namespace fs = std::filesystem;

inline constexpr std::string_view kArchiveDirectoryName = "claude-archive";
inline constexpr std::string_view kTranscriptSuffix = ".jsonl";
inline constexpr std::string_view kCompressedSuffix = ".jsonl.zst";
inline constexpr std::string_view kSubagentPrefix = "agent-";

inline fs::path default_archive_root() {
  if (auto home = home_directory())
    return fs::path(*home) / std::string(kArchiveDirectoryName);
  return fs::path(std::string(kArchiveDirectoryName));
}

inline bool ends_with(std::string_view text, std::string_view suffix) {
  return text.size() >= suffix.size() &&
         text.compare(text.size() - suffix.size(), suffix.size(), suffix) == 0;
}

inline std::optional<std::string> parent_session_id(std::string_view file_name) {
  if (ends_with(file_name, kCompressedSuffix))
    return std::string(file_name.substr(0, file_name.size() - kCompressedSuffix.size()));
  if (ends_with(file_name, kTranscriptSuffix))
    return std::string(file_name.substr(0, file_name.size() - kTranscriptSuffix.size()));
  return std::nullopt;
}

inline std::optional<std::string> subagent_agent_id(std::string_view file_name) {
  if (file_name.size() < kSubagentPrefix.size() ||
      file_name.compare(0, kSubagentPrefix.size(), kSubagentPrefix) != 0)
    return std::nullopt;
  return parent_session_id(file_name.substr(kSubagentPrefix.size()));
}

inline bool is_compressed_transcript(const fs::path &path) {
  return ends_with(path.filename().string(), kCompressedSuffix);
}

// Immediate subdirectories, sorted ascending by name so the effective root
// order is deterministic. A missing or unreadable path yields an empty list;
// the caller emits the diagnostic.
inline std::vector<fs::path> expand_archive_root(const fs::path &archive_path) {
  std::vector<fs::path> hosts;
  std::error_code iterate_error;
  for (const auto &entry : fs::directory_iterator(archive_path, iterate_error)) {
    std::error_code type_error;
    if (entry.is_directory(type_error))
      hosts.push_back(entry.path());
  }
  std::sort(hosts.begin(), hosts.end());
  return hosts;
}

// Whole-file load, inflating a .zst in memory. Returns nullopt for an
// unreadable file (silent, matching the prior padded_string::load posture) and
// for an undecodable frame (one stderr line, per SPEC "Filters").
//
// ZSTD_getFrameContentSize gives the exact plaintext size for frames replica
// writes (the Python zstandard one-shot compressor always records it). The
// streaming fallback covers a frame that omits the size, which replica does
// not produce but a hand-placed file might.
inline std::optional<simdjson::padded_string>
load_transcript(const fs::path &path) {
  if (!is_compressed_transcript(path)) {
    simdjson::padded_string data;
    if (simdjson::padded_string::load(path.string()).get(data) != simdjson::SUCCESS)
      return std::nullopt;
    return data;
  }

  std::ifstream input(path, std::ios::binary);
  if (!input)
    return std::nullopt;
  std::string compressed((std::istreambuf_iterator<char>(input)),
                         std::istreambuf_iterator<char>());

  auto report_failure = [&]() {
    std::cerr << "walker: unreadable archive file, skipping: " << path.string()
              << "\n";
    return std::nullopt;
  };

  unsigned long long declared =
      ZSTD_getFrameContentSize(compressed.data(), compressed.size());
  if (declared == ZSTD_CONTENTSIZE_ERROR)
    return report_failure();

  std::string plain;
  if (declared != ZSTD_CONTENTSIZE_UNKNOWN) {
    plain.resize(static_cast<size_t>(declared));
    size_t written = ZSTD_decompress(plain.data(), plain.size(),
                                     compressed.data(), compressed.size());
    if (ZSTD_isError(written) || written != plain.size())
      return report_failure();
  } else {
    ZSTD_DStream *stream = ZSTD_createDStream();
    if (stream == nullptr)
      return report_failure();
    ZSTD_initDStream(stream);
    std::string chunk(ZSTD_DStreamOutSize(), '\0');
    ZSTD_inBuffer in{compressed.data(), compressed.size(), 0};
    bool failed = false;
    while (in.pos < in.size) {
      ZSTD_outBuffer out{chunk.data(), chunk.size(), 0};
      size_t status = ZSTD_decompressStream(stream, &out, &in);
      if (ZSTD_isError(status)) {
        failed = true;
        break;
      }
      plain.append(chunk.data(), out.pos);
    }
    ZSTD_freeDStream(stream);
    if (failed)
      return report_failure();
  }

  // padded_string's string_view constructor copies and adds the tail padding
  // simdjson's on-demand parser requires.
  return simdjson::padded_string(std::string_view(plain));
}

} // namespace walker

#endif // WALKER_ARCHIVE_HPP
