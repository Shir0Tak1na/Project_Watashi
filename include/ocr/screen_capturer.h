#pragma once

#include <cstdint>
#include <string>

#include <opencv2/opencv.hpp>

namespace projectwatashi {

struct ScreenRect {
    int x = 0;
    int y = 0;
    int width = 0;
    int height = 0;
};

class ScreenCapturer {
public:
    virtual ~ScreenCapturer() = default;

    virtual cv::Mat captureRegion(const ScreenRect& rect) = 0;
    virtual cv::Mat capturePrimaryMonitor() = 0;
};

} // namespace projectwatashi
