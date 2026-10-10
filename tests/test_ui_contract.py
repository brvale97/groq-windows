import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")
ENGINE_SOURCE = (ROOT / "engine.py").read_text(encoding="utf-8")
SETTINGS_SOURCE = (ROOT / "settings_ui.py").read_text(encoding="utf-8")
HOTKEYS_SOURCE = (ROOT / "hotkeys.py").read_text(encoding="utf-8")
REQUIREMENTS = (ROOT / "requirements.txt").read_text(encoding="utf-8")


class NetworkTrustContractTests(unittest.TestCase):
    def test_windows_certificate_store_is_enabled_before_groq_import(self) -> None:
        inject = ENGINE_SOURCE.index("truststore.inject_into_ssl()")
        groq_import = ENGINE_SOURCE.index("from groq import Groq")
        self.assertLess(inject, groq_import)
        # The app loads the engine (and thus the trust store) before anything else uses Groq.
        self.assertLess(APP_SOURCE.index("import engine"), APP_SOURCE.index("from engine import"))
        self.assertIn("truststore==0.10.4", REQUIREMENTS)


class ExistingUiContractTests(unittest.TestCase):
    def test_existing_status_texts_are_unchanged(self) -> None:
        for snippet in (
            '"Transcriberen"',
            'self.bubble.show_notice("Transcriptie te kort")',
            'NOTICE_VISIBLE_MS = 3000',
        ):
            self.assertIn(snippet, APP_SOURCE)
        for snippet in (
            'self.notify("Opname gestopt. Transcriberen...")',
            'self.notify("Klaar. Gebruik je shortcut voor een nieuwe opname.")',
        ):
            self.assertIn(snippet, ENGINE_SOURCE)

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
            "Automatisch alinea's maken",
            "Start automatisch met Windows",
            "Microfoon testen",
            "Terugluisteren",
            "Vernieuwen",
            "Annuleren",
            "Opslaan",
        ):
            self.assertIn(f'"{label}"', SETTINGS_SOURCE, label)
        self.assertNotIn("Geluiden testen", SETTINGS_SOURCE)

    def test_final_period_option_defaults_to_off_and_is_wired(self) -> None:
        config_source = (ROOT / "config_store.py").read_text(encoding="utf-8")
        for snippet in (
            "remove_final_period: bool = False",
            "remove_final_period=bool(data.get(\"remove_final_period\", False))",
        ):
            self.assertIn(snippet, config_source)
        self.assertIn("remove_final_period=session.remove_final_period", ENGINE_SOURCE)
        self.assertIn("remove_final_period=self.remove_period_switch.isChecked()", SETTINGS_SOURCE)

    def test_paragraph_option_defaults_to_on_and_is_wired(self) -> None:
        config_source = (ROOT / "config_store.py").read_text(encoding="utf-8")
        self.assertIn("auto_paragraphs: bool = True", config_source)
        self.assertIn('auto_paragraphs=bool(data.get("auto_paragraphs", True))', config_source)
        self.assertEqual(ENGINE_SOURCE.count("auto_paragraphs=config.auto_paragraphs"), 2)
        self.assertIn("auto_paragraphs=self.paragraphs_switch.isChecked()", SETTINGS_SOURCE)

    def test_startup_splash_waits_for_the_visible_tray_icon(self) -> None:
        for snippet in (
            '"Wordt geladen in het systeemvak..."',
            '"Klaar — actief in het systeemvak"',
            'self.tray.show()',
            'QTimer.singleShot(SPLASH_READY_VISIBLE_MS, self._finish_startup)',
            'elapsed_ms >= SPLASH_TRAY_TIMEOUT_MS',
            'self._cleanup_failed_startup()',
        ):
            self.assertIn(snippet, APP_SOURCE)

    def test_words_and_replacements_live_on_one_dictionary_page(self) -> None:
        self.assertIn('("dictionary", "Woordenboek"', SETTINGS_SOURCE)
        self.assertIn('"Woorden",', SETTINGS_SOURCE)
        self.assertIn('"Vervangingen",', SETTINGS_SOURCE)
        self.assertNotIn("Woordenboek openen", SETTINGS_SOURCE)

    def test_tkinter_and_pystray_are_gone(self) -> None:
        for source in (APP_SOURCE, ENGINE_SOURCE, SETTINGS_SOURCE):
            self.assertNotIn("tkinter", source)
            self.assertNotIn("pystray", source)
        self.assertNotIn("pystray", REQUIREMENTS)
        self.assertIn("PySide6-Essentials==", REQUIREMENTS)


class HistoryContractTests(unittest.TestCase):
    def test_history_is_recorded_after_a_successful_transcription(self) -> None:
        start = ENGINE_SOURCE.index("    def transcribe_and_output(")
        end = ENGINE_SOURCE.index("    def transcribe(", start)
        body = ENGINE_SOURCE[start:end]
        self.assertLess(
            body.index("pyperclip.copy(clipboard_text(pasted_text))"),
            body.index("self.transcript_callback(text)"),
        )
        self.assertIn('("Geschiedenis", lambda: self.open_settings("history"))', APP_SOURCE)
        self.assertIn('("history", "Geschiedenis"', SETTINGS_SOURCE)
        self.assertIn('"Kopiëren"', SETTINGS_SOURCE)


    def test_pasted_text_gets_a_trailing_space_but_history_does_not(self) -> None:
        start = ENGINE_SOURCE.index("    def transcribe_and_output(")
        end = ENGINE_SOURCE.index("    def transcribe(", start)
        body = ENGINE_SOURCE[start:end]
        self.assertIn("pasted_text = append_trailing_space(text)", body)
        self.assertIn("pyperclip.copy(clipboard_text(pasted_text))", body)
        self.assertIn("self.transcript_callback(text)", body)


class ShortcutReliabilityContractTests(unittest.TestCase):
    """Guard the fixes for 'the shortcut stops working until I restart'."""

    def test_low_level_keyboard_hook_library_is_gone(self) -> None:
        for source in (APP_SOURCE, ENGINE_SOURCE):
            self.assertNotIn("import keyboard", source)
            self.assertNotIn("keyboard.add_hotkey", source)
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertNotIn("keyboard==", requirements)

    def test_shortcut_uses_register_hotkey_with_no_repeat(self) -> None:
        self.assertIn("user32.RegisterHotKey(", HOTKEYS_SOURCE)
        self.assertIn("MOD_NOREPEAT", HOTKEYS_SOURCE)
        self.assertIn("HotkeyListener(self.engine.on_shortcut)", APP_SOURCE)

    def test_shortcut_handler_never_blocks_the_reporting_thread(self) -> None:
        start = ENGINE_SOURCE.index("    def on_shortcut(self) -> None:")
        end = ENGINE_SOURCE.index("    def toggle_recording(self) -> None:")
        self.assertIn("threading.Thread(target=self.toggle_recording", ENGINE_SOURCE[start:end])

    def test_bubble_click_stops_a_running_recording(self) -> None:
        self.assertIn("StatusBubble(self.on_bubble_click)", APP_SOURCE)
        start = APP_SOURCE.index("    def on_bubble_click(self) -> None:")
        end = APP_SOURCE.index("    def on_tray_activated(self, reason) -> None:")
        body = APP_SOURCE[start:end]
        self.assertIn('if state == "recording":', body)
        self.assertIn("self.engine.on_shortcut()", body)
        self.assertIn('elif state == "idle":', body)


if __name__ == "__main__":
    unittest.main()
