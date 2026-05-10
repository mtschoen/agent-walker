// beacons-latest and beacons-history subcommands. See ../SPEC.md
// "Subcommands" for the contract.

#ifndef WALKER_BEACONS_HPP
#define WALKER_BEACONS_HPP

#include <string>
#include <vector>

namespace walker::beacons {

int run_latest(const std::vector<std::string>& args);
int run_history(const std::vector<std::string>& args);

}  // namespace walker::beacons

#endif  // WALKER_BEACONS_HPP
