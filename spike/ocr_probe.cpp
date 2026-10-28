// Minimal PP-OCRv4 det+rec probe using the official ONNX Runtime C++ package.
//
// It deliberately does not use OpenCV and does not link the Python wheel's
// DLL. Pre/post-processing is the same arithmetic as RapidOCR's
// DetPreProcess / DBPostProcess / resize_norm_img, expressed in plain C++.
//
// Usage:
//   ocr_probe.exe frame.bgr det.onnx rec.onnx chars.txt [--runs N]
//
// stdout: one JSON object per recognised line: {"text":"...","box":[x1,y1,x2,y2],"score":0.9}
// stderr: run_ms=<milliseconds> for every measured run.

#include <onnxruntime_cxx_api.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <limits>
#include <memory>
#include <numeric>
#include <queue>
#include <sstream>
#include <string>
#include <vector>

namespace {

struct Image {
    int width = 0;
    int height = 0;
    std::vector<uint8_t> bgr;
};

struct Box {
    float x1 = 0, y1 = 0, x2 = 0, y2 = 0;
};

struct Detected {
    Box box;
    std::string text;
    float score = 0;
};

float clampf(float value, float low, float high) {
    return std::max(low, std::min(high, value));
}

int clampi(int value, int low, int high) {
    return std::max(low, std::min(high, value));
}

Image load_bgr(const std::string& path, int width, int height) {
    std::ifstream file(path, std::ios::binary);
    if (!file) {
        throw std::runtime_error("cannot open frame: " + path);
    }
    Image image;
    image.width = width;
    image.height = height;
    image.bgr.resize(static_cast<size_t>(width) * height * 3);
    file.read(reinterpret_cast<char*>(image.bgr.data()), image.bgr.size());
    if (!file || static_cast<size_t>(file.gcount()) != image.bgr.size()) {
        throw std::runtime_error("frame is not 1200*200*3 bytes: " + path);
    }
    return image;
}

std::vector<std::string> load_chars(const std::string& path) {
    std::ifstream file(path);
    if (!file) {
        throw std::runtime_error("cannot open chars: " + path);
    }
    std::vector<std::string> chars;
    std::string line;
    while (std::getline(file, line)) {
        if (!line.empty() && line.back() == '\r') {
            line.pop_back();
        }
        if (!line.empty()) {
            chars.push_back(line);
        }
    }
    chars.insert(chars.begin(), "blank");
    chars.push_back(" ");
    return chars;
}

std::vector<uint8_t> resize_bgr(const Image& source, int width, int height) {
    std::vector<uint8_t> out(static_cast<size_t>(width) * height * 3);
    const float scale_x = static_cast<float>(source.width) / width;
    const float scale_y = static_cast<float>(source.height) / height;
    for (int y = 0; y < height; ++y) {
        float sy = (y + 0.5f) * scale_y - 0.5f;
        sy = clampf(sy, 0.0f, static_cast<float>(source.height - 1));
        const int y0 = static_cast<int>(std::floor(sy));
        const int y1 = std::min(y0 + 1, source.height - 1);
        const float fy = sy - y0;
        for (int x = 0; x < width; ++x) {
            float sx = (x + 0.5f) * scale_x - 0.5f;
            sx = clampf(sx, 0.0f, static_cast<float>(source.width - 1));
            const int x0 = static_cast<int>(std::floor(sx));
            const int x1 = std::min(x0 + 1, source.width - 1);
            const float fx = sx - x0;
            for (int c = 0; c < 3; ++c) {
                const float p00 = source.bgr[(static_cast<size_t>(y0) * source.width + x0) * 3 + c];
                const float p01 = source.bgr[(static_cast<size_t>(y0) * source.width + x1) * 3 + c];
                const float p10 = source.bgr[(static_cast<size_t>(y1) * source.width + x0) * 3 + c];
                const float p11 = source.bgr[(static_cast<size_t>(y1) * source.width + x1) * 3 + c];
                const float top = p00 + (p01 - p00) * fx;
                const float bottom = p10 + (p11 - p10) * fx;
                const float value = top + (bottom - top) * fy;
                out[(static_cast<size_t>(y) * width + x) * 3 + c] =
                    static_cast<uint8_t>(clampf(value, 0.0f, 255.0f));
            }
        }
    }
    return out;
}

Image crop_bgr(const Image& source, int x1, int y1, int x2, int y2) {
    Image out;
    out.width = std::max(1, x2 - x1);
    out.height = std::max(1, y2 - y1);
    out.bgr.resize(static_cast<size_t>(out.width) * out.height * 3);
    for (int y = 0; y < out.height; ++y) {
        const int sy = clampi(y1 + y, 0, source.height - 1);
        for (int x = 0; x < out.width; ++x) {
            const int sx = clampi(x1 + x, 0, source.width - 1);
            for (int c = 0; c < 3; ++c) {
                out.bgr[(static_cast<size_t>(y) * out.width + x) * 3 + c] =
                    source.bgr[(static_cast<size_t>(sy) * source.width + sx) * 3 + c];
            }
        }
    }
    return out;
}

struct Model {
    std::unique_ptr<Ort::Session> session;
    std::string input_name;
    std::string output_name;

