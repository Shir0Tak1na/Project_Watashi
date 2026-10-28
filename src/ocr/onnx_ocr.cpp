// PP-OCRv4 detection + optional angle classification + recognition, through the
// official ONNX Runtime C++ API.
//
// The arithmetic is the same as the Python reference (RapidOCR's
// DetPreProcess / DBPostProcess / resize_norm_img / CTCLabelDecode), expressed
// here over raw BGR buffers. Nothing in this file decodes an image file: the
// caller hands over pixels it already owns.
//
// The throwaway probe under spike/ measured the same steps; this is a clean
// implementation organised as a class with the parts that need no model
// exposed for testing (see ocr_detail in ocr.hpp).

#include "watashi/ocr.hpp"

#include <onnxruntime_cxx_api.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <fstream>
#include <limits>
#include <memory>
#include <numeric>
#include <queue>
#include <string>
#include <utility>
#include <vector>

#if defined(_WIN32)
#define NOMINMAX
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#endif

namespace watashi {
namespace {

constexpr float kDetThreshold = 0.3F;
constexpr float kBoxThreshold = 0.5F;
constexpr float kUnclipRatio = 1.6F;
constexpr int kRecHeight = 48;
constexpr int kMinRecWidth = 320;
constexpr int kClsWidth = 192;

float clampf(float value, float low, float high) {
    return std::max(low, std::min(high, value));
}

int clampi(int value, int low, int high) {
    return std::max(low, std::min(high, value));
}

/// Bilinear resize over BGR8, matching cv2.resize(..., INTER_LINEAR) closely
/// enough that the detector mask lands within a pixel of the reference.
std::vector<std::uint8_t> resize_bgr(
    const std::uint8_t* bgr,
    int source_width,
    int source_height,
    int width,
    int height) {
    std::vector<std::uint8_t> out(static_cast<std::size_t>(width) * height * 3);
    const float scale_x = static_cast<float>(source_width) / static_cast<float>(width);
    const float scale_y = static_cast<float>(source_height) / static_cast<float>(height);
    for (int y = 0; y < height; ++y) {
        float sy = (static_cast<float>(y) + 0.5F) * scale_y - 0.5F;
        sy = clampf(sy, 0.0F, static_cast<float>(source_height - 1));
        const int y0 = static_cast<int>(std::floor(sy));
        const int y1 = std::min(y0 + 1, source_height - 1);
        const float fy = sy - static_cast<float>(y0);
        for (int x = 0; x < width; ++x) {
            float sx = (static_cast<float>(x) + 0.5F) * scale_x - 0.5F;
            sx = clampf(sx, 0.0F, static_cast<float>(source_width - 1));
            const int x0 = static_cast<int>(std::floor(sx));
            const int x1 = std::min(x0 + 1, source_width - 1);
            const float fx = sx - static_cast<float>(x0);
            for (int channel = 0; channel < 3; ++channel) {
                const float p00 = bgr[(static_cast<std::size_t>(y0) * source_width + x0) * 3 + channel];
                const float p01 = bgr[(static_cast<std::size_t>(y0) * source_width + x1) * 3 + channel];
                const float p10 = bgr[(static_cast<std::size_t>(y1) * source_width + x0) * 3 + channel];
                const float p11 = bgr[(static_cast<std::size_t>(y1) * source_width + x1) * 3 + channel];
                const float top = p00 + (p01 - p00) * fx;
                const float bottom = p10 + (p11 - p10) * fx;
                const float value = top + (bottom - top) * fy;
                out[(static_cast<std::size_t>(y) * width + x) * 3 + channel] =
                    static_cast<std::uint8_t>(clampf(value, 0.0F, 255.0F));
            }
        }
    }
    return out;
}

/// Copy a rectangle out of a frame, clamped to the frame so a detector box that
/// touches (or slightly exceeds) the edge cannot read out of bounds.
std::vector<std::uint8_t> crop_bgr(
    const ImageView& frame,
    int x1,
    int y1,
    int x2,
    int y2,
    int& width,
    int& height) {
    width = std::max(1, x2 - x1);
    height = std::max(1, y2 - y1);
    std::vector<std::uint8_t> out(static_cast<std::size_t>(width) * height * 3);
    for (int y = 0; y < height; ++y) {
        const int sy = clampi(y1 + y, 0, frame.height - 1);
        for (int x = 0; x < width; ++x) {
            const int sx = clampi(x1 + x, 0, frame.width - 1);
            for (int channel = 0; channel < 3; ++channel) {
                out[(static_cast<std::size_t>(y) * width + x) * 3 + channel] =
                    frame.bgr[(static_cast<std::size_t>(sy) * frame.width + sx) * 3 + channel];
            }
        }
    }
    return out;
}

std::vector<std::string> load_charset(const std::string& path) {
    std::ifstream file(path, std::ios::binary);
    if (!file) {
        throw OcrError("cannot open character table: " + path);
    }
    std::vector<std::string> charset;
    std::string line;
    while (std::getline(file, line)) {
        if (!line.empty() && line.back() == '\r') {
            line.pop_back();
        }
        if (!line.empty()) {
            charset.push_back(line);
        }
    }
    if (charset.empty()) {
        throw OcrError("character table is empty: " + path);
    }
    // Same layout as CTCLabelDecode: blank first, then the table, then space.
    charset.insert(charset.begin(), "blank");
    charset.emplace_back(" ");
    return charset;
}

/// Rotate 180 degrees, keeping BGR order inside each pixel.
std::vector<std::uint8_t> rotate_180(const std::vector<std::uint8_t>& bgr, int width, int height) {
    std::vector<std::uint8_t> turned(bgr.size());
    for (int y = 0; y < height; ++y) {
        for (int x = 0; x < width; ++x) {
            const std::size_t source =
                (static_cast<std::size_t>(height - 1 - y) * width + (width - 1 - x)) * 3;
            const std::size_t target = (static_cast<std::size_t>(y) * width + x) * 3;
            turned[target] = bgr[source];
            turned[target + 1] = bgr[source + 1];
            turned[target + 2] = bgr[source + 2];
        }
    }
    return turned;
}

bool file_readable(const std::string& path) {
    std::ifstream file(path, std::ios::binary);
    return static_cast<bool>(file);
}

bool ascii_alnum(char value) {
    return (value >= '0' && value <= '9') || (value >= 'A' && value <= 'Z') ||
           (value >= 'a' && value <= 'z');
}

#if defined(_WIN32)
std::wstring utf8_wide(const std::string& text) {
    if (text.empty()) {
        return {};
    }
    const int size =
        MultiByteToWideChar(CP_UTF8, 0, text.c_str(), -1, nullptr, 0);
    if (size <= 0) {
        throw OcrError("path is not valid UTF-8: " + text);
    }
    std::wstring wide(static_cast<std::size_t>(size), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, text.c_str(), -1, wide.data(), size);
    wide.pop_back();
    return wide;
}
#endif

const ORTCHAR_T* ort_path(const std::string& path
#if defined(_WIN32)
,
                          std::wstring& storage
#endif
) {
#if defined(_WIN32)
    storage = utf8_wide(path);
    return storage.c_str();
#else
    return path.c_str();
#endif
}

/// One loaded graph plus the names it was opened with.
class Session {
public:
    Session(Ort::Env& env, const std::string& path, const Ort::SessionOptions& options)
        : session_(nullptr) {
#if defined(_WIN32)
        std::wstring storage;
        const ORTCHAR_T* model_path = ort_path(path, storage);
#else
        const ORTCHAR_T* model_path = ort_path(path);
#endif
        try {
            session_ = std::make_unique<Ort::Session>(env, model_path, options);
        } catch (const Ort::Exception& error) {
            throw OcrError("cannot load model " + path + ": " + error.what());
        }
        Ort::AllocatorWithDefaultOptions allocator;
        input_name_ = session_->GetInputNameAllocated(0, allocator).get();
        output_name_ = session_->GetOutputNameAllocated(0, allocator).get();
    }

