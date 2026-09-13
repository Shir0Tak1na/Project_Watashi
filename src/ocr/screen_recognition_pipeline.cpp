#include "ocr/screen_recognition_pipeline.h"

#include <chrono>
#include <thread>

namespace projectwatashi {

ScreenRecognitionPipeline::ScreenRecognitionPipeline(std::unique_ptr<ScreenCapturer> capturer,
                                                   std::unique_ptr<OcrEngine> ocr_engine)
    : capturer_(std::move(capturer)),
      ocr_engine_(std::move(ocr_engine)),
      scheduler_(2) {
}

ScreenRecognitionPipeline::~ScreenRecognitionPipeline() {
    stop();
}

void ScreenRecognitionPipeline::start() {
    running_ = true;
    capture_thread_ = std::thread(&ScreenRecognitionPipeline::captureLoop, this);
}

void ScreenRecognitionPipeline::stop() {
    running_ = false;
    if (capture_thread_.joinable()) {
        capture_thread_.join();
    }
}

void ScreenRecognitionPipeline::setCallback(OcrResultCallback callback) {
    callback_ = std::move(callback);
}

void ScreenRecognitionPipeline::setLanguage(const std::string& language) {
    language_ = language;
}

void ScreenRecognitionPipeline::captureLoop() {
    while (running_) {
        if (!selector_.isSelectionReady()) {
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
            continue;
        }

        auto rect = selector_.getSelectedRect();
        auto write_frame = frame_buffer_.acquireWriteFrame();
        write_frame->image = capturer_->captureRegion(rect);
        write_frame->timestamp = std::chrono::steady_clock::now();

        auto processed = preprocessor_.preprocess(write_frame->image);
        frame_buffer_.swap();

        scheduler_.enqueue(processed, language_, ocr_engine_.get(),
            [this](const std::vector<OcrTextBox>& result) {
                onOcrFinished(result);
            });

        selector_.stopSelection();
        std::this_thread::sleep_for(std::chrono::milliseconds(16));
    }
}

void ScreenRecognitionPipeline::onOcrFinished(const std::vector<OcrTextBox>& results) {
    if (callback_) {
        callback_(results);
    }
}

} // namespace projectwatashi
