add_executable(watashi_tests
    test_events.cpp
    test_session.cpp
)
target_link_libraries(watashi_tests PRIVATE watashi_core Catch2::Catch2WithMain)

# One ctest entry per TEST_CASE, which is what makes a failure name the case
# instead of the binary.
include(Catch)
catch_discover_tests(watashi_tests)
