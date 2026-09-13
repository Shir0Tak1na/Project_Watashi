#pragma once

#include <atomic>
#include <functional>
#include <memory>
#include <thread>
#include <vector>

#include "ocr/frame_buffer.h"
#include "ocr/image_preprocessor.h"
#include "ocr/ocr_engine.h"
#include "ocr/ocr_task_scheduler.h"
#include "ocr/region_selector.h"
#include "ocr/screen_capturer.h"

namespace projectwatashi {

class ScreenRecognitionPipeline {
public:
    using OcrResultCallback = std::function<void(const std::vector<OcrTextBox>&)>;

    ScreenRecognitionPipeline(std::unique_ptr<ScreenCapturer> capturer,
                              std::unique_ptr<OcrEngine> ocr_engine);
    ~ScreenRecognitionPipeline();

    void start();
    void stop();
    void setCallback(OcrResultCallback callback);
    void setLanguage(const std::string& language);

private:
    void captureLoop();
    void onOcrFinished(const std::vector<OcrTextBox>& results);

    std::unique_ptr<ScreenCapturer> capturer_;
    std::unique_ptr<OcrEngine> ocr_engine_;
    RegionSelector selector_;
    ImagePreprocessor preprocessor_;
    FrameBuffer frame_buffer_;
    OcrTaskScheduler scheduler_;
    OcrResultCallback callback_;
    std::atomic_bool running_{false};
    std::string language_ = "ch_en";
    std::thread capture_thread_;
};

} // namespace projectwatashi
