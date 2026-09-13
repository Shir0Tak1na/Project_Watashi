"""Resident memory reporting, for the dashboard and the memory profiles.

Deliberately dependency free: psutil is not installed, and adding it just to read
one number is not worth a dependency. Falls back to 0.0 rather than raising, so a
platform it does not understand degrades to "unknown" instead of breaking the
pipeline.

Getting this right on Windows needs explicit ``argtypes``/``restype``: without
them ctypes passes the process HANDLE as a 32 bit int, truncates it on 64 bit
Python, the call fails and every reading silently comes back zero.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import sys
import threading
from pathlib import Path


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _windows_working_set() -> tuple[float, float]:
    fn = ctypes.windll.psapi.GetProcessMemoryInfo
    fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessMemoryCounters), wintypes.DWORD]
    fn.restype = wintypes.BOOL
    get_current = ctypes.windll.kernel32.GetCurrentProcess
    get_current.restype = wintypes.HANDLE

    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
    if not fn(get_current(), ctypes.byref(counters), counters.cb):
        return 0.0, 0.0
    return counters.WorkingSetSize / 2**20, counters.PeakWorkingSetSize / 2**20


def _linux_working_set() -> tuple[float, float]:
    """statm reports pages; field 2 is resident, field 1 is peak-ish (vm size)."""
    try:
        fields = Path("/proc/self/statm").read_text().split()
        page = 4096
        resident = int(fields[1]) * page / 2**20
        return resident, resident
    except Exception:
        return 0.0, 0.0


def working_set_mib() -> float:
    """Current resident set size in MiB, or 0.0 when unavailable."""
    return _working_set()[0]


def peak_working_set_mib() -> float:
    """Peak resident set size in MiB, or 0.0 when unavailable."""
    return _working_set()[1]


def _working_set() -> tuple[float, float]:
    try:
        if sys.platform.startswith("win"):
            return _windows_working_set()
        if sys.platform.startswith("linux"):
            return _linux_working_set()
    except Exception:
        pass
    return 0.0, 0.0


class MemorySampler:
    """Samples the working set at most once per interval.

    Two robustness details, both earned from observed behaviour:

    * **Readings are validated.** A single failed call once returned a
      pre-model value and, because readings are cached, that low figure was then
      reported as fact for the rest of the session. A reading below
      ``minimum_mib`` is treated as a failure and the last good one is kept --
      reporting "unknown" is better than reporting a wrong number.
    * **It is locked.** The sampler is called from the pipeline worker thread
      (stats events) and from the main thread (final report), so the cached
      fields need protecting.
    """

    def __init__(self, interval: float = 2.0, minimum_mib: float = 1.0) -> None:
        self.interval = interval
        self.minimum_mib = minimum_mib
        self._last_at = 0.0
        self._value = 0.0
        self._lock = threading.Lock()
        self.rejected = 0

    def sample(self, now: float) -> float:
        with self._lock:
            if now - self._last_at < self.interval:
                return self._value
            reading = working_set_mib()
            if reading >= self.minimum_mib:
                self._value = reading
                self._last_at = now
            else:
                # either the platform is unsupported or the call failed; stop
                # retrying every tick, and keep whatever we last trusted
                self.rejected += 1
                self._last_at = now
            return self._value