    [[nodiscard]] Ort::Value run(
        const std::vector<float>& input,
        const std::vector<std::int64_t>& shape) const {
        Ort::MemoryInfo memory =
            Ort::MemoryInfo::CreateCpu(OrtArenaAllocator, OrtMemTypeDefault);
        Ort::Value tensor = Ort::Value::CreateTensor<float>(
            memory,
            const_cast<float*>(input.data()),
            input.size(),
            shape.data(),
            shape.size());
        const char* input_names[] = {input_name_.c_str()};
        const char* output_names[] = {output_name_.c_str()};
        try {
            auto outputs = session_->Run(
                Ort::RunOptions{nullptr}, input_names, &tensor, 1, output_names, 1);
            return std::move(outputs[0]);
        } catch (const Ort::Exception& error) {
            throw OcrError(std::string("inference failed: ") + error.what());
        }
    }

private:
    std::unique_ptr<Ort::Session> session_;
    std::string input_name_;
    std::string output_name_;
};

struct Point {
    double x = 0.0;
    double y = 0.0;
};

double cross(const Point& origin, const Point& a, const Point& b) {
    return (a.x - origin.x) * (b.y - origin.y) - (a.y - origin.y) * (b.x - origin.x);
}

/// Andrew's monotone chain. Boundary pixels are enough: the hull of the mask is
/// the hull of its outline.
std::vector<Point> convex_hull(std::vector<Point> points) {
    std::sort(points.begin(), points.end(), [](const Point& left, const Point& right) {
        return left.x != right.x ? left.x < right.x : left.y < right.y;
    });
    points.erase(
        std::unique(points.begin(), points.end(), [](const Point& left, const Point& right) {
            return left.x == right.x && left.y == right.y;
        }),
        points.end());
    if (points.size() < 3) {
        return points;
    }

    std::vector<Point> lower;
    for (const Point& point : points) {
        while (lower.size() >= 2 && cross(lower[lower.size() - 2], lower.back(), point) <= 0.0) {
            lower.pop_back();
        }
        lower.push_back(point);
    }
    std::vector<Point> upper;
    for (auto it = points.rbegin(); it != points.rend(); ++it) {
        while (upper.size() >= 2 && cross(upper[upper.size() - 2], upper.back(), *it) <= 0.0) {
            upper.pop_back();
        }
        upper.push_back(*it);
    }
    lower.pop_back();
    upper.pop_back();
    lower.insert(lower.end(), upper.begin(), upper.end());
    return lower;
}

struct RotatedRect {
    double cx = 0.0;
    double cy = 0.0;
    double ux = 1.0;  // long-axis unit vector
    double uy = 0.0;
    double vx = 0.0;
    double vy = 1.0;
    double width = 0.0;
    double height = 0.0;
    double area = 0.0;
};

/// cv2.minAreaRect by rotating calipers over the hull edges: the smallest
/// bounding rectangle is aligned with one edge of the hull.
bool min_area_rect(const std::vector<Point>& hull, RotatedRect& out) {
    if (hull.size() < 3) {
        return false;
    }
    double best_area = std::numeric_limits<double>::max();
    RotatedRect best;
    for (std::size_t index = 0; index < hull.size(); ++index) {
        const Point& a = hull[index];
        const Point& b = hull[(index + 1) % hull.size()];
        const double ex = b.x - a.x;
        const double ey = b.y - a.y;
        const double length = std::sqrt(ex * ex + ey * ey);
        if (length == 0.0) {
            continue;
        }
        const double ux = ex / length;
        const double uy = ey / length;
        const double vx = -uy;
        const double vy = ux;
        double u_min = std::numeric_limits<double>::max();
        double u_max = std::numeric_limits<double>::lowest();
        double v_min = std::numeric_limits<double>::max();
        double v_max = std::numeric_limits<double>::lowest();
        for (const Point& point : hull) {
            const double u = point.x * ux + point.y * uy;
            const double v = point.x * vx + point.y * vy;
            u_min = std::min(u_min, u);
            u_max = std::max(u_max, u);
            v_min = std::min(v_min, v);
            v_max = std::max(v_max, v);
        }
        const double width = u_max - u_min;
        const double height = v_max - v_min;
        const double area = width * height;
        if (area < best_area) {
            best_area = area;
            best.ux = ux;
            best.uy = uy;
            best.vx = vx;
            best.vy = vy;
            best.width = width;
            best.height = height;
            best.area = area;
            const double cu = (u_min + u_max) / 2.0;
            const double cv = (v_min + v_max) / 2.0;
            best.cx = cu * ux + cv * vx;
            best.cy = cu * uy + cv * vy;
        }
    }
    out = best;
    return best_area < std::numeric_limits<double>::max();
}

struct Detected {
    Box box;
    std::string text;
    double score = 0.0;
};

/// Crops on the same line are one subtitle line: join them in reading order and
/// union their geometry, inserting a space across an ASCII word boundary.
std::vector<OcrLine> merge_lines(std::vector<Detected> detected) {
    if (detected.empty()) {
        return {};
    }
    auto center_y = [](const Box& box) {
        return static_cast<float>(box.y) + static_cast<float>(box.height) / 2.0F;
    };

    std::vector<float> heights;
    heights.reserve(detected.size());
    for (const auto& item : detected) {
        heights.push_back(static_cast<float>(item.box.height));
    }
    std::sort(heights.begin(), heights.end());
    const float typical = heights[heights.size() / 2];
    const float tolerance = std::max(4.0F, typical * 0.6F);

    std::sort(detected.begin(), detected.end(), [&](const Detected& left, const Detected& right) {
        const float lc = center_y(left.box);
        const float rc = center_y(right.box);
        return lc != rc ? lc < rc : left.box.x < right.box.x;
    });

    std::vector<std::vector<Detected>> groups;
    for (const auto& item : detected) {
        const float center = center_y(item.box);
        bool placed = false;
        for (auto& group : groups) {
            float reference = 0.0F;
            for (const auto& member : group) {
                reference += center_y(member.box);
            }
            reference /= static_cast<float>(group.size());
            if (std::fabs(center - reference) <= tolerance) {
                group.push_back(item);
                placed = true;
                break;
            }
        }
        if (!placed) {
            groups.push_back({item});
        }
    }

    std::vector<OcrLine> lines;
    for (auto& group : groups) {
        std::sort(group.begin(), group.end(), [](const Detected& left, const Detected& right) {
            return left.box.x < right.box.x;
        });
        std::string text;
        double score = 0.0;
        int x1 = std::numeric_limits<int>::max();
        int y1 = std::numeric_limits<int>::max();
        int x2 = 0;
        int y2 = 0;
        for (std::size_t index = 0; index < group.size(); ++index) {
            if (index && !text.empty() && !group[index].text.empty() &&
                ascii_alnum(text.back()) && ascii_alnum(group[index].text.front())) {
                text += " ";
            }
            text += group[index].text;
            score += group[index].score;
            x1 = std::min(x1, group[index].box.x);
            y1 = std::min(y1, group[index].box.y);
            x2 = std::max(x2, group[index].box.x + group[index].box.width);
            y2 = std::max(y2, group[index].box.y + group[index].box.height);
        }
        if (text.empty()) {
            continue;
        }
        OcrLine line;
        line.text = text;
        line.confidence = score / static_cast<double>(group.size());
        line.box = Box{x1, y1, x2 - x1, y2 - y1};
        lines.push_back(std::move(line));
    }

    std::sort(lines.begin(), lines.end(), [](const OcrLine& left, const OcrLine& right) {
        const int ly = left.box ? left.box->y : 0;
        const int ry = right.box ? right.box->y : 0;
        return ly != ry ? ly < ry : (left.box ? left.box->x : 0) < (right.box ? right.box->x : 0);
    });
    return lines;
}

}  // namespace

namespace ocr_detail {

DetInput prepare_det(const ImageView& frame, int limit_side_len) {
    if (frame.empty()) {
        throw OcrError("detector preprocessing got an empty frame");
    }
    const int longest = std::max(frame.width, frame.height);
    const float ratio = (longest > limit_side_len)
                            ? static_cast<float>(limit_side_len) / static_cast<float>(longest)
                            : 1.0F;
    int height = static_cast<int>(std::lround(static_cast<float>(frame.height) * ratio / 32.0F) * 32);
    int width = static_cast<int>(std::lround(static_cast<float>(frame.width) * ratio / 32.0F) * 32);
    height = std::max(height, 32);
    width = std::max(width, 32);

    const std::vector<std::uint8_t> resized =
        resize_bgr(frame.bgr, frame.width, frame.height, width, height);
    DetInput input;
    input.width = width;
    input.height = height;
    input.data.resize(static_cast<std::size_t>(3) * height * width);
    for (int channel = 0; channel < 3; ++channel) {
        for (int y = 0; y < height; ++y) {
            for (int x = 0; x < width; ++x) {
                const float pixel = resized[(static_cast<std::size_t>(y) * width + x) * 3 + channel];
                input.data[(static_cast<std::size_t>(channel) * height + y) * width + x] =
                    pixel / 127.5F - 1.0F;
            }
        }
    }
    return input;
}

std::vector<Box> db_boxes(
    const std::vector<float>& probabilities,
    int width,
    int height,
    int source_width,
    int source_height) {
    if (width <= 0 || height <= 0 ||
        probabilities.size() != static_cast<std::size_t>(width) * height) {
        throw OcrError("detector output does not match its reported shape");
    }
    if (source_width <= 0 || source_height <= 0) {
        throw OcrError("source frame has no size");
    }

    std::vector<std::uint8_t> mask(probabilities.size(), 0);
    for (std::size_t index = 0; index < probabilities.size(); ++index) {
        mask[index] = probabilities[index] > kDetThreshold ? 1 : 0;
    }

    // cv2.dilate with a 2x2 kernel and OpenCV's default anchor (1, 1): the
    // region grows towards larger x/y.
    std::vector<std::uint8_t> dilated(mask.size(), 0);
    for (int y = 0; y < height; ++y) {
        for (int x = 0; x < width; ++x) {
            for (int dy = 0; dy <= 1; ++dy) {
                for (int dx = 0; dx <= 1; ++dx) {
                    const int yy = y - dy;
                    const int xx = x - dx;
                    if (yy >= 0 && xx >= 0 && mask[static_cast<std::size_t>(yy) * width + xx]) {
                        dilated[static_cast<std::size_t>(y) * width + x] = 1;
                    }
                }
            }
        }
    }

    std::vector<int> labels(mask.size(), -1);
    std::vector<Box> boxes;
    const int dx[8] = {-1, 0, 1, -1, 1, -1, 0, 1};
    const int dy[8] = {-1, -1, -1, 0, 0, 1, 1, 1};

    for (int y = 0; y < height; ++y) {
        for (int x = 0; x < width; ++x) {
            const std::size_t start = static_cast<std::size_t>(y) * width + x;
            if (!dilated[start] || labels[start] >= 0) {
                continue;
            }
            std::queue<std::pair<int, int>> pending;
            pending.push({x, y});
            labels[start] = 1;
            int min_x = x;
            int max_x = x;
            int min_y = y;
            int max_y = y;
            std::vector<Point> boundary;
            while (!pending.empty()) {
                const auto [cx, cy] = pending.front();
                pending.pop();
                min_x = std::min(min_x, cx);
                max_x = std::max(max_x, cx);
                min_y = std::min(min_y, cy);
                max_y = std::max(max_y, cy);
                bool on_edge = false;
                for (int k = 0; k < 8 && !on_edge; ++k) {
                    const int nx = cx + dx[k];
                    const int ny = cy + dy[k];
                    if (nx < 0 || ny < 0 || nx >= width || ny >= height ||
                        !dilated[static_cast<std::size_t>(ny) * width + nx]) {
                        on_edge = true;
                    }
                }
                if (on_edge) {
                    boundary.push_back(Point{static_cast<double>(cx), static_cast<double>(cy)});
                }
                for (int k = 0; k < 8; ++k) {
                    const int nx = cx + dx[k];
                    const int ny = cy + dy[k];
                    if (nx < 0 || ny < 0 || nx >= width || ny >= height) {
                        continue;
                    }
                    const std::size_t index = static_cast<std::size_t>(ny) * width + nx;
                    if (dilated[index] && labels[index] < 0) {
                        labels[index] = 1;
                        pending.push({nx, ny});
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
                    sum += probabilities[static_cast<std::size_t>(yy) * width + xx];
                }
            }
            const double score = sum / (static_cast<double>(box_width) * box_height);
            if (score < kBoxThreshold) {
                continue;
            }

            // Minimum-area rotated rectangle over the boundary, then unclip.
            // pyclipper offsets every side outward by distance, so the axis
            // aligned bounds of the result are centre +/- (half sides projected
            // on each axis + distance).
            const std::vector<Point> hull = convex_hull(std::move(boundary));
            RotatedRect rect;
            double distance = 0.0;
            double extent_x = 0.0;
            double extent_y = 0.0;
            double center_x = (min_x + max_x + 1) / 2.0;
            double center_y = (min_y + max_y + 1) / 2.0;
            if (min_area_rect(hull, rect)) {
                distance = rect.area * kUnclipRatio / (2.0 * (rect.width + rect.height));
                extent_x = rect.width / 2.0 * std::fabs(rect.ux) +
                           rect.height / 2.0 * std::fabs(rect.vx) + distance;
                extent_y = rect.width / 2.0 * std::fabs(rect.uy) +
                           rect.height / 2.0 * std::fabs(rect.vy) + distance;
                center_x = rect.cx;
                center_y = rect.cy;
            } else {
                // A degenerate blob (a line, a single pixel): fall back to the
                // axis-aligned estimate rather than dropping a visible box.
                distance = static_cast<double>(box_width) * box_height * kUnclipRatio /
                           (2.0 * (box_width + box_height));
                extent_x = box_width / 2.0 + distance;
                extent_y = box_height / 2.0 + distance;
            }

            const float x1 = static_cast<float>(center_x - extent_x) /
                             static_cast<float>(width) * static_cast<float>(source_width);
            const float y1 = static_cast<float>(center_y - extent_y) /
                             static_cast<float>(height) * static_cast<float>(source_height);
            const float x2 = static_cast<float>(center_x + extent_x) /
                             static_cast<float>(width) * static_cast<float>(source_width);
            const float y2 = static_cast<float>(center_y + extent_y) /
                             static_cast<float>(height) * static_cast<float>(source_height);
            const int ix1 = clampi(static_cast<int>(std::lround(x1)), 0, source_width - 1);
            const int iy1 = clampi(static_cast<int>(std::lround(y1)), 0, source_height - 1);
            const int ix2 = clampi(static_cast<int>(std::lround(x2)), 0, source_width - 1);
            const int iy2 = clampi(static_cast<int>(std::lround(y2)), 0, source_height - 1);
            if (ix2 - ix1 <= 3 || iy2 - iy1 <= 3) {
                continue;
            }
            boxes.push_back(Box{ix1, iy1, ix2 - ix1, iy2 - iy1});
        }
    }
    return boxes;
}

std::string ctc_decode(
    const std::vector<float>& logits,
    int steps,
    int classes,
    const std::vector<std::string>& charset,
    double& confidence) {
    confidence = 0.0;
    if (steps < 0 || classes <= 0 ||
        logits.size() != static_cast<std::size_t>(steps) * static_cast<std::size_t>(classes)) {
        throw OcrError("recognition output does not match its reported shape");
    }
    std::string text;
    double total = 0.0;
    int kept = 0;
    int previous = -1;
    for (int step = 0; step < steps; ++step) {
        int best = 0;
        float best_probability = -std::numeric_limits<float>::infinity();
        for (int symbol = 0; symbol < classes; ++symbol) {
            const float value = logits[(static_cast<std::size_t>(step) * classes) + symbol];
            if (value > best_probability) {
                best_probability = value;
                best = symbol;
            }
        }
        if (best != 0 && best != previous && best < static_cast<int>(charset.size())) {
            text += charset[static_cast<std::size_t>(best)];
            total += best_probability;
            ++kept;
        }
        previous = best;
    }
    if (kept > 0) {
        confidence = total / kept;
    }
    return text;
}

}  // namespace ocr_detail

struct OnnxOcr::Impl {
    explicit Impl(Options options) : settings(std::move(options)), env(ORT_LOGGING_LEVEL_WARNING, "watashi") {
        if (settings.det_model.empty() || settings.rec_model.empty() ||
            settings.charset_file.empty()) {
            throw OcrError("det model, rec model and charset are all required");
        }
        for (const std::string& path : {settings.det_model, settings.rec_model,
                                        settings.charset_file}) {
            if (!file_readable(path)) {
                throw OcrError("cannot read " + path);
            }
        }
        if (!settings.cls_model.empty() && !file_readable(settings.cls_model)) {
            throw OcrError("cannot read " + settings.cls_model);
        }

        charset = load_charset(settings.charset_file);

        Ort::SessionOptions session_options;
        session_options.SetGraphOptimizationLevel(GraphOptimizationLevel::ORT_ENABLE_ALL);
        session_options.SetIntraOpNumThreads(std::max(1, settings.intra_threads));
        session_options.SetInterOpNumThreads(std::max(1, settings.inter_threads));
        if (settings.use_memory_arena) {
            session_options.EnableCpuMemArena();
        }

        det = std::make_unique<Session>(env, settings.det_model, session_options);
        rec = std::make_unique<Session>(env, settings.rec_model, session_options);
        if (!settings.cls_model.empty()) {
            cls = std::make_unique<Session>(env, settings.cls_model, session_options);
        }
    }

