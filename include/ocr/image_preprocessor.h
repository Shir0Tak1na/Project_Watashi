#pragma once

#include <opencv2/opencv.hpp>

namespace projectwatashi {

class ImagePreprocessor {
public:
    cv::Mat preprocess(const cv::Mat& input) const;
};

} // namespace projectwatashi
