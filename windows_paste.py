"""Paste the clipboard into the active window with a Ctrl+V that every app sees.

The old paste pressed and released Ctrl+V in the same instant the transcript
reached the clipboard. Some apps then pasted nothing:

* Clipboard tools (Windows clipboard history, cloud clipboard, clipboard
  managers) open the clipboard to read every new text, and a long dictation
  takes them longer to read. An app that finds the clipboard locked at that
  moment usually gives up instead of retrying.
* A key of the shortcut that is still held turns Ctrl+V into, say, Ctrl+Alt+V.
* A chord sent without scan codes and without any hold can be missed by apps
  that read scan codes or sample the key state.

So, like Handy, OpenWhispr and VoiceInk: wait until the modifiers are up and
the clipboard is free, then send Ctrl+V through SendInput with scan codes and
short pauses between the key events.

The shortcut itself can also move the keyboard focus. RegisterHotKey swallows
the Z of Alt+Z but lets Alt through, so when Alt goes up after Z the active
window sees a lone Alt. Electron apps with a menu bar, such as Signal, then
put the focus on that (often hidden) menu bar; the next Alt press gives it
back. So the stop press usually undid the start press, but after a click into
a text field during the recording the stop press took the focus away from it
and Ctrl+V went to the menu bar.
mask_menu_release() taps an unassigned key while Alt is still down, so no
press of the shortcut is a lone Alt (the AutoHotkey menu mask).
"""

from __future__ import annotations

import ctypes
import logging
import os
import time
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from typing import Protocol

VK_CONTROL = 0x11
VK_V = 0x56
# Unassigned virtual key. Tapping it while Alt or Win is down keeps their
# release from opening the menu bar or the Start menu (the AutoHotkey trick).
VK_MASK = 0xE8
VK_LSHIFT, VK_RSHIFT, VK_LCONTROL, VK_RCONTROL, VK_LMENU, VK_RMENU = range(0xA0, 0xA6)
VK_LWIN, VK_RWIN = 0x5B, 0x5C
MODIFIER_KEYS = (VK_LSHIFT, VK_RSHIFT, VK_LCONTROL, VK_RCONTROL, VK_LMENU, VK_RMENU, VK_LWIN, VK_RWIN)
# Released alone, these open the menu bar or the Start menu.
MENU_KEYS = frozenset({VK_LMENU, VK_RMENU, VK_LWIN, VK_RWIN})
EXTENDED_KEYS = frozenset({VK_RCONTROL, VK_RMENU, VK_LWIN, VK_RWIN})

MODIFIER_RELEASE_TIMEOUT_SECONDS = 1.5
# Clipboard tools react to the clipboard update a few milliseconds after the
# copy; probing before that would find a clipboard that is free only for now.
CLIPBOARD_SETTLE_SECONDS = 0.08
CLIPBOARD_FREE_TIMEOUT_SECONDS = 0.6
CLIPBOARD_FREE_PROBES = 3
POLL_SECONDS = 0.01
KEY_STEP_SECONDS = 0.025

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
MAPVK_VK_TO_VSC = 0
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class INPUTUNION(ctypes.Union):
    # MOUSEINPUT is the largest member; without it the struct is too small and
    # SendInput rejects every event.
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", INPUTUNION)]


class KeyboardApi(Protocol):
    def held_modifiers(self) -> tuple[int, ...]: ...
    def clipboard_free(self) -> bool: ...
    def send_key(self, vk: int, up: bool) -> None: ...
    def foreground_app(self) -> str: ...


@dataclass(frozen=True)
class PasteReport:
    target: str
    waited_ms: int
    clipboard_free: bool
    released_modifiers: tuple[int, ...]


