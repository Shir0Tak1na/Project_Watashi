"""Global hotkeys, so a click-through overlay can still be controlled.

The overlay is deliberately click-through: mouse input passes straight to
whatever is underneath, which is what makes it usable over a video. That also
means it can never receive a click of its own, so there is nowhere to put a
button and no way to reach it by mouse. A global hotkey is the only control that
works without breaking the click-through promise.

Implemented with Win32 ``RegisterHotKey`` through ctypes: no new dependency, and
it works while another application has focus, which is the whole point.

    Ctrl+Alt+P   pause / resume recognition
    Ctrl+Alt+H   hide / show the overlay without stopping recognition
    Ctrl+Alt+Q   quit

The thread that registers the hotkeys owns the message loop that receives them,
because ``WM_HOTKEY`` is delivered to the registering thread.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import sys
import threading
from dataclasses import dataclass
from typing import Callable

_IS_WINDOWS = sys.platform.startswith("win")

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

#: Virtual key codes we allow in hotkey strings.
_KEYS: dict[str, int] = {
    "P": 0x50, "H": 0x48, "Q": 0x51, "S": 0x53, "R": 0x52, "SPACE": 0x20,
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74, "F6": 0x75,
    "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
}


def parse_hotkey(spec: str) -> tuple[int, int]:
    """Parse ``"ctrl+alt+p"`` into ``(modifiers, virtual_key)``."""
    parts = [p.strip().upper() for p in spec.replace(" ", "").split("+") if p.strip()]
    if not parts:
        raise ValueError(f"empty hotkey spec {spec!r}")
    key = parts[-1]
    if key not in _KEYS:
        raise ValueError(
            f"unsupported key {key!r} in {spec!r}; known: {', '.join(sorted(_KEYS))}"
        )
    modifiers = MOD_NOREPEAT
    for name in parts[:-1]:
        if name in ("CTRL", "CONTROL"):
            modifiers |= MOD_CONTROL
        elif name == "ALT":
            modifiers |= MOD_ALT
        elif name == "SHIFT":
            modifiers |= MOD_SHIFT
        elif name in ("WIN", "SUPER"):
            raise ValueError("the Windows key is reserved; use ctrl/alt/shift")
        else:
            raise ValueError(f"unsupported modifier {name!r} in {spec!r}")
    return modifiers, _KEYS[key]


@dataclass
class HotkeyBinding:
    spec: str
    action: Callable[[], None]
    identifier: int = 0
    registered: bool = False


class HotkeyManager:
    """Registers global hotkeys and dispatches them on a dedicated thread."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._bindings: list[HotkeyBinding] = []
        self._ready = threading.Event()
        self._stop = threading.Event()
        self.available = _IS_WINDOWS
        self.failures: list[str] = []

    def add(self, spec: str, action: Callable[[], None]) -> HotkeyBinding:
        binding = HotkeyBinding(spec=spec, action=action, identifier=len(self._bindings) + 1)
        self._bindings.append(binding)
        return binding

    def start(self, timeout: float = 3.0) -> bool:
        """Register everything and start listening. False when nothing registered."""
        if not _IS_WINDOWS:
            self.failures.append("global hotkeys are Windows only in this build")
            return False
        if not self._bindings:
            self.failures.append("no hotkeys were configured")
            return False
        if self._thread is not None:
            return any(b.registered for b in self._bindings)

        self._thread = threading.Thread(target=self._run, name="watashi-hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=timeout)
        return any(b.registered for b in self._bindings)

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread_id:
            user32 = ctypes.windll.user32
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    # -- the message loop -------------------------------------------------- #

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        user32.RegisterHotKey.argtypes = [
            wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT
        ]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]

        self._thread_id = int(kernel32.GetCurrentThreadId())

        for binding in self._bindings:
            try:
                modifiers, key = parse_hotkey(binding.spec)
            except ValueError as exc:
                binding.registered = False
                self.failures.append(f"{binding.spec}: {exc}")
                continue
            ok = bool(user32.RegisterHotKey(None, binding.identifier, modifiers, key))
            binding.registered = ok
            if not ok:
                # most often another application already owns the combination
                self.failures.append(
                    f"{binding.spec}: already taken by another application"
                )

        self._ready.set()

        message = wintypes.MSG()
        while not self._stop.is_set():
            got = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
            if got in (0, -1):
                break
            if message.message == WM_HOTKEY:
                self._dispatch(int(message.wParam))

        for binding in self._bindings:
            if binding.registered:
                user32.UnregisterHotKey(None, binding.identifier)
                binding.registered = False

    def _dispatch(self, identifier: int) -> None:
        for binding in self._bindings:
            if binding.identifier != identifier:
                continue
            try:
                binding.action()
            except Exception as exc:  # a hotkey must never kill the loop
                print(f"[hotkeys] action for {binding.spec} failed: {exc}")
            return

    def describe(self) -> list[str]:
        lines = []
        for binding in self._bindings:
            state = "registered" if binding.registered else "NOT registered"
            lines.append(f"{binding.spec:14s} {state}")
        return lines