    Model(Ort::Env& env, const std::string& path) {
        Ort::SessionOptions options;
        options.SetGraphOptimizationLevel(GraphOptimizationLevel::ORT_ENABLE_ALL);
        options.SetIntraOpNumThreads(4);
        options.SetInterOpNumThreads(1);
        options.EnableCpuMemArena();
        session = std::make_unique<Ort::Session>(
            env, std::wstring(path.begin(), path.end()).c_str(), options);
        Ort::AllocatorWithDefaultOptions allocator;
        input_name = session->GetInputNameAllocated(0, allocator).get();
        output_name = session->GetOutputNameAllocated(0, allocator).get();
    }

    Ort::Value run(const std::vector<float>& data, const std::vector<int64_t>& shape) {
        Ort::MemoryInfo memory = Ort::MemoryInfo::CreateCpu(
            OrtArenaAllocator, OrtMemTypeDefault);
        Ort::Value input = Ort::Value::CreateTensor<float>(
            memory, const_cast<float*>(data.data()), data.size(),
            shape.data(), shape.size());
        const char* input_names[] = {input_name.c_str()};
        const char* output_names[] = {output_name.c_str()};
        auto outputs = session->Run(
            Ort::RunOptions{nullptr}, input_names, &input, 1, output_names, 1);
        return std::move(outputs[0]);
    }
};

std::vector<float> det_input(const Image& image, int& det_width, int& det_height) {
    const int longest = std::max(image.width, image.height);
    const int limit = longest < 960 ? 960 : (longest < 1500 ? 1500 : 2000);
    const float ratio = (longest > limit) ? static_cast<float>(limit) / longest : 1.0f;
    det_height = static_cast<int>(std::lround(image.height * ratio / 32.0f) * 32);
    det_width = static_cast<int>(std::lround(image.width * ratio / 32.0f) * 32);
    det_height = std::max(det_height, 32);
    det_width = std::max(det_width, 32);

    const std::vector<uint8_t> resized = resize_bgr(image, det_width, det_height);
    std::vector<float> input(static_cast<size_t>(3) * det_height * det_width);
    for (int c = 0; c < 3; ++c) {
        for (int y = 0; y < det_height; ++y) {
            for (int x = 0; x < det_width; ++x) {
                const float pixel = resized[(static_cast<size_t>(y) * det_width + x) * 3 + c];
                input[(static_cast<size_t>(c) * det_height + y) * det_width + x] =
                    pixel / 127.5f - 1.0f;
            }
        }
    }
    return input;
}

std::vector<Detected> det_boxes(const Image& image, Model& det) {
    int det_width = 0;
    int det_height = 0;
    std::vector<float> input = det_input(image, det_width, det_height);
    std::vector<int64_t> shape = {1, 3, det_height, det_width};
    Ort::Value output = det.run(input, shape);
    auto info = output.GetTensorTypeAndShapeInfo();
    const std::vector<int64_t> out_shape = info.GetShape();
    if (out_shape.size() != 4) {
        throw std::runtime_error("det output is not [1,1,H,W]");
    }
    const int height = static_cast<int>(out_shape[2]);
    const int width = static_cast<int>(out_shape[3]);
    const float* probabilities = output.GetTensorData<float>();

    std::vector<uint8_t> mask(static_cast<size_t>(height) * width, 0);
    for (int y = 0; y < height; ++y) {
        for (int x = 0; x < width; ++x) {
            mask[static_cast<size_t>(y) * width + x] =
                probabilities[static_cast<size_t>(y) * width + x] > 0.3f ? 1 : 0;
        }
    }
    // cv2.dilate with a 2x2 kernel and OpenCV's default anchor (1, 1): a pixel is
    // set when any source pixel in the 2x2 window ending at it is set, so the
    // region grows towards larger x/y, exactly like the Python reference.
    std::vector<uint8_t> dilated(mask.size(), 0);
    for (int y = 0; y < height; ++y) {
        for (int x = 0; x < width; ++x) {
            for (int dy = 0; dy <= 1; ++dy) {
                for (int dx = 0; dx <= 1; ++dx) {
                    const int yy = y - dy;
                    const int xx = x - dx;
                    if (yy >= 0 && xx >= 0 &&
                        mask[static_cast<size_t>(yy) * width + xx]) {
                        dilated[static_cast<size_t>(y) * width + x] = 1;
                    }
                }
            }
        }
    }

    std::vector<int> labels(mask.size(), -1);
    std::vector<Detected> result;
    const int dx[8] = {-1, 0, 1, -1, 1, -1, 0, 1};
    const int dy[8] = {-1, -1, -1, 0, 0, 1, 1, 1};
    for (int y = 0; y < height; ++y) {
        for (int x = 0; x < width; ++x) {
            const size_t start = static_cast<size_t>(y) * width + x;
            if (!dilated[start] || labels[start] >= 0) {
                continue;
            }
            std::queue<std::pair<int, int>> queue;
            queue.push({x, y});
            labels[start] = 1;
            int min_x = x, max_x = x, min_y = y, max_y = y;
            double sum_x = 0, sum_y = 0, sum_xx = 0, sum_yy = 0, sum_xy = 0;
            double count = 0;
            while (!queue.empty()) {
                const auto [cx, cy] = queue.front();
                queue.pop();
                min_x = std::min(min_x, cx);
                max_x = std::max(max_x, cx);
                min_y = std::min(min_y, cy);
                max_y = std::max(max_y, cy);
                count += 1;
                sum_x += cx;
                sum_y += cy;
                sum_xx += static_cast<double>(cx) * cx;
                sum_yy += static_cast<double>(cy) * cy;
                sum_xy += static_cast<double>(cx) * cy;
                for (int k = 0; k < 8; ++k) {
                    const int nx = cx + dx[k];
                    const int ny = cy + dy[k];
                    if (nx < 0 || ny < 0 || nx >= width || ny >= height) {
                        continue;
                    }
                    const size_t index = static_cast<size_t>(ny) * width + nx;
                    if (dilated[index] && labels[index] < 0) {
                        labels[index] = 1;
                        queue.push({nx, ny});
                    }
                }
            }

            const int box_width = max_x - min_x + 1;
            const int box_height = max_y - min_y + 1;
            if (std::min(box_width, box_height) < 3) {
                continue;
            }
            double sum = 0.0;
            for (int yy = min_y; yy <= max_y; ++yy) {
                for (int xx = min_x; xx <= max_x; ++xx) {
                    sum += probabilities[static_cast<size_t>(yy) * width + xx];
                }
            }
            const float score = static_cast<float>(sum / (box_width * box_height));
            if (score < 0.5f) {
                continue;
            }
            // Unclip distance. The Python reference measures the minimum-area
            // rotated rectangle (cv2.minAreaRect) of the contour; its shape is
            // recovered here from the component's second central moments, which
            // for a filled rotated rectangle of sides W,H gives
            //   mu20 = (W^2 cos^2 t + H^2 sin^2 t) / 12, etc.
            const double mean_x = sum_x / count;
            const double mean_y = sum_y / count;
            const double mu20 = sum_xx / count - mean_x * mean_x;
            const double mu02 = sum_yy / count - mean_y * mean_y;
            const double mu11 = sum_xy / count - mean_x * mean_y;
            const double theta = 0.5 * std::atan2(2.0 * mu11, mu20 - mu02);
            const double ct = std::cos(theta);
            const double st = std::sin(theta);
            const double side_w =
                std::sqrt(std::max(1.0, (mu20 * ct * ct + mu02 * st * st) * 12.0));
            const double side_h =
                std::sqrt(std::max(1.0, (mu02 * ct * ct + mu20 * st * st) * 12.0));
            const double long_side = std::max(side_w, side_h);
            const double short_side = std::min(side_w, side_h);
            const float distance = static_cast<float>(long_side * short_side * 1.6 /
                                                      (2.0 * (long_side + short_side)));
            const float x1 = (min_x - distance) / width * image.width;
            const float y1 = (min_y - distance) / height * image.height;
            const float x2 = (max_x + 1 + distance) / width * image.width;
            const float y2 = (max_y + 1 + distance) / height * image.height;
            const int ix1 = clampi(static_cast<int>(std::lround(x1)), 0, image.width - 1);
            const int iy1 = clampi(static_cast<int>(std::lround(y1)), 0, image.height - 1);
            const int ix2 = clampi(static_cast<int>(std::lround(x2)), 0, image.width - 1);
            const int iy2 = clampi(static_cast<int>(std::lround(y2)), 0, image.height - 1);
            if (ix2 - ix1 <= 3 || iy2 - iy1 <= 3) {
                continue;
            }
            result.push_back({Box{static_cast<float>(ix1), static_cast<float>(iy1),
                                  static_cast<float>(ix2), static_cast<float>(iy2)},
                              "", score});
        }
    }
    std::sort(result.begin(), result.end(), [](const Detected& a, const Detected& b) {
        const float ac = (a.box.y1 + a.box.y2) / 2;
        const float bc = (b.box.y1 + b.box.y2) / 2;
        return ac != bc ? ac < bc : a.box.x1 < b.box.x1;
    });
    return result;
}

std::pair<std::string, float> recognize_crop(
    const Image& image, const Box& box, Model& rec, const std::vector<std::string>& chars,
    int batch_width) {
    const int x1 = clampi(static_cast<int>(box.x1), 0, image.width - 1);
    const int y1 = clampi(static_cast<int>(box.y1), 0, image.height - 1);
    const int x2 = clampi(static_cast<int>(box.x2), x1 + 1, image.width - 1);
    const int y2 = clampi(static_cast<int>(box.y2), y1 + 1, image.height - 1);
    const Image crop = crop_bgr(image, x1, y1, x2, y2);
    const int resized_w = std::min(
        batch_width, static_cast<int>(std::ceil(48.0 * crop.width / crop.height)));
    const int width = batch_width;
    const std::vector<uint8_t> resized = resize_bgr(crop, resized_w, 48);

    std::vector<float> input(static_cast<size_t>(3) * 48 * width, 0.0f);
    for (int y = 0; y < 48; ++y) {
        for (int x = 0; x < resized_w; ++x) {
            for (int c = 0; c < 3; ++c) {
                const float pixel = resized[(static_cast<size_t>(y) * resized_w + x) * 3 + c];
                input[(static_cast<size_t>(c) * 48 + y) * width + x] =
                    pixel / 127.5f - 1.0f;
            }
        }
    }
    std::vector<int64_t> shape = {1, 3, 48, width};
    Ort::Value output = rec.run(input, shape);
    auto info = output.GetTensorTypeAndShapeInfo();
    const std::vector<int64_t> out_shape = info.GetShape();
    if (out_shape.size() != 3) {
        throw std::runtime_error("rec output is not [1,T,C]");
    }
    const int steps = static_cast<int>(out_shape[1]);
    const int classes = static_cast<int>(out_shape[2]);
    const float* logits = output.GetTensorData<float>();
    if (classes != static_cast<int>(chars.size())) {
        std::cerr << "warning: rec classes " << classes << " != chars " << chars.size()
                  << "\n";
    }

    std::string text;
    std::vector<float> confidences;
    int previous = -1;
    for (int t = 0; t < steps; ++t) {
        int best = 0;
        float best_prob = -std::numeric_limits<float>::infinity();
        for (int c = 0; c < classes; ++c) {
            const float value = logits[(static_cast<size_t>(t) * classes) + c];
            if (value > best_prob) {
                best_prob = value;
                best = c;
            }
        }
        if (best != 0 && best != previous && best < static_cast<int>(chars.size())) {
            text += chars[best];
            confidences.push_back(best_prob);
        }
        previous = best;
    }
    const float score = confidences.empty()
                            ? 0.0f
                            : std::accumulate(confidences.begin(), confidences.end(), 0.0f) /
                                  confidences.size();
    return {text, score};
}

bool ascii_alnum(char c) {
    return (c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z');
}

std::vector<Detected> merge_lines(std::vector<Detected> detected) {
    if (detected.empty()) {
        return detected;
    }
    std::vector<float> heights;
    for (const auto& item : detected) {
        heights.push_back(item.box.y2 - item.box.y1);
    }
    std::sort(heights.begin(), heights.end());
    const float typical = heights[heights.size() / 2];
    const float tolerance = std::max(4.0f, typical * 0.6f);

    std::sort(detected.begin(), detected.end(), [](const Detected& a, const Detected& b) {
        const float ac = (a.box.y1 + a.box.y2) / 2;
        const float bc = (b.box.y1 + b.box.y2) / 2;
        return ac != bc ? ac < bc : a.box.x1 < b.box.x1;
    });

    std::vector<std::vector<Detected>> groups;
    for (const auto& item : detected) {
        const float center = (item.box.y1 + item.box.y2) / 2;
        bool placed = false;
        for (auto& group : groups) {
            float ref = 0;
            for (const auto& member : group) {
                ref += (member.box.y1 + member.box.y2) / 2;
            }
            ref /= group.size();
            if (std::fabs(center - ref) <= tolerance) {
                group.push_back(item);
                placed = true;
                break;
            }
        }
        if (!placed) {
            groups.push_back({item});
        }
    }

    std::vector<Detected> lines;
    for (auto& group : groups) {
        std::sort(group.begin(), group.end(), [](const Detected& a, const Detected& b) {
            return a.box.x1 < b.box.x1;
        });
        std::string text;
        float score = 0;
        Box box{std::numeric_limits<float>::max(), std::numeric_limits<float>::max(),
                0, 0};
        for (size_t i = 0; i < group.size(); ++i) {
            if (i && !text.empty() && !group[i].text.empty()) {
                if (ascii_alnum(text.back()) && ascii_alnum(group[i].text.front())) {
                    text += " ";
                }
            }
            text += group[i].text;
            score += group[i].score;
            box.x1 = std::min(box.x1, group[i].box.x1);
            box.y1 = std::min(box.y1, group[i].box.y1);
            box.x2 = std::max(box.x2, group[i].box.x2);
            box.y2 = std::max(box.y2, group[i].box.y2);
        }
        if (text.empty()) {
            continue;
        }
        score /= group.size();
        lines.push_back({box, text, score});
    }
    std::sort(lines.begin(), lines.end(), [](const Detected& a, const Detected& b) {
        return a.box.y1 != b.box.y1 ? a.box.y1 < b.box.y1 : a.box.x1 < b.box.x1;
    });
    return lines;
}

std::vector<Detected> recognize(
    const Image& image, Model& det, Model& rec, const std::vector<std::string>& chars) {
    std::vector<Detected> boxes = det_boxes(image, det);
    float max_ratio = 320.0f / 48.0f;
    for (const auto& item : boxes) {
        const float ratio = (item.box.x2 - item.box.x1) / (item.box.y2 - item.box.y1);
        max_ratio = std::max(max_ratio, ratio);
    }
    const int batch_width = std::max(320, static_cast<int>(48.0f * max_ratio));
    for (auto& item : boxes) {
        auto [text, score] = recognize_crop(image, item.box, rec, chars, batch_width);
        item.text = text;
        item.score = score;
    }
    return merge_lines(boxes);
}

std::string json_escape(const std::string& text) {
    std::ostringstream out;
    for (unsigned char c : text) {
        switch (c) {
            case '"': out << "\\\""; break;
            case '\\': out << "\\\\"; break;
            case '\n': out << "\\n"; break;
            case '\r': out << "\\r"; break;
            case '\t': out << "\\t"; break;
            default:
                if (c < 0x20) {
                    out << "\\u" << std::hex << static_cast<int>(c);
                } else {
                    out << static_cast<char>(c);
                }
        }
    }
    return out.str();
}

}  // namespace

int main(int argc, char* argv[]) {
    if (argc < 5) {
        std::cerr << "usage: ocr_probe frame.bgr det.onnx rec.onnx chars.txt [--runs N]\n";
        return 2;
    }
    try {
        int runs = 1;
        for (int i = 5; i < argc; ++i) {
            if (std::string(argv[i]) == "--runs" && i + 1 < argc) {
                runs = std::max(1, std::atoi(argv[i + 1]));
                ++i;
            }
        }
        const Image image = load_bgr(argv[1], 1200, 200);
        const std::vector<std::string> chars = load_chars(argv[4]);
        Ort::Env env(ORT_LOGGING_LEVEL_WARNING, "ocr_probe");
        Model det(env, argv[2]);
        Model rec(env, argv[3]);

        // Warm-up: exactly one full pass, not included in the measured runs.
        recognize(image, det, rec, chars);

        std::vector<Detected> lines;
        for (int run = 0; run < runs; ++run) {
            const auto start = std::chrono::steady_clock::now();
            lines = recognize(image, det, rec, chars);
            const auto end = std::chrono::steady_clock::now();
            const double ms = std::chrono::duration<double, std::milli>(end - start).count();
            std::cerr << "run_ms=" << ms << "\n";
        }
        for (const auto& line : lines) {
            std::cout << "{\"text\":\"" << json_escape(line.text) << "\",\"box\":["
                      << static_cast<int>(line.box.x1) << ","
                      << static_cast<int>(line.box.y1) << ","
                      << static_cast<int>(line.box.x2) << ","
                      << static_cast<int>(line.box.y2) << "],\"score\":"
                      << line.score << "}\n";
        }
        return 0;
    } catch (const std::exception& exc) {
        std::cerr << "ocr_probe failed: " << exc.what() << "\n";
        return 1;
    }
}
