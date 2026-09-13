#pragma once

#include <string>

namespace projectwatashi {

class SqliteDb {
public:
    explicit SqliteDb(const std::string& db_path);
    ~SqliteDb();

    bool open();
    bool close();
    bool execute(const std::string& sql);

private:
    std::string db_path_;
    void* handle_ = nullptr;
};

} // namespace projectwatashi
