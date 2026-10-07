"""Exercise the real Qt settings window without microphone, credentials or tray.

Runs on Windows (interactive desktop) and elsewhere with QT_QPA_PLATFORM=offscreen.
"""
import time
import unittest
from unittest import mock

from qt_support import qt_app

try:
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication, QPushButton, QWidget
except ImportError:  # pragma: no cover - PySide6 missing
    QApplication = None

from audio_player import PlaybackStatus


def is_open(window) -> bool:
    import shiboken6

    return shiboken6.isValid(window) and window.isVisible()
from microphone_test import MicrophoneTestStatus



def make_config(**changes):
    import config_store

    return config_store.Config(**changes)


class FakeController:
    app_name = "Groq Insert Dictation"
    app_version = "test"
    app_dir = r"C:\Users\test\AppData\Roaming\GroqInsertDictation"

    def __init__(self, config) -> None:
        self.config = config
        self.applied = []
        self.suspended = 0
        self.resumed = 0
        self.stopped = 0
        self.played = []
        self.playback = PlaybackStatus()

    def list_input_devices(self, *, refresh=False):
        return getattr(self, "devices", [("", "Windows-standaard: Microfoonarray (Realtek Audio)"), ("3", "3: Test microfoon")])

    def autostart_enabled(self):
        return False

    def apply_settings(self, new_config):
        self.applied.append(new_config)
        self.config = new_config

    def suspend_hotkey(self):
        self.suspended += 1

    def resume_hotkey(self):
        self.resumed += 1

    def test_api_key(self, api_key):
        return "ok"

    def history_entries(self):
        return tuple(getattr(self, "history", ()))

    def recording_entries(self):
        return tuple(getattr(self, "recordings", ()))

    def recording_busy(self):
        return getattr(self, "busy", False)

    def retry_recording(self, recording_id):
        self.retried = recording_id
        self.busy = True

    def play_recording(self, recording_id, offset=0.0):
        self.played.append((recording_id, offset))
        duration = next((e.duration for e in self.recording_entries() if e.id == recording_id), 0.1)
        self.playback = PlaybackStatus(recording_id, offset, duration, True)
        return duration

    def stop_playback(self):
        self.stopped += 1
        self.playback = PlaybackStatus(self.playback.key, self.playback.position, self.playback.duration, False)

    def playback_status(self):
        return self.playback

    def copy_text(self, text):
        self.copied = text

    def clear_history(self):
        self.history = []
        self.recordings = []

    def start_microphone_test(self, selector):
        self.tested_device = selector
        self.microphone_status = MicrophoneTestStatus(state="recording", duration=5)

    def microphone_test_status(self):
        return getattr(self, "microphone_status", MicrophoneTestStatus())

    def play_microphone_test(self):
        self.microphone_played = True
        self.playback = PlaybackStatus("microphone-test", 0.0, 5.0, True)
        return 5.0

    def stop_microphone_test(self):
        self.microphone_stopped = True
        self.microphone_status = MicrophoneTestStatus()

    def check_for_updates_manual(self):
        pass

    def open_log(self):
        pass

    def restart(self):
        pass


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class SettingsWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = qt_app()
        # Message boxes would block; record them instead.
        self.questions = []
        self.errors = []
        patcher = mock.patch("settings_ui.ask", side_effect=lambda *args, **kwargs: self.questions.append(args) or True)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("settings_ui.show_error", side_effect=lambda *args: self.errors.append(args))
        patcher.start()
        self.addCleanup(patcher.stop)

    def open_window(self, config):
        import settings_ui

        controller = FakeController(config)
        window = settings_ui.SettingsWindow(controller)
        window.show()
        self.pump()
        self.addCleanup(self.close_window, window)
        return settings_ui, controller, window

    def close_window(self, window) -> None:
        try:
            window.dirty = False
            window.close()
        except RuntimeError:
            pass
        self.pump()

    def pump(self, seconds: float = 0.02) -> None:
        deadline = time.monotonic() + seconds
        while True:
            self.app.processEvents()
            if time.monotonic() >= deadline:
                break
            time.sleep(0.005)

    @staticmethod
    def button(window, text) -> QPushButton:
        return next(b for b in window.findChildren(QPushButton) if b.text() == text and b.isVisibleTo(window))

    def test_navigation_layout_and_cancel_preserve_configuration(self):
        _, controller, window = self.open_window(make_config(api_key="test-only"))
        self.assertEqual(window.current_page, "dictate")
        for key, title, _subtitle, _glyph in window.PAGES:
            window.nav_buttons[key].click()
            self.pump()
            self.assertEqual(window.current_page, key)
            self.assertTrue(window.nav_buttons[key].isChecked())
            self.assertEqual(window.page_title.text(), title)
            self.assertTrue(window.save_button.isVisible())
            # Every visible control on the page lies inside the window.
            page = window.pages[key]
            for child in page.findChildren(QWidget):
                if child.isVisible() and child.width() > 0:
                    top_left = child.mapTo(window, child.rect().topLeft())
                    self.assertGreaterEqual(top_left.x(), 0, child)
                    self.assertLessEqual(top_left.x() + child.width(), window.width() + 1, child)
        self.assertFalse(window.dirty)
        window.cancel_button.click()
        self.pump()
        self.assertEqual(controller.applied, [])
        self.assertEqual(controller.config.api_key, "test-only")
        self.assertEqual(self.questions, [])

    def test_window_opens_on_connection_page_without_api_key(self):
        _, _controller, window = self.open_window(make_config(api_key=""))
        self.assertEqual(window.current_page, "connection")

    def test_default_device_refresh_updates_name_without_changing_selection(self):
        _, controller, window = self.open_window(make_config(api_key="test-only"))
        self.assertIn("Microfoonarray (Realtek Audio)", window.selected_device_label())
        self.assertIn("Microfoonarray (Realtek Audio)", window.device_details_label.text())
        controller.devices = [("", "Windows-standaard: USB Headset Microfoon"), ("3", "3: Test microfoon")]
        window.refresh_devices()
        self.assertEqual(window.selected_device_id(), "")
        self.assertIn("USB Headset Microfoon", window.device_details_label.text())
        self.assertFalse(window.dirty)
        window.save()
        self.assertEqual(controller.applied[-1].input_device, "")

    def test_explicit_microphone_stays_selected_when_default_changes_or_it_disappears(self):
        _, controller, window = self.open_window(make_config(api_key="test-only", input_device="3"))
        controller.devices = [("", "Windows-standaard: USB Headset Microfoon"), ("3", "3: Test microfoon")]
        window.refresh_devices()
        self.assertEqual(window.selected_device_id(), "3")
        self.assertEqual(window.device_details_label.text(), "Geselecteerd: 3: Test microfoon")
        controller.devices = [("", "Windows-standaard: USB Headset Microfoon")]
        window.refresh_devices()
        self.assertEqual(window.selected_device_id(), "3")
        self.assertEqual(window.device_details_label.text(), "Geselecteerd: Niet beschikbaar: 3")
        self.assertFalse(window.dirty)

    def test_choosing_a_microphone_marks_the_form_dirty(self):
        _, controller, window = self.open_window(make_config(api_key="test-only"))
        window.device_combo.setCurrentIndex(window.device_combo.findData("3"))
        self.assertTrue(window.dirty)
        self.assertEqual(window.status_label.property("dirty"), "true")
        window.save()
        self.assertEqual(controller.applied[-1].input_device, "3")

    def test_microphone_test_uses_unsaved_selection_and_closes_without_saving(self):
        _, controller, window = self.open_window(make_config(api_key="test-only", input_device="3"))
        self.assertFalse(any(b.text() == "Geluiden testen" for b in window.findChildren(QPushButton)))
        window.device_combo.setCurrentIndex(window.device_combo.findData(""))
        window.toggle_microphone_test()
        self.pump()
        self.assertEqual(controller.tested_device, "")
        self.assertTrue(window.microphone_test_active)
        self.assertFalse(window.refresh_devices_button.isEnabled())
        self.assertFalse(window.device_combo.isEnabled())
        self.assertTrue(window.microphone_panel.isVisible())
        self.assertEqual(window.microphone_test_button.text(), "Stop test")
        controller.microphone_status = MicrophoneTestStatus("recording", level=0.5, elapsed=2.5, duration=5)
        window._microphone_tick()
        self.assertEqual(window.microphone_level.value(), 50)
        self.assertIn("2.5 / 5 seconden", window.microphone_result_label.text())
        controller.microphone_status = MicrophoneTestStatus("ready", elapsed=5, duration=5, heard_audio=True)
        window._microphone_tick()
        self.assertFalse(window.microphone_timer.isActive())
        self.assertIn("Geluid ontvangen", window.microphone_result_label.text())
        self.assertTrue(window.microphone_listen_button.isEnabled())
        window.listen_microphone_test()
        self.assertTrue(controller.microphone_played)
        self.assertEqual(window.microphone_listen_button.text(), "Stop afspelen")
        controller.playback = PlaybackStatus("microphone-test", 5.0, 5.0, False, finished=True)
        window._microphone_playback_tick()
        self.assertEqual(window.microphone_listen_button.text(), "Terugluisteren")
        window.dirty = False  # Selecting the default microphone was only for the test.
        window.close()
        self.pump()
        self.assertTrue(controller.microphone_stopped)
        self.assertEqual(controller.applied, [])
        self.assertEqual(controller.config.input_device, "3")

    def test_silent_or_failed_microphone_test_is_reported(self):
        _, controller, window = self.open_window(make_config(api_key="test-only"))
        window.toggle_microphone_test()
        controller.microphone_status = MicrophoneTestStatus("ready", elapsed=5, duration=5, heard_audio=False)
        window._microphone_tick()
        self.assertIn("Geen geluid gemeten", window.microphone_result_label.text())
        window.toggle_microphone_test()
        controller.microphone_status = MicrophoneTestStatus("error", error="De microfoon is gestopt of losgekoppeld.")
        window._microphone_tick()
        self.assertIn("losgekoppeld", window.microphone_result_label.text())
        self.assertFalse(window.microphone_listen_button.isEnabled())

    def test_dictionary_edits_and_save_round_trip(self):
        _, controller, window = self.open_window(make_config(api_key="test-only", shortcut="alt+z"))
        window.select_page("dictionary")
        window.word_entry.setText("Clinon")
        window.add_word()
        window.source_entry.setText("Grok")
        window.target_entry.setText("Groq")
        window.add_replacement()
        self.assertTrue(window.dirty)
        self.assertEqual(window.custom_words, ["Clinon"])
        self.assertEqual(window.word_replacements, [("Grok", "Groq")])
        self.assertEqual(window.words_editor.listbox.count(), 1)
        window.word_entry.setText("clinon")
        window.add_word()
        self.assertIn("staat al", self.errors[-1][2])
        window.shortcut_entry.setText("Ctrl + Shift + F9")
        window.save()
        self.pump()
        self.assertEqual(len(controller.applied), 1)
        saved = controller.applied[0]
        self.assertEqual(saved.custom_words, ("Clinon",))
        self.assertEqual(saved.word_replacements, (("Grok", "Groq"),))
        self.assertEqual(saved.shortcut, "ctrl+shift+f9")
        self.assertEqual(saved.api_key, "test-only")
        self.assertFalse(is_open(window))

    def test_removing_dictionary_items(self):
        _, controller, window = self.open_window(make_config(api_key="test-only", custom_words=("Groq", "Clinon")))
        editor = window.words_editor
        self.assertFalse(editor.remove_button.isEnabled())
        editor.listbox.setCurrentRow(0)
        self.assertTrue(editor.remove_button.isEnabled())
        editor.remove_button.click()
        self.assertEqual(window.custom_words, ["Clinon"])
        self.assertTrue(window.dirty)

    def test_switches_and_text_fields_round_trip(self):
        _, controller, window = self.open_window(make_config(api_key="old", language="nl", prompt="Oud"))
        window.paste_switch.click()
        window.remove_period_switch.click()
        window.autostart_switch.click()
        window.language_combo.setEditText("en")
        self.assertEqual(window.language_hint.text(), "Engels")
        window.prompt_text.setPlainText("Projectoverleg")
        self.assertEqual(window.prompt_counter.text(), "14 tekens")
        window.model_combo.setCurrentText("whisper-large-v3")
        window.api_key_entry.setText("new-key")
        window.save()
        saved = controller.applied[-1]
        self.assertFalse(saved.paste_after_transcription)
        self.assertTrue(saved.remove_final_period)
        self.assertFalse(saved.autostart)
        self.assertEqual((saved.language, saved.prompt, saved.model, saved.api_key), ("en", "Projectoverleg", "whisper-large-v3", "new-key"))

    def test_api_key_is_hidden_until_revealed(self):
        from PySide6.QtWidgets import QLineEdit

        _, _controller, window = self.open_window(make_config(api_key="secret"))
        self.assertEqual(window.api_key_entry.echoMode(), QLineEdit.EchoMode.Password)
        window.toggle_api_key_visibility()
        self.assertEqual(window.api_key_entry.echoMode(), QLineEdit.EchoMode.Normal)
        self.assertEqual(window.reveal_button.text(), "Verbergen")

    def test_connection_test_reports_on_the_gui_thread(self):
        _, controller, window = self.open_window(make_config(api_key="key"))
        window.test_connection()
        deadline = time.monotonic() + 2
        while window.connection_result.text() != "ok" and time.monotonic() < deadline:
            self.pump()
        self.assertEqual(window.connection_result.text(), "ok")
        self.assertEqual(window.connection_result.objectName(), "success")
        self.assertTrue(window.test_button.isEnabled())

    def test_unsaved_changes_are_confirmed_before_closing(self):
        _, controller, window = self.open_window(make_config(api_key="test-only"))
        window.paste_switch.click()
        with mock.patch("settings_ui.ask", return_value=False) as ask:
            window.close()
            self.pump()
            ask.assert_called_once()
        self.assertTrue(window.isVisible())
        window.close()
        self.pump()
        self.assertFalse(is_open(window))
        self.assertEqual(controller.applied, [])

    def test_history_page_lists_entries_and_copies_text(self):
        from history import HistoryEntry

        _, controller, window = self.open_window(make_config(api_key="test-only"))
        controller.history = [HistoryEntry("Tweede tekst", 2_000_000_000.0), HistoryEntry("Eerste tekst", 1_900_000_000.0)]
        window.refresh_history()
        window.select_page("history")
        window.history_tabs.setCurrentIndex(1)
        self.pump()
        copy_buttons = [b for b in window.history_scroller.findChildren(QPushButton) if b.text() == "Kopiëren"]
        self.assertEqual(len(copy_buttons), 2)
        copy_buttons[0].click()
        self.assertEqual(controller.copied, "Tweede tekst")
        self.assertEqual(copy_buttons[0].text(), "Gekopieerd ✓")
        self.assertFalse(window.dirty)

    def test_saved_failed_audio_is_retryable_without_any_text_history(self):
        from history import RecordingEntry

        _, controller, window = self.open_window(make_config(api_key="test-only"))
        controller.recordings = [RecordingEntry(
            text="", created_at=2_000_000_000.0, id="a" * 32, duration=3.0,
            status="failed", error="Network unavailable",
        )]
        window.refresh_history()
        window.select_page("history")
        self.pump()
        retry = self.button(window, "Opnieuw transcriberen")
        self.assertTrue(retry.isEnabled())
        self.assertTrue(window.clear_history_button.isEnabled())
        retry.click()
        self.pump()
        self.assertEqual(controller.retried, "a" * 32)
        self.assertFalse(self.button(window, "Opnieuw transcriberen").isEnabled())
        self.assertFalse(window.clear_history_button.isEnabled())
        self.assertFalse(window.dirty)

    def test_clearing_history_asks_first(self):
        from history import HistoryEntry

        _, controller, window = self.open_window(make_config(api_key="test-only"))
        controller.history = [HistoryEntry("Tekst", 2_000_000_000.0)]
        window.refresh_history()
        window.clear_history()
        self.assertEqual(len(self.questions), 1)
        self.assertEqual(controller.history, [])
        self.assertFalse(window.clear_history_button.isEnabled())

    def test_recording_player_plays_pauses_seeks_and_resets_after_audio_ends(self):
        from history import RecordingEntry

        _, controller, window = self.open_window(make_config(api_key="test-only"))
        a, b = "a" * 32, "b" * 32
        entries = [
            RecordingEntry(text="Hallo", created_at=2_000_000_000.0, id=a, duration=0.3, status="done"),
            RecordingEntry(text="", created_at=1_900_000_000.0, id=b, duration=10.0, status="failed"),
        ]
        controller.recordings = entries
        window.refresh_history()
        window.select_page("history")
        self.pump()
        self.assertEqual(set(window.players), {a, b})
        self.assertTrue(window.players[a].isVisible())
        self.assertFalse(window.players[a].playing)

        window.seek_recording(entries[1], 0.5)  # Seeking while paused only moves the knob.
        self.assertEqual(controller.played, [])
        self.assertAlmostEqual(window.players[b].position, 5.0)
        window.toggle_recording_playback(entries[1])
        self.assertEqual(controller.played, [(b, 5.0)])
        self.assertTrue(window.players[b].playing)
        window.seek_recording(entries[1], 0.2)
        self.assertEqual(controller.played[-1], (b, 2.0))

        controller.playback = PlaybackStatus(b, 3.0, 10.0, True)
        window.toggle_recording_playback(entries[0])  # Another recording pauses the first.
        self.assertFalse(window.players[b].playing)
        self.assertAlmostEqual(window.playback_positions[b], 3.0)
        self.assertTrue(window.players[a].playing)
        window.toggle_recording_playback(entries[0])
        self.assertFalse(window.players[a].playing)

        # The real device position drives the knob, and the end resets it.
        window.playback_positions[a] = 0.0
        window.toggle_recording_playback(entries[0])
        controller.playback = PlaybackStatus(a, 0.15, 0.3, True)
        window._playback_tick()
        self.assertAlmostEqual(window.players[a].position, 0.15)
        controller.playback = PlaybackStatus(a, 0.3, 0.3, False, finished=True)
        window._playback_tick()
        self.assertFalse(window.players[a].playing)
        self.assertEqual(window.players[a].position, 0.0)
        self.assertFalse(window.playback_timer.isActive())
        self.assertFalse(window.dirty)

    def test_player_widget_reports_toggle_and_seek_from_the_mouse(self):
        import settings_ui
        from PySide6.QtCore import QPoint
        from PySide6.QtTest import QTest

        player = settings_ui.RecordingPlayer(10.0)
        player.resize(320, 34)
        player.show()
        self.addCleanup(player.close)
        toggles, seeks = [], []
        player.toggled.connect(lambda: toggles.append(True))
        player.seek_requested.connect(seeks.append)
        QTest.mouseClick(player, Qt.MouseButton.LeftButton, pos=QPoint(10, 17))
        self.assertEqual(toggles, [True])
        start, end = player._track_bounds()
        QTest.mouseClick(player, Qt.MouseButton.LeftButton, pos=QPoint(round((start + end) / 2), 17))
        self.assertAlmostEqual(seeks[-1], 0.5, delta=0.02)

    def test_shortcut_capture_pauses_and_restores_the_global_hotkey(self):
        _, controller, window = self.open_window(make_config(api_key="test-only"))
        window.start_capture()
        self.assertEqual(controller.suspended, 1)
        self.assertTrue(window.capturing)
        self.assertEqual(window.shortcut_entry.property("capturing"), "true")
        # A lone modifier keeps listening.
        window.capture_key(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Control, Qt.KeyboardModifier.ControlModifier))
        self.assertTrue(window.capturing)
        event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_F9, Qt.KeyboardModifier.ControlModifier, 0x78, 0x78, 0)
        window.capture_key(event)
        self.assertFalse(window.capturing)
        self.assertEqual(controller.resumed, 1)
        self.assertEqual(window.shortcut_entry.text(), "ctrl+f9")
        self.assertTrue(window.dirty)

    def test_escape_cancels_capture_and_keeps_the_old_shortcut(self):
        _, controller, window = self.open_window(make_config(api_key="test-only", shortcut="insert"))
        window.start_capture()
        window.capture_key(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
        self.assertFalse(window.capturing)
        self.assertEqual(window.shortcut_entry.text(), "insert")
        self.assertEqual(controller.resumed, 1)

    def test_capture_takes_keys_from_any_focused_field(self):
        _, controller, window = self.open_window(make_config(api_key="test-only"))
        window.start_capture()
        window.select_page("connection")
        window.api_key_entry.setFocus()
        event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Insert, Qt.KeyboardModifier.NoModifier, 0x2D, 0x2D, 0)
        consumed = window.eventFilter(window.api_key_entry, event)
        self.assertTrue(consumed)
        self.assertEqual(window.shortcut_entry.text(), "insert")
        self.assertEqual(window.api_key_entry.text(), "test-only")

    def test_rejected_shortcut_keeps_other_saved_changes(self):
        from hotkeys import HotkeyError

        _, controller, window = self.open_window(make_config(api_key="test-only", shortcut="insert"))

        def refuse(new_config):
            import dataclasses

            controller.config = dataclasses.replace(new_config, shortcut="insert")
            raise HotkeyError("bezet")

        controller.apply_settings = refuse
        window.shortcut_entry.setText("alt+z")
        window.paste_switch.click()
        window.save()
        self.assertTrue(window.isVisible())
        self.assertTrue(window.dirty)
        self.assertEqual(window.status_label.text(), "Alleen de shortcut is niet opgeslagen.")
        self.assertIn("bezet", self.errors[-1][2])


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class QtKeyNameTests(unittest.TestCase):
    def setUp(self) -> None:
        qt_app()

    def test_keys_map_to_hotkey_spelling(self):
        import settings_ui

        self.assertEqual(settings_ui.qt_key_name(Qt.Key.Key_Insert.value), "insert")
        self.assertEqual(settings_ui.qt_key_name(Qt.Key.Key_F9.value), "f9")
        self.assertEqual(settings_ui.qt_key_name(Qt.Key.Key_Z.value), "z")
        self.assertEqual(settings_ui.qt_key_name(Qt.Key.Key_5.value, keypad=True), "kp 5")
        self.assertEqual(settings_ui.qt_key_name(Qt.Key.Key_Comma.value), ",")
        self.assertEqual(settings_ui.qt_key_name(Qt.Key.Key_PageDown.value), "page down")

    def test_events_become_registrable_shortcuts(self):
        import settings_ui

        alt_z = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Z, Qt.KeyboardModifier.AltModifier, 0x2C, 0x5A, 0, "z")
        self.assertEqual(settings_ui.hotkey_from_qt_event(alt_z), "alt+z")
        win = QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_Space,
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier, 0x39, 0x20, 0,
        )
        self.assertEqual(settings_ui.hotkey_from_qt_event(win), "ctrl+shift+space")
        lone = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Shift, Qt.KeyboardModifier.ShiftModifier, 0x2A, 0x10, 0)
        self.assertIsNone(settings_ui.hotkey_from_qt_event(lone))


if __name__ == "__main__":
    unittest.main()
