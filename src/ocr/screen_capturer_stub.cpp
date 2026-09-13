#include "ocr/screen_capturer.h"

namespace projectwatashi {

class DefaultScreenCapturer : public ScreenCapturer {
public:
    cv::Mat captureRegion(const ScreenRect& rect) override {
        cv::Mat image(rect.height > 0 ? rect.height : 720,
                      rect.width > 0 ? rect.width : 1280,
                      CV_8UC4,
                      cv::Scalar(0, 0, 0, 255));
        return image;
    }

    cv::Mat capturePrimaryMonitor() override {
        return cv::Mat(720, 1280, CV_8UC4, cv::Scalar(0, 0, 0, 255));
    }
};

} // namespace projectwatashi
