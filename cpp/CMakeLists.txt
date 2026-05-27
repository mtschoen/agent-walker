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
    target_compile_options(walker PRIVATE /W3 $<$<CONFIG:Release>:/O2 /GL>)
    target_link_options(walker PRIVATE $<$<CONFIG:Release>:/LTCG>)
else()
    target_compile_options(walker PRIVATE $<$<CONFIG:Release>:-O3>)
endif()
