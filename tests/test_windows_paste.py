"""Ctrl+V timing without a desktop: a fake keyboard and a fake clock."""
import ctypes
import os
import unittest

import windows_paste
from windows_paste import (
    VK_CONTROL, VK_LMENU, VK_LSHIFT, VK_LWIN, VK_MASK, VK_RCONTROL, VK_RMENU, VK_V, mask_menu_release, paste_clipboard,
)

CHORD = [(VK_CONTROL, False), (VK_V, False), (VK_V, True), (VK_CONTROL, True)]


class FakeKeyboard:
    """Modifiers and clipboard change state at given (fake) times."""

    def __init__(self, clock, modifiers=(), modifiers_until=0.0, clipboard_busy=()):
        self.clock = clock
        self.modifiers = tuple(modifiers)
        self.modifiers_until = modifiers_until
        self.clipboard_busy = clipboard_busy  # (start, end) spans in seconds
        self.events = []
        self.event_times = []

    def held_modifiers(self):
        return self.modifiers if self.clock.now < self.modifiers_until else ()

    def clipboard_free(self):
        return not any(start <= self.clock.now < end for start, end in self.clipboard_busy)

    def send_key(self, vk, up):
        self.events.append((vk, up))
        self.event_times.append(self.clock.now)

    def foreground_app(self):
        return "Signal.exe"


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class PasteTimingTests(unittest.TestCase):
    def paste(self, **state):
        clock = FakeClock()
        keyboard = FakeKeyboard(clock, **state)
        with self.assertLogs(level="INFO") as logs:
            report = paste_clipboard(keyboard, sleep=clock.sleep, clock=clock)
        return keyboard, report, "\n".join(logs.output)

    def test_ctrl_v_waits_for_clipboard_tools_and_holds_each_key(self):
        keyboard, report, log = self.paste()

        self.assertEqual(keyboard.events, CHORD)
        self.assertGreaterEqual(keyboard.event_times[0], windows_paste.CLIPBOARD_SETTLE_SECONDS)
        steps = [b - a for a, b in zip(keyboard.event_times, keyboard.event_times[1:])]
        self.assertTrue(all(step >= windows_paste.KEY_STEP_SECONDS - 1e-9 for step in steps))
        self.assertTrue(report.clipboard_free)
        self.assertEqual(report.released_modifiers, ())
        self.assertIn("Ctrl+V naar Signal.exe", log)

    def test_waits_until_the_shortcut_keys_are_released(self):
        keyboard, report, _ = self.paste(modifiers=(VK_LMENU,), modifiers_until=0.4)

        self.assertEqual(keyboard.events, CHORD)
        self.assertGreaterEqual(keyboard.event_times[0], 0.4)
        self.assertEqual(report.released_modifiers, ())

    def test_releases_keys_still_held_after_the_timeout_without_opening_start(self):
        keyboard, report, log = self.paste(modifiers=(VK_LWIN, VK_RCONTROL), modifiers_until=60)

        self.assertEqual(
            keyboard.events,
            [(VK_MASK, False), (VK_MASK, True), (VK_LWIN, True), (VK_RCONTROL, True), *CHORD],
        )
        self.assertGreaterEqual(keyboard.event_times[0], windows_paste.MODIFIER_RELEASE_TIMEOUT_SECONDS)
        self.assertEqual(report.released_modifiers, (VK_LWIN, VK_RCONTROL))
        self.assertIn("vastgehouden toetsen losgelaten", log)

    def test_waits_while_a_clipboard_tool_reads_a_long_text(self):
        keyboard, report, _ = self.paste(clipboard_busy=[(0.05, 0.3)])

        self.assertEqual(keyboard.events, CHORD)
        self.assertGreaterEqual(keyboard.event_times[0], 0.3)
        self.assertTrue(report.clipboard_free)

    def test_a_clipboard_that_is_free_only_for_a_moment_does_not_count(self):
        keyboard, _, _ = self.paste(clipboard_busy=[(0.0, 0.1), (0.11, 0.2)])

        self.assertGreaterEqual(keyboard.event_times[0], 0.2)

    def test_pastes_anyway_when_the_clipboard_stays_locked(self):
        keyboard, report, log = self.paste(clipboard_busy=[(0.0, 60)])

        self.assertEqual(keyboard.events, CHORD)
        self.assertFalse(report.clipboard_free)
        self.assertLess(keyboard.event_times[0], 1.0)
        self.assertIn("klembord bleef bezet", log)

    def test_unknown_foreground_app_does_not_stop_the_paste(self):
        clock = FakeClock()
        keyboard = FakeKeyboard(clock)
        keyboard.foreground_app = lambda: (_ for _ in ()).throw(OSError("access denied"))
        with self.assertLogs(level="INFO"):
            report = paste_clipboard(keyboard, sleep=clock.sleep, clock=clock)
        self.assertEqual(keyboard.events, CHORD)
        self.assertEqual(report.target, "")


class MenuMaskTests(unittest.TestCase):
    """Alt+Z must not leave the active window with a lone Alt release."""

    def mask(self, *held):
        keyboard = FakeKeyboard(FakeClock(), modifiers=held, modifiers_until=1)
        return keyboard, mask_menu_release(keyboard)

    def test_taps_the_mask_key_while_alt_or_win_is_down(self):
        for key in (VK_LMENU, VK_RMENU, VK_LWIN):
            keyboard, masked = self.mask(key, VK_LSHIFT)
            self.assertTrue(masked)
            self.assertEqual(keyboard.events, [(VK_MASK, False), (VK_MASK, True)])

    def test_sends_nothing_for_ctrl_or_shift_or_released_keys(self):
        for held in ((), (VK_RCONTROL,), (VK_LSHIFT,)):
            keyboard, masked = self.mask(*held)
            self.assertFalse(masked)
            self.assertEqual(keyboard.events, [])


@unittest.skipUnless(os.name == "nt", "Win32 structure layout")
class WindowsStructureTests(unittest.TestCase):
    def test_input_has_the_size_sendinput_expects(self):
        self.assertEqual(ctypes.sizeof(windows_paste.INPUT), 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)

    def test_keyboard_api_reads_state_without_changing_the_clipboard(self):
        keyboard = windows_paste.WindowsKeyboard()
        self.assertIsInstance(keyboard.held_modifiers(), tuple)
        self.assertIsInstance(keyboard.foreground_app(), str)
        sequence = ctypes.windll.user32.GetClipboardSequenceNumber()
        keyboard.clipboard_free()
        self.assertEqual(ctypes.windll.user32.GetClipboardSequenceNumber(), sequence)


if __name__ == "__main__":
    unittest.main()
