"""End-to-end check on a real Windows desktop: tray app, hotkey, microphone, bubble, paste.

Opt-in (it moves the mouse, presses keys and opens windows):
    set GROQ_DESKTOP_E2E=1 and run inside an interactive session.
Groq is replaced by a local fake; the real microphone, Win32 hotkey, Qt tray,
status bubble and Ctrl+V into another process are used. Settings, history and
secrets live in a temporary folder and never touch the installed app.
"""
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest import mock

TARGET_SCRIPT = r'''
import pathlib, sys, tkinter as tk
out = pathlib.Path(sys.argv[1])
root = tk.Tk()
root.title("Groq E2E doelvenster")
root.geometry("640x220+80+80")
text = tk.Text(root, font=("Segoe UI", 12))
text.pack(fill="both", expand=True)
text.focus_set()
def dump():
    out.write_text(text.get("1.0", "end-1c"), encoding="utf-8")
    root.after(100, dump)
dump()
root.mainloop()
'''

HOTKEY = "ctrl+alt+shift+f24"
VK = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10, "f24": 0x87}


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("pad", ctypes.c_byte * 32)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def user32():
    lib = ctypes.WinDLL("user32", use_last_error=True)
    lib.GetForegroundWindow.restype = wintypes.HWND
    lib.FindWindowW.restype = wintypes.HWND
    lib.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    lib.GetAncestor.restype = wintypes.HWND
    lib.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    lib.SetForegroundWindow.argtypes = [wintypes.HWND]
    lib.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    lib.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    lib.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    lib.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    return lib


def send_keys(*codes: int, up: bool = False) -> None:
    lib = user32()
    for code in codes:
        item = INPUT(type=1)
        item.ki = KEYBDINPUT(code, 0, 0x0002 if up else 0, 0, 0)
        lib.SendInput(1, ctypes.byref(item), ctypes.sizeof(INPUT))
        time.sleep(0.02)


def press_hotkey() -> None:
    modifiers = [VK["ctrl"], VK["alt"], VK["shift"]]
    send_keys(*modifiers)
    send_keys(VK["f24"])
    send_keys(VK["f24"], up=True)
    send_keys(*reversed(modifiers), up=True)


def click(x: int, y: int) -> None:
    lib = user32()
    lib.SetCursorPos(x, y)
    time.sleep(0.05)
    for flag in (0x0002, 0x0004):  # left down, left up
        item = INPUT(type=0)
        item.mi = MOUSEINPUT(0, 0, 0, flag, 0, 0)
        lib.SendInput(1, ctypes.byref(item), ctypes.sizeof(INPUT))
        time.sleep(0.03)


def foreground_root() -> int:
    lib = user32()
    hwnd = lib.GetForegroundWindow()
    return int(lib.GetAncestor(hwnd, 2) or 0) if hwnd else 0  # GA_ROOT


