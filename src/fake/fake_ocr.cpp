#include "watashi/pipeline.hpp"

#include <vector>

namespace watashi {

std::vector<OcrLine> FakeOcr::recognize(int frame_index) const {
    // Deterministic: the same frame index always yields the same two lines, so
    // a failing test can be reproduced by number alone. The small horizontal
    // drift is what makes consecutive frames visibly different, the way a real
    // subtitle strip moves by a pixel or two.
    const int drift = frame_index % 4;
    return {
        OcrLine{"the sword intent of this sect is a myth", 0.97,
                Box{10 + drift, 20, 720, 48}},
        OcrLine{"gg wp noob", 0.93, Box{10 + drift, 90, 240, 56}},
    };
}

}  // namespace watashi
