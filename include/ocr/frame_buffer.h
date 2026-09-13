#pragma once

#include <atomic>
#include <chrono>
#include <memory>
#include <mutex>

#include <opencv2/opencv.hpp>

namespace projectwatashi {

struct FrameData {
    cv::Mat image;
    std::chrono::steady_clock::time_point timestamp;
};

class FrameBuffer {
public:
    FrameBuffer();
    ~FrameBuffer();

    std::shared_ptr<FrameData> acquireWriteFrame();
    std::shared_ptr<FrameData> acquireReadFrame();
    void swap();

private:
    std::mutex mutex_;
    std::shared_ptr<FrameData> front_;
    std::shared_ptr<FrameData> back_;
};

} // namespace projectwatashi
