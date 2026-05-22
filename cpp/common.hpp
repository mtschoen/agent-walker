// Shared helpers used by both cost mode (main.cpp) and beacons subcommands
// (beacons.cpp). Header-only — keeps build wiring simple.

#ifndef WALKER_COMMON_HPP
#define WALKER_COMMON_HPP

#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <optional>
#include <string>
#include <string_view>

namespace walker {

namespace fs = std::filesystem;

inline fs::path default_projects_root() {
    const char* home = std::getenv("HOME");
    if (!home) home = std::getenv("USERPROFILE");
    if (home) return fs::path(home) / ".claude" / "projects";
    return fs::path(".claude/projects");
}

inline double current_unix() {
    auto tp = std::chrono::system_clock::now();
    return static_cast<double>(
        std::chrono::duration_cast<std::chrono::milliseconds>(tp.time_since_epoch()).count()
    ) / 1000.0;
}

// ISO 8601 timestamp parsing — accepts "...Z" or "+HH:MM" offset.
// Returns seconds since Unix epoch, or nullopt on failure. Operates on
// the input string_view directly — no allocation.
inline std::optional<double> parse_iso8601(std::string_view ts) {
    bool had_z = (!ts.empty() && ts.back() == 'Z');
    if (had_z) ts.remove_suffix(1);

    if (ts.size() < 19) return std::nullopt;

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
