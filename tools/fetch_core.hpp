// Model fetching, minus the command line.
//
// models.lock.json records, per model, the licence and source plus every file
// with its SHA-256 and byte count. This core turns that document into safe
// destination paths, verifies files and downloads one file through a partial
// file that is only renamed after the hash matches.
//
// The command line is in fetch_models.cpp. Keeping the logic here lets the
// tests cover resume, offline refusal and hash-mismatch deletion without a
// network.

#pragma once

#include <cstdint>
#include <filesystem>
#include <string>
#include <string_view>
#include <vector>

namespace watashi::fetch {

struct FileSpec {
    std::string path;
    std::string sha256;
    std::uint64_t size = 0;
    /// Optional explicit URL; otherwise it is derived from the model's source.
    std::string url;
};

struct ModelSpec {
    std::string id;
    std::string kind;
    std::string path;
    std::string source;
    std::string license;
    /// Optional URL for the whole model; a "{path}" placeholder is replaced.
    std::string url;
    std::vector<FileSpec> files;
};

struct Lock {
    int version = 0;
    std::uint64_t total_bytes = 0;
    std::vector<ModelSpec> models;
};

/// Reads and validates models.lock.json. Paths must be relative and free of
/// ".."; a malformed document is refused rather than half-applied.
bool load_lock(const std::filesystem::path& path, Lock& lock, std::string* error);

/// Lower-case hex SHA-256 of the file, or an empty string on an I/O error.
std::string sha256_file(const std::filesystem::path& path, std::string* error);

/// True when the file exists, has the recorded size (when recorded) and its
/// SHA-256 equals the recorded one. ``error`` says which check failed.
bool verify_file(
    const std::filesystem::path& path,
    const FileSpec& spec,
    std::string* error);

/// The URL to download one file: explicit file URL, then a model URL with a
/// {path} placeholder (or model URL + / + path), then a Hugging Face source
/// turned into /resolve/main/<path>. Empty means "no automatic download".
std::string download_url(const ModelSpec& model, const FileSpec& file);

/// Destination for one file under ``dest``. A model path is joined to a bare
/// file name; a file that already carries the lock's "models/..." prefix is
/// used as is. Absolute paths and ".." are refused.
std::filesystem::path target_path(
    const std::filesystem::path& dest,
    const ModelSpec& model,
    const FileSpec& file,
    std::string* error);

struct DownloadOptions {
    /// Keep and continue an existing .part file with an HTTP Range request
    /// (or a local offset for file:// sources).
    bool resume = false;
    /// Never open a network connection. A file:// source is still local.
    bool offline = false;
};

/// Downloads one file to ``target`` through ``target.part`` and renames it
/// only after the size and SHA-256 match. A hash or size mismatch deletes the
/// partial file and returns false; a network failure keeps it for --resume.
bool download_file(
    const std::string& url,
    const std::filesystem::path& target,
    const FileSpec& spec,
    const DownloadOptions& options,
    std::string* error);

/// file:// only, exposed so tests can exercise the copy path directly.
bool copy_local_file(
    const std::filesystem::path& source,
    const std::filesystem::path& target,
    const FileSpec& spec,
    bool resume,
    std::string* error);

}  // namespace watashi::fetch
