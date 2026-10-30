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

# CMake 4 refuses projects that declare cmake_minimum_required below 3.5.
# cpu_features (pulled in by CTranslate2) and ruy still declare older floors;
# raising the minimum keeps them configurable without patching vendored sources.
set(CMAKE_POLICY_VERSION_MINIMUM 3.5)

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
        URL https://codeload.github.com/nlohmann/json/tar.gz/refs/tags/v3.11.3
        URL_HASH SHA256=0d8ef5af7f9794e3263480193c491549b2ba6cc74bb018906202ada498a79406
        DOWNLOAD_EXTRACT_TIMESTAMP FALSE)
    FetchContent_MakeAvailable(nlohmann_json)
endif()

find_package(Catch2 3 CONFIG QUIET)
if(NOT Catch2_FOUND)
    message(STATUS "Catch2 v3 not found; fetching v3.5.2")
    FetchContent_Declare(Catch2
        URL https://codeload.github.com/catchorg/Catch2/tar.gz/refs/tags/v3.5.2
        URL_HASH SHA256=269543a49eb76f40b3f93ff231d4c24c27a7e16c90e47d2e45bcc564de470c6e
        DOWNLOAD_EXTRACT_TIMESTAMP FALSE)
    # Build it static. CTranslate2 declares BUILD_SHARED_LIBS ON later (it needs
    # a shared library so a static build's /MT runtime cannot mix with ours), and
    # a cache-wide ON would otherwise also turn Catch2 into DLLs that have to be
    # copied beside the test binary. The shadowing normal variable is removed
    # right after so CTranslate2 still gets its shared build.
    set(BUILD_SHARED_LIBS OFF)
    FetchContent_MakeAvailable(Catch2)
    unset(BUILD_SHARED_LIBS)
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
# CTranslate2 and SentencePiece (W2)
# --------------------------------------------------------------------------- #
#
# NLLB-200 is distributed as a CTranslate2 model, so the native engine links the
# C++ library directly instead of driving the Python extension. CTranslate2 is
# cloned at the tag matching the model format (4.8.x) with only the submodules a
# CPU build needs: spdlog (header-only logging) and cpu_features (ISA detection).
# CUDA, tests and the CLI are off.
#
# The GEMM backend is oneDNN, not Ruy. This is not a preference: the Python
# CTranslate2 wheel this project ships with is built against oneDNN (its DLL
# contains dnnl and intel OpenMP markers), and the two int8 backends do not
# always pick the same token on ambiguous input ("gg wp noob"). Using the same
# backend is what makes the 120-case reference comparison exact instead of
# "almost". Intel publishes oneDNN as NuGet packages; the vcomp variant matches
# MSVC's /openmp runtime.
#
# SentencePiece provides the NLLB tokenizer. The model ships both
# ``sentencepiece.bpe.model`` and a Hugging Face ``tokenizer.json``; libsentencepiece
# is used because it is the original tokenizer and its pieces were verified
# identical to the Python ``tokenizers`` output before this was wired in.
include(FetchContent)

set(WATASHI_CT2_TAG "v4.8.4")
set(WATASHI_SPM_TAG "v0.2.1")
set(WATASHI_SPM_SHA256
    "c1a59e9259c9653ad0ade653dadff074cd31f0a6ff2a11316f67bee4189a8f1b")

# --- CTranslate2 sources ---------------------------------------------------- #
#
# Release tarballs, not a git clone: a shallow clone with recursively fetched
# submodules reset frequently here, and a tarball is smaller and pinned by
# content. The two submodules a CPU build needs are placed into the paths
# CTranslate2's own CMakeLists expects (its tarball does not include submodules).
set(WATASHI_CT2_ARCHIVE
    "${CMAKE_BINARY_DIR}/_deps/ctranslate2-${WATASHI_CT2_TAG}.tar.gz")
set(WATASHI_CT2_SHA256
    "934b80edcf90da4d38c536a832df79e166b12bc3750540b1cb208cf6943beecf")
set(WATASHI_CT2_SRC "${CMAKE_BINARY_DIR}/_deps/ctranslate2-${WATASHI_CT2_TAG}-src")

set(WATASHI_SPDLOG_ARCHIVE "${CMAKE_BINARY_DIR}/_deps/spdlog-1.10.0.tar.gz")
set(WATASHI_SPDLOG_SHA256
    "697f91700237dbae2326b90469be32b876b2b44888302afbc7aceb68bcfe8224")
set(WATASHI_CPUFEATURES_ARCHIVE "${CMAKE_BINARY_DIR}/_deps/cpu_features-0.9.0.tar.gz")
set(WATASHI_CPUFEATURES_SHA256
    "bdb3484de8297c49b59955c3b22dba834401bc2df984ef5cfc17acbe69c5018e")

function(watashi_download url path sha256)
    if(EXISTS "${path}")
        return()
    endif()
    message(STATUS "Downloading ${url}")
    file(DOWNLOAD "${url}" "${path}" EXPECTED_HASH SHA256=${sha256}
        TLS_VERIFY ON STATUS _status)
    list(GET _status 0 _code)
    if(NOT _code EQUAL 0)
        file(REMOVE "${path}")
        message(FATAL_ERROR "download failed (${_status}): ${url}")
    endif()
