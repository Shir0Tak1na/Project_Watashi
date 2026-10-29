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
#include "watashi/corpus.hpp"
#include "watashi/ocr.hpp"
#include "watashi/pipeline.hpp"
#include "watashi/session.hpp"
#include "watashi/translator.hpp"

namespace {

constexpr int kFrameWidth = 1200;
constexpr int kFrameHeight = 200;

void usage(std::ostream& out) {
    out << "usage: watashi_cli --ocr fake|onnx [options]\n"
           "       watashi_cli --translator corpus [options]\n"
           "\n"
           "fake OCR (default):\n"
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
           "  --repeat N             timed passes per frame, default 1\n"
           "\n"
           "corpus (no model, no OCR):\n"
           "  --corpus LAYER=PATH    LAYER is user, domain or general; repeatable\n"
           "  --rules PATH           rule file, repeatable\n"
           "  --target LANG          target language, default zh-CN\n"
           "  --scene NAME           selected scene\n"
           "  --window TITLE         window title, for when_window entries\n"
           "  --text TEXT            one input line, repeatable\n"
           "  --text-file PATH       one input line per file line\n";
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

struct CorpusArgs {
    std::vector<std::string> user_dirs;
    std::vector<std::string> domain_dirs;
    std::vector<std::string> general_dirs;
    std::vector<std::string> rule_files;
    std::string target = "zh-CN";
    std::string scene;
    std::string window;
    std::vector<std::string> texts;
    bool json_lines = false;
};

void load_text_file(const std::string& path, std::vector<std::string>& texts) {
    std::ifstream file(path, std::ios::binary);
    if (!file) {
        throw std::runtime_error("cannot open text file: " + path);
    }
    const std::string content((std::istreambuf_iterator<char>(file)),
                              std::istreambuf_iterator<char>());
    std::size_t start = 0;
    while (start < content.size()) {
        const auto newline = content.find('\n', start);
        std::string line = content.substr(
            start, newline == std::string::npos ? std::string::npos : newline - start);
        if (!line.empty() && line.back() == '\r') {
            line.pop_back();
        }
        texts.push_back(std::move(line));
        if (newline == std::string::npos) {
            break;
        }
        start = newline + 1;
    }
}

int run_corpus(const CorpusArgs& args) {
    watashi::CorpusStore::Options options;
    options.user_dirs = args.user_dirs;
    options.domain_dirs = args.domain_dirs;
    options.general_dirs = args.general_dirs;
    options.rule_files = args.rule_files;
    watashi::CorpusStore store(options);
    watashi::CorpusTranslator translator(store);

    std::int64_t seq = 0;
    const watashi::TranslateContext context{args.target, args.scene, args.window};
    for (const auto& text : args.texts) {
        const auto started = std::chrono::steady_clock::now();
        const watashi::Translation translated = translator.translate(text, context);
        const double elapsed_ms = std::chrono::duration<double, std::milli>(
                                      std::chrono::steady_clock::now() - started)
                                      .count();

        watashi::OverlayUpdate update;
        update.source_text = text;
        update.target_text = translated.target;
        update.coverage = translated.coverage;
        update.confidence = translated.confidence;
        update.latency_ms = elapsed_ms;
        update.trace = translated.trace;
        update.backend = translated.backend;
        watashi::OverlayLine line;
        line.source = text;
        line.target = translated.target;
        line.confidence = translated.confidence;
        line.coverage = translated.coverage;
        update.lines.push_back(line);

        const watashi::Envelope envelope = watashi::encode_event(
            watashi::event_type::kSubtitle, update.to_json(), ++seq, watashi::now_seconds());
        if (args.json_lines) {
            watashi::write_json_line(std::cout, envelope);
        } else {
            std::cout << text << " -> " << translated.target << " [" << translated.backend
                      << " coverage=" << translated.coverage << "]\n";
        }
    }
    if (store.last_error().empty()) {
        std::cerr << "corpus: " << store.size() << " entries, " << store.rule_count()
                  << " rules\n";
    } else {
        std::cerr << "corpus: " << store.size() << " entries, " << store.rule_count()
                  << " rules; last error: " << store.last_error() << "\n";
    }
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
    std::string translator_name = "fake";
    CorpusArgs corpus;
    std::vector<std::string> frames;
    watashi::OnnxOcr::Options onnx;

    for (int index = 1; index < argc; ++index) {
        const std::string argument = argv[index];
        try {
            if (argument == "--ocr" && index + 1 < argc) {
                mode = argv[++index];
            } else if (argument == "--translator" && index + 1 < argc) {
                translator_name = argv[++index];
            } else if (argument == "--corpus" && index + 1 < argc) {
                const std::string spec = argv[++index];
                const auto separator = spec.find('=');
                if (separator == std::string::npos) {
                    throw std::invalid_argument("expected LAYER=PATH");
                }
                const std::string layer = spec.substr(0, separator);
                const std::string path = spec.substr(separator + 1);
                if (layer == "user") {
                    corpus.user_dirs.push_back(path);
                } else if (layer == "domain") {
                    corpus.domain_dirs.push_back(path);
                } else if (layer == "general") {
                    corpus.general_dirs.push_back(path);
                } else {
                    throw std::invalid_argument("layer must be user, domain or general");
                }
            } else if (argument == "--rules" && index + 1 < argc) {
                corpus.rule_files.emplace_back(argv[++index]);
            } else if (argument == "--target" && index + 1 < argc) {
                corpus.target = argv[++index];
            } else if (argument == "--scene" && index + 1 < argc) {
                corpus.scene = argv[++index];
            } else if (argument == "--window" && index + 1 < argc) {
                corpus.window = argv[++index];
            } else if (argument == "--text" && index + 1 < argc) {
                corpus.texts.emplace_back(argv[++index]);
            } else if (argument == "--text-file" && index + 1 < argc) {
                load_text_file(argv[++index], corpus.texts);
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
        if (translator_name == "corpus") {
            corpus.json_lines = json_lines;
            if (corpus.texts.empty()) {
                std::cerr << "watashi_cli: corpus mode needs --text or --text-file\n";
                return 2;
            }
            return run_corpus(corpus);
        }
        if (translator_name != "fake") {
            std::cerr << "watashi_cli: --translator must be fake or corpus\n";
            return 2;
        }
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
