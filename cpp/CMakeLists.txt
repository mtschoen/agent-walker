cmake_minimum_required(VERSION 3.20)
project(walker VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

include(FetchContent)

# nlohmann/json — header-only, single-include variant
FetchContent_Declare(
    nlohmann_json
    URL https://github.com/nlohmann/json/releases/download/v3.11.3/include.zip
    URL_HASH SHA256=a22461d13119ac5c78f205d3df1db13403e58ce1bb1794edc9313677313f4a9d
)
FetchContent_MakeAvailable(nlohmann_json)

add_executable(walker main.cpp)

target_include_directories(walker PRIVATE ${nlohmann_json_SOURCE_DIR}/single_include)

if(MSVC)
    target_compile_options(walker PRIVATE /W3 $<$<CONFIG:Release>:/O2 /GL>)
    target_link_options(walker PRIVATE $<$<CONFIG:Release>:/LTCG>)
else()
    target_compile_options(walker PRIVATE $<$<CONFIG:Release>:-O3>)
endif()
