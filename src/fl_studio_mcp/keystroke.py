"""Send Ctrl+Alt+Y (Windows) or Cmd+Opt+Y (macOS) to FL Studio to fire the
companion piano-roll pyscript.

Windows: Win32 SendInput.
macOS:   System Events raises the running FL Studio process, then Quartz posts
         Cmd+Opt+Y as HID-level key events (real Cmd/Opt key presses).
Other:   no-op; user must press the hotkey manually.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

log = logging.getLogger("fl_studio_mcp.keystroke")

HOTKEY_NAME = "Ctrl+Alt+Y (Win) / Cmd+Opt+Y (Mac)"

_PR_REQUEST_FILE = Path(os.path.expandvars(
    r"%USERPROFILE%\Documents\Image-Line\FL Studio\Settings\Piano roll scripts\fLMCP_request.json"
)) if sys.platform == "win32" else Path.home() / "Documents/Image-Line/FL Studio/Settings/Piano roll scripts/fLMCP_request.json"

_PR_STATE_FILE = _PR_REQUEST_FILE.with_name("fLMCP_state.json")


def request_file() -> Path:
    return _PR_REQUEST_FILE


def state_file() -> Path:
    return _PR_STATE_FILE


def clear_state() -> None:
    try:
        if _PR_STATE_FILE.exists():
            _PR_STATE_FILE.unlink()
    except Exception:
        pass


def wait_for_state(deadline_sec: float = 3.0) -> dict | None:
    """Poll for the state file produced by the piano-roll pyscript."""
    import json
    end = time.monotonic() + deadline_sec
    while time.monotonic() < end:
        if _PR_STATE_FILE.exists():
            try:
                return json.loads(_PR_STATE_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        time.sleep(0.01)
    return None


_FL_PROCESS = "OsxFL"    # FL Studio's process name on macOS (all versions)
_FOCUS_SETTLE_SEC = 1.0  # wait after the piano-roll focus call before the hotkey

# Raise the *running* FL Studio process and wait (up to ~2s) until it is frontmost.
# Targeting the process rather than an app name works for any FL version and never
# launches a different installed version (2025 and 2026 share a bundle id).
_RAISE_FL_SCRIPT = (
    'tell application "System Events"\n'
    '  if not (exists process "%s") then return "not running"\n'
    '  set frontmost of process "%s" to true\n'
    '  repeat 40 times\n'
    '    set fm to name of first application process whose frontmost is true\n'
    '    if fm is "%s" then exit repeat\n'
    '    delay 0.05\n'
    '  end repeat\n'
    'end tell\n'
    'return fm\n'
) % (_FL_PROCESS, _FL_PROCESS, _FL_PROCESS)


def _send_cmd_opt_y_quartz() -> None:
    """Post Cmd down, Opt down, Y down, Y up, Opt up, Cmd up at the HID event tap."""
    import time as _time
    from Quartz import (CGEventSourceCreate, kCGEventSourceStateHIDSystemState,
                        CGEventCreateKeyboardEvent, CGEventSetFlags, CGEventPost,
                        kCGHIDEventTap, kCGEventFlagMaskCommand, kCGEventFlagMaskAlternate)

    src = CGEventSourceCreate(kCGEventSourceStateHIDSystemState)
    CMD, OPT, Y = 55, 58, 16
    BOTH = kCGEventFlagMaskCommand | kCGEventFlagMaskAlternate

    def key(code, down, flags):
        e = CGEventCreateKeyboardEvent(src, code, down)
        CGEventSetFlags(e, flags)
        CGEventPost(kCGHIDEventTap, e)
        _time.sleep(0.03)

    key(CMD, True, kCGEventFlagMaskCommand)
    key(OPT, True, BOTH)
    key(Y, True, BOTH)
    key(Y, False, BOTH)
    key(OPT, False, kCGEventFlagMaskCommand)
    key(CMD, False, 0)


def send_hotkey_mac(focus_piano_roll=None) -> bool:
    """Bring FL Studio to the front, focus the piano roll, send Cmd+Opt+Y.

      1. raise the running FL Studio process and wait until it is frontmost
      2. call focus_piano_roll() (bridge ui.showWindow) while FL is foreground, then settle
      3. post Cmd+Opt+Y as Quartz HID events — FL 2026 ignores System Events
         `keystroke ... using {...}`, where the modifiers are only event flags

    Returns True if the keystroke was posted, False if FL could not be brought
    to the front (nothing is sent in that case). A posted keystroke can still be
    ignored by FL; callers detect that by waiting for the pyscript's state file.
    """
    if sys.platform != "darwin":
        return False
    try:
        import subprocess

        result = subprocess.run(
            ["osascript", "-e", _RAISE_FL_SCRIPT],
            timeout=5, capture_output=True, text=True,
        )
        frontmost = result.stdout.strip()
        if result.returncode != 0 or frontmost != _FL_PROCESS:
            log.warning("could not bring FL Studio to the front (rc=%s, frontmost=%r, err=%r)",
                        result.returncode, frontmost, result.stderr.strip())
            return False
        if focus_piano_roll is not None:
            focus_piano_roll()
            time.sleep(_FOCUS_SETTLE_SEC)
        _send_cmd_opt_y_quartz()
        return True
    except Exception as e:
        log.warning("mac keystroke failed: %s", e)
        return False


def send_hotkey_windows() -> bool:
    """Find FL Studio window and send Ctrl+Alt+Y. Returns True on success."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        import ctypes.wintypes as w

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        # 1) Find FL Studio window
        target_hwnd = [0]

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, w.HWND, w.LPARAM)

        def _enum(hwnd, _):
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return 1
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
            if "FL Studio" in title and user32.IsWindowVisible(hwnd):
                target_hwnd[0] = hwnd
                return 0
            return 1

        user32.EnumWindows(WNDENUMPROC(_enum), 0)
        if not target_hwnd[0]:
            log.warning("FL Studio window not found for keystroke")
            return False

        # 2) Bring to foreground (best effort)
        user32.ShowWindow(target_hwnd[0], 9)  # SW_RESTORE
        user32.SetForegroundWindow(target_hwnd[0])
        time.sleep(0.08)

        # 3) SendInput key combo Ctrl+Alt+Y
        # Virtual key codes
        VK_CONTROL = 0x11
        VK_MENU = 0x12  # alt
        VK_Y = 0x59
        KEYEVENTF_KEYUP = 0x0002
        INPUT_KEYBOARD = 1

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD),
                        ("time", w.DWORD), ("dwExtraInfo", ctypes.POINTER(w.ULONG))]

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD),
                        ("dwFlags", w.DWORD), ("time", w.DWORD),
                        ("dwExtraInfo", ctypes.POINTER(w.ULONG))]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", w.DWORD), ("wParamL", w.WORD), ("wParamH", w.WORD)]

        class _UNION(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", w.DWORD), ("u", _UNION)]

        def make_input(vk, up=False):
            i = INPUT()
            i.type = INPUT_KEYBOARD
            i.u.ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, None)
            return i

        seq = [
            make_input(VK_CONTROL, up=False),
            make_input(VK_MENU, up=False),
            make_input(VK_Y, up=False),
            make_input(VK_Y, up=True),
            make_input(VK_MENU, up=True),
            make_input(VK_CONTROL, up=True),
        ]
        arr = (INPUT * len(seq))(*seq)
        user32.SendInput.argtypes = (w.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        sent = user32.SendInput(len(seq), arr, ctypes.sizeof(INPUT))
        return sent == len(seq)
    except Exception as e:
        log.warning("keystroke failed: %s", e)
        return False
