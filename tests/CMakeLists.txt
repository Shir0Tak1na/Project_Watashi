# Test targets for the C++ tree.
#
# This file exists because the top-level `CMakeLists.txt` has always done
# `add_subdirectory(tests)` under `BUILD_TESTS` (default ON) while no
# `tests/CMakeLists.txt` existed. CMake treats that as a configure error:
#
#     add_subdirectory given source "tests" which is not an existing directory
#
# so the project could not be configured at all, before reaching a compiler.
#
# It deliberately does not invent a test. No C++ test sources exist: the working,
# verified implementation is in `prototype/`, driven by `prototype/selfcheck_*.py`
# and `prototype/watashi_proto.py --selftest` (298 assertions, all passing). A test
# that asserts nothing would produce a green `ctest` run that means nothing, which
# is worse than no test target.

if(NOT BUILD_TESTS)
    return()
endif()

file(GLOB_RECURSE TEST_SOURCES "${CMAKE_CURRENT_SOURCE_DIR}/*.cpp")

if(NOT TEST_SOURCES)
    message(STATUS
        "tests/: no C++ test sources yet, so no test target is defined. "
        "The verified suite is prototype/selfcheck_*.py "
        "(e.g. prototype\\run.cmd selfcheck_session --summary).")
    return()
endif()

# Once sources appear they cannot link against `ProjectWatashi`, because that is
# an executable rather than a library. Sharing the engine with a test binary needs
# the port to factor the non-`main` sources into a library target that both link.
# Naming that here rather than silently producing a broken target.
add_executable(watashi_tests ${TEST_SOURCES})
target_link_libraries(watashi_tests PRIVATE ${PROJECT_WATASHI_LINK_LIBS})

# One `ctest` entry for the binary, not one per source file: several sources build
# a single binary, and registering it once per file would report the same run
# repeatedly under different names. Per-case granularity belongs to whichever test
# framework the port adopts (Catch2 / GoogleTest), which can report its own cases.
add_test(NAME cpp.watashi_tests COMMAND watashi_tests)

list(LENGTH TEST_SOURCES test_count)
message(STATUS "tests/: watashi_tests built from ${test_count} source file(s)")
