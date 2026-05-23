// events subcommand: emit one NDJSON record per accepted assistant turn.
// See ../SPEC.md §events for the full contract.

#ifndef WALKER_EVENTS_HPP
#define WALKER_EVENTS_HPP

#include <string>
#include <vector>

namespace walker::events {

int run(const std::vector<std::string>& argv);

}  // namespace walker::events

#endif  // WALKER_EVENTS_HPP
