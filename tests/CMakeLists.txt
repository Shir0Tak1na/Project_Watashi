add_executable(watashi_tests
    test_capture.cpp
    test_commands.cpp
    test_corpus.cpp
    test_events.cpp
    test_fetch.cpp
    test_nmt_semantics.cpp
    test_observability.cpp
    test_ocr.cpp
    test_presentation.cpp
    test_rules.cpp
    test_session.cpp
    test_settings.cpp
    test_ui.cpp
    test_ui_actions.cpp
)
target_link_libraries(watashi_tests
    PRIVATE watashi_core watashi_ui_core watashi_fetch_core watashi_obs
            Catch2::Catch2WithMain)
watashi_copy_onnxruntime(watashi_tests)
watashi_copy_ctranslate2(watashi_tests)

# One ctest entry per TEST_CASE, which is what makes a failure name the case
# instead of the binary.
include(Catch)
# 77 is the skip code the fetch test uses when the host's file filter reports a
# successful delete without removing the file: ctest must show it as skipped,
# never as a pass.
catch_discover_tests(watashi_tests PROPERTIES SKIP_RETURN_CODE 77)

# The one test that needs a real compositor. It exits 3 when there is no
# display, so ctest reports a failure rather than a skip: a screen path that
# never looked at a screen must not look green.
if(WIN32)
    add_executable(watashi_screen_tests capture_screen_test.cpp)
    target_link_libraries(watashi_screen_tests PRIVATE watashi_core)
    watashi_copy_onnxruntime(watashi_screen_tests)
    add_test(NAME capture_screen COMMAND watashi_screen_tests)

    # Child process for the ChildStream tests. It is a fixture executable, not
    # a test: it prints a fixed number of lines and exits with a chosen code.
    add_executable(watashi_ui_stream_fixture ui_stream_fixture.cpp)

    # The overlay against the real compositor. Exits 3 without a desktop, so
    # ctest does not treat an unexercised window path as green.
    add_executable(watashi_ui_window_tests ui_window_test.cpp)
    target_link_libraries(watashi_ui_window_tests PRIVATE watashi_ui_core)
    add_test(NAME overlay_window COMMAND watashi_ui_window_tests)

    # W4.3: --version is a build stamp, not a window. It must answer before the
    # overlay touches DPI, the engine or the log directory.
    add_test(NAME ui_version COMMAND $<TARGET_FILE:watashi_ui> --version)
    set_tests_properties(ui_version PROPERTIES
        PASS_REGULAR_EXPRESSION "Project Watashi [0-9]+\\.[0-9]+\\.[0-9]+")
    add_test(NAME cli_version COMMAND $<TARGET_FILE:watashi_cli> --version)
    set_tests_properties(cli_version PROPERTIES
        PASS_REGULAR_EXPRESSION "Project Watashi [0-9]+\\.[0-9]+\\.[0-9]+")
    add_test(NAME fetch_version COMMAND $<TARGET_FILE:watashi_fetch> --version)
    set_tests_properties(fetch_version PROPERTIES
        PASS_REGULAR_EXPRESSION "Project Watashi [0-9]+\\.[0-9]+\\.[0-9]+")
endif()

# config/settings_ui.json is generated from the Python schema. This fails when
# the committed document and the schema drift apart, which is what makes "the
# UI renders whatever the schema declares" true over time.
find_package(Python3 COMPONENTS Interpreter QUIET)
if(Python3_Interpreter_FOUND)
    add_test(
        NAME settings_ui_schema
        COMMAND ${Python3_EXECUTABLE}
                ${CMAKE_SOURCE_DIR}/tools/export_settings_ui.py --check)
    set_tests_properties(settings_ui_schema PROPERTIES
        WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})

    # Per-user install/uninstall, the manifest and the unsigned signing path.
    # Each case stages cmake --install into its own temporary prefix and uses a
    # test-only HKCU key and Start-menu directory.
    if(WIN32)
        foreach(case_name IN ITEMS manifest install remove_user_data unsigned)
            add_test(
                NAME installer_${case_name}
                COMMAND ${Python3_EXECUTABLE}
                        ${CMAKE_SOURCE_DIR}/tests/installer_test.py
                        --case ${case_name}
                        --build ${CMAKE_BINARY_DIR}
                        --config $<CONFIG>
                        --source ${CMAKE_SOURCE_DIR})
            set_tests_properties(installer_${case_name} PROPERTIES
                WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
                # A host whose token cannot open its own HKCU subkeys reports
                # 77, and ctest shows it as skipped rather than green.
                SKIP_RETURN_CODE 77)
        endforeach()
    endif()
else()
    message(WARNING "Python3 not found: settings and installer checks are disabled")
endif()
