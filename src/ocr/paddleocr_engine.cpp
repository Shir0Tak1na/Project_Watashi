#include "ocr/paddleocr_engine.h"

namespace projectwatashi {

std::vector<OcrTextBox> PaddleOcrEngine::recognize(const cv::Mat& image,
                                                 const std::string& language) {
    std::vector<OcrTextBox> results;

    // This is a placeholder implementation for the real PaddleOCR C++ adapter.
    // In a real project this would call the native PaddleOCR C++ SDK or OCR model.
    if (image.empty()) {
        return results;
    }

    OcrTextBox box;
    box.text = "local-ocr-result";
    box.confidence = 0.94f;
    box.rect = cv::Rect(0, 0, image.cols, image.rows);
    results.push_back(box);

    (void)language;
    return results;
}

} // namespace projectwatashi
