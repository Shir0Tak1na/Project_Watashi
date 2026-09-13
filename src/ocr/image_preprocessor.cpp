#include "ocr/image_preprocessor.h"

namespace projectwatashi {

cv::Mat ImagePreprocessor::preprocess(const cv::Mat& input) const {
    cv::Mat image = input.clone();

    if (image.empty()) {
        return image;
    }

    if (image.channels() == 4) {
        cv::cvtColor(image, image, cv::COLOR_BGRA2BGR);
    }

    if (image.channels() == 3) {
        cv::cvtColor(image, image, cv::COLOR_BGR2GRAY);
    }

    cv::GaussianBlur(image, image, cv::Size(3, 3), 0);
    cv::adaptiveThreshold(image, image, 255,
                          cv::ADAPTIVE_THRESH_GAUSSIAN_C,
                          cv::THRESH_BINARY, 31, 10);

    return image;
}

} // namespace projectwatashi
