// Minimal JSON string escaper shared by the search and events subcommands.
// Emits a quoted, escaped JSON string: the seven short escapes, control bytes
// < 0x20 as \uXXXX, and every other byte verbatim — so already-valid UTF-8
// input is emitted unchanged.

#ifndef WALKER_JSON_WRITER_HPP
#define WALKER_JSON_WRITER_HPP

#include <cstdio>
#include <ostream>
#include <string_view>

namespace walker {

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