    [[nodiscard]] std::vector<Detected> detect(const ImageView& frame) const {
        const ocr_detail::DetInput input = ocr_detail::prepare_det(frame);
        Ort::Value output = det->run(
            input.data, {1, 3, input.height, input.width});
        const auto info = output.GetTensorTypeAndShapeInfo();
        const std::vector<std::int64_t> shape = info.GetShape();
        if (shape.size() != 4) {
            throw OcrError("detector output is not [1,1,H,W]");
        }
        const int height = static_cast<int>(shape[2]);
        const int width = static_cast<int>(shape[3]);
        const float* probabilities = output.GetTensorData<float>();
        const std::vector<float> values(probabilities, probabilities + (static_cast<std::size_t>(height) * width));
        const std::vector<Box> boxes =
            ocr_detail::db_boxes(values, width, height, frame.width, frame.height);

        std::vector<Detected> detected;
        detected.reserve(boxes.size());
        for (const auto& box : boxes) {
            detected.push_back(Detected{box, "", 0.0});
        }
        return detected;
    }

    /// 180-degree rotation is the only thing PaddleOCR's cls model decides.
    [[nodiscard]] bool classify_upside_down(std::vector<std::uint8_t>& bgr, int width, int height) const {
        if (!cls) {
            return false;
        }
        const std::vector<std::uint8_t> resized = resize_bgr(bgr.data(), width, height, kClsWidth, kRecHeight);
        std::vector<float> input(static_cast<std::size_t>(3) * kRecHeight * kClsWidth);
        for (int channel = 0; channel < 3; ++channel) {
            for (int y = 0; y < kRecHeight; ++y) {
                for (int x = 0; x < kClsWidth; ++x) {
                    const float pixel =
                        resized[(static_cast<std::size_t>(y) * kClsWidth + x) * 3 + channel];
                    input[(static_cast<std::size_t>(channel) * kRecHeight + y) * kClsWidth + x] =
                        pixel / 255.0F * 2.0F - 1.0F;
                }
            }
        }
        Ort::Value output = cls->run(input, {1, 3, kRecHeight, kClsWidth});
        const auto info = output.GetTensorTypeAndShapeInfo();
        const std::vector<std::int64_t> shape = info.GetShape();
        if (shape.size() != 2 || shape[1] != 2) {
            return false;
        }
        const float* scores = output.GetTensorData<float>();
        return scores[1] > scores[0] && scores[1] > 0.9F;
    }

