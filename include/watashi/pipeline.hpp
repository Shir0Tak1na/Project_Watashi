// The frame path: OCR in, overlay payload out.
//
// Recognition and translation are pure interfaces, so the pipeline never knows
// whether the text came from a synthetic frame, a screen grab or an ONNX model,
// and the tests can drive it with no model, no capture and no clock.

#pragma once

#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include <nlohmann/json.hpp>

#include "watashi/ocr.hpp"
#include "watashi/translator.hpp"

namespace watashi {

/// One published line: what was read, what it became, and where it was.
struct OverlayLine {
    std::string source;
    std::string target;
    std::optional<Box> box;
    double confidence = 1.0;
    double coverage = 1.0;
    //: NMT provenance, empty/zero for the deterministic backends.
    int protected_terms = 0;
    int lost_placeholders = 0;
    bool used_model = false;
    std::string fallback_reason;

    [[nodiscard]] nlohmann::json to_json() const;
};

/// One frame, in the shape ``subtitle.data`` travels in.
struct OverlayUpdate {
    std::string source_text;
    std::string target_text;
    double coverage = 1.0;
    double confidence = 1.0;
    double latency_ms = 0.0;
    //: Measured split of the work above. Internal only: ``to_json`` publishes
    //: ``latency_ms`` because that is the field the contract has, while the
    //: session reports the two halves in ``stats``.
    double ocr_ms = 0.0;
    double translate_ms = 0.0;
    std::string trace;
    std::string backend;
    bool refined = false;
    std::vector<OverlayLine> lines;

    [[nodiscard]] nlohmann::json to_json() const;
};

/// Recognises and translates one frame. Holds references, so the sources must
/// outlive it -- which is exactly the lifetime ``Session`` gives them.
class Pipeline {
public:
    Pipeline(const OcrSource& ocr, const Translator& translator,
             TranslateContext context = {});

    /// ``started_seconds`` is the monotonic (steady clock) stamp the frame
    /// arrived on. It is only used for ``latency_ms``; no wall clock is read
    /// while building the payload.
    [[nodiscard]] OverlayUpdate process(const ImageView& frame, double started_seconds) const;

private:
    const OcrSource& ocr_;
    const Translator& translator_;
    TranslateContext context_;
};

/// Deterministic stand-in for OCR: two fixed lines with boxes. Consecutive calls
/// select different geometry, as a real stream would.
class FakeOcr final : public OcrSource {
public:
    [[nodiscard]] OcrResult recognize(const ImageView& frame) const override;

private:
    mutable int calls_ = 0;
};

}  // namespace watashi
