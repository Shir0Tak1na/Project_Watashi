#include "ocr/frame_buffer.h"

namespace projectwatashi {

FrameBuffer::FrameBuffer() {
    front_ = std::make_shared<FrameData>();
    back_ = std::make_shared<FrameData>();
}

FrameBuffer::~FrameBuffer() = default;

std::shared_ptr<FrameData> FrameBuffer::acquireWriteFrame() {
    std::lock_guard<std::mutex> lock(mutex_);
    return back_;
}

std::shared_ptr<FrameData> FrameBuffer::acquireReadFrame() {
    std::lock_guard<std::mutex> lock(mutex_);
    return front_;
}

void FrameBuffer::swap() {
    std::lock_guard<std::mutex> lock(mutex_);
    std::swap(front_, back_);
}

} // namespace projectwatashi
