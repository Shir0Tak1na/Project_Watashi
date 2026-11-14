# The release version, read from version.txt at the top of the tree.
#
# version.txt is the single source of truth: CMakeLists.txt does not spell the
# number out, CPack does not, and the binaries get it through
# WATASHI_PRODUCT_VERSION. config/install_manifest.json, the licence manifest,
# the committed SBOM and the docs are checked against it by
# tests/version_test.py, so a release bump is one edit plus that check.
#
# Included before project(), which is why it cannot rely on PROJECT_SOURCE_DIR.

set(WATASHI_VERSION_FILE "${CMAKE_CURRENT_LIST_DIR}/../version.txt")
file(STRINGS "${WATASHI_VERSION_FILE}" _watashi_version_lines LIMIT_COUNT 1)
string(STRIP "${_watashi_version_lines}" WATASHI_VERSION)
if(NOT WATASHI_VERSION MATCHES "^[0-9]+\\.[0-9]+\\.[0-9]+$")
    message(FATAL_ERROR
        "version.txt must contain MAJOR.MINOR.PATCH on its first line, got "
        "'${WATASHI_VERSION}'")
endif()

string(REPLACE "." ";" _watashi_version_parts "${WATASHI_VERSION}")
list(GET _watashi_version_parts 0 WATASHI_VERSION_MAJOR)
list(GET _watashi_version_parts 1 WATASHI_VERSION_MINOR)
list(GET _watashi_version_parts 2 WATASHI_VERSION_PATCH)

# The version stamp is compiled into the binaries and printed by --version, so
# a report reader can tell which build produced it. A missing git (source
# archive, CI checkout option) degrades to "unknown" instead of failing.
execute_process(
    COMMAND git rev-parse --short=12 HEAD
    WORKING_DIRECTORY "${CMAKE_CURRENT_LIST_DIR}/.."
    OUTPUT_VARIABLE WATASHI_GIT_COMMIT
    OUTPUT_STRIP_TRAILING_WHITESPACE
    RESULT_VARIABLE WATASHI_GIT_RESULT
    ERROR_QUIET)
if(NOT WATASHI_GIT_RESULT EQUAL 0 OR WATASHI_GIT_COMMIT STREQUAL "")
    set(WATASHI_GIT_COMMIT "unknown")
endif()