endfunction()

# Extract an archive whose root is a single directory into ``target_dir``.
function(watashi_stage_tarball archive target_dir)
    if(EXISTS "${target_dir}/CMakeLists.txt")
        return()
    endif()
    set(_unpack "${target_dir}.unpack")
    file(REMOVE_RECURSE "${_unpack}")
    file(ARCHIVE_EXTRACT INPUT "${archive}" DESTINATION "${_unpack}")
    file(MAKE_DIRECTORY "${target_dir}")
    file(GLOB _entries LIST_DIRECTORIES true "${_unpack}/*")
    list(LENGTH _entries _count)
    if(_count EQUAL 1)
        file(GLOB _children LIST_DIRECTORIES true "${_entries}/*")
        foreach(_child ${_children})
            get_filename_component(_name "${_child}" NAME)
            file(RENAME "${_child}" "${target_dir}/${_name}")
        endforeach()
    else()
        foreach(_entry ${_entries})
            get_filename_component(_name "${_entry}" NAME)
            file(RENAME "${_entry}" "${target_dir}/${_name}")
        endforeach()
    endif()
    file(REMOVE_RECURSE "${_unpack}")
endfunction()

watashi_download(
    "https://codeload.github.com/OpenNMT/CTranslate2/tar.gz/refs/tags/${WATASHI_CT2_TAG}"
    "${WATASHI_CT2_ARCHIVE}" "${WATASHI_CT2_SHA256}")
watashi_download(
    "https://codeload.github.com/gabime/spdlog/tar.gz/refs/tags/v1.10.0"
    "${WATASHI_SPDLOG_ARCHIVE}" "${WATASHI_SPDLOG_SHA256}")
watashi_download(
    "https://codeload.github.com/google/cpu_features/tar.gz/refs/tags/v0.9.0"
    "${WATASHI_CPUFEATURES_ARCHIVE}" "${WATASHI_CPUFEATURES_SHA256}")

watashi_stage_tarball("${WATASHI_CT2_ARCHIVE}" "${WATASHI_CT2_SRC}")
watashi_stage_tarball("${WATASHI_SPDLOG_ARCHIVE}" "${WATASHI_CT2_SRC}/third_party/spdlog")
watashi_stage_tarball("${WATASHI_CPUFEATURES_ARCHIVE}"
    "${WATASHI_CT2_SRC}/third_party/cpu_features")

# --- oneDNN (Intel NuGet, pinned) ------------------------------------------ #
set(WATASHI_DNNL_VERSION "2023.2.0.49513")
set(WATASHI_DNNL_DEVEL_URL
    "https://www.nuget.org/api/v2/package/onednn.cpu_vcomp.devel.win-x64/${WATASHI_DNNL_VERSION}")
set(WATASHI_DNNL_DEVEL_SHA256
    "b544fdaa787554fd6562119442eaf851a5a9829af203651bb7ff9b3f9ccac74c")
set(WATASHI_DNNL_REDIST_URL
    "https://www.nuget.org/api/v2/package/onednn.cpu_vcomp.redist.win-x64/${WATASHI_DNNL_VERSION}")
set(WATASHI_DNNL_REDIST_SHA256
    "02f802ed752e6f5ab87f3a0fd611990e63244febb41fb559175b400a46c1ee62")
set(WATASHI_DNNL_DIR "${CMAKE_BINARY_DIR}/_deps/onednn-${WATASHI_DNNL_VERSION}")

if(NOT EXISTS "${WATASHI_DNNL_DIR}/devel/build/native/include/dnnl.h")
    if(NOT EXISTS "${WATASHI_DNNL_DIR}/devel.nupkg")
        message(STATUS "Downloading oneDNN ${WATASHI_DNNL_VERSION} (devel) from nuget.org")
        file(DOWNLOAD "${WATASHI_DNNL_DEVEL_URL}" "${WATASHI_DNNL_DIR}/devel.nupkg"
            EXPECTED_HASH SHA256=${WATASHI_DNNL_DEVEL_SHA256}
            TLS_VERIFY ON
            STATUS WATASHI_DNNL_DEVEL_DOWNLOAD)
        list(GET WATASHI_DNNL_DEVEL_DOWNLOAD 0 WATASHI_DNNL_DEVEL_CODE)
        if(NOT WATASHI_DNNL_DEVEL_CODE EQUAL 0)
            file(REMOVE "${WATASHI_DNNL_DIR}/devel.nupkg")
            message(FATAL_ERROR "oneDNN devel download failed (${WATASHI_DNNL_DEVEL_DOWNLOAD})")
        endif()
    endif()
    file(ARCHIVE_EXTRACT INPUT "${WATASHI_DNNL_DIR}/devel.nupkg"
        DESTINATION "${WATASHI_DNNL_DIR}/devel")
endif()

