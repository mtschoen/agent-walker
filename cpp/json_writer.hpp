// Minimal JSON string escaper shared by the search and events subcommands.
// Emits a quoted, escaped JSON string: the seven short escapes, control bytes
// < 0x20 as \uXXXX, and every other byte verbatim — so already-valid UTF-8
// input is emitted unchanged.

#ifndef WALKER_JSON_WRITER_HPP
#define WALKER_JSON_WRITER_HPP

#include <cstdio>
#include <ostream>
#include <string>
#include <string_view>

namespace walker {

// String-appending overload: escapes `s` (quoted) onto `out`. Used by the
// buffered emit paths that build the whole output in memory and write it in one
// syscall (per-record std::ostream writes dominated events-mode wall time).
inline void write_json_string(std::string& out, std::string_view s) {
    out.push_back('"');
    for (unsigned char c : s) {
        switch (c) {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (c < 0x20) {
                    char buffer[8];
                    std::snprintf(buffer, sizeof(buffer), "\\u%04x", c);
                    out += buffer;
                } else {
                    out.push_back(static_cast<char>(c));
                }
        }
    }
    out.push_back('"');
}

inline void write_json_string(std::ostream& os, std::string_view s) {
    os.put('"');
    for (unsigned char c : s) {
        switch (c) {
            case '"':  os << "\\\""; break;
            case '\\': os << "\\\\"; break;
            case '\b': os << "\\b"; break;
            case '\f': os << "\\f"; break;
            case '\n': os << "\\n"; break;
            case '\r': os << "\\r"; break;
            case '\t': os << "\\t"; break;
            default:
                if (c < 0x20) {
                    char buffer[8];
                    std::snprintf(buffer, sizeof(buffer), "\\u%04x", c);
                    os << buffer;
                } else {
                    os.put(static_cast<char>(c));
                }
        }
    }
    os.put('"');
}

}  // namespace walker

#endif  // WALKER_JSON_WRITER_HPP
