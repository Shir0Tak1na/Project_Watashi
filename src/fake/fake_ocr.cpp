#include "watashi/pipeline.hpp"

#include "watashi/ocr.hpp"

namespace watashi {

OcrResult FakeOcr::recognize(const ImageView& frame) const {
    // The frame is ignored on purpose: this source produces the same text for
    // any pixels, which is what makes the default CLI mode deterministic and
    // lets CI run without models. The call counter keeps consecutive frames
    // visibly different the way a real subtitle strip drifts.
    (void)frame;
    const int drift = (calls_++) % 4;

    OcrResult result;
    result.lines = {
        OcrLine{"the sword intent of this sect is a myth", 0.97,
                Box{10 + drift, 20, 720, 48}},
        OcrLine{"gg wp noob", 0.93, Box{10 + drift, 90, 240, 56}},
    };
    return result;
}

}  // namespace watashi
