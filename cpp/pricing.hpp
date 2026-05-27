// Per-MTok pricing for assistant turns. Shared by cost mode (main.cpp) and
// the events subcommand (events.cpp) so the rate table and cost formula live
// in exactly one place — they must not drift between the two callers.
//
// Keep in lockstep with the canonical rates in
// ~/schoen-claude-status/statusline_lib.py and the other impls (rust/go/zig).

#ifndef WALKER_PRICING_HPP
#define WALKER_PRICING_HPP

#include <algorithm>
#include <cctype>
#include <cstdint>
#include <string_view>

namespace walker {

struct Rates {
    double input;   // per MTok
    double output;  // per MTok
};

inline Rates rates_for(std::string_view model) {
    // Case-insensitive substring scan without copying or lowercasing the
    // model string — tolower-comparing only the needle bytes is fine since
    // "opus"/"haiku" are ASCII.
    auto contains_ci = [&](std::string_view needle) {
        if (needle.size() > model.size()) return false;
        auto eq = [](unsigned char a, unsigned char b) {
            return std::tolower(a) == std::tolower(b);
        };
        return std::search(model.begin(), model.end(),
                           needle.begin(), needle.end(), eq) != model.end();
    };
    if (contains_ci("opus"))  return {5.0, 25.0};
    if (contains_ci("haiku")) return {1.0, 5.0};
    return {3.0, 15.0};  // sonnet or unknown -> sonnet rates
}

inline double cost_for(
    uint64_t input_tokens,
    uint64_t output_tokens,
    uint64_t cache_read_tokens,
    uint64_t cache_write_tokens,
    std::string_view model)
{
    auto [input_rate, output_rate] = rates_for(model);
    return (
        static_cast<double>(input_tokens) * input_rate
        + static_cast<double>(cache_read_tokens) * input_rate * 0.10
        + static_cast<double>(cache_write_tokens) * input_rate * 1.25
        + static_cast<double>(output_tokens) * output_rate
    ) / 1'000'000.0;
}

}  // namespace walker

#endif  // WALKER_PRICING_HPP
