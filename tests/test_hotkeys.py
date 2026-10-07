import os
import threading
import unittest

from hotkeys import HotkeyError, HotkeyListener, hotkey_from_key_press, normalize_hotkey_text, parse_hotkey


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


class KeyPressTests(unittest.TestCase):
    def test_lone_modifier_is_ignored(self) -> None:
        self.assertIsNone(hotkey_from_key_press(key_name="alt", alt=True))
        self.assertIsNone(hotkey_from_key_press(key_name="ctrl", ctrl=True))
        self.assertIsNone(hotkey_from_key_press(key_name="", virtual_key=0xA2, ctrl=True))  # VK_LCONTROL

    def test_modifiers_are_ordered_like_saved_settings(self) -> None:
        self.assertEqual(hotkey_from_key_press(key_name="z", alt=True), "alt+z")
        self.assertEqual(hotkey_from_key_press(key_name="f9", virtual_key=0x78, ctrl=True, shift=True), "ctrl+shift+f9")
        self.assertEqual(hotkey_from_key_press(key_name="space", shift=True, windows=True, ctrl=True), "ctrl+shift+windows+space")

    def test_single_keys_like_insert_and_f9_work_without_modifiers(self) -> None:
        self.assertEqual(hotkey_from_key_press(key_name="insert", virtual_key=0x2D), "insert")
        self.assertEqual(hotkey_from_key_press(key_name="f9"), "f9")

    @unittest.skipUnless(os.name == "nt", "virtual-key codes are Windows-specific")
    def test_virtual_key_wins_over_the_shifted_symbol(self) -> None:
        # Shift+1 types "!" on most layouts; the shortcut is still shift+1.
        self.assertEqual(hotkey_from_key_press(key_name="!", virtual_key=0x31, shift=True), "shift+1")
        self.assertEqual(hotkey_from_key_press(key_name="kp 5", virtual_key=0x65), "kp 5")

    def test_unknown_keys_are_ignored(self) -> None:
        self.assertIsNone(hotkey_from_key_press(key_name=""))
        self.assertIsNone(hotkey_from_key_press(key_name="volume knob"))


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
