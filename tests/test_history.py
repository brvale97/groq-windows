import json
import tempfile
import time
import unittest
from pathlib import Path

from history import MAX_HISTORY_ENTRIES, HistoryEntry, TranscriptionHistory


class TranscriptionHistoryTests(unittest.TestCase):
    def test_keeps_only_the_newest_entries_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history = TranscriptionHistory(Path(directory) / "history.json")
            for index in range(MAX_HISTORY_ENTRIES + 3):
                history.add(f"tekst {index}", created_at=1_000 + index)
            texts = [entry.text for entry in history.entries]
            self.assertEqual(len(texts), MAX_HISTORY_ENTRIES)
            self.assertEqual(texts[0], f"tekst {MAX_HISTORY_ENTRIES + 2}")
            self.assertEqual(texts[-1], "tekst 3")

    def test_round_trips_through_disk_and_ignores_garbage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            TranscriptionHistory(path).add("Bewaard", created_at=1_700_000_000)
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["entries"][0]["text"], "Bewaard")
            self.assertEqual(TranscriptionHistory(path).entries[0].text, "Bewaard")

            path.write_text("{not json", encoding="utf-8")
            with self.assertLogs(level="WARNING"):
                self.assertEqual(TranscriptionHistory(path).entries, ())

    def test_blank_text_is_ignored_and_clear_empties_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history = TranscriptionHistory(Path(directory) / "history.json")
            self.assertIsNone(history.add("   "))
            history.add("iets")
            history.clear()
            self.assertEqual(history.entries, ())
            self.assertEqual(json.loads(history.path.read_text(encoding="utf-8"))["entries"], [])

    def test_labels_and_preview(self) -> None:
        now = time.time()
        today = HistoryEntry("Een hele lange tekst " * 10, now)
        self.assertTrue(today.label(now).startswith("Vandaag "))
        yesterday = HistoryEntry("x", now - 86_400)
        self.assertTrue(yesterday.label(now).startswith("Gisteren "))
        self.assertTrue(today.preview(30).endswith("…"))
        self.assertLessEqual(len(today.preview(30)), 30)


if __name__ == "__main__":
    unittest.main()
