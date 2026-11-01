// models.lock.json parsing, SHA-256 verification, file:// downloads, resume,
// offline refusal and hash-mismatch deletion. No network is used.

#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include <catch2/catch_test_macros.hpp>

#include <chrono>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <random>
#include <string>
#include <thread>
#include <vector>

#include <nlohmann/json.hpp>

#include "fetch_core.hpp"

namespace {

std::filesystem::path module_directory() {
    wchar_t module[32768]{};
    GetModuleFileNameW(nullptr, module, 32768);
    return std::filesystem::path(module).parent_path();
}

std::filesystem::path repo_root() {
    auto directory = module_directory();
    for (int step = 0; step < 6; ++step) {
        if (std::filesystem::exists(directory / "models.lock.json")) {
            return directory;
        }
        directory = directory.parent_path();
    }
    return module_directory();
}

struct TempDir {
    TempDir() {
        std::random_device device;
        std::mt19937 generator(device());
        const auto stamp = std::chrono::steady_clock::now().time_since_epoch().count();
        path = module_directory() /
               ("watashi_fetch_" + std::to_string(stamp) + "_" +
                std::to_string(generator()));
        std::filesystem::create_directories(path);
    }
    ~TempDir() {
        std::error_code error;
        std::filesystem::remove_all(path, error);
    }
    std::filesystem::path path;
};

void write_file(const std::filesystem::path& path, const std::string& contents) {
    std::filesystem::create_directories(path.parent_path());
    std::ofstream file(path, std::ios::binary | std::ios::trunc);
    file.write(contents.data(), static_cast<std::streamsize>(contents.size()));
}

std::string read_file(const std::filesystem::path& path) {
    std::ifstream file(path, std::ios::binary);
    return std::string(
        (std::istreambuf_iterator<char>(file)), std::istreambuf_iterator<char>());
}

/// Some sandboxed sessions report a successful DeleteFileW while the file
/// stays on disk. That is a host filter, not a product bug; the caller reports
/// it as an explicit skip (exit 77) instead of pretending the assertion ran.
bool deletion_works(const std::filesystem::path& directory) {
    const auto probe = directory / "delete_probe.txt";
    write_file(probe, "probe");
    for (int attempt = 0; attempt < 25; ++attempt) {
        std::filesystem::remove(probe);
        if (!std::filesystem::exists(probe)) {
            return true;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
    return false;
}

std::string file_url(const std::filesystem::path& path) {
    const std::string generic =
        std::filesystem::absolute(path).generic_string();
    return "file:///" + generic;
}

watashi::fetch::FileSpec spec_for(
    const std::string& path,
    const std::string& contents,
    const std::string& sha) {
    watashi::fetch::FileSpec spec;
    spec.path = path;
    spec.size = contents.size();
    spec.sha256 = sha;
    return spec;
}

const std::string kAbcSha =
    "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";

nlohmann::json minimal_lock(const nlohmann::json& file) {
    return nlohmann::json{
        {"lock_version", 1},
        {"models", nlohmann::json::array({nlohmann::json{
            {"id", "test-model"},
            {"kind", "test"},
            {"source", "file:///..."},
            {"license", "MIT"},
            {"files", nlohmann::json::array({file})},
        }})},
    };
}

}  // namespace

TEST_CASE("the shipped lock parses every model", "[fetch]") {
    watashi::fetch::Lock lock;
    std::string error;
    REQUIRE(watashi::fetch::load_lock(
        repo_root() / "models.lock.json", lock, &error));
    REQUIRE(lock.version == 1);
    REQUIRE(lock.models.size() == 3);
    REQUIRE(lock.total_bytes == 1149752969ull);

    const auto& nllb = lock.models[0];
    REQUIRE(nllb.id == "nllb-200-distilled-600M-ct2-int8");
    REQUIRE(nllb.license == "CC-BY-NC-4.0");
    REQUIRE(nllb.files.size() == 7);
    REQUIRE(nllb.files[1].size == 622595991ull);
    REQUIRE(nllb.files[1].sha256.size() == 64);

    REQUIRE(lock.models[1].files.size() == 4);
    REQUIRE(lock.models[2].files.size() == 3);
}

TEST_CASE("a synthetic lock with explicit URLs parses", "[fetch]") {
    const nlohmann::json document = minimal_lock(nlohmann::json{
        {"path", "model.bin"},
        {"sha256", kAbcSha},
        {"size", 3},
        {"url", "https://example.invalid/model.bin"},
    });
    TempDir dir;
    write_file(dir.path / "lock.json", document.dump());
    watashi::fetch::Lock lock;
    std::string error;
    REQUIRE(watashi::fetch::load_lock(dir.path / "lock.json", lock, &error));
    REQUIRE(lock.models.size() == 1);
    REQUIRE(lock.models[0].files[0].url == "https://example.invalid/model.bin");
}

TEST_CASE("malformed locks are refused", "[fetch]") {
    {
        TempDir dir;
        write_file(dir.path / "lock.json", "{\"models\": []}");
        watashi::fetch::Lock lock;
        std::string error;
        REQUIRE_FALSE(watashi::fetch::load_lock(dir.path / "lock.json", lock, &error));
        REQUIRE(error.find("lock_version") != std::string::npos);
    }
    {
        TempDir dir;
        write_file(
            dir.path / "lock.json",
            minimal_lock(nlohmann::json{
                {"path", "model.bin"},
                {"size", 1},
            }).dump());
        watashi::fetch::Lock lock;
        std::string error;
        REQUIRE_FALSE(watashi::fetch::load_lock(dir.path / "lock.json", lock, &error));
        REQUIRE(error.find("sha256") != std::string::npos);
    }
    {
        TempDir dir;
        write_file(
            dir.path / "lock.json",
            minimal_lock(nlohmann::json{
                {"path", "../escape.bin"},
                {"sha256", kAbcSha},
                {"size", 3},
            }).dump());
        watashi::fetch::Lock lock;
        std::string error;
        REQUIRE_FALSE(watashi::fetch::load_lock(dir.path / "lock.json", lock, &error));
        REQUIRE(error.find("unsafe") != std::string::npos);
    }
}

TEST_CASE("destinations join a model path and keep the models prefix", "[fetch]") {
    watashi::fetch::ModelSpec model;
    model.id = "m";
    model.path = "models/llm/m";
    watashi::fetch::FileSpec file;
    file.path = "model.bin";
    std::string error;
    const auto target = watashi::fetch::target_path(
        std::filesystem::path("root"), model, file, &error);
    REQUIRE(error.empty());
    REQUIRE(target.generic_string() == "root/models/llm/m/model.bin");

    watashi::fetch::ModelSpec ocr;
    ocr.id = "ocr";
    watashi::fetch::FileSpec onnx;
    onnx.path = "models/ch_det.onnx";
    const auto ocr_target =
        watashi::fetch::target_path(std::filesystem::path("root"), ocr, onnx, &error);
    REQUIRE(ocr_target.generic_string() == "root/models/ch_det.onnx");
}

TEST_CASE("target paths refuse traversal", "[fetch]") {
    watashi::fetch::ModelSpec model;
    model.path = "..";
    watashi::fetch::FileSpec file;
    file.path = "model.bin";
    std::string error;
    const auto target = watashi::fetch::target_path(
        std::filesystem::path("root"), model, file, &error);
    REQUIRE(target.empty());
    REQUIRE(error.find("unsafe") != std::string::npos);
}

TEST_CASE("sha256 matches a known digest", "[fetch]") {
    TempDir dir;
    const auto path = dir.path / "abc.bin";
    write_file(path, "abc");
    std::string error;
    REQUIRE(watashi::fetch::sha256_file(path, &error) == kAbcSha);
    REQUIRE(error.empty());
}

TEST_CASE("a correct file verifies", "[fetch]") {
    TempDir dir;
    const auto path = dir.path / "abc.bin";
    write_file(path, "abc");
    const auto spec = spec_for("abc.bin", "abc", kAbcSha);
    std::string error;
    REQUIRE(watashi::fetch::verify_file(path, spec, &error));
}

TEST_CASE("a wrong hash is reported", "[fetch]") {
    TempDir dir;
    const auto path = dir.path / "abc.bin";
    write_file(path, "abc");
    const auto spec = spec_for("abc.bin", "abc", std::string(64, '0'));
    std::string error;
    REQUIRE_FALSE(watashi::fetch::verify_file(path, spec, &error));
    REQUIRE(error.find("hash mismatch") != std::string::npos);
}

TEST_CASE("a wrong size is reported", "[fetch]") {
    TempDir dir;
    const auto path = dir.path / "abc.bin";
    write_file(path, "abc");
    auto spec = spec_for("abc.bin", "abc", kAbcSha);
    spec.size = 99;
    std::string error;
    REQUIRE_FALSE(watashi::fetch::verify_file(path, spec, &error));
    REQUIRE(error.find("size mismatch") != std::string::npos);
}

TEST_CASE("a file URL download is verified and renamed atomically", "[fetch]") {
    TempDir dir;
    const auto source = dir.path / "source.bin";
    write_file(source, "hello model");
    const auto spec = spec_for("models/test.bin", "hello model", kAbcSha);
    // Use the real digest rather than the abc constant.
    watashi::fetch::FileSpec actual = spec;
    std::string error;
    actual.sha256 = watashi::fetch::sha256_file(source, &error);
    REQUIRE(error.empty());
    const auto target = dir.path / "dest" / "models" / "test.bin";
    REQUIRE(watashi::fetch::download_file(
        file_url(source), target, actual, {}, &error));
    REQUIRE(read_file(target) == "hello model");
    REQUIRE_FALSE(std::filesystem::exists(target.string() + ".part"));
}

TEST_CASE("a hash mismatch deletes the partial file", "[fetch]") {
    TempDir dir;
    const auto source = dir.path / "source.bin";
    write_file(source, "hello model");
    auto spec = spec_for("models/test.bin", "hello model", std::string(64, '0'));
    const auto target = dir.path / "dest" / "models" / "test.bin";
    std::string error;
    REQUIRE_FALSE(watashi::fetch::download_file(
        file_url(source), target, spec, {}, &error));
    REQUIRE((error.find("verification") != std::string::npos ||
             error.find("hash mismatch") != std::string::npos));
    REQUIRE_FALSE(std::filesystem::exists(target));
    if (!deletion_works(dir.path)) {
        std::fprintf(stderr,
                     "SKIP (77): this host reports DeleteFileW success while the "
                     "file remains; physical deletion cannot be verified here\n");
        std::exit(77);
    }
    REQUIRE_FALSE(std::filesystem::exists(target.string() + ".part"));
}

TEST_CASE("resume continues from the partial file", "[fetch]") {
    TempDir dir;
    const auto source = dir.path / "source.bin";
    write_file(source, "hello model");
    std::string error;
    auto spec = spec_for("models/test.bin", "hello model",
                         watashi::fetch::sha256_file(source, &error));
    REQUIRE(error.empty());
    const auto target = dir.path / "dest" / "models" / "test.bin";
    write_file(target.string() + ".part", "hello");
    watashi::fetch::DownloadOptions options;
    options.resume = true;
    REQUIRE(watashi::fetch::download_file(
        file_url(source), target, spec, options, &error));
    REQUIRE(read_file(target) == "hello model");
    REQUIRE_FALSE(std::filesystem::exists(target.string() + ".part"));
}

TEST_CASE("without resume a stale partial file is discarded", "[fetch]") {
    TempDir dir;
    const auto source = dir.path / "source.bin";
    write_file(source, "hello model");
    std::string error;
    auto spec = spec_for("models/test.bin", "hello model",
                         watashi::fetch::sha256_file(source, &error));
    const auto target = dir.path / "dest" / "models" / "test.bin";
    write_file(target.string() + ".part", "garbage");
    REQUIRE(watashi::fetch::download_file(
        file_url(source), target, spec, {}, &error));
    REQUIRE(read_file(target) == "hello model");
}

TEST_CASE("offline refuses an http URL before any connection", "[fetch]") {
    TempDir dir;
    auto spec = spec_for("models/test.bin", "hello", std::string(64, '0'));
    const auto target = dir.path / "dest" / "models" / "test.bin";
    watashi::fetch::DownloadOptions options;
    options.offline = true;
    std::string error;
    REQUIRE_FALSE(watashi::fetch::download_file(
        "http://127.0.0.1:1/model.bin", target, spec, options, &error));
    REQUIRE(error.find("offline") != std::string::npos);
    REQUIRE_FALSE(std::filesystem::exists(target));
}

TEST_CASE("offline still allows a local file mirror", "[fetch]") {
    TempDir dir;
    const auto source = dir.path / "source.bin";
    write_file(source, "local");
    std::string error;
    auto spec = spec_for("models/test.bin", "local",
                         watashi::fetch::sha256_file(source, &error));
    const auto target = dir.path / "dest" / "models" / "test.bin";
    watashi::fetch::DownloadOptions options;
    options.offline = true;
    REQUIRE(watashi::fetch::download_file(
        file_url(source), target, spec, options, &error));
    REQUIRE(read_file(target) == "local");
}

TEST_CASE("download URLs prefer explicit, then model, then Hugging Face", "[fetch]") {
    watashi::fetch::ModelSpec model;
    model.id = "m";
    model.source = "https://huggingface.co/Systran/faster-whisper-small";
    watashi::fetch::FileSpec file;
    file.path = "model.bin";

    file.url = "https://example.invalid/direct.bin";
    REQUIRE(watashi::fetch::download_url(model, file) ==
            "https://example.invalid/direct.bin");
    file.url.clear();

    model.url = "https://example.invalid/{path}";
    REQUIRE(watashi::fetch::download_url(model, file) ==
            "https://example.invalid/model.bin");
    model.url.clear();

    REQUIRE(watashi::fetch::download_url(model, file) ==
            "https://huggingface.co/Systran/faster-whisper-small/resolve/main/model.bin");

    model.source = "PyPI: rapidocr-onnxruntime==1.4.4";
    REQUIRE(watashi::fetch::download_url(model, file).empty());
}
