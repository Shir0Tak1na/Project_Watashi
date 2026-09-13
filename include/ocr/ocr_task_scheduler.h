#pragma once

#include <functional>
#include <memory>
#include <vector>

#include <opencv2/opencv.hpp>

#include "ocr/ocr_engine.h"

namespace projectwatashi {

class OcrTaskScheduler {
public:
    using Callback = std::function<void(const std::vector<OcrTextBox>&)>;

    explicit OcrTaskScheduler(size_t worker_count = 2);
    ~OcrTaskScheduler();

    void enqueue(const cv::Mat& image,
                 const std::string& language,
                 OcrEngine* engine,
                 Callback callback);

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace projectwatashi
