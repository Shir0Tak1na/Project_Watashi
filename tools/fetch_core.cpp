#include "fetch_core.hpp"

#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <bcrypt.h>
#include <winhttp.h>

#include <algorithm>
#include <cctype>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace watashi::fetch {
namespace {

std::string trim(const std::string& text) {
    std::size_t begin = 0;
    std::size_t end = text.size();
    while (begin < end && std::isspace(static_cast<unsigned char>(text[begin]))) {
        ++begin;
    }
    while (end > begin && std::isspace(static_cast<unsigned char>(text[end - 1]))) {
        --end;
    }
    return text.substr(begin, end - begin);
}

bool lower_starts_with(const std::string& text, const char* prefix) {
    const std::size_t length = std::strlen(prefix);
    if (text.size() < length) {
        return false;
    }
    for (std::size_t index = 0; index < length; ++index) {
        if (std::tolower(static_cast<unsigned char>(text[index])) !=
            std::tolower(static_cast<unsigned char>(prefix[index]))) {
            return false;
        }
    }
    return true;
}

std::string lower(std::string text) {
    std::transform(text.begin(), text.end(), text.begin(), [](char c) {
        return static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    });
    return text;
}

/// A lock path must be relative, use forward slashes and contain no "..".
bool safe_relative_path(const std::string& raw, std::string* normalized) {
    const std::string text = trim(raw);
    if (text.empty() || text[0] == '/' || text[0] == '\\') {
        return false;
    }
    if (text.size() >= 2 && text[1] == ':') {
        return false;
    }
    std::string result;
    std::string part;
    for (std::size_t index = 0; index <= text.size(); ++index) {
        const char c = index < text.size() ? text[index] : '/';
        if (c == '/' || c == '\\') {
            if (part == "..") {
                return false;
            }
            if (!part.empty() && part != ".") {
                if (!result.empty()) {
                    result.push_back('/');
                }
                result += part;
            }
            part.clear();
        } else {
            part.push_back(c);
        }
    }
    if (result.empty()) {
        return false;
    }
    if (normalized != nullptr) {
        *normalized = result;
    }
    return true;
}

bool parse_file(
    const nlohmann::json& document,
    FileSpec& file,
    std::string* error) {
    if (!document.is_object()) {
        if (error != nullptr) *error = "a file entry is not an object";
        return false;
    }
    const auto path = document.find("path");
    const auto sha = document.find("sha256");
    if (path == document.end() || !path->is_string()) {
        if (error != nullptr) *error = "file entry has no string path";
        return false;
    }
    file.path = path->get<std::string>();
    std::string normalized;
    if (!safe_relative_path(file.path, &normalized)) {
        if (error != nullptr) *error = "unsafe file path: " + file.path;
        return false;
    }
    file.path = normalized;
    if (sha == document.end() || !sha->is_string()) {
        if (error != nullptr) *error = "file " + file.path + " has no sha256";
        return false;
    }
    file.sha256 = lower(trim(sha->get<std::string>()));
    if (file.sha256.size() != 64) {
        if (error != nullptr) *error = "file " + file.path + " has a bad sha256";
        return false;
    }
    const auto size = document.find("size");
    if (size == document.end() ||
        !(size->is_number_integer() || size->is_number_unsigned())) {
        if (error != nullptr) *error = "file " + file.path + " has no size";
        return false;
    }
    if (size->get<double>() < 0.0) {
        if (error != nullptr) *error = "file " + file.path + " has a negative size";
        return false;
    }
    file.size = size->get<std::uint64_t>();
    if (const auto url = document.find("url");
        url != document.end() && url->is_string()) {
        file.url = url->get<std::string>();
    }
    return true;
}

std::string hex_sha256(const unsigned char* digest, std::size_t length) {
    static const char digits[] = "0123456789abcdef";
    std::string text;
    text.reserve(length * 2);
    for (std::size_t index = 0; index < length; ++index) {
        text.push_back(digits[digest[index] >> 4]);
        text.push_back(digits[digest[index] & 0x0f]);
    }
    return text;
}

bool remove_partial(const std::filesystem::path& path, DWORD* last_error) {
    for (int attempt = 0; attempt < 20; ++attempt) {
        std::error_code error_code;
        std::filesystem::remove(path, error_code);
        if (!std::filesystem::exists(path)) {
            return true;
        }
        SetFileAttributesW(path.wstring().c_str(), FILE_ATTRIBUTE_NORMAL);
        if (DeleteFileW(path.wstring().c_str())) {
            return true;
        }
        if (last_error != nullptr) {
            *last_error = GetLastError();
        }
        Sleep(50);
    }
    return !std::filesystem::exists(path);
}

bool replace_file(
    const std::filesystem::path& temporary,
    const std::filesystem::path& target,
    std::string* error) {
    if (!MoveFileExW(
            temporary.wstring().c_str(),
            target.wstring().c_str(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
        if (error != nullptr) {
            *error = "cannot replace " + target.string() + " (Win32 error " +
                     std::to_string(GetLastError()) + ")";
        }
        return false;
    }
    return true;
}

std::string decode_file_url(std::string url) {
    const std::size_t prefix = std::string("file://").size();
    std::string text = url.substr(prefix);
    std::string decoded;
    decoded.reserve(text.size());
    auto hex_value = [](char c) -> int {
        if (c >= '0' && c <= '9') return c - '0';
        if (c >= 'a' && c <= 'f') return c - 'a' + 10;
        if (c >= 'A' && c <= 'F') return c - 'A' + 10;
        return -1;
    };
    for (std::size_t index = 0; index < text.size(); ++index) {
        if (text[index] == '%' && index + 2 < text.size()) {
            const int high = hex_value(text[index + 1]);
            const int low = hex_value(text[index + 2]);
            if (high >= 0 && low >= 0) {
                decoded.push_back(static_cast<char>(high * 16 + low));
                index += 2;
                continue;
            }
        }
        decoded.push_back(text[index]);
    }
    // file:///E:/x and file://E:/x both name the Windows path E:/x.
    if (!decoded.empty() && decoded[0] == '/') {
        decoded.erase(0, 1);
    }
    return decoded;
}

bool http_download(
    const std::string& url,
    const std::filesystem::path& target,
    const FileSpec& spec,
    bool resume,
    std::string* error) {
    const std::filesystem::path temporary = target.string() + ".part";
    std::filesystem::create_directories(target.parent_path());
    if (!resume) {
        std::filesystem::remove(temporary);
    }
    std::uint64_t offset = 0;
    if (resume) {
        std::error_code size_error;
        const auto size = std::filesystem::file_size(temporary, size_error);
        if (!size_error) {
            offset = size;
        }
    }

    const std::wstring wide_url(url.begin(), url.end());
    URL_COMPONENTS components{};
    components.dwStructSize = sizeof(components);
    components.dwSchemeLength = static_cast<DWORD>(-1);
    components.dwHostNameLength = static_cast<DWORD>(-1);
    components.dwUrlPathLength = static_cast<DWORD>(-1);
    components.dwExtraInfoLength = static_cast<DWORD>(-1);
    if (!WinHttpCrackUrl(wide_url.c_str(), 0, 0, &components)) {
        if (error != nullptr) *error = "cannot parse URL " + url;
        return false;
    }
    const std::wstring host(components.lpszHostName, components.dwHostNameLength);
    std::wstring path(components.lpszUrlPath, components.dwUrlPathLength);
    if (components.dwExtraInfoLength > 0) {
        path += std::wstring(
            components.lpszExtraInfo, components.dwExtraInfoLength);
    }

    HINTERNET session = WinHttpOpen(
        L"watashi-fetch/1.0", WINHTTP_ACCESS_TYPE_DEFAULT_PROXY,
        WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
    if (session == nullptr) {
        if (error != nullptr) *error = "WinHttpOpen failed";
        return false;
    }
    const int timeout = 60000;
    WinHttpSetTimeouts(session, timeout, timeout, timeout, timeout);

    HINTERNET connection = WinHttpConnect(
        session, host.c_str(), static_cast<INTERNET_PORT>(components.nPort), 0);
    HINTERNET request = nullptr;
    bool success = false;
    if (connection != nullptr) {
        request = WinHttpOpenRequest(
            connection, L"GET", path.c_str(), nullptr, WINHTTP_NO_REFERER,
            WINHTTP_DEFAULT_ACCEPT_TYPES, 0);
    }
    if (request != nullptr) {
        if (offset > 0) {
            const std::wstring range =
                L"Range: bytes=" + std::to_wstring(offset) + L"-";
            WinHttpAddRequestHeaders(
                request, range.c_str(), static_cast<DWORD>(-1),
                WINHTTP_ADDREQ_FLAG_ADD | WINHTTP_ADDREQ_FLAG_REPLACE);
        }
        if (WinHttpSendRequest(
                request, WINHTTP_NO_ADDITIONAL_HEADERS, 0,
                WINHTTP_NO_REQUEST_DATA, 0, 0, 0) &&
            WinHttpReceiveResponse(request, nullptr)) {
            DWORD status = 0;
            DWORD status_size = sizeof(status);
            WinHttpQueryHeaders(
                request,
                WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                WINHTTP_HEADER_NAME_BY_INDEX, &status, &status_size,
                WINHTTP_NO_HEADER_INDEX);
            if (status == 206) {
                // append at offset
            } else if (status == 200) {
                offset = 0;
                std::filesystem::remove(temporary);
            } else {
                if (error != nullptr) {
                    *error = "HTTP " + std::to_string(status) + " for " + url;
                }
            }

            if (status == 200 || status == 206) {
                std::ofstream file(
                    temporary,
                    std::ios::binary |
                        (offset > 0 ? std::ios::app : std::ios::trunc));
                if (!file) {
                    if (error != nullptr) {
                        *error = "cannot write " + temporary.string();
                    }
                } else {
                    std::vector<unsigned char> buffer(65536);
                    success = true;
                    while (true) {
                        DWORD read = 0;
                        if (!WinHttpReadData(
                                request, buffer.data(),
                                static_cast<DWORD>(buffer.size()), &read)) {
                            if (error != nullptr) *error = "read failed for " + url;
                            success = false;
                            break;
                        }
                        if (read == 0) {
                            break;
                        }
                        file.write(
                            reinterpret_cast<const char*>(buffer.data()), read);
                        if (!file) {
                            if (error != nullptr) {
                                *error = "write failed for " + temporary.string();
                            }
                            success = false;
                            break;
                        }
                    }
                }
            }
        } else {
            if (error != nullptr) *error = "send failed for " + url;
        }
    }
    if (request != nullptr) WinHttpCloseHandle(request);
    if (connection != nullptr) WinHttpCloseHandle(connection);
    WinHttpCloseHandle(session);

    if (!success) {
        return false;
    }

    std::string verify_error;
    if (!verify_file(temporary, spec, &verify_error)) {
        DWORD delete_error = 0;
        const bool deleted = remove_partial(temporary, &delete_error);
        if (error != nullptr) {
            *error = "downloaded file failed verification: " + verify_error;
            if (!deleted) {
                *error += " (partial file could not be deleted, Win32 error " +
                          std::to_string(delete_error) + ")";
            }
        }
        return false;
    }
    return replace_file(temporary, target, error);
}

}  // namespace

bool load_lock(const std::filesystem::path& path, Lock& lock, std::string* error) {
    lock = Lock{};
    std::ifstream file(path, std::ios::binary);
    if (!file) {
        if (error != nullptr) *error = "cannot read " + path.string();
        return false;
    }
    nlohmann::json document;
    try {
        file >> document;
    } catch (const nlohmann::json::exception& exception) {
        if (error != nullptr) *error = path.string() + ": " + exception.what();
        return false;
    }
    if (!document.is_object()) {
        if (error != nullptr) *error = "lock must be a JSON object";
        return false;
    }
    const auto version = document.find("lock_version");
    if (version == document.end() || !version->is_number_integer()) {
        if (error != nullptr) *error = "lock has no lock_version";
        return false;
    }
    lock.version = version->get<int>();
    const auto models = document.find("models");
    if (models == document.end() || !models->is_array()) {
        if (error != nullptr) *error = "lock has no models array";
        return false;
    }
    for (const auto& model_json : *models) {
        if (!model_json.is_object()) {
            if (error != nullptr) *error = "a model entry is not an object";
            return false;
        }
        ModelSpec model;
        const auto id = model_json.find("id");
        if (id == model_json.end() || !id->is_string()) {
            if (error != nullptr) *error = "model has no string id";
            return false;
        }
        model.id = id->get<std::string>();
        for (const char* name : {"kind", "path", "source", "license", "url"}) {
            const auto found = model_json.find(name);
            if (found == model_json.end() || !found->is_string()) {
                continue;
            }
            std::string* target = nullptr;
            if (std::string(name) == "kind") target = &model.kind;
            if (std::string(name) == "path") target = &model.path;
            if (std::string(name) == "source") target = &model.source;
            if (std::string(name) == "license") target = &model.license;
            if (std::string(name) == "url") target = &model.url;
            if (target != nullptr) {
                *target = found->get<std::string>();
            }
        }
        const auto files = model_json.find("files");
        if (files == model_json.end() || !files->is_array() || files->empty()) {
            if (error != nullptr) *error = "model " + model.id + " has no files";
            return false;
        }
        for (const auto& file_json : *files) {
            FileSpec file;
            if (!parse_file(file_json, file, error)) {
                return false;
            }
            lock.total_bytes += file.size;
            model.files.push_back(std::move(file));
        }
        lock.models.push_back(std::move(model));
    }
    return true;
}

std::string sha256_file(const std::filesystem::path& path, std::string* error) {
    std::ifstream file(path, std::ios::binary);
    if (!file) {
        if (error != nullptr) *error = "cannot read " + path.string();
        return {};
    }
    BCRYPT_ALG_HANDLE algorithm = nullptr;
    BCRYPT_HASH_HANDLE hash = nullptr;
    std::vector<unsigned char> digest(32);
    bool ok = false;
    if (BCryptOpenAlgorithmProvider(
            &algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) == 0 &&
        BCryptCreateHash(algorithm, &hash, nullptr, 0, nullptr, 0, 0) == 0) {
        std::vector<unsigned char> buffer(1024 * 1024);
        ok = true;
        while (file) {
            file.read(
                reinterpret_cast<char*>(buffer.data()),
                static_cast<std::streamsize>(buffer.size()));
            const std::streamsize read = file.gcount();
            if (read > 0 &&
                BCryptHashData(
                    hash, buffer.data(), static_cast<ULONG>(read), 0) != 0) {
                ok = false;
                break;
            }
        }
        if (ok && BCryptFinishHash(hash, digest.data(), 32, 0) == 0) {
            ok = true;
        } else {
            ok = false;
        }
    }
    if (hash != nullptr) BCryptDestroyHash(hash);
    if (algorithm != nullptr) BCryptCloseAlgorithmProvider(algorithm, 0);
    if (!ok) {
        if (error != nullptr) *error = "cannot hash " + path.string();
        return {};
    }
    return hex_sha256(digest.data(), digest.size());
}

bool verify_file(
    const std::filesystem::path& path,
    const FileSpec& spec,
    std::string* error) {
    std::error_code exists_error;
    if (!std::filesystem::exists(path, exists_error)) {
        if (error != nullptr) *error = "missing " + path.string();
        return false;
    }
    if (spec.size > 0) {
        std::error_code size_error;
        const auto size = std::filesystem::file_size(path, size_error);
        if (size_error || size != spec.size) {
            if (error != nullptr) {
                *error = "size mismatch for " + path.string() + " (expected " +
                         std::to_string(spec.size) + ", got " +
                         (size_error ? "unknown" : std::to_string(size)) + ")";
            }
            return false;
        }
    }
    if (spec.sha256.empty()) {
        return true;
    }
    std::string hash_error;
    const std::string actual = sha256_file(path, &hash_error);
    if (actual.empty()) {
        if (error != nullptr) *error = hash_error;
        return false;
    }
    if (actual != lower(spec.sha256)) {
        if (error != nullptr) {
            *error = "hash mismatch for " + path.string() + " (expected " +
                     spec.sha256 + ", got " + actual + ")";
        }
        return false;
    }
    return true;
}

std::string download_url(const ModelSpec& model, const FileSpec& file) {
    if (!file.url.empty()) {
        return file.url;
    }
    if (!model.url.empty()) {
        if (model.url.find("{path}") != std::string::npos) {
            std::string replaced = model.url;
            const std::string placeholder = "{path}";
            replaced.replace(
                replaced.find(placeholder), placeholder.size(), file.path);
            return replaced;
        }
        return model.url + "/" + file.path;
    }
    if (lower_starts_with(model.source, "https://huggingface.co/") ||
        lower_starts_with(model.source, "http://huggingface.co/")) {
        std::string base = model.source;
        while (!base.empty() && base.back() == '/') {
            base.pop_back();
        }
        return base + "/resolve/main/" + file.path;
    }
    return {};
}

std::filesystem::path target_path(
    const std::filesystem::path& dest,
    const ModelSpec& model,
    const FileSpec& file,
    std::string* error) {
    std::string model_path;
    if (!model.path.empty() && !safe_relative_path(model.path, &model_path)) {
        if (error != nullptr) *error = "unsafe model path: " + model.path;
        return {};
    }
    std::string relative;
    if (lower_starts_with(file.path, "models/")) {
        relative = file.path;
    } else if (!model_path.empty() &&
               lower_starts_with(file.path, (model_path + "/").c_str())) {
        relative = file.path;
    } else if (!model_path.empty()) {
        relative = model_path + "/" + file.path;
    } else {
        relative = file.path;
    }
    if (!safe_relative_path(relative, &relative)) {
        if (error != nullptr) *error = "unsafe destination path: " + relative;
        return {};
    }
    if (dest.empty()) {
        if (error != nullptr) *error = "destination root is empty";
        return {};
    }
    return dest / std::filesystem::path(relative);
}

bool copy_local_file(
    const std::filesystem::path& source,
    const std::filesystem::path& target,
    const FileSpec& spec,
    bool resume,
    std::string* error) {
    const std::filesystem::path temporary = target.string() + ".part";
    std::filesystem::create_directories(target.parent_path());
    if (!resume) {
        std::filesystem::remove(temporary);
    }
    std::uint64_t offset = 0;
    if (resume) {
        std::error_code size_error;
        const auto size = std::filesystem::file_size(temporary, size_error);
        if (!size_error) {
            offset = size;
        }
    }
    std::error_code source_error;
    const auto source_size = std::filesystem::file_size(source, source_error);
    if (source_error) {
        if (error != nullptr) *error = "cannot read source " + source.string();
        return false;
    }
    if (offset > source_size) {
        offset = 0;
    }
    std::ifstream input(source, std::ios::binary);
    if (!input) {
        if (error != nullptr) *error = "cannot read " + source.string();
        return false;
    }
    input.seekg(static_cast<std::streamoff>(offset));
    std::ofstream output(
        temporary,
        std::ios::binary | (offset > 0 ? std::ios::app : std::ios::trunc));
    if (!output) {
        if (error != nullptr) *error = "cannot write " + temporary.string();
        return false;
    }
    std::vector<char> buffer(1024 * 1024);
    while (input) {
        input.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
        const std::streamsize read = input.gcount();
        if (read > 0) {
            output.write(buffer.data(), read);
        }
    }
    input.close();
    output.close();
    if (!output) {
        remove_partial(temporary, nullptr);
        if (error != nullptr) *error = "write failed for " + temporary.string();
        return false;
    }

    std::string verify_error;
    if (!verify_file(temporary, spec, &verify_error)) {
        DWORD delete_error = 0;
        const bool deleted = remove_partial(temporary, &delete_error);
        if (error != nullptr) {
            *error = "downloaded file failed verification: " + verify_error;
            if (!deleted) {
                *error += " (partial file could not be deleted, Win32 error " +
                          std::to_string(delete_error) + ")";
            }
        }
        return false;
    }
    return replace_file(temporary, target, error);
}

bool download_file(
    const std::string& url,
    const std::filesystem::path& target,
    const FileSpec& spec,
    const DownloadOptions& options,
    std::string* error) {
    if (url.empty()) {
        if (error != nullptr) *error = "no download URL";
        return false;
    }
    if (lower_starts_with(url, "file://")) {
        return copy_local_file(
            decode_file_url(url), target, spec, options.resume, error);
    }
    if (lower_starts_with(url, "http://") || lower_starts_with(url, "https://")) {
        if (options.offline) {
            if (error != nullptr) {
                *error = "offline: refusing to download " + url;
            }
            return false;
        }
        return http_download(url, target, spec, options.resume, error);
    }
    if (error != nullptr) *error = "unsupported URL scheme: " + url;
    return false;
}

}  // namespace watashi::fetch
