import os
import threading
import unittest
from types import SimpleNamespace

import hotkeys
from hotkeys import HotkeyError, HotkeyListener, hotkey_from_tk_event, normalize_hotkey_text, parse_hotkey


class ParseHotkeyTests(unittest.TestCase):
    def test_existing_settings_values_keep_their_meaning(self) -> None:
        for text, expected, virtual_key in (
            ("insert", "insert", 0x2D),
            ("alt+z", "alt+z", 0x5A),
            ("ctrl+shift+f9", "ctrl+shift+f9", 0x78),
            ("page down", "page down", 0x22),
            ("windows+space", "windows+space", 0x20),
        ):
            hotkey = parse_hotkey(text)
            self.assertEqual(str(hotkey), expected)
            self.assertEqual(hotkey.virtual_key, virtual_key)

    def test_user_spelling_is_normalized(self) -> None:
        self.assertEqual(normalize_hotkey_text(" Ctrl + Shift + F9 "), "ctrl+shift+f9")
        self.assertEqual(normalize_hotkey_text("Control-Z"), "ctrl+z")
        self.assertEqual(normalize_hotkey_text("shift+alt+ctrl+a"), "ctrl+alt+shift+a")
        self.assertEqual(normalize_hotkey_text("Alt+Page_Down"), "alt+page down")
        self.assertEqual(normalize_hotkey_text("ctrl++"), "ctrl+=")
        self.assertEqual(normalize_hotkey_text(""), "")

    def test_modifier_flags_match_win32_constants(self) -> None:
        hotkey = parse_hotkey("ctrl+alt+shift+windows+k")
        self.assertEqual(hotkey.modifier_flags, 0x0002 | 0x0001 | 0x0004 | 0x0008)

    def test_invalid_shortcuts_raise_a_dutch_error(self) -> None:
        for text in ("alt", "ctrl+shift", "a+b", "ctrl+onbekend"):
            with self.assertRaises(HotkeyError):
                parse_hotkey(text)
        with self.assertRaisesRegex(HotkeyError, "Vul eerst"):
            parse_hotkey("")


class TkEventTests(unittest.TestCase):
    @staticmethod
    def event(keysym: str, state: int, keycode: int = 0, char: str = "") -> SimpleNamespace:
        return SimpleNamespace(keysym=keysym, state=state, keycode=keycode, char=char)

    def test_lone_modifier_is_ignored(self) -> None:
        self.assertIsNone(hotkey_from_tk_event(self.event("Alt_L", 0)))
        self.assertIsNone(hotkey_from_tk_event(self.event("Control_L", 0x4)))

    def test_windows_alt_bit_is_recognized(self) -> None:
        event = self.event("z", hotkeys.TK_STATE_ALT_WINDOWS, keycode=0x5A, char="z")
        self.assertEqual(hotkey_from_tk_event(event), "alt+z")

    @unittest.skipUnless(os.name == "nt", "Num Lock is only reported as Mod1 on Windows")
    def test_num_lock_does_not_become_alt_on_windows(self) -> None:
        event = self.event("Delete", hotkeys.TK_STATE_MOD1, keycode=0x2E)
        self.assertEqual(hotkey_from_tk_event(event), "delete")

    def test_control_and_shift_bits(self) -> None:
        event = self.event("F9", 0x4 | 0x1, keycode=0x78)
        self.assertEqual(hotkey_from_tk_event(event), "ctrl+shift+f9")


class ListenerTests(unittest.TestCase):
    def test_dispatch_runs_callback_off_the_loop_thread_and_swallows_errors(self) -> None:
        seen = threading.Event()
        threads: list[str] = []

        def callback() -> None:
            threads.append(threading.current_thread().name)
            seen.set()
            raise RuntimeError("boom")

        listener = HotkeyListener(callback)
        with self.assertLogs(level="ERROR"):
            listener._dispatch()
            self.assertTrue(seen.wait(2))
        self.assertNotEqual(threads[0], threading.current_thread().name)

    @unittest.skipIf(os.name == "nt", "non-Windows fallback only")
    def test_non_windows_listener_validates_without_registering(self) -> None:
        listener = HotkeyListener(lambda: None)
        listener.start()
        self.assertEqual(str(listener.set_hotkey("alt+z")), "alt+z")
        with self.assertRaises(HotkeyError):
            listener.set_hotkey("alt")
        listener.clear()
        self.assertIsNone(listener.current)
        listener.stop()

    @unittest.skipUnless(os.name == "nt", "Windows integration test")
    def test_windows_listener_registers_and_releases_a_hotkey(self) -> None:
        listener = HotkeyListener(lambda: None)
        listener.start()
        try:
            try:
                hotkey = listener.set_hotkey("ctrl+alt+shift+f24")
            except HotkeyError as exc:
                if "interactieve Windows-sessie" in str(exc):
                    self.skipTest("RegisterHotKey needs an interactive desktop (not available over SSH)")
                raise
            self.assertEqual(str(hotkey), "ctrl+alt+shift+f24")
            self.assertEqual(listener.current, hotkey)
            listener.clear()
            self.assertIsNone(listener.current)
        finally:
            listener.stop()


if __name__ == "__main__":
    unittest.main()
