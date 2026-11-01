// watashi_fetch: verify and download the weights named by models.lock.json.
//
//   watashi_fetch --check --dest PATH       verify local files; no network
//   watashi_fetch --fetch --dest PATH       fetch missing files
//   watashi_fetch --fetch --resume ...      continue .part files
//   watashi_fetch --fetch --offline ...     refuse every http(s) download
//
// Nothing is downloaded unless --fetch is explicit. The lock records every
// file's size and SHA-256; a download is written to a .part file and renamed
// only after both match. A mismatch deletes the partial file.
//
// The NLLB weights are CC-BY-NC-4.0 (non-commercial). That licence is printed
// before a fetch and repeated in docs/INSTALL.md and THIRD_PARTY_LICENSES.md.

#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include <cstdlib>
#include <filesystem>
#include <iostream>
#include <string>
#include <vector>

#include "fetch_core.hpp"
#include "obs/obs.hpp"

namespace {

std::filesystem::path module_directory() {
    wchar_t module[32768]{};
    if (GetModuleFileNameW(nullptr, module, 32768) == 0) {
        return std::filesystem::current_path();
    }
    return std::filesystem::path(module).parent_path();
}

std::filesystem::path default_lock() {
    const std::filesystem::path beside = module_directory() / "models.lock.json";
    if (std::filesystem::exists(beside)) {
        return beside;
    }
    return std::filesystem::path("models.lock.json");
}

std::filesystem::path default_dest() {
#ifdef _WIN32
    char* buffer = nullptr;
    std::size_t length = 0;
    if (_dupenv_s(&buffer, &length, "LOCALAPPDATA") != 0 || buffer == nullptr) {
        return std::filesystem::path("models");
    }
    const std::string local_app_data(buffer);
    std::free(buffer);
    if (local_app_data.empty()) {
        return std::filesystem::path("models");
    }
#else
    const char* local_app_data = std::getenv("LOCALAPPDATA");
    if (local_app_data == nullptr || *local_app_data == '\0') {
        return std::filesystem::path("models");
    }
    const std::string value(local_app_data);
#endif
    return std::filesystem::path(local_app_data) / "ProjectWatashi" / "user";
}

void usage() {
    std::cout <<
        "usage: watashi_fetch [--lock PATH] [--dest PATH] [--check|--fetch]\n"
        "                     [--resume] [--offline]\n"
        "\n"
        "  --lock PATH    lock file, default models.lock.json beside the exe\n"
        "  --dest PATH    download root, default %LOCALAPPDATA%\\ProjectWatashi\\user\n"
        "  --check        verify local files (size + SHA-256), never download\n"
        "  --fetch        download missing files; the only mode that uses the network\n"
        "  --resume       continue .part files from their current size\n"
        "  --offline      refuse every http(s) download; file:// mirrors still work\n"
        "  --version      print product version, git commit and build type\n"
        "\n"
        "No model weight is bundled with the installer. The NLLB translation model\n"
        "is CC-BY-NC-4.0 (non-commercial); see docs/INSTALL.md.\n";
}

std::string human_bytes(std::uint64_t bytes) {
    const double value = static_cast<double>(bytes);
    if (bytes < 1024) {
        return std::to_string(bytes) + " B";
    }
    if (bytes < 1024ull * 1024ull) {
        return std::to_string(static_cast<int>(value / 1024.0)) + " KiB";
    }
    if (bytes < 1024ull * 1024ull * 1024ull) {
        return std::to_string(static_cast<int>(value / (1024.0 * 1024.0))) + " MiB";
    }
    return std::to_string(static_cast<int>(value / (1024.0 * 1024.0 * 1024.0))) +
           " GiB";
}

struct Counts {
    int ok = 0;
    int missing = 0;
    int downloaded = 0;
    int manual = 0;
    int failed = 0;
};

}  // namespace

