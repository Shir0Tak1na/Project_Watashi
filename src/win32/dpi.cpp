#include "watashi/win32/dpi.hpp"

#include <cstdint>

#if defined(_WIN32)
#define NOMINMAX
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#endif

namespace watashi {

bool enable_per_monitor_dpi() {
#if defined(_WIN32)
    // PerMonitorV2 exists from Windows 10 1703. The context is -4 by value and
    // the symbol is loaded dynamically so this builds against older SDKs.
    using SetContext = BOOL(WINAPI*)(HANDLE);
    if (HMODULE user32 = GetModuleHandleW(L"user32.dll")) {
        auto set_context =
            reinterpret_cast<SetContext>(GetProcAddress(user32, "SetProcessDpiAwarenessContext"));
        if (set_context != nullptr) {
            const HANDLE per_monitor_v2 = reinterpret_cast<HANDLE>(static_cast<std::intptr_t>(-4));
            if (set_context(per_monitor_v2)) {
                return true;
            }
        }
    }
    // Windows 8.1: PROCESS_PER_MONITOR_DPI_AWARE == 2.
    using SetAwareness = HRESULT(WINAPI*)(int);
    if (HMODULE shcore = LoadLibraryW(L"shcore.dll")) {
        auto set_awareness =
            reinterpret_cast<SetAwareness>(GetProcAddress(shcore, "SetProcessDpiAwareness"));
        const bool ok = set_awareness != nullptr && set_awareness(2) == S_OK;
        FreeLibrary(shcore);
        if (ok) {
            return true;
        }
    }
    // Vista and later; system-DPI awareness is the best that is left.
    using SetSystem = BOOL(WINAPI*)();
    if (HMODULE user32 = GetModuleHandleW(L"user32.dll")) {
        auto set_system =
            reinterpret_cast<SetSystem>(GetProcAddress(user32, "SetProcessDPIAware"));
        if (set_system != nullptr && set_system()) {
            return true;
        }
    }
#endif
    return false;
}

}  // namespace watashi
