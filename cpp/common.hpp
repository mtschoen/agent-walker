// Shared helpers used by both cost mode (main.cpp) and beacons subcommands
// (beacons.cpp). Header-only — keeps build wiring simple.

#ifndef WALKER_COMMON_HPP
#define WALKER_COMMON_HPP

#include <algorithm>
#include <chrono>
#include <cstddef>
#include <cstdlib>
#include <filesystem>
#include <optional>
#include <string>
#include <string_view>

namespace walker {

namespace fs = std::filesystem;

// Worker-pool sizing seam shared by all four parallel modes: min(8, hardware
// threads), with a fixed fallback because std::thread::hardware_concurrency()
// is allowed to return 0 when the count is unknown. Pure so a unit test can
// drive the zero case on any host (COVERAGE-PLAN section 5, option 1).
inline size_t effective_workers(unsigned hardware_threads) {
    size_t workers = std::min<size_t>(8, hardware_threads);
    return workers == 0 ? 4 : workers;
}

// Single source for the C++ impl's --version string, used by both entry
// points (main.cpp cost mode + events.cpp). Mirrors the go/zig convention of
// one constant rather than a hardcoded copy per file; keep CMakeLists'
// project() version aligned for packaging metadata.
inline constexpr const char* VERSION = "cpp/0.4.1";

// Read an environment variable into an owning string. On MSVC, uses
// _dupenv_s (its recommended replacement for the deprecated getenv);
// elsewhere, std::getenv. Returns nullopt when the variable is unset.
// Copying to std::string sidesteps both the C4996 deprecation and the
// pointer-lifetime caveat of getenv.
inline std::optional<std::string> read_environment_variable(const char* name) {
#ifdef _MSC_VER
    char* buffer = nullptr;
    size_t size = 0;
    if (_dupenv_s(&buffer, &size, name) != 0 || buffer == nullptr)
        return std::nullopt;
    std::string value(buffer);
    free(buffer);
    return value;
#else
    const char* value = std::getenv(name);
    if (!value) return std::nullopt;
    return std::string(value);
#endif
}

// Resolve the user's home directory. On Windows, USERPROFILE is canonical
// (HOME is often unset, or a git-bash POSIX path like /c/Users/... that is
// not a valid native path), so prefer it; elsewhere HOME is canonical. The
// fallback covers the rarer inverse case on each platform.
inline std::optional<std::string> home_directory() {
#ifdef _WIN32
    if (auto profile = read_environment_variable("USERPROFILE")) return profile;
    return read_environment_variable("HOME");
#else
    if (auto home = read_environment_variable("HOME")) return home;
    return read_environment_variable("USERPROFILE");
#endif
}

inline fs::path default_projects_root() {
    if (auto home = home_directory()) return fs::path(*home) / ".claude" / "projects";
    return fs::path(".claude/projects");
}

inline double current_unix() {
    auto tp = std::chrono::system_clock::now();
    return static_cast<double>(
        std::chrono::duration_cast<std::chrono::milliseconds>(tp.time_since_epoch()).count()
    ) / 1000.0;
}

// (slug, session_id) -> discovery map key. The NUL separator can't appear in
// either component, so the join is unambiguous. Shared by main.cpp/events.cpp.
inline std::string group_key(const std::string& slug, const std::string& session_id) {
    return slug + '\0' + session_id;
}

// Portable file_time_type -> Unix seconds. std::chrono::clock_cast is C++20
// but missing from Apple Clang's libc++ (as of Xcode 16); this offset trick
// works on every implementation. Integer-second precision is plenty for
// comparing mtime against the discovery cutoff. Shared by main.cpp/events.cpp.
inline double file_mtime_to_unix(fs::file_time_type mtime) {
    auto sys_time = std::chrono::time_point_cast<std::chrono::system_clock::duration>(
        mtime - fs::file_time_type::clock::now() + std::chrono::system_clock::now());
    auto seconds = std::chrono::time_point_cast<std::chrono::seconds>(sys_time);
    return static_cast<double>(seconds.time_since_epoch().count());
}

// ISO 8601 timestamp parsing — accepts "...Z" or "+HH:MM" offset.
// Returns seconds since Unix epoch, or nullopt on failure. Operates on
// the input string_view directly — no allocation.
inline std::optional<double> parse_iso8601(std::string_view ts) {
    bool had_z = (!ts.empty() && ts.back() == 'Z');
    if (had_z) ts.remove_suffix(1);

    if (ts.size() < 19) return std::nullopt;
    // Positional digit parsing alone would accept any separator bytes;
    // require the canonical ISO 8601 separators (go/zig parity - they
    // reject e.g. a space in place of the 'T').
    if (ts[4] != '-' || ts[7] != '-' || ts[10] != 'T' || ts[13] != ':' ||
        ts[16] != ':')
        return std::nullopt;

    auto parse_int = [](const char* p, int len) -> int {
        int v = 0;
        for (int i = 0; i < len; ++i) {
            if (p[i] < '0' || p[i] > '9') return -1;
            v = v * 10 + (p[i] - '0');
        }
        return v;
    };

    int year   = parse_int(ts.data() + 0, 4);
    int month  = parse_int(ts.data() + 5, 2);
    int day    = parse_int(ts.data() + 8, 2);
    int hour   = parse_int(ts.data() + 11, 2);
    int minute = parse_int(ts.data() + 14, 2);
    int sec    = parse_int(ts.data() + 17, 2);

    if (year < 0 || month < 0 || day < 0 || hour < 0 || minute < 0 || sec < 0)
        return std::nullopt;
    if (month < 1 || month > 12 || day < 1 || day > 31) return std::nullopt;
    if (hour > 23 || minute > 59 || sec > 60) return std::nullopt;

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

    int tz_offset_sec = 0;
    if (!had_z && pos < ts.size()) {
        char sign = ts[pos];
        if (sign == '+' || sign == '-') {
            if (pos + 5 < ts.size() + 1) {
                int tz_h = parse_int(ts.data() + pos + 1, 2);
                int tz_m = parse_int(ts.data() + pos + 4, 2);
                if (tz_h < 0 || tz_m < 0) return std::nullopt;
                tz_offset_sec = (tz_h * 3600 + tz_m * 60) * (sign == '-' ? -1 : 1);
            }
        }
    }

    int a = (14 - month) / 12;
    int y = year + 4800 - a;
    int m = month + 12 * a - 3;
    int jdn = day + (153 * m + 2) / 5 + 365 * y + y / 4 - y / 100 + y / 400 - 32045;
    static const int unix_epoch_jdn = 2440588;
    int64_t day_diff = static_cast<int64_t>(jdn) - unix_epoch_jdn;

    int64_t epoch_sec = day_diff * 86400LL
        + static_cast<int64_t>(hour) * 3600
        + static_cast<int64_t>(minute) * 60
        + static_cast<int64_t>(sec)
        - tz_offset_sec;

    return static_cast<double>(epoch_sec) + frac;
}

}  // namespace walker

#endif  // WALKER_COMMON_HPP
