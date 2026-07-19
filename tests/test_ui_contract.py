import unittest
from pathlib import Path


APP_SOURCE = (Path(__file__).parents[1] / "app.py").read_text(encoding="utf-8")


class ExistingUiContractTests(unittest.TestCase):
    def test_status_bubble_timing_and_geometry_are_unchanged(self) -> None:
        for snippet in (
            'self.root.after(3000, self.hide)',
            'self.root.after(70, tick)',
            'self.root.after(120, tick)',
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
            "Start automatisch met Windows",
            "Geluiden testen",
            "Annuleren",
            "Opslaan",
        ):
            self.assertIn(f'text="{label}"', APP_SOURCE)

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

    def test_words_and_replacements_share_one_dictionary_dialog(self) -> None:
        self.assertIn('ttk.LabelFrame(dialog_frame, text="Vervangingen"', APP_SOURCE)
        self.assertIn("create_replacements_section()", APP_SOURCE)
        self.assertNotIn('text="Vervangingen..."', APP_SOURCE)
        self.assertNotIn("replacements_dialog = Toplevel(dialog)", APP_SOURCE)


if __name__ == "__main__":
    unittest.main()