int main(int argc, char** argv) {
    std::filesystem::path lock_path = default_lock();
    std::filesystem::path dest = default_dest();
    bool check = false;
    bool fetch = false;
    bool resume = false;
    bool offline = false;

    for (int index = 1; index < argc; ++index) {
        const std::string argument = argv[index];
        if (argument == "--lock" && index + 1 < argc) {
            lock_path = argv[++index];
        } else if (argument == "--dest" && index + 1 < argc) {
            dest = argv[++index];
        } else if (argument == "--check") {
            check = true;
        } else if (argument == "--fetch") {
            fetch = true;
        } else if (argument == "--resume") {
            resume = true;
        } else if (argument == "--offline") {
            offline = true;
        } else if (argument == "--help" || argument == "-h") {
            usage();
            return 0;
        } else if (argument == "--version") {
            std::cout << watashi::obs::version_text() << std::flush;
            return 0;
        } else {
            std::cerr << "watashi_fetch: unknown argument " << argument << "\n";
            usage();
            return 2;
        }
    }
    if (check && fetch) {
        std::cerr << "watashi_fetch: choose --check or --fetch, not both\n";
        return 2;
    }
    if (resume && !fetch) {
        std::cerr << "watashi_fetch: --resume only affects --fetch\n";
        return 2;
    }

    watashi::fetch::Lock lock;
    std::string error;
    if (!watashi::fetch::load_lock(lock_path, lock, &error)) {
        std::cerr << "watashi_fetch: " << error << "\n";
        return 1;
    }

    std::cout << "watashi_fetch: lock=" << lock_path.string()
              << " dest=" << dest.string()
              << " mode=" << (fetch ? "fetch" : (check ? "check" : "list")) << "\n";
    std::cout << "models=" << lock.models.size()
              << " bytes=" << human_bytes(lock.total_bytes) << "\n";

    watashi::fetch::DownloadOptions options;
    options.resume = resume;
    options.offline = offline;

    Counts counts;
    for (const auto& model : lock.models) {
        std::uint64_t bytes = 0;
        for (const auto& file : model.files) {
            bytes += file.size;
        }
        std::cout << "- " << model.id << " [" << model.kind << "] "
                  << model.files.size() << " file(s) " << human_bytes(bytes)
                  << " license=" << (model.license.empty() ? "unknown" : model.license)
                  << "\n";
        if (model.license.empty()) {
            std::cout << "  warning: the lock does not state this model's licence\n";
        } else if (model.license == "CC-BY-NC-4.0") {
            std::cout << "  note: CC-BY-NC-4.0 is non-commercial; do not use this "
                         "model commercially\n";
        }
        for (const auto& file : model.files) {
            std::string path_error;
            const std::filesystem::path target =
                watashi::fetch::target_path(dest, model, file, &path_error);
            if (!path_error.empty()) {
                std::cerr << "  [FAIL] " << file.path << ": " << path_error << "\n";
                ++counts.failed;
                continue;
            }
            std::string verify_error;
            const bool present = watashi::fetch::verify_file(target, file, &verify_error);
            const std::string label = model.id + "/" + file.path;
            if (present) {
                ++counts.ok;
                if (check) {
                    std::cout << "  [ok] " << label << "\n";
                }
                continue;
            }
            if (!fetch) {
                ++counts.missing;
                std::cout << "  [missing] " << label << ": " << verify_error << "\n";
                continue;
            }

            const std::string url = watashi::fetch::download_url(model, file);
            if (url.empty()) {
                ++counts.manual;
                std::cout << "  [manual] " << label << ": no automatic URL; source="
                          << (model.source.empty() ? "unknown" : model.source) << "\n";
                continue;
            }
            std::cout << "  [fetch] " << label << " <- " << url
                      << (resume ? " (resume)" : "") << "\n";
            std::string download_error;
            if (!watashi::fetch::download_file(
                    url, target, file, options, &download_error)) {
                ++counts.failed;
                std::cerr << "  [FAIL] " << label << ": " << download_error << "\n";
            } else {
                ++counts.downloaded;
                std::cout << "  [ok] " << label << " verified\n";
            }
        }
    }

    std::cout << "summary: ok=" << counts.ok << " missing=" << counts.missing
              << " downloaded=" << counts.downloaded
              << " manual=" << counts.manual << " failed=" << counts.failed << "\n";
    if (counts.manual > 0) {
        std::cout << "manual models have no direct URL in models.lock.json; see "
                     "docs/INSTALL.md\n";
    }
    if (counts.failed > 0) {
        return 1;
    }
    if (check && counts.missing > 0) {
        return 1;
    }
    return 0;
}
