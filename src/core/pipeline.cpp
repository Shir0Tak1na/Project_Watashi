#include "watashi/pipeline.hpp"

#include <algorithm>
#include <chrono>
#include <set>
#include <string>
#include <utility>

namespace watashi {
namespace {

/// Which recogniser actually read the frame. The seam publishes no backend
/// query on purpose, and the only two implementations in this build are the
/// deterministic FakeOcr and the ONNX Runtime path, so the type is enough.
std::string ocr_backend_name(const OcrSource& ocr) {
    return dynamic_cast<const FakeOcr*>(&ocr) != nullptr ? "fake" : "onnx";
}

}  // namespace

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
        {"protected_terms", protected_terms},
        {"lost_placeholders", lost_placeholders},
        {"used_model", used_model},
        {"fallback_reason", fallback_reason.empty() ? nlohmann::json(nullptr)
                                                    : nlohmann::json(fallback_reason)},
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

void Pipeline::set_context(TranslateContext context) {
    context_ = std::move(context);
}

OverlayUpdate Pipeline::process(const ImageView& frame, double started_seconds) const {
    OverlayUpdate update;
    const OcrResult recognised = ocr_.recognize(frame);
    update.ocr_ms = recognised.elapsed_ms;

    const auto translation_started = std::chrono::steady_clock::now();
    std::vector<std::string> sources;
    sources.reserve(recognised.lines.size());
    for (const auto& line : recognised.lines) {
        sources.push_back(line.text);
    }
    // One call per frame: the NMT backend batches its model inference across
    // the lines, the deterministic backends loop inside the default.
    const std::vector<Translation> translations =
        translator_.translate_batch(sources, context_);
    // Provenance is whatever actually answered: the OCR source names itself and
    // every translation carries the backend/trace of the layer that produced
    // it. Distinct values are kept once, in first-seen order for traces and
    // sorted for backends, so one frame reads the same way every run.
    std::set<std::string> backend_names;
    std::vector<std::string> trace_lines;
    for (std::size_t index = 0; index < recognised.lines.size() &&
                                index < translations.size();
         ++index) {
        const auto& line = recognised.lines[index];
        const Translation& translated = translations[index];
        if (!translated.backend.empty()) {
            backend_names.insert(translated.backend);
        }
        if (!translated.trace.empty() &&
            std::find(trace_lines.begin(), trace_lines.end(), translated.trace) ==
                trace_lines.end()) {
            trace_lines.push_back(translated.trace);
        }
        OverlayLine published;
        published.source = line.text;
        published.target = translated.target;
        published.box = line.box;
        published.confidence = line.confidence;
        published.coverage = translated.coverage;
        published.protected_terms = translated.protected_terms;
        published.lost_placeholders = translated.lost_placeholders;
        published.used_model = translated.used_model;
        published.fallback_reason = translated.fallback_reason;

        if (!update.source_text.empty()) {
            update.source_text += '\n';
            update.target_text += '\n';
        }
        update.source_text += published.source;
        update.target_text += published.target;
        update.lines.push_back(std::move(published));
    }
    const auto finished = std::chrono::steady_clock::now();

    const std::string ocr_name = ocr_backend_name(ocr_);
    // The CI skeleton (fake OCR and fake translator) keeps the exact values it
    // has always published; anything that ran a model or a corpus names itself.
    const bool skeleton =
        ocr_name == "fake" &&
        (backend_names.empty() ||
         (backend_names.size() == 1 && *backend_names.begin() == "fake"));
    if (skeleton) {
        update.backend = "fake";
        update.trace = "fake: skeleton path, no OCR and no model";
    } else {
        update.backend = ocr_name;
        for (const auto& name : backend_names) {
            if (name != ocr_name) {
                update.backend += ", " + name;
            }
        }
        update.trace = "ocr: " + ocr_name;
        for (const auto& line : trace_lines) {
            update.trace += '\n' + line;
        }
    }
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
