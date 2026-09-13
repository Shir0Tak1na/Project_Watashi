#pragma once

#include "ocr/screen_capturer.h"

namespace projectwatashi {

class WindowsScreenCapturer : public ScreenCapturer {
public:
    cv::Mat captureRegion(const ScreenRect& rect) override;
    cv::Mat capturePrimaryMonitor() override;
};

} // namespace projectwatashi