    [[nodiscard]] std::pair<std::string, double> recognize_crop(
        const ImageView& frame,
        const Box& box,
        int batch_width) const {
        int width = 0;
        int height = 0;
        std::vector<std::uint8_t> bgr = crop_bgr(
            frame, box.x, box.y, box.x + box.width, box.y + box.height, width, height);
        if (classify_upside_down(bgr, width, height)) {
            bgr = rotate_180(bgr, width, height);
        }

        const int resized_w = std::min(
            batch_width,
            static_cast<int>(std::ceil(static_cast<double>(kRecHeight) * width / height)));
        const std::vector<std::uint8_t> resized =
            resize_bgr(bgr.data(), width, height, resized_w, kRecHeight);

        std::vector<float> input(static_cast<std::size_t>(3) * kRecHeight * batch_width, 0.0F);
        for (int y = 0; y < kRecHeight; ++y) {
            for (int x = 0; x < resized_w; ++x) {
                for (int channel = 0; channel < 3; ++channel) {
                    const float pixel =
                        resized[(static_cast<std::size_t>(y) * resized_w + x) * 3 + channel];
                    input[(static_cast<std::size_t>(channel) * kRecHeight + y) * batch_width + x] =
                        pixel / 127.5F - 1.0F;
                }
            }
        }

        Ort::Value output = rec->run(input, {1, 3, kRecHeight, batch_width});
        const auto info = output.GetTensorTypeAndShapeInfo();
        const std::vector<std::int64_t> shape = info.GetShape();
        if (shape.size() != 3) {
            throw OcrError("recognition output is not [1,T,C]");
        }
        const int steps = static_cast<int>(shape[1]);
        const int classes = static_cast<int>(shape[2]);
        if (classes != static_cast<int>(charset.size())) {
            throw OcrError("recognition classes (" + std::to_string(classes) +
                           ") do not match the character table (" +
                           std::to_string(charset.size()) + ")");
        }
        const float* logits = output.GetTensorData<float>();
        const std::vector<float> values(
            logits, logits + (static_cast<std::size_t>(steps) * classes));
        double confidence = 0.0;
        std::string text = ocr_detail::ctc_decode(values, steps, classes, charset, confidence);
        return {text, confidence};
    }

