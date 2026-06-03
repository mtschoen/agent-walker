cmake_minimum_required(VERSION 3.20)
# --version string is authoritative in cpp/common.hpp (walker::VERSION);
# keep this in sync for packaging metadata.
project(walker VERSION 0.4.1 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

include(FetchContent)

# simdjson — built from source via FetchContent. The on-demand API
# eliminates per-line allocation that nlohmann/json incurred.
set(SIMDJSON_DEVELOPER_MODE OFF CACHE BOOL "" FORCE)
FetchContent_Declare(
    simdjson
    GIT_REPOSITORY https://github.com/simdjson/simdjson.git
    GIT_TAG v4.6.4
    GIT_SHALLOW TRUE
)
FetchContent_MakeAvailable(simdjson)

add_executable(walker main.cpp beacons.cpp events.cpp search.cpp)
target_link_libraries(walker PRIVATE simdjson::simdjson)

if(MSVC)
    # /GL + /LTCG = whole-program optimization (MSVC's LTO). Already enabled.
    target_compile_options(walker PRIVATE /W3 $<$<CONFIG:Release>:/O2 /GL>)
    target_link_options(walker PRIVATE $<$<CONFIG:Release>:/LTCG>)
else()
    # -flto matches the MSVC /GL+/LTCG whole-program path: the GCC/Clang Release
    # binary (Linux CI + the shipped install.sh build) was previously -O3 only,
    # with no cross-TU inlining. -flto must be on both compile and link.
    # Portable (not a -march=native CPU-specific flag), so the install artifact
    # still runs anywhere.
    target_compile_options(walker PRIVATE $<$<CONFIG:Release>:-O3 -flto>)
    target_link_options(walker PRIVATE $<$<CONFIG:Release>:-flto>)
endif()

# Clang source-based coverage. Applied to the walker target only so vendored
# simdjson (build/_deps) stays uninstrumented; system/STL headers that leak
# coverage mapping are filtered out of the report via -ignore-filename-regex
# in shared/coverage.py. Configure a dedicated build dir with
# -DWALKER_COVERAGE=ON (Clang required) — never mix with the Release build.
option(WALKER_COVERAGE "Build walker with Clang source-based coverage instrumentation" OFF)
if(WALKER_COVERAGE)
    target_compile_options(walker PRIVATE -fprofile-instr-generate -fcoverage-mapping -O0 -g)
    target_link_options(walker PRIVATE -fprofile-instr-generate -fcoverage-mapping)
endif()
