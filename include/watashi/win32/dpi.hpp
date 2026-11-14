// The one Win32 call the overlay needs from the engine side.
//
// It lives in its own header and library (watashi_win32) so a UI build does not
// have to link watashi_core -- and therefore ONNX Runtime or CTranslate2 -- just
// to make itself DPI aware.

#pragma once

namespace watashi {

/// Turn this process PerMonitorV2 DPI aware, falling back to system-DPI
/// awareness on older Windows. Call once, before any geometry is read: after
/// the first window is created Windows locks the awareness in.
/// Returns true when at least one path succeeded.
bool enable_per_monitor_dpi();

}  // namespace watashi
