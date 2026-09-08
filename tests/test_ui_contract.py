import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")
SETTINGS_SOURCE = (ROOT / "settings_ui.py").read_text(encoding="utf-8")
HOTKEYS_SOURCE = (ROOT / "hotkeys.py").read_text(encoding="utf-8")


class ExistingUiContractTests(unittest.TestCase):
    def test_status_bubble_timing_and_geometry_are_unchanged(self) -> None:
        for snippet in (
            'self.root.after(3000, self.hide)',
            'self.root.after(70, tick)',
            'self.root.after(120, tick)',
            'bottom_centered_window_geometry(',
            'self.window_width = 176 if state == "recording" else 162 if state == "processing" else self.button_size',
            'self.window_height = 48 if state in {"recording", "processing"} else self.button_size',
        ):
            self.assertIn(snippet, APP_SOURCE)

    def test_existing_status_text_and_colors_are_unchanged(self) -> None:
        for snippet in (
            '"idle": "#E81123"',
            '"recording": "#ff2e3d"',
            'text="Transcriberen"',
            'self.notify("Opname gestopt. Transcriberen...")',
            'self.notify("Klaar. Gebruik je shortcut voor een nieuwe opname.")',
            'self.bubble.show_notice("Transcriptie te kort")',
        ):
            self.assertIn(snippet, APP_SOURCE)

    def test_existing_settings_labels_remain_present(self) -> None:
        for label in (
            "Groq API key",
            "Model",
            "Taal",
            "Prompt",
            "Shortcut",
            "Microfoon",
            "Transcriptie automatisch plakken",
            "Punt aan het einde verwijderen",
            "Start automatisch met Windows",
            "Geluiden testen",
            "Annuleren",
            "Opslaan",
        ):
            self.assertTrue(
                f'text="{label}"' in SETTINGS_SOURCE or f'"{label}"' in SETTINGS_SOURCE,
                label,
            )

    def test_final_period_option_defaults_to_off_and_is_wired(self) -> None:
        for snippet in (
            "remove_final_period: bool = False",
            "remove_final_period=bool(data.get(\"remove_final_period\", False))",
            "remove_final_period=session.remove_final_period",
        ):
            self.assertIn(snippet, APP_SOURCE)
        for snippet in (
            "BooleanVar(value=config.remove_final_period)",
            "remove_final_period=self.remove_period.get()",
        ):
            self.assertIn(snippet, SETTINGS_SOURCE)

    def test_startup_splash_waits_for_the_visible_tray_icon(self) -> None:
        for snippet in (
            'StringVar(value="Wordt geladen in het systeemvak...")',
            'self.status.set("Klaar — actief in het systeemvak")',
            'self.icon.run_detached(self._setup_tray)',
            'icon.visible = True',
            'self.tray_startup_complete.set()',
            'self.tray_startup_complete.is_set() and self.splash.minimum_time_has_elapsed()',
            'self.root.after(SPLASH_READY_VISIBLE_MS, self._finish_startup)',
            'elapsed_ms >= SPLASH_TRAY_TIMEOUT_MS',
            'self._cleanup_failed_startup()',
        ):
            self.assertIn(snippet, APP_SOURCE)

    def test_settings_open_only_after_the_splash_has_closed(self) -> None:
        finish_start = APP_SOURCE.index("    def _finish_startup(self) -> None:")
        next_method = APP_SOURCE.index("\n    def ", finish_start + 5)
        finish_source = APP_SOURCE[finish_start:next_method]
        self.assertLess(finish_source.index("self.splash.destroy()"), finish_source.index("self.open_settings"))

    def test_words_and_replacements_live_on_one_dictionary_page(self) -> None:
        self.assertIn('("dictionary", "Woordenboek"', SETTINGS_SOURCE)
        self.assertIn('"Woorden",', SETTINGS_SOURCE)
        self.assertIn('"Vervangingen",', SETTINGS_SOURCE)
        self.assertNotIn("Woordenboek openen", SETTINGS_SOURCE)
        self.assertNotIn("Toplevel(window)", SETTINGS_SOURCE)


class HistoryContractTests(unittest.TestCase):
    def test_history_is_recorded_after_a_successful_transcription(self) -> None:
        start = APP_SOURCE.index("    def transcribe_and_output(")
        end = APP_SOURCE.index("    def transcribe(", start)
        body = APP_SOURCE[start:end]
        self.assertLess(body.index("pyperclip.copy(text)\n"), body.index("self.transcript_callback(text)"))
        self.assertIn('pystray.MenuItem("Geschiedenis"', APP_SOURCE)
        self.assertIn('("history", "Geschiedenis"', SETTINGS_SOURCE)
        self.assertIn('text="Kopiëren"', SETTINGS_SOURCE)


class ShortcutReliabilityContractTests(unittest.TestCase):
    """Guard the fixes for 'the shortcut stops working until I restart'."""

    def test_low_level_keyboard_hook_library_is_gone(self) -> None:
        self.assertNotIn("import keyboard", APP_SOURCE)
        self.assertNotIn("keyboard.add_hotkey", APP_SOURCE)
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertNotIn("keyboard==", requirements)

    def test_shortcut_uses_register_hotkey_with_no_repeat(self) -> None:
        self.assertIn("user32.RegisterHotKey(", HOTKEYS_SOURCE)
        self.assertIn("MOD_NOREPEAT", HOTKEYS_SOURCE)
        self.assertIn("HotkeyListener(self.engine.on_shortcut)", APP_SOURCE)

    def test_shortcut_handler_never_blocks_the_reporting_thread(self) -> None:
        start = APP_SOURCE.index("    def on_shortcut(self) -> None:")
        end = APP_SOURCE.index("    def toggle_recording(self) -> None:")
        self.assertIn("threading.Thread(target=self.toggle_recording", APP_SOURCE[start:end])

    def test_bubble_click_stops_a_running_recording(self) -> None:
        self.assertIn("StatusBubble(self.root, self.on_bubble_click)", APP_SOURCE)
        start = APP_SOURCE.index("    def on_bubble_click(self) -> None:")
        end = APP_SOURCE.index("    def install_hotkey(self) -> None:")
        body = APP_SOURCE[start:end]
        self.assertIn('if state == "recording":', body)
        self.assertIn("self.engine.on_shortcut()", body)
        self.assertIn('elif state == "idle":', body)


if __name__ == "__main__":
    unittest.main()
