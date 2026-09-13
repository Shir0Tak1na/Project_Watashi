#pragma once

#include <string>
#include <vector>

#include <opencv2/opencv.hpp>

namespace projectwatashi {

struct OcrTextBox {
    std::string text;
    float confidence = 0.0f;
    cv::Rect rect;
};

class OcrEngine {
public:
    virtual ~OcrEngine() = default;

    virtual std::vector<OcrTextBox> recognize(const cv::Mat& image,
                                             const std::string& language) = 0;
};

} // namespace projectwatashi