if(NOT EXISTS "${WATASHI_DNNL_DIR}/redist/runtimes/native/dnnl.dll")
    if(NOT EXISTS "${WATASHI_DNNL_DIR}/redist.nupkg")
        message(STATUS "Downloading oneDNN ${WATASHI_DNNL_VERSION} (redist) from nuget.org")
        file(DOWNLOAD "${WATASHI_DNNL_REDIST_URL}" "${WATASHI_DNNL_DIR}/redist.nupkg"
            EXPECTED_HASH SHA256=${WATASHI_DNNL_REDIST_SHA256}
            TLS_VERIFY ON
            STATUS WATASHI_DNNL_REDIST_DOWNLOAD)
        list(GET WATASHI_DNNL_REDIST_DOWNLOAD 0 WATASHI_DNNL_REDIST_CODE)
        if(NOT WATASHI_DNNL_REDIST_CODE EQUAL 0)
            file(REMOVE "${WATASHI_DNNL_DIR}/redist.nupkg")
            message(FATAL_ERROR "oneDNN redist download failed (${WATASHI_DNNL_REDIST_DOWNLOAD})")
        endif()
    endif()
    file(ARCHIVE_EXTRACT INPUT "${WATASHI_DNNL_DIR}/redist.nupkg"
        DESTINATION "${WATASHI_DNNL_DIR}/redist")
endif()
set(WATASHI_DNNL_INCLUDE "${WATASHI_DNNL_DIR}/devel/build/native/include")
set(WATASHI_DNNL_LIB "${WATASHI_DNNL_DIR}/devel/build/native/lib/dnnl.lib")
set(WATASHI_DNNL_DLL "${WATASHI_DNNL_DIR}/redist/runtimes/native/dnnl.dll")

set(BUILD_CLI OFF CACHE BOOL "" FORCE)
set(BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(WITH_MKL OFF CACHE BOOL "" FORCE)
set(WITH_DNNL ON CACHE BOOL "" FORCE)
set(WITH_OPENBLAS OFF CACHE BOOL "" FORCE)
set(WITH_ACCELERATE OFF CACHE BOOL "" FORCE)
set(WITH_RUY OFF CACHE BOOL "" FORCE)
set(WITH_CUDA OFF CACHE BOOL "" FORCE)
set(WITH_CUDNN OFF CACHE BOOL "" FORCE)
# MSVC's own OpenMP runtime (/openmp); the Intel iomp5 default is not installed
# and the vcomp oneDNN package is built against the same runtime.
set(OPENMP_RUNTIME "COMP" CACHE STRING "" FORCE)
set(DNNL_INCLUDE_DIR "${WATASHI_DNNL_INCLUDE}" CACHE PATH "" FORCE)
set(DNNL_LIBRARY "${WATASHI_DNNL_LIB}" CACHE FILEPATH "" FORCE)

add_subdirectory("${WATASHI_CT2_SRC}"
    "${CMAKE_BINARY_DIR}/_deps/ctranslate2-build" EXCLUDE_FROM_ALL)

set(SPM_ENABLE_SHARED OFF CACHE BOOL "" FORCE)
set(SPM_BUILD_TEST OFF CACHE BOOL "" FORCE)
set(SPM_ENABLE_TCMALLOC OFF CACHE BOOL "" FORCE)
FetchContent_Declare(sentencepiece
    URL https://codeload.github.com/google/sentencepiece/tar.gz/refs/tags/${WATASHI_SPM_TAG}.tar.gz
    URL_HASH SHA256=${WATASHI_SPM_SHA256}
    DOWNLOAD_EXTRACT_TIMESTAMP FALSE)
FetchContent_MakeAvailable(sentencepiece)

# Anything linked against CTranslate2 needs its GEMM backend and DLL beside it.
function(watashi_copy_ctranslate2 target)
    add_custom_command(TARGET ${target} POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "$<TARGET_FILE:ctranslate2>"
            "$<TARGET_FILE_DIR:${target}>"
        COMMENT "Copying ctranslate2.dll next to ${target}")
    add_custom_command(TARGET ${target} POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "${WATASHI_DNNL_DLL}"
            "$<TARGET_FILE_DIR:${target}>"
        COMMENT "Copying dnnl.dll next to ${target}")
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
    src/nmt/semantics.cpp
    src/nmt/local_nmt.cpp
    src/nmt/nmt_translator.cpp
)
target_include_directories(watashi_core
    PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include
    PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src ${sentencepiece_SOURCE_DIR}/src
)
target_link_libraries(watashi_core
    PUBLIC nlohmann_json::nlohmann_json
    PRIVATE watashi_onnxruntime ctranslate2 sentencepiece
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
watashi_copy_ctranslate2(watashi_cli)

# Phase 0 verification: load the CTranslate2 model from C++ and translate the
# four probe sentences. Kept as a target so the check is reproducible from the
# build tree instead of a throwaway spike.
add_executable(watashi_nmt_probe tools/nmt_probe.cpp)
target_link_libraries(watashi_nmt_probe PRIVATE ctranslate2 sentencepiece)
target_include_directories(watashi_nmt_probe PRIVATE ${sentencepiece_SOURCE_DIR}/src)
watashi_copy_ctranslate2(watashi_nmt_probe)

include(CTest)
if(BUILD_TESTING)
    add_subdirectory(tests)
endif()
