#include "ocr/ocr_task_scheduler.h"

#include <condition_variable>
#include <mutex>
#include <queue>
#include <thread>
#include <vector>

namespace projectwatashi {

struct OcrTask {
    cv::Mat image;
    std::string language;
    OcrEngine* engine = nullptr;
    OcrTaskScheduler::Callback callback;
};

struct OcrTaskScheduler::Impl {
    std::mutex mutex;
    std::condition_variable cv;
    std::queue<OcrTask> tasks;
    std::vector<std::thread> workers;
    bool stop = false;

    Impl(size_t worker_count) {
        for (size_t i = 0; i < worker_count; ++i) {
            workers.emplace_back([this]() {
                for (;;) {
                    OcrTask task;
                    {
                        std::unique_lock<std::mutex> lock(mutex);
                        cv.wait(lock, [this] { return stop || !tasks.empty(); });
                        if (stop && tasks.empty()) {
                            return;
                        }
                        task = std::move(tasks.front());
                        tasks.pop();
                    }

                    if (!task.engine) {
                        continue;
                    }

                    auto result = task.engine->recognize(task.image, task.language);
                    if (task.callback) {
                        task.callback(result);
                    }
                }
            });
        }
    }

    ~Impl() {
        {
            std::lock_guard<std::mutex> lock(mutex);
            stop = true;
        }
        cv.notify_all();
        for (auto& worker : workers) {
            if (worker.joinable()) {
                worker.join();
            }
        }
    }
};

OcrTaskScheduler::OcrTaskScheduler(size_t worker_count)
    : impl_(std::make_unique<Impl>(worker_count)) {
}

OcrTaskScheduler::~OcrTaskScheduler() = default;

void OcrTaskScheduler::enqueue(const cv::Mat& image,
                              const std::string& language,
                              OcrEngine* engine,
                              Callback callback) {
    OcrTask task;
    task.image = image.clone();
    task.language = language;
    task.engine = engine;
    task.callback = std::move(callback);

    {
        std::lock_guard<std::mutex> lock(impl_->mutex);
        impl_->tasks.push(std::move(task));
    }
    impl_->cv.notify_one();
}

} // namespace projectwatashi