def wait_for(predicate, timeout: float, step: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return bool(predicate())


@unittest.skipUnless(os.name == "nt" and os.getenv("GROQ_DESKTOP_E2E") == "1", "opt-in Windows desktop test")
class DesktopEndToEndTests(unittest.TestCase):
    def test_hotkey_dictation_and_bubble_click_paste_into_the_other_window(self):
        out_dir = Path(os.getenv("GROQ_E2E_OUT", tempfile.mkdtemp(prefix="groq-e2e-")))
        out_dir.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="groq-e2e-work-"))
        target_text = work / "target.txt"
        (work / "target.py").write_text(TARGET_SCRIPT, encoding="utf-8")
        target = subprocess.Popen([sys.executable, str(work / "target.py"), str(target_text)])
        self.addCleanup(target.kill)
        lib = user32()
        self.assertTrue(wait_for(lambda: lib.FindWindowW(None, "Groq E2E doelvenster"), 10), "target window")
        target_hwnd = int(lib.GetAncestor(lib.FindWindowW(None, "Groq E2E doelvenster"), 2))

        def focus_target() -> bool:
            send_keys(0x12)  # A key press grants this process the right to switch the foreground.
            send_keys(0x12, up=True)
            lib.SetForegroundWindow(target_hwnd)
            return wait_for(lambda: foreground_root() == target_hwnd, 3)

        self.assertTrue(focus_target(), "target window did not become foreground")

        from qt_support import qt_app

        qt = qt_app()
        import app
        import config_store
        import engine

        texts = iter(["Eerste dictaat via sneltoets.", "Tweede dictaat, gestopt met de bubbel."])

        class FakeGroq:
            def __init__(self, api_key):
                self.audio = mock.Mock()
                self.audio.transcriptions.create.side_effect = lambda **kwargs: mock.Mock(text=next(texts))

        data = work / "appdata"
        data.mkdir()
        (data / "settings.json").write_text(json.dumps({
            "shortcut": HOTKEY, "autostart": False, "input_device": "", "language": "nl",
        }), encoding="utf-8")
        patches = [
            mock.patch.object(config_store, "APP_DIR", data),
            mock.patch.object(config_store, "SETTINGS_PATH", data / "settings.json"),
            mock.patch.object(config_store, "HISTORY_PATH", data / "history.json"),
            mock.patch.object(config_store, "RECORDINGS_DIR", data / "recordings"),
            mock.patch.object(config_store, "LOG_PATH", data / "app.log"),
            mock.patch.object(config_store, "SOUNDS_DIR", data / "sounds"),
            mock.patch.object(config_store, "try_read_api_key_from_keyring", return_value=(True, "local-fake-key")),
            mock.patch.object(config_store, "write_api_key_to_keyring", return_value=True),
            mock.patch.object(app, "set_autostart"),
            mock.patch.object(engine, "Groq", FakeGroq),
            mock.patch.object(engine, "play_sound"),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        tray = app.TrayApp()
        results: dict = {"foreground": [], "errors": []}

        def grab_bubble(name: str) -> None:
            from PIL import ImageGrab

            hwnd = int(tray.bubble.winId())
            rect = wintypes.RECT()
            lib.GetWindowRect(hwnd, ctypes.byref(rect))
            pad = 40
            box = (rect.left - pad, rect.top - pad, rect.right + pad, rect.bottom + pad)
            ImageGrab.grab(bbox=box, all_screens=True).save(out_dir / f"bubble-{name}.png")

        def on_main(function):
            done = threading.Event()
            box = {}

            def run():
                try:
                    box["value"] = function()
                finally:
                    done.set()

            tray.bridge.run_on_main(run)
            done.wait(5)
            return box.get("value")

        def scenario() -> None:
            try:
                self.assertTrue(wait_for(lambda: tray.startup_finished, 15), "startup")
                self.assertTrue(focus_target())
                # 1. Hotkey starts, hotkey stops; the transcript is pasted into the target.
                press_hotkey()
                self.assertTrue(wait_for(lambda: tray.engine.state == "recording", 5), "recording via hotkey")
                self.assertTrue(wait_for(lambda: tray.bubble.isVisible(), 2))
                time.sleep(0.8)
                results["foreground"].append(("recording", foreground_root() == target_hwnd))
                hwnd = int(tray.bubble.winId())
                results["bubble_noactivate"] = bool(lib.GetWindowLongPtrW(hwnd, -20) & 0x08000000)
                on_main(lambda: grab_bubble("recording"))
                time.sleep(1.0)
                press_hotkey()
                self.assertTrue(wait_for(lambda: target_text.read_text(encoding="utf-8").startswith("Eerste dictaat"), 10),
                                f"first paste, target has {target_text.read_text(encoding='utf-8')!r}")
                results["foreground"].append(("after first paste", foreground_root() == target_hwnd))
                self.assertTrue(wait_for(lambda: tray.engine.state == "idle", 5))
                # 2. Hotkey starts, a click on the bubble stops; focus must stay in the target.
                press_hotkey()
                self.assertTrue(wait_for(lambda: tray.engine.state == "recording", 5), "second recording")
                time.sleep(1.6)
                rect = wintypes.RECT()
                lib.GetWindowRect(int(tray.bubble.winId()), ctypes.byref(rect))
                click((rect.left + rect.right) // 2 + 60, (rect.top + rect.bottom) // 2)
                results["foreground"].append(("after bubble click", foreground_root() == target_hwnd))
                self.assertTrue(wait_for(lambda: tray.engine.state in {"processing", "idle"}, 3), "bubble click stops")
                if tray.engine.state == "processing":
                    on_main(lambda: grab_bubble("processing"))
                self.assertTrue(wait_for(lambda: "Tweede dictaat" in target_text.read_text(encoding="utf-8"), 10),
                                f"second paste, target has {target_text.read_text(encoding='utf-8')!r}")
                results["target_text"] = target_text.read_text(encoding="utf-8")
                results["recordings"] = [entry.status for entry in tray.recordings.entries]
                results["history"] = [entry.text for entry in tray.history.entries]
                results["frames"] = [round(entry.duration, 2) for entry in tray.recordings.entries]
            except Exception as exc:  # Report on the main thread.
                results["errors"].append(repr(exc))
            finally:
                tray.bridge.run_on_main(tray.quit)

        worker = threading.Thread(target=scenario, daemon=True)
        worker.start()
        tray.run()
        worker.join(5)
        (out_dir / "desktop-e2e.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(results["errors"], [])
        self.assertTrue(results["bubble_noactivate"])
        self.assertTrue(all(ok for _name, ok in results["foreground"]), results["foreground"])
        self.assertEqual(results["target_text"], "Eerste dictaat via sneltoets. Tweede dictaat, gestopt met de bubbel. ")
        self.assertEqual(results["recordings"], ["done", "done"])
        self.assertTrue(all(duration > 1.0 for duration in results["frames"]), results["frames"])
        qt.processEvents()


if __name__ == "__main__":
    unittest.main()
