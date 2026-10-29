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
# ONNX Runtime (W1.1)
# --------------------------------------------------------------------------- #
#
# Pinned to the official Microsoft package, version and digest spelled out here.
# It is fetched into the build tree on demand: no path outside the repository is
# referenced, so a clean clone and CI configure the same way. The CLI only loads
# a model when --ocr onnx is asked for, which is why CI never needs one.
set(WATASHI_ORT_VERSION "1.30.0")
set(WATASHI_ORT_URL
    "https://www.nuget.org/api/v2/package/Microsoft.ML.OnnxRuntime/${WATASHI_ORT_VERSION}")
set(WATASHI_ORT_SHA256
    "30df119bd9164eaa187666d93147448fdd4a9a1a3caf83f1505daf7af2d1c5d2")
set(WATASHI_ORT_DIR "${CMAKE_BINARY_DIR}/_deps/ort-${WATASHI_ORT_VERSION}")
set(WATASHI_ORT_ARCHIVE "${WATASHI_ORT_DIR}/onnxruntime.nupkg")

if(NOT WIN32)
    message(FATAL_ERROR "ONNX Runtime is pinned to the win-x64 package in this port")
endif()

if(NOT EXISTS "${WATASHI_ORT_DIR}/pkg/build/native/include/onnxruntime_cxx_api.h")
    if(NOT EXISTS "${WATASHI_ORT_ARCHIVE}")
        message(STATUS "Downloading ONNX Runtime ${WATASHI_ORT_VERSION} from nuget.org")
        file(DOWNLOAD "${WATASHI_ORT_URL}" "${WATASHI_ORT_ARCHIVE}"
            EXPECTED_HASH SHA256=${WATASHI_ORT_SHA256}
            TLS_VERIFY ON
            STATUS WATASHI_ORT_DOWNLOAD)
        list(GET WATASHI_ORT_DOWNLOAD 0 WATASHI_ORT_CODE)
        if(NOT WATASHI_ORT_CODE EQUAL 0)
            file(REMOVE "${WATASHI_ORT_ARCHIVE}")
            message(FATAL_ERROR
                "ONNX Runtime download failed (${WATASHI_ORT_DOWNLOAD}); "
                "the pinned URL and digest are in CMakeLists.txt")
        endif()
    endif()
    file(ARCHIVE_EXTRACT INPUT "${WATASHI_ORT_ARCHIVE}" DESTINATION "${WATASHI_ORT_DIR}/pkg")
endif()

set(WATASHI_ORT_INCLUDE "${WATASHI_ORT_DIR}/pkg/build/native/include")
set(WATASHI_ORT_LIB "${WATASHI_ORT_DIR}/pkg/runtimes/win-x64/native/onnxruntime.lib")
set(WATASHI_ORT_DLL "${WATASHI_ORT_DIR}/pkg/runtimes/win-x64/native/onnxruntime.dll")

add_library(watashi_onnxruntime SHARED IMPORTED GLOBAL)
set_target_properties(watashi_onnxruntime PROPERTIES
    IMPORTED_IMPLIB "${WATASHI_ORT_LIB}"
    IMPORTED_LOCATION "${WATASHI_ORT_DLL}"
    INTERFACE_INCLUDE_DIRECTORIES "${WATASHI_ORT_INCLUDE}")

# Anything that links the runtime needs the DLL beside it on Windows.
function(watashi_copy_onnxruntime target)
    add_custom_command(TARGET ${target} POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "${WATASHI_ORT_DLL}"
            "$<TARGET_FILE_DIR:${target}>/onnxruntime.dll"
        COMMENT "Copying onnxruntime.dll next to ${target}")
endfunction()

# --------------------------------------------------------------------------- #
# targets
# --------------------------------------------------------------------------- #
#
# watashi_core is the whole engine skeleton and holds no main(): the CLI and the
# tests differ only in how they drive it. Choosing this shape now means the port
# can grow without having to split the library out of an executable later.
add_library(watashi_core STATIC
    src/capture/capture.cpp
    src/capture/change_detector.cpp
    src/capture/synthetic_capture.cpp
    src/core/events.cpp
    src/core/pipeline.cpp
    src/core/session.cpp
    src/io/jsonl.cpp
    src/fake/fake_ocr.cpp
    src/fake/fake_translator.cpp
    src/ocr/onnx_ocr.cpp
    src/corpus/corpus_store.cpp
    src/corpus/rules.cpp
    src/corpus/corpus_translator.cpp
)
target_include_directories(watashi_core
    PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include
    PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src
)
target_link_libraries(watashi_core
    PUBLIC nlohmann_json::nlohmann_json
    PRIVATE watashi_onnxruntime
)
target_compile_features(watashi_core PUBLIC cxx_std_17)

# stats.memory_mib comes from the process working set on Windows; capture needs
# GDI, USER32 (DPI awareness, window enumeration) and DWM's frame bounds.
if(WIN32)
    target_link_libraries(watashi_core PRIVATE psapi user32 gdi32 dwmapi winmm)
endif()

add_executable(watashi_cli src/cli/main.cpp)
target_link_libraries(watashi_cli PRIVATE watashi_core)
watashi_copy_onnxruntime(watashi_cli)

include(CTest)
if(BUILD_TESTING)
    add_subdirectory(tests)
endif()
