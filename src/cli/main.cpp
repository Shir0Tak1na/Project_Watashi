// watashi_cli --synthetic --duration <seconds> --json-lines
//
// The walking skeleton's only frame source is the fake one; the CLI exists to
// prove the stream shape (ready first, monotonic seq, stopped last) is what
// tools/validate_stream.py expects. It does not touch Python, a screen or a
// model.

#include <cstdlib>
#include <exception>
#include <iostream>
#include <string>

#include "watashi/events.hpp"
#include "watashi/pipeline.hpp"
#include "watashi/session.hpp"

namespace {

void usage(std::ostream& out) {
    out << "usage: watashi_cli --synthetic [--duration SECONDS] [--fps N] "
           "[--json-lines]\n"
           "\n"
           "  --synthetic        drive the fake OCR/translator path (required)\n"
           "  --duration         run length in seconds, default 2\n"
           "  --fps              synthetic frames per second, default 10\n"
           "  --json-lines       write the NDJSON event stream to stdout\n";
}

}  // namespace

int main(int argc, char** argv) {
    bool synthetic = false;
    bool json_lines = false;
    double duration = 2.0;
    double fps = 10.0;

    for (int index = 1; index < argc; ++index) {
        const std::string argument = argv[index];
        try {
            if (argument == "--synthetic") {
                synthetic = true;
            } else if (argument == "--json-lines") {
                json_lines = true;
            } else if (argument == "--duration" && index + 1 < argc) {
                duration = std::stod(argv[++index]);
            } else if (argument == "--fps" && index + 1 < argc) {
                fps = std::stod(argv[++index]);
            } else if (argument == "--help" || argument == "-h") {
                usage(std::cout);
                return 0;
            } else {
                std::cerr << "unknown argument: " << argument << "\n";
                usage(std::cerr);
                return 2;
            }
        } catch (const std::exception& error) {
            std::cerr << "invalid value for " << argument << ": " << error.what() << "\n";
            return 2;
        }
    }

    if (!synthetic) {
        std::cerr << "watashi_cli: --synthetic is the only frame source in this "
                     "skeleton\n";
        usage(std::cerr);
        return 2;
    }

    watashi::FakeOcr ocr;
    watashi::FakeTranslator translator;
    watashi::Session session({duration, fps}, ocr, translator);

    const auto sink = [json_lines](const watashi::Envelope& envelope) {
        if (json_lines) {
            watashi::write_json_line(std::cout, envelope);
        } else {
            std::cout << '[' << envelope.seq << "] " << envelope.type << '\n';
        }
    };

    try {
        session.run(sink);
    } catch (const std::exception& error) {
        std::cerr << "watashi_cli: " << error.what() << "\n";
        return 1;
    }
    std::cout.flush();
    return 0;
}