def paste_clipboard(
    api: KeyboardApi | None = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> PasteReport:
    """Send Ctrl+V to the foreground window once it can receive it."""
    api = api or WindowsKeyboard()
    started = clock()

    held = _wait(api.held_modifiers, lambda keys: not keys, MODIFIER_RELEASE_TIMEOUT_SECONDS, sleep, clock)
    sleep(max(0.0, CLIPBOARD_SETTLE_SECONDS - (clock() - started)))
    clipboard_free = _wait_for_free_clipboard(api, sleep, clock)

    try:
        target = api.foreground_app()
    except Exception:
        target = ""
    if held:
        # The user is still holding a shortcut key after a long wait: release it
        # rather than send Ctrl+Alt+V or Win+Ctrl+V.
        events = [(VK_MASK, False), (VK_MASK, True), *((key, True) for key in held)]
    else:
        events = []
    events += [(VK_CONTROL, False), (VK_V, False), (VK_V, True), (VK_CONTROL, True)]
    for index, (vk, up) in enumerate(events):
        if index:
            sleep(KEY_STEP_SECONDS)
        api.send_key(vk, up)

    report = PasteReport(target, round((clock() - started) * 1000), clipboard_free, tuple(held))
    logging.info(
        "Ctrl+V naar %s na %d ms%s%s", report.target or "onbekend venster", report.waited_ms,
        "" if clipboard_free else "; klembord bleef bezet",
        f"; vastgehouden toetsen losgelaten: {', '.join(hex(key) for key in held)}" if held else "",
    )
    return report


def mask_menu_release(api: KeyboardApi | None = None) -> bool:
    """Tap VK_MASK while Alt or Win is down, so releasing it opens no menu.

    Call it as soon as the shortcut fires: once Alt is up, the active window
    has already seen a lone Alt.
    """
    api = api or WindowsKeyboard()
    if MENU_KEYS.isdisjoint(api.held_modifiers()):
        return False
    api.send_key(VK_MASK, False)
    api.send_key(VK_MASK, True)
    return True


def _wait(read, done, timeout, sleep, clock):
    deadline = clock() + timeout
    value = read()
    while not done(value) and clock() < deadline:
        sleep(POLL_SECONDS)
        value = read()
    return value


def _wait_for_free_clipboard(api: KeyboardApi, sleep, clock) -> bool:
    """True once the clipboard was free on a few probes in a row."""
    deadline = clock() + CLIPBOARD_FREE_TIMEOUT_SECONDS
    streak = 0
    while True:
        streak = streak + 1 if api.clipboard_free() else 0
        if streak >= CLIPBOARD_FREE_PROBES:
            return True
        if clock() >= deadline:
            return False
        sleep(POLL_SECONDS)


class WindowsKeyboard:
    """The Win32 calls behind paste_clipboard()."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Automatisch plakken werkt alleen op Windows.")
        self.user32 = user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        user32.GetAsyncKeyState.restype = ctypes.c_short
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.CloseClipboard.restype = wintypes.BOOL
        user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
        user32.MapVirtualKeyW.restype = wintypes.UINT
        user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        user32.SendInput.restype = wintypes.UINT
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    def held_modifiers(self) -> tuple[int, ...]:
        return tuple(key for key in MODIFIER_KEYS if self.user32.GetAsyncKeyState(key) & 0x8000)

    def clipboard_free(self) -> bool:
        # Opening and closing without emptying leaves the content untouched.
        if not self.user32.OpenClipboard(None):
            return False
        self.user32.CloseClipboard()
        return True

    def send_key(self, vk: int, up: bool) -> None:
        event = INPUT(type=INPUT_KEYBOARD)
        event.union.ki = KEYBDINPUT(
            wVk=vk,
            wScan=self.user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC),
            dwFlags=(KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if vk in EXTENDED_KEYS else 0),
        )
        if self.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT)) != 1:
            raise ctypes.WinError(ctypes.get_last_error())

    def foreground_app(self) -> str:
        """Executable name of the foreground window, for the log."""
        pid = wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(self.user32.GetForegroundWindow(), ctypes.byref(pid))
        process = self.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not process:
            return ""
        try:
            size = wintypes.DWORD(1024)
            path = ctypes.create_unicode_buffer(size.value)
            if not self.kernel32.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)):
                return ""
            return path.value.rsplit("\\", 1)[-1]
        finally:
            self.kernel32.CloseHandle(process)
