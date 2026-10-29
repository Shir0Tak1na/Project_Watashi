#include "watashi/pipeline.hpp"

#include <chrono>
#include <string>
#include <utility>

namespace watashi {

nlohmann::json Box::to_json() const {
    // The wire form is the corner pair the Python contract publishes, not the
    // width/height pair this struct stores.
    return nlohmann::json::array({x, y, x + width, y + height});
}

nlohmann::json OverlayLine::to_json() const {
    return nlohmann::json{
        {"source", source},
        {"target", target},
        {"box", box ? box->to_json() : nlohmann::json(nullptr)},
        {"confidence", confidence},
        {"coverage", coverage},
    };
}

nlohmann::json OverlayUpdate::to_json() const {
    nlohmann::json lines_json = nlohmann::json::array();
    for (const auto& line : lines) {
        lines_json.push_back(line.to_json());
    }
    return nlohmann::json{
        {"source", source_text},
        {"target", target_text},
        {"coverage", coverage},
        {"confidence", confidence},
        {"latency_ms", latency_ms},
        {"trace", trace},
        {"refined", refined},
        {"backend", backend},
        {"lines", std::move(lines_json)},
    };
}

Pipeline::Pipeline(
    const OcrSource& ocr,
    const Translator& translator,
    TranslateContext context)
    : ocr_(ocr), translator_(translator), context_(std::move(context)) {}

OverlayUpdate Pipeline::process(const ImageView& frame, double started_seconds) const {
    OverlayUpdate update;
    const OcrResult recognised = ocr_.recognize(frame);
    update.ocr_ms = recognised.elapsed_ms;

    const auto translation_started = std::chrono::steady_clock::now();
    for (const auto& line : recognised.lines) {
        const Translation translated = translator_.translate(line.text, context_);
        OverlayLine published;
        published.source = line.text;
        published.target = translated.target;
        published.box = line.box;
        published.confidence = line.confidence;
        published.coverage = translated.coverage;

        if (!update.source_text.empty()) {
            update.source_text += '\n';
            update.target_text += '\n';
        }
        update.source_text += published.source;
        update.target_text += published.target;
        update.lines.push_back(std::move(published));
    }
    const auto finished = std::chrono::steady_clock::now();

    update.backend = "fake";
    update.trace = "fake: skeleton path, no OCR and no model";
    if (!update.lines.empty()) {
        double confidence = 0.0;
        double coverage = 0.0;
        for (const auto& line : update.lines) {
            confidence += line.confidence;
            coverage += line.coverage;
        }
        const auto count = static_cast<double>(update.lines.size());
        update.confidence = confidence / count;
        update.coverage = coverage / count;
    }

    update.translate_ms =
        std::chrono::duration<double, std::milli>(finished - translation_started).count();

    // Derived from the stamp the frame arrived with, never sampled at paint
    // time: that is the rule the Python pipeline follows for its fragments.
    const double finished_seconds =
        std::chrono::duration<double>(finished.time_since_epoch()).count();
    update.latency_ms = (finished_seconds - started_seconds) * 1000.0;
    return update;
}

}  // namespace watashi
