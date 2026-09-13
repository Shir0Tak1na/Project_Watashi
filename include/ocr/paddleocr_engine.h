#pragma once

#include <string>
#include <vector>

#include "ocr/ocr_engine.h"

namespace projectwatashi {

class PaddleOcrEngine : public OcrEngine {
public:
    std::vector<OcrTextBox> recognize(const cv::Mat& image,
                                     const std::string& language) override;
};

} // namespace projectwatashi
