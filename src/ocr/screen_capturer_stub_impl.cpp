#include "ocr/screen_capturer_stub.h"

namespace projectwatashi {

cv::Mat DefaultScreenCapturer::captureRegion(const ScreenRect& rect) {
    cv::Mat image(rect.height > 0 ? rect.height : 720,
                  rect.width > 0 ? rect.width : 1280,
                  CV_8UC4,
                  cv::Scalar(0, 0, 0, 255));
    return image;
}

cv::Mat DefaultScreenCapturer::capturePrimaryMonitor() {
    return cv::Mat(720, 1280, CV_8UC4, cv::Scalar(0, 0, 0, 255));
}

} // namespace projectwatashi
