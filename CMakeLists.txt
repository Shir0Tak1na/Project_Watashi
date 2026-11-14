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

# The release version lives in version.txt. cmake/Version.cmake must run before
# project() so the project version comes from that one file.
include(cmake/Version.cmake)

project(ProjectWatashi VERSION ${WATASHI_VERSION} LANGUAGES CXX)

# CMake 4 refuses projects that declare cmake_minimum_required below 3.5.
# cpu_features (pulled in by CTranslate2) and ruy still declare older floors;
# raising the minimum keeps them configurable without patching vendored sources.
set(CMAKE_POLICY_VERSION_MINIMUM 3.5)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(MSVC)
    # /utf-8 is not optional: the sources and the corpus files are UTF-8, and
    # without it MSVC reads string literals in the active code page.
    add_compile_options(/W4 /permissive- /utf-8)
else()
    add_compile_options(-Wall -Wextra)
endif()

include(cmake/Dependencies.cmake)
include(cmake/Targets.cmake)

include(CTest)
if(BUILD_TESTING)
    add_subdirectory(tests)
endif()

include(cmake/Packaging.cmake)