    [[nodiscard]] OcrResult recognize(const ImageView& frame) const {
        if (frame.empty()) {
            throw OcrError("no frame to recognise");
        }
        const auto started = std::chrono::steady_clock::now();
        std::vector<Detected> detected = detect(frame);

        float max_ratio = static_cast<float>(kMinRecWidth) / kRecHeight;
        for (const auto& item : detected) {
            const float ratio = static_cast<float>(item.box.width) /
                                static_cast<float>(std::max(1, item.box.height));
            max_ratio = std::max(max_ratio, ratio);
        }
        const int batch_width =
            std::max(kMinRecWidth, static_cast<int>(static_cast<float>(kRecHeight) * max_ratio));

        for (auto& item : detected) {
            const auto [text, confidence] = recognize_crop(frame, item.box, batch_width);
            item.text = text;
            item.score = confidence;
        }

        OcrResult result;
        result.lines = merge_lines(std::move(detected));
        result.elapsed_ms =
            std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - started)
                .count();
        return result;
    }

    Options settings;
    Ort::Env env;
    std::unique_ptr<Session> det;
    std::unique_ptr<Session> rec;
    std::unique_ptr<Session> cls;
    std::vector<std::string> charset;
};

OnnxOcr::OnnxOcr(Options options)
    : impl_(std::make_unique<Impl>(std::move(options))) {}

OnnxOcr::~OnnxOcr() = default;
OnnxOcr::OnnxOcr(OnnxOcr&&) noexcept = default;
OnnxOcr& OnnxOcr::operator=(OnnxOcr&&) noexcept = default;

OcrResult OnnxOcr::recognize(const ImageView& frame) const {
    return impl_->recognize(frame);
}

}  // namespace watashi
