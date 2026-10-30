// Deterministic child for the ChildStream unit tests.
//
// It prints exactly the lines asked for, optionally without a trailing newline
// and with a few stderr lines, then exits with the requested code. Nothing here
// is an engine: the point is to exercise pipes, EOF, the exit code and the
// stderr tail without a model or a screen.

#include <chrono>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <string>
#include <thread>

#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

int main(int argc, char** argv) {
    int count = 3;
    int exit_code = 0;
    int sleep_ms = 0;
    int stderr_lines = 0;
    bool final_newline = true;
    std::string text = "line";
    std::string stderr_text = "stderr";

    for (int index = 1; index < argc; ++index) {
        const std::string argument = argv[index];
        if (argument == "--count" && index + 1 < argc) {
            count = std::atoi(argv[++index]);
        } else if (argument == "--exit-code" && index + 1 < argc) {
            exit_code = std::atoi(argv[++index]);
        } else if (argument == "--sleep-ms" && index + 1 < argc) {
            sleep_ms = std::atoi(argv[++index]);
        } else if (argument == "--stderr-lines" && index + 1 < argc) {
            stderr_lines = std::atoi(argv[++index]);
        } else if (argument == "--stderr" && index + 1 < argc) {
            stderr_text = argv[++index];
        } else if (argument == "--text" && index + 1 < argc) {
            text = argv[++index];
        } else if (argument == "--no-final-newline") {
            final_newline = false;
        }
    }

    for (int index = 0; index < count; ++index) {
        const bool last = index + 1 == count;
        std::cout << text << ' ' << (index + 1);
        if (!last || final_newline) {
            std::cout << '\n';
        }
        std::cout.flush();
    }
    for (int index = 0; index < stderr_lines; ++index) {
        std::cerr << stderr_text << ' ' << (index + 1) << '\n';
    }
    std::cerr.flush();

    if (sleep_ms > 0) {
        std::this_thread::sleep_for(std::chrono::milliseconds(sleep_ms));
    }
    return exit_code;
}
