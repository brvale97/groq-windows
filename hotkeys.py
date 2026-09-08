"""Global shortcut handling built on the Win32 ``RegisterHotKey`` API.

Why not a low-level keyboard hook (the ``keyboard`` package)?

* Windows silently removes a ``WH_KEYBOARD_LL`` hook whose callback is slow
  (``LowLevelHooksTimeout``, 300 ms by default). Opening a microphone or
  sleeping inside the hook callback is therefore a time bomb: the shortcut
  stops working until the process restarts, without any error.
* Hooks that suppress keys must track modifier state themselves. A missed
  key-up (lock screen, UAC prompt, elevated window) leaves that state stuck.
* Hooks in a normal process do not receive keys while an elevated window has
  focus. ``RegisterHotKey`` is handled by the system and keeps working.

``RegisterHotKey`` gives us a shortcut that is consumed by the OS (so it is
never typed into the active window) and delivered as ``WM_HOTKEY`` to a
message loop that we own. The listener runs that loop on a dedicated thread
and hands every activation to a fresh worker thread, so the loop itself
never blocks.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass


MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_QUIT = 0x0012
WM_HOTKEY = 0x0312
WM_APP = 0x8000
WM_APPLY_HOTKEY = WM_APP + 1
PM_NOREMOVE = 0x0000
ERROR_HOTKEY_ALREADY_REGISTERED = 1409
ERROR_REQUIRES_INTERACTIVE_WINDOWSTATION = 1459

HOTKEY_ID = 1

MODIFIER_ORDER = ("ctrl", "alt", "shift", "windows")
MODIFIER_FLAGS = {
    "ctrl": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "windows": MOD_WIN,
}

# Spellings that users, ``keyboard``-style settings files, or Tk keysyms may
# use for the same modifier or key.
ALIASES = {
    "control": "ctrl",
    "ctl": "ctrl",
    "strg": "ctrl",
    "option": "alt",
    "menu": "alt",
    "win": "windows",
    "cmd": "windows",
    "super": "windows",
    "meta": "windows",
    "return": "enter",
    "escape": "esc",
    "del": "delete",
    "ins": "insert",
    "pgup": "page up",
    "pageup": "page up",
    "prior": "page up",
    "pgdn": "page down",
    "pagedown": "page down",
    "next": "page down",
    "spacebar": "space",
    "back": "backspace",
    "print": "print screen",
    "printscreen": "print screen",
    "snapshot": "print screen",
    "scrolllock": "scroll lock",
    "capslock": "caps lock",
    "numlock": "num lock",
    "app": "apps",
    "application": "apps",
    "context menu": "apps",
    "plus": "=",
    "+": "=",
    "minus": "-",
    "equal": "=",
    "bracketleft": "[",
    "bracketright": "]",
    "semicolon": ";",
    "apostrophe": "'",
    "quoteright": "'",
    "grave": "`",
    "quoteleft": "`",
    "backslash": "\\",
    "comma": ",",
    "period": ".",
    "slash": "/",
    "decimal": ".",
}

NAMED_KEYS = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "pause": 0x13,
    "caps lock": 0x14,
    "esc": 0x1B,
    "space": 0x20,
    "page up": 0x21,
    "page down": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "print screen": 0x2C,
    "insert": 0x2D,
    "delete": 0x2E,
    "apps": 0x5D,
    "num lock": 0x90,
    "scroll lock": 0x91,
    "volume mute": 0xAD,
    "volume down": 0xAE,
    "volume up": 0xAF,
    "next track": 0xB0,
    "previous track": 0xB1,
    "stop media": 0xB2,
    "play/pause media": 0xB3,
}
NAMED_KEYS.update({f"f{number}": 0x6F + number for number in range(1, 25)})
NAMED_KEYS.update({str(digit): 0x30 + digit for digit in range(10)})
NAMED_KEYS.update({letter: ord(letter.upper()) for letter in "abcdefghijklmnopqrstuvwxyz"})
for _prefix in ("kp", "num", "numpad"):
    NAMED_KEYS.update({f"{_prefix} {digit}": 0x60 + digit for digit in range(10)})
    NAMED_KEYS.update(
        {
            f"{_prefix} multiply": 0x6A,
            f"{_prefix} add": 0x6B,
            f"{_prefix} subtract": 0x6D,
            f"{_prefix} decimal": 0x6E,
            f"{_prefix} divide": 0x6F,
            f"{_prefix} enter": 0x0D,
        }
    )

# US layout fallback for punctuation when VkKeyScanW is unavailable (tests on
# non-Windows machines). On Windows the current layout is consulted first.
US_PUNCTUATION = {
    ";": 0xBA,
    "=": 0xBB,
    ",": 0xBC,
    "-": 0xBD,
    ".": 0xBE,
    "/": 0xBF,
    "`": 0xC0,
    "[": 0xDB,
    "\\": 0xDC,
    "]": 0xDD,
    "'": 0xDE,
}


class HotkeyError(ValueError):
    """Raised when a shortcut cannot be parsed or registered."""


@dataclass(frozen=True)
class Hotkey:
    modifiers: tuple[str, ...]
    key: str
    virtual_key: int

    @property
    def modifier_flags(self) -> int:
        flags = 0
        for modifier in self.modifiers:
            flags |= MODIFIER_FLAGS[modifier]
        return flags

    def __str__(self) -> str:
        return "+".join((*self.modifiers, self.key))


def _normalize_part(part: str) -> str:
    cleaned = part.strip().lower().replace("_", " ")
    cleaned = " ".join(cleaned.split())
    return ALIASES.get(cleaned, cleaned)


def split_hotkey_text(value: str) -> list[str]:
    """Split ``ctrl+shift+z`` (or ``ctrl-z``) into parts, keeping a literal ``+`` key."""
    text = str(value).strip()
    if not text:
        return []
    separator = "+" if "+" in text else "-" if len(text) > 1 else ""
    if not separator:
        return [text]
    parts: list[str] = []
    current = ""
    for character in text:
        if character == separator and current.strip():
            parts.append(current)
            current = ""
        else:
            current += character
    if current.strip():
        parts.append(current)
    elif text.endswith(separator) and parts:
        # "ctrl++" or "ctrl+" means the separator character itself is the key.
        parts.append(separator)
    return parts


def _virtual_key_for_character(character: str) -> int | None:
    if os.name == "nt":
        import ctypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.VkKeyScanW.argtypes = [ctypes.c_wchar]
        user32.VkKeyScanW.restype = ctypes.c_short
        result = user32.VkKeyScanW(character)
        if result != -1:
            return result & 0xFF
    return US_PUNCTUATION.get(character)


def parse_hotkey(value: str) -> Hotkey:
    """Turn user text such as ``Ctrl + Shift + F9`` into a registrable hotkey."""
    parts = [_normalize_part(part) for part in split_hotkey_text(str(value))]
    parts = [part for part in parts if part]
    if not parts:
        raise HotkeyError("Vul eerst een shortcut in.")

    modifiers: list[str] = []
    keys: list[str] = []
    for part in parts:
        if part in MODIFIER_FLAGS:
            if part not in modifiers:
                modifiers.append(part)
        else:
            keys.append(part)

    if not keys:
        raise HotkeyError(
            "Een shortcut heeft naast Ctrl, Alt, Shift of Windows ook een gewone toets nodig, bijvoorbeeld alt+z."
        )
    if len(keys) > 1:
        raise HotkeyError(
            f"Een shortcut kan maar één gewone toets bevatten. Gevonden: {', '.join(keys)}."
        )

    key = keys[0]
    virtual_key = NAMED_KEYS.get(key)
    if virtual_key is None and len(key) == 1:
        virtual_key = _virtual_key_for_character(key)
    if virtual_key is None:
        raise HotkeyError(f"De toets '{key}' wordt niet herkend als shortcut-toets.")

    ordered = tuple(modifier for modifier in MODIFIER_ORDER if modifier in modifiers)
    return Hotkey(modifiers=ordered, key=key, virtual_key=virtual_key)


def normalize_hotkey_text(value: str) -> str:
    """Canonical spelling of a shortcut; returns an empty string for empty input."""
    if not str(value).strip():
        return ""
    return str(parse_hotkey(value))


def validate_hotkey(value: str) -> Hotkey:
    return parse_hotkey(value)


TK_KEYSYM_ALIASES = {
    "Return": "enter",
    "KP_Enter": "enter",
    "Escape": "esc",
    "BackSpace": "backspace",
    "Delete": "delete",
    "Insert": "insert",
    "Tab": "tab",
    "space": "space",
    "Prior": "page up",
    "Next": "page down",
    "Home": "home",
    "End": "end",
    "Up": "up",
    "Down": "down",
    "Left": "left",
    "Right": "right",
    "Pause": "pause",
    "Print": "print screen",
    "Snapshot": "print screen",
    "Scroll_Lock": "scroll lock",
    "Caps_Lock": "caps lock",
    "Num_Lock": "num lock",
    "App": "apps",
    "Menu": "apps",
    "Control_L": "ctrl",
    "Control_R": "ctrl",
    "Shift_L": "shift",
    "Shift_R": "shift",
    "Alt_L": "alt",
    "Alt_R": "alt",
    "Win_L": "windows",
    "Win_R": "windows",
    "Super_L": "windows",
    "Super_R": "windows",
}

TK_STATE_SHIFT = 0x0001
TK_STATE_CONTROL = 0x0004
# Tk reports Alt as Mod1 (0x8) on X11, but on Windows Mod1 means Num Lock and
# Alt is the dedicated ALT_MASK bit (0x20000). Reading 0x8 on Windows turns
# every captured key into "alt+..." whenever Num Lock is on.
TK_STATE_ALT_WINDOWS = 0x20000
TK_STATE_MOD1 = 0x0008


def normalize_tk_key(keysym: str) -> str:
    return _normalize_part(TK_KEYSYM_ALIASES.get(keysym, keysym))


VK_NAMES: dict[int, str] = {}
for _name, _code in NAMED_KEYS.items():
    if _name.startswith(("num ", "numpad ")):
        continue
    VK_NAMES.setdefault(_code, _name)


def hotkey_from_tk_event(event) -> str | None:
    """Build shortcut text from a Tk ``<KeyPress>``; ``None`` for a lone modifier."""
    key = normalize_tk_key(event.keysym)
    if key in MODIFIER_FLAGS:
        return None

    # On Windows Tk exposes the virtual-key code directly, which is exactly
    # what RegisterHotKey needs and is independent of Shift/AltGr symbols.
    keycode = int(getattr(event, "keycode", 0) or 0)
    if os.name == "nt" and keycode in VK_NAMES:
        key = VK_NAMES[keycode]
    elif len(key) != 1 and key not in NAMED_KEYS:
        char = getattr(event, "char", "") or ""
        if len(char) == 1 and char.isprintable() and not char.isspace():
            key = char.lower()

    state = int(event.state)
    parts: list[str] = []
    if state & TK_STATE_CONTROL:
        parts.append("ctrl")
    if state & TK_STATE_ALT_WINDOWS or (os.name != "nt" and state & TK_STATE_MOD1):
        parts.append("alt")
    if state & TK_STATE_SHIFT:
        parts.append("shift")
    parts.append(key)
    try:
        return str(parse_hotkey("+".join(parts)))
    except HotkeyError:
        return None


class HotkeyListener:
    """Own a Win32 message loop that receives ``WM_HOTKEY`` for one shortcut.

    ``set_hotkey`` is safe to call from any thread; it blocks until the loop
    thread has (un)registered the shortcut and raises :class:`HotkeyError`
    when Windows refuses, for example because another program already owns
    the combination. On non-Windows platforms the listener only validates.
    """

    def __init__(self, callback) -> None:
        self.callback = callback
        self.current: Hotkey | None = None
        self._pending: Hotkey | None = None
        self._pending_lock = threading.Lock()
        self._applied = threading.Event()
        self._apply_error: Exception | None = None
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._stopped = False

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if os.name != "nt" or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="hotkey-loop", daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            raise HotkeyError("De shortcut-listener kon niet worden gestart.")

    def stop(self) -> None:
        self._stopped = True
        if os.name != "nt" or self._thread is None or self._thread_id is None:
            self.current = None
            return
        import ctypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        self._thread.join(3)
        self._thread = None
        self.current = None

    # -- registration --------------------------------------------------------

    def set_hotkey(self, value: str | None) -> Hotkey | None:
        hotkey = parse_hotkey(value) if value else None
        if os.name != "nt":
            self.current = hotkey
            return hotkey
        if self._thread is None:
            self.start()
        assert self._thread_id is not None

        import ctypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        with self._pending_lock:
            self._pending = hotkey
            self._apply_error = None
            self._applied.clear()
        if not user32.PostThreadMessageW(self._thread_id, WM_APPLY_HOTKEY, 0, 0):
            raise HotkeyError("De shortcut kon niet worden doorgegeven aan de listener.")
        if not self._applied.wait(5):
            raise HotkeyError("De shortcut-listener reageert niet.")
        if self._apply_error is not None:
            raise self._apply_error
        return self.current

    def clear(self) -> None:
        self.set_hotkey(None)

    # -- loop thread ---------------------------------------------------------

    def _run(self) -> None:  # pragma: no cover - Windows only
        import ctypes
        import ctypes.wintypes as wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class MSG(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM),
                ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD),
                ("pt", wintypes.POINT),
            ]

        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.UnregisterHotKey.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = wintypes.BOOL
        user32.PeekMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
        user32.PeekMessageW.restype = wintypes.BOOL
        user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]

        message = MSG()
        # Force creation of this thread's message queue before publishing the id.
        user32.PeekMessageW(ctypes.byref(message), None, WM_APP, WM_APP, PM_NOREMOVE)
        self._thread_id = kernel32.GetCurrentThreadId()
        self._ready.set()

        registered = False
        try:
            while True:
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result <= 0:
                    break
                if message.message == WM_HOTKEY and int(message.wParam) == HOTKEY_ID:
                    self._dispatch()
                elif message.message == WM_APPLY_HOTKEY:
                    with self._pending_lock:
                        wanted = self._pending
                    if registered:
                        user32.UnregisterHotKey(None, HOTKEY_ID)
                        registered = False
                        self.current = None
                    if wanted is not None:
                        ok = user32.RegisterHotKey(
                            None,
                            HOTKEY_ID,
                            wanted.modifier_flags | MOD_NOREPEAT,
                            wanted.virtual_key,
                        )
                        if ok:
                            registered = True
                            self.current = wanted
                            logging.info("Hotkey registered: %s", wanted)
                        else:
                            code = ctypes.get_last_error()
                            if code == ERROR_HOTKEY_ALREADY_REGISTERED:
                                detail = "een ander programma gebruikt deze toetscombinatie al"
                            elif code == ERROR_REQUIRES_INTERACTIVE_WINDOWSTATION:
                                detail = "de app draait niet in een interactieve Windows-sessie (bijvoorbeeld via SSH of een service)"
                            else:
                                detail = f"Windows-fout {code}"
                            self._apply_error = HotkeyError(
                                f"De shortcut '{wanted}' kon niet worden geactiveerd: {detail}."
                            )
                    self._applied.set()
                else:
                    user32.TranslateMessage(ctypes.byref(message))
                    user32.DispatchMessageW(ctypes.byref(message))
        finally:
            if registered:
                user32.UnregisterHotKey(None, HOTKEY_ID)
            self.current = None
            self._applied.set()

    def _dispatch(self) -> None:
        # Never run user code on the message loop thread: a slow callback
        # would delay the next WM_HOTKEY, and an exception would end the loop.
        def run() -> None:
            try:
                self.callback()
            except Exception:
                logging.exception("Hotkey callback failed")

        threading.Thread(target=run, name="hotkey-callback", daemon=True).start()
