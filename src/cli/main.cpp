// watashi_cli
//
// Two modes, one binary:
//
//   watashi_cli --synthetic --duration 2 --json-lines      fake OCR (default)
//   watashi_cli --ocr onnx --frame f.bgr --det d --rec r --chars c
//
// The fake path is what CI runs: it needs no model and no screen. The ONNX path
// loads the PP-OCRv4 det/rec models the caller points at and prints one JSON
// object per frame. Raw BGR buffers only -- the CLI never decodes an image file.

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <exception>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

#include "watashi/events.hpp"
#include "watashi/ocr.hpp"
#include "watashi/pipeline.hpp"
#include "watashi/session.hpp"

namespace {

constexpr int kFrameWidth = 1200;
constexpr int kFrameHeight = 200;

void usage(std::ostream& out) {
    out << "usage: watashi_cli --ocr fake|onnx [options]\n"
           "\n"
           "fake (default):\n"
           "  --synthetic            drive the fake OCR/translator path (required)\n"
           "  --duration SECONDS     run length, default 2\n"
           "  --fps N                synthetic frames per second, default 10\n"
           "  --json-lines           write the NDJSON event stream to stdout\n"
           "\n"
           "onnx:\n"
           "  --frame PATH           raw BGR8 frame, repeatable\n"
           "  --det PATH             detection model\n"
           "  --rec PATH             recognition model\n"
           "  --chars PATH           character table, one character per line\n"
           "  --cls PATH             optional angle classifier\n"
           "  --size WxH             frame size, default 1200x200\n"
           "  --repeat N             timed passes per frame, default 1\n";
}

std::vector<std::uint8_t> load_frame(const std::string& path, int width, int height) {
    std::ifstream file(path, std::ios::binary);
    if (!file) {
        throw watashi::OcrError("cannot open frame: " + path);
    }
    std::vector<std::uint8_t> bytes(static_cast<std::size_t>(width) * height * 3);
    file.read(reinterpret_cast<char*>(bytes.data()), static_cast<std::streamsize>(bytes.size()));
    if (!file || static_cast<std::size_t>(file.gcount()) != bytes.size()) {
        throw watashi::OcrError(
            "frame " + path + " is not " + std::to_string(width) + "x" +
            std::to_string(height) + "x3 bytes");
    }
    return bytes;
}

nlohmann::json lines_json(const std::vector<watashi::OcrLine>& lines) {
    nlohmann::json array = nlohmann::json::array();
    for (const auto& line : lines) {
        array.push_back(nlohmann::json{
            {"text", line.text},
            {"box", line.box ? line.box->to_json() : nlohmann::json(nullptr)},
            {"score", line.confidence},
        });
    }
    return array;
}

int run_fake(double duration, double fps, bool json_lines) {
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
    session.run(sink);
    std::cout.flush();
    return 0;
}

int run_onnx(
    const std::vector<std::string>& frames,
    const watashi::OnnxOcr::Options& options,
    int width,
    int height,
    int repeat) {
    if (frames.empty()) {
        std::cerr << "watashi_cli: --ocr onnx needs at least one --frame\n";
        return 2;
    }

    watashi::OnnxOcr engine(options);

    std::vector<std::vector<std::uint8_t>> buffers;
    buffers.reserve(frames.size());
    std::vector<watashi::ImageView> views;
    views.reserve(frames.size());
    for (const auto& path : frames) {
        buffers.push_back(load_frame(path, width, height));
        views.push_back(watashi::ImageView{buffers.back().data(), width, height});
    }

    // One untimed pass so kernel/arena initialisation is not charged to the
    // first measured frame.
    (void)engine.recognize(views.front());

    std::vector<watashi::OcrResult> results(frames.size());
    for (std::size_t index = 0; index < views.size(); ++index) {
        for (int pass = 0; pass < repeat; ++pass) {
            const auto started = std::chrono::steady_clock::now();
            results[index] = engine.recognize(views[index]);
            const double ms = std::chrono::duration<double, std::milli>(
                                  std::chrono::steady_clock::now() - started)
                                  .count();
            std::cerr << "run_ms=" << ms << '\n';
        }
        std::cout << nlohmann::json{
                         {"frame", frames[index]},
                         {"lines", lines_json(results[index].lines)},
                     }
                         .dump()
                  << '\n';
    }
    std::cout.flush();
    return 0;
}

}  // namespace

int main(int argc, char** argv) {
    bool synthetic = false;
    bool json_lines = false;
    double duration = 2.0;
    double fps = 10.0;
    int repeat = 1;
    int width = kFrameWidth;
    int height = kFrameHeight;
    std::string mode = "fake";
    std::vector<std::string> frames;
    watashi::OnnxOcr::Options onnx;

    for (int index = 1; index < argc; ++index) {
        const std::string argument = argv[index];
        try {
            if (argument == "--ocr" && index + 1 < argc) {
                mode = argv[++index];
            } else if (argument == "--synthetic") {
                synthetic = true;
            } else if (argument == "--json-lines") {
                json_lines = true;
            } else if (argument == "--duration" && index + 1 < argc) {
                duration = std::stod(argv[++index]);
            } else if (argument == "--fps" && index + 1 < argc) {
                fps = std::stod(argv[++index]);
            } else if (argument == "--repeat" && index + 1 < argc) {
                repeat = std::max(1, std::atoi(argv[++index]));
            } else if (argument == "--frame" && index + 1 < argc) {
                frames.emplace_back(argv[++index]);
            } else if (argument == "--det" && index + 1 < argc) {
                onnx.det_model = argv[++index];
            } else if (argument == "--rec" && index + 1 < argc) {
                onnx.rec_model = argv[++index];
            } else if (argument == "--chars" && index + 1 < argc) {
                onnx.charset_file = argv[++index];
            } else if (argument == "--cls" && index + 1 < argc) {
                onnx.cls_model = argv[++index];
            } else if (argument == "--size" && index + 1 < argc) {
                const std::string size = argv[++index];
                const auto separator = size.find('x');
                if (separator == std::string::npos) {
                    throw std::invalid_argument("expected WxH");
                }
                width = std::stoi(size.substr(0, separator));
                height = std::stoi(size.substr(separator + 1));
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

    try {
        if (mode == "onnx") {
            onnx.intra_threads = 4;
            onnx.inter_threads = 1;
            onnx.use_memory_arena = true;
            return run_onnx(frames, onnx, width, height, repeat);
        }
        if (mode != "fake") {
            std::cerr << "watashi_cli: --ocr must be fake or onnx\n";
            return 2;
        }
        if (!synthetic) {
            std::cerr << "watashi_cli: --synthetic is the only frame source in fake mode\n";
            usage(std::cerr);
            return 2;
        }
        return run_fake(duration, fps, json_lines);
    } catch (const watashi::OcrError& error) {
        std::cerr << "watashi_cli: " << error.what() << "\n";
        return 1;
    } catch (const std::exception& error) {
        std::cerr << "watashi_cli: " << error.what() << "\n";
        return 1;
    }
}
