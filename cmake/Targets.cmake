# Library and executable targets. Included by the top-level CMakeLists.txt
# after cmake/Dependencies.cmake, which provides the imported targets and the
# two DLL-copy helpers used below.

# --------------------------------------------------------------------------- #
# engine
# --------------------------------------------------------------------------- #
#
# watashi_core is the whole engine skeleton and holds no main(): the CLI and the
# tests differ only in how they drive it. Choosing this shape now means the port
# can grow without having to split the library out of an executable later.
add_library(watashi_core STATIC
    src/capture/capture.cpp
    src/capture/change_detector.cpp
    src/capture/synthetic_capture.cpp
    src/core/commands.cpp
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

# W4.3 observability: the log ring, the optional local minidump writer and the
# version stamp. Owner of the code that links it decides what to record; the
# library itself makes no network calls and records no subtitle text.
add_library(watashi_obs STATIC src/obs/obs.cpp)
target_include_directories(watashi_obs PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_compile_features(watashi_obs PUBLIC cxx_std_17)
target_compile_definitions(watashi_obs PUBLIC
    WATASHI_PRODUCT_VERSION="${WATASHI_VERSION}"
    WATASHI_GIT_COMMIT="${WATASHI_GIT_COMMIT}"
    WATASHI_BUILD_TYPE="$<CONFIG>")
if(WIN32)
    target_link_libraries(watashi_obs PUBLIC dbghelp)
endif()

add_executable(watashi_cli src/cli/main.cpp)
target_link_libraries(watashi_cli PRIVATE watashi_core watashi_obs)
watashi_copy_onnxruntime(watashi_cli)
watashi_copy_ctranslate2(watashi_cli)

# --------------------------------------------------------------------------- #
# native overlay (W3.0)
# --------------------------------------------------------------------------- #
#
# watashi_ui is a viewer, not a second engine: it links the event contract so
# both sides decode the same wire format, spawns the CLI and paints the frames.
# It deliberately does not link watashi_core: the engine stays behind the child
# process, so the overlay binary carries no inference runtime.
# Win32 USER32/GDI32 are already in the toolchain, which is why ADR 0009 chose
# it over Qt6 (see docs/adr/0009-ui-toolkit.md).
if(WIN32)
    add_library(watashi_ui_core STATIC
        src/ui/actions.cpp
        src/ui/control.cpp
        src/ui/control_window.cpp
        src/ui/model.cpp
        src/ui/presentation.cpp
        src/ui/settings.cpp
        src/ui/settings_window.cpp
        src/ui/layout.cpp
        src/ui/renderer.cpp
        src/ui/layered_window.cpp
        src/ui/child_stream.cpp
        src/ui/tray.cpp
    )
    target_include_directories(watashi_ui_core
        PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include
        PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src
    )
    target_link_libraries(watashi_ui_core
        PUBLIC nlohmann_json::nlohmann_json
        PRIVATE watashi_core user32 gdi32 psapi shell32
    )
    target_compile_features(watashi_ui_core PUBLIC cxx_std_17)

    add_executable(watashi_ui src/ui/main.cpp)
    target_link_libraries(watashi_ui PRIVATE watashi_ui_core watashi_obs)
endif()

# Phase 0 verification: load the CTranslate2 model from C++ and translate the
# four probe sentences. Kept as a target so the check is reproducible from the
# build tree instead of a throwaway spike.
add_executable(watashi_nmt_probe tools/nmt_probe.cpp)
target_link_libraries(watashi_nmt_probe PRIVATE ctranslate2 sentencepiece)
target_include_directories(watashi_nmt_probe PRIVATE ${sentencepiece_SOURCE_DIR}/src)
watashi_copy_ctranslate2(watashi_nmt_probe)

# --------------------------------------------------------------------------- #
# model fetching (W4.2)
# --------------------------------------------------------------------------- #
#
# The core is a static library so the tests can exercise resume, offline
# refusal and hash-mismatch deletion without a network. The executable is what
# ships; it only downloads when --fetch is explicit.
if(WIN32)
    add_library(watashi_fetch_core STATIC tools/fetch_core.cpp)
    target_include_directories(watashi_fetch_core
        PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/tools)
    target_link_libraries(watashi_fetch_core
        PUBLIC nlohmann_json::nlohmann_json winhttp bcrypt)
    target_compile_features(watashi_fetch_core PUBLIC cxx_std_17)

    add_executable(watashi_fetch tools/fetch_models.cpp)
    target_link_libraries(watashi_fetch PRIVATE watashi_fetch_core watashi_obs)
endif()
