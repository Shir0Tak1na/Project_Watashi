cmake_minimum_required(VERSION 3.21)

# Dependency resolution. The supported path is the vcpkg manifest in
# ``vcpkg.json``; this block only selects the toolchain when one is already
# installed, so a plain ``cmake -S . -B build`` works and CI can pass vcpkg
# explicitly. It has to sit above ``project()``: a toolchain file must be in
# place before the first ``project()`` call or it is ignored.
if(NOT DEFINED CMAKE_TOOLCHAIN_FILE)
    foreach(root "$ENV{VCPKG_ROOT}" "$ENV{VCPKG_INSTALLATION_ROOT}")
        if(root AND EXISTS "${root}/scripts/buildsystems/vcpkg.cmake")
            set(CMAKE_TOOLCHAIN_FILE "${root}/scripts/buildsystems/vcpkg.cmake"
                CACHE FILEPATH "vcpkg toolchain file")
            break()
        endif()
    endforeach()
endif()

project(ProjectWatashi LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(MSVC)
    add_compile_options(/W4 /permissive- /utf-8)
else()
    add_compile_options(-Wall -Wextra)
endif()

# --------------------------------------------------------------------------- #
# dependencies
# --------------------------------------------------------------------------- #
#
# nlohmann-json is the event payload writer; Catch2 v3 is the test runner.
# Both are declared in vcpkg.json. When the vcpkg toolchain is not in play the
# same pinned tags are fetched instead, so the skeleton configures on a machine
# without vcpkg as long as it can reach GitHub once.
include(FetchContent)

find_package(nlohmann_json 3.11 CONFIG QUIET)
if(NOT nlohmann_json_FOUND)
    message(STATUS "nlohmann-json not found; fetching v3.11.3")
    FetchContent_Declare(nlohmann_json
        GIT_REPOSITORY https://github.com/nlohmann/json.git
        GIT_TAG v3.11.3
        GIT_SHALLOW TRUE)
    FetchContent_MakeAvailable(nlohmann_json)
endif()

find_package(Catch2 3 CONFIG QUIET)
if(NOT Catch2_FOUND)
    message(STATUS "Catch2 v3 not found; fetching v3.5.2")
    FetchContent_Declare(Catch2
        GIT_REPOSITORY https://github.com/catchorg/Catch2.git
        GIT_TAG v3.5.2
        GIT_SHALLOW TRUE)
    FetchContent_MakeAvailable(Catch2)
endif()

# --------------------------------------------------------------------------- #
# targets
# --------------------------------------------------------------------------- #
#
# watashi_core is the whole engine skeleton and holds no main(): the CLI and the
# tests differ only in how they drive it. Choosing this shape now means the port
# can grow without having to split the library out of an executable later.
add_library(watashi_core STATIC
    src/core/events.cpp
    src/core/pipeline.cpp
    src/core/session.cpp
    src/io/jsonl.cpp
    src/fake/fake_ocr.cpp
    src/fake/fake_translator.cpp
)
target_include_directories(watashi_core
    PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include
    PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src
)
target_link_libraries(watashi_core PUBLIC nlohmann_json::nlohmann_json)
target_compile_features(watashi_core PUBLIC cxx_std_17)

# stats.memory_mib comes from the process working set on Windows.
if(WIN32)
    target_link_libraries(watashi_core PRIVATE psapi)
endif()

add_executable(watashi_cli src/cli/main.cpp)
target_link_libraries(watashi_cli PRIVATE watashi_core)

include(CTest)
if(BUILD_TESTING)
    add_subdirectory(tests)
endif()
