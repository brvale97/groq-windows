"""Exercise real Tk widgets without microphone, credentials, or tray changes."""
import os
import time
import unittest
from unittest import mock


class FakeController:
    app_name = "Groq Insert Dictation"
    app_version = "test"
    app_dir = r"C:\Users\test\AppData\Roaming\GroqInsertDictation"

    def __init__(self, config) -> None:
        self.config = config
        self.applied = []
        self.suspended = 0
        self.resumed = 0

    def list_input_devices(self):
        return [("", "Windows default input"), ("3", "3: Test microfoon")]

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

    def play_recording(self, recording_id):
        self.played = [*getattr(self, "played", []), recording_id]
        return 0.1

    def stop_playback(self):
        self.stopped = getattr(self, "stopped", 0) + 1

    def copy_text(self, text):
        self.copied = text

    def clear_history(self):
        self.history = []
        self.recordings = []

    def test_sounds(self):
        pass

    def check_for_updates_manual(self):
        pass

    def open_log(self):
        pass

    def restart(self):
        pass


@unittest.skipUnless(os.name == 'nt', 'Windows UI test')
class SettingsWindowTests(unittest.TestCase):
    def open_window(self, config):
        import app

        root = app.Tk()
        root.withdraw()
        app.apply_theme(root)
        controller = FakeController(config)
        window = app.SettingsWindow(root, controller)
        window.deiconify()
        for _ in range(50):
            root.update()
            if window.winfo_viewable():
                break
            time.sleep(0.02)
        if not window.winfo_viewable():
            root.destroy()
            self.skipTest('An interactive Windows desktop is required')
        return app, root, controller, window

    @staticmethod
    def descendants(widget):
        for child in widget.winfo_children():
            yield child
            yield from SettingsWindowTests.descendants(child)

    def button(self, app, window, text):
        return next(
            w for w in self.descendants(window)
            if isinstance(w, app.ttk.Button) and w.cget('text') == text
        )

    def test_navigation_layout_and_cancel_preserve_configuration(self):
        import app

        app_module, root, controller, window = self.open_window(app.Config(api_key='test-only'))
        try:
            for page in ('Dicteren', 'Geschiedenis', 'Herkenning', 'Woordenboek', 'Verbinding', 'Over'):
                self.button(app_module, window, page).invoke()
                root.update()
                self.assertIn('selected', self.button(app_module, window, page).state())
                self.assertTrue(self.button(app_module, window, 'Opslaan').winfo_viewable())
                for w in self.descendants(window):
                    if isinstance(w.master, app_module.Canvas):
                        continue  # content of a scrollable area may exceed its viewport
                    if w.winfo_viewable() and not isinstance(w, app_module.Toplevel):
                        self.assertLessEqual(w.winfo_x() + w.winfo_width(), w.master.winfo_width() + 2, str(w))
                        self.assertLessEqual(w.winfo_y() + w.winfo_height(), w.master.winfo_height() + 2, str(w))
            self.assertFalse(window.dirty)
            self.button(app_module, window, 'Annuleren').invoke()
            root.update()
            self.assertFalse(window.winfo_exists())
            self.assertEqual(controller.applied, [])
            self.assertEqual(controller.config.api_key, 'test-only')
        finally:
            root.destroy()

    def test_dictionary_edits_and_save_round_trip(self):
        import app

        app_module, root, controller, window = self.open_window(app.Config(api_key='test-only', shortcut='alt+z'))
        try:
            window.select_page('dictionary')
            window.word_value.set('Clinon')
            window.add_word()
            window.source_value.set('Grok')
            window.target_value.set('Groq')
            window.add_replacement()
            root.update()
            self.assertTrue(window.dirty)
            self.assertEqual(window.custom_words, ['Clinon'])
            self.assertEqual(window.word_replacements, [('Grok', 'Groq')])
            window.shortcut.set('Ctrl + Shift + F9')
            window.save()
            root.update()
            self.assertEqual(len(controller.applied), 1)
            saved = controller.applied[0]
            self.assertEqual(saved.custom_words, ('Clinon',))
            self.assertEqual(saved.word_replacements, (('Grok', 'Groq'),))
            self.assertEqual(saved.shortcut, 'ctrl+shift+f9')
            self.assertEqual(saved.api_key, 'test-only')
            self.assertFalse(window.winfo_exists())
        finally:
            root.destroy()

    def test_history_page_lists_entries_and_copies_text(self):
        import app
        from history import HistoryEntry

        app_module, root, controller, window = self.open_window(app.Config(api_key='test-only'))
        try:
            controller.history = [HistoryEntry('Tweede tekst', 2_000_000_000.0), HistoryEntry('Eerste tekst', 1_900_000_000.0)]
            window.refresh_history()
            window.select_page('history')
            window.history_tabs.select(window.history_scroller)
            root.update()
            copy_buttons = [w for w in self.descendants(window) if isinstance(w, app_module.ttk.Button) and w.cget('text') == 'Kopiëren']
            self.assertEqual(len(copy_buttons), 2)
            copy_buttons[0].invoke()
            root.update()
            self.assertEqual(controller.copied, 'Tweede tekst')
            self.assertEqual(copy_buttons[0].cget('text'), 'Gekopieerd ✓')
            self.assertFalse(window.dirty)
        finally:
            root.destroy()

    def test_saved_failed_audio_is_retryable_without_any_text_history(self):
        import app
        from history import RecordingEntry

        app_module, root, controller, window = self.open_window(app.Config(api_key='test-only'))
        try:
            controller.recordings = [RecordingEntry(
                text='', created_at=2_000_000_000.0, id='a' * 32, duration=3.0,
                status='failed', error='Network unavailable',
            )]
            window.refresh_history()
            window.select_page('history')
            root.update()
            retry = self.button(app_module, window, 'Opnieuw transcriberen')
            self.assertTrue(retry.winfo_viewable())
            self.assertNotIn('disabled', window.clear_history_button.state())
            retry.invoke()
            root.update()
            self.assertEqual(controller.retried, 'a' * 32)
            self.assertIn('disabled', self.button(app_module, window, 'Opnieuw transcriberen').state())
            self.assertIn('disabled', window.clear_history_button.state())
            self.assertFalse(window.dirty)
        finally:
            root.destroy()

    def test_recording_playback_toggles_and_resets_after_audio_ends(self):
        import app
        from history import RecordingEntry

        app_module, root, controller, window = self.open_window(app.Config(api_key='test-only'))
        try:
            controller.recordings = [
                RecordingEntry(text='Hallo', created_at=2_000_000_000.0, id='a' * 32, duration=0.1, status='done'),
                RecordingEntry(text='', created_at=1_900_000_000.0, id='b' * 32, duration=0.1, status='failed'),
            ]
            window.refresh_history()
            window.select_page('history')
            root.update()
            play_buttons = [w for w in self.descendants(window) if isinstance(w, app_module.ttk.Button) and w.cget('text') == 'Afspelen']
            self.assertEqual(len(play_buttons), 2)
            play_buttons[0].invoke()
            self.assertEqual(controller.played, ['a' * 32])
            self.assertEqual(play_buttons[0].cget('text'), 'Stoppen')
            play_buttons[1].invoke()
            self.assertEqual(controller.played, ['a' * 32, 'b' * 32])
            self.assertEqual(play_buttons[0].cget('text'), 'Afspelen')
            self.assertEqual(play_buttons[1].cget('text'), 'Stoppen')
            play_buttons[1].invoke()
            self.assertEqual(play_buttons[1].cget('text'), 'Afspelen')
            self.assertEqual(controller.stopped, 2)
            play_buttons[0].invoke()
            deadline = time.monotonic() + 2
            while play_buttons[0].cget('text') == 'Stoppen' and time.monotonic() < deadline:
                root.update()
                time.sleep(0.02)
            self.assertEqual(play_buttons[0].cget('text'), 'Afspelen')
            self.assertFalse(window.dirty)
        finally:
            root.destroy()

    def test_shortcut_capture_pauses_and_restores_the_global_hotkey(self):
        import app

        app_module, root, controller, window = self.open_window(app.Config(api_key='test-only'))
        try:
            window.start_capture()
            self.assertEqual(controller.suspended, 1)
            self.assertTrue(window.capturing)
            event = mock.Mock(keysym='F9', keycode=0x78, char='', state=0x4)
            window._capture_key(event)
            self.assertFalse(window.capturing)
            self.assertEqual(controller.resumed, 1)
            self.assertEqual(window.shortcut.get(), 'ctrl+f9')
        finally:
            root.destroy()
