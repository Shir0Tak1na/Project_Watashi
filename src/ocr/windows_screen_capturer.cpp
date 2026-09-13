#include "ocr/windows_screen_capturer.h"

namespace projectwatashi {

cv::Mat WindowsScreenCapturer::captureRegion(const ScreenRect& rect) {
    cv::Mat image(rect.height, rect.width, CV_8UC4, cv::Scalar::all(0));
    return image;
}

cv::Mat WindowsScreenCapturer::capturePrimaryMonitor() {
    cv::Mat image(720, 1280, CV_8UC4, cv::Scalar::all(0));
    return image;
}

} // namespace projectwatashi
