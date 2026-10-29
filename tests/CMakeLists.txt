add_executable(watashi_tests
    test_capture.cpp
    test_corpus.cpp
    test_events.cpp
    test_ocr.cpp
    test_rules.cpp
    test_session.cpp
)
target_link_libraries(watashi_tests PRIVATE watashi_core Catch2::Catch2WithMain)
watashi_copy_onnxruntime(watashi_tests)

# One ctest entry per TEST_CASE, which is what makes a failure name the case
# instead of the binary.
include(Catch)
catch_discover_tests(watashi_tests)

# The one test that needs a real compositor. It exits 3 when there is no
# display, so ctest reports a failure rather than a skip: a screen path that
# never looked at a screen must not look green.
if(WIN32)
    add_executable(watashi_screen_tests capture_screen_test.cpp)
    target_link_libraries(watashi_screen_tests PRIVATE watashi_core)
    watashi_copy_onnxruntime(watashi_screen_tests)
    add_test(NAME capture_screen COMMAND watashi_screen_tests)
endif()
