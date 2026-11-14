# Writes dist/SHA256SUMS.txt for the CPack archive.
#
# Run by the dist_checksums target:
#   cmake -DDIST_DIR=<repo>/dist -DZIP_NAME=ProjectWatashi-<version>-win64.zip \
#         -P cmake/WriteChecksums.cmake
#
# file(WRITE) emits the bytes as given: UTF-8 without BOM, LF-only, and the two
# spaces sha256sum -c requires.

if(NOT DEFINED DIST_DIR OR NOT DEFINED ZIP_NAME)
    message(FATAL_ERROR "WriteChecksums.cmake needs DIST_DIR and ZIP_NAME")
endif()

set(_zip "${DIST_DIR}/${ZIP_NAME}")
if(NOT EXISTS "${_zip}")
    message(FATAL_ERROR "package not found: ${_zip}")
endif()

file(SHA256 "${_zip}" _hash)
# file(WRITE) opens in text mode on Windows and turns LF into CRLF, which makes
# "<hash>  <name>" unusable for sha256sum -c. execute_process(OUTPUT_FILE) writes
# the child's stdout verbatim, so -E echo gives the LF-only file we want.
execute_process(
    COMMAND "${CMAKE_COMMAND}" -E echo "${_hash}  ${ZIP_NAME}"
    OUTPUT_FILE "${DIST_DIR}/SHA256SUMS.txt"
    RESULT_VARIABLE _echo_result)
if(NOT _echo_result EQUAL 0)
    message(FATAL_ERROR "could not write ${DIST_DIR}/SHA256SUMS.txt")
endif()
message(STATUS "wrote ${DIST_DIR}/SHA256SUMS.txt")
