import json
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest import mock

from history import MAX_HISTORY_ENTRIES, MAX_RECORDING_ENTRIES, HistoryEntry, RecordingHistory, TranscriptionHistory


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


class RecordingHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.source = self.directory / "source.wav"
        with wave.open(str(self.source), "wb") as audio:
            audio.setnchannels(2)
            audio.setsampwidth(2)
            audio.setframerate(16_000)
            audio.writeframes(b"\x00\x10" * 32_000)
        self.history = RecordingHistory(self.directory / "recordings")

    def test_audio_and_metadata_survive_restart_and_source_deletion(self) -> None:
        entry = self.history.add(self.source, created_at=1_700_000_000)
        original = self.source.read_bytes()
        self.source.unlink()
        self.history.update(entry.id, status="failed", error="Connection failed")
        loaded = RecordingHistory(self.history.directory)
        self.assertEqual(loaded.audio_path(entry).read_bytes(), original)
        self.assertEqual(loaded.get(entry.id).error, "Connection failed")
        self.assertEqual(loaded.get(entry.id).duration, 1.0)
        self.assertEqual(loaded.get(entry.id).created_at, 1_700_000_000)

    def test_keeps_twenty_recordings_independently_of_text_history(self) -> None:
        transcript_history = TranscriptionHistory(self.directory / "history.json")
        transcript_history.add("Bestaande tekst")
        entries = [self.history.add(self.source, created_at=1_000 + i) for i in range(MAX_RECORDING_ENTRIES + 3)]
        self.assertEqual([entry.id for entry in self.history.entries], [entry.id for entry in reversed(entries[3:])])
        self.assertEqual(len(list(self.history.directory.glob("*.wav"))), MAX_RECORDING_ENTRIES)
        self.assertEqual(len(list(self.history.directory.glob("*.json"))), MAX_RECORDING_ENTRIES)
        for entry in entries[:3]:
            self.assertFalse(self.history.audio_path(entry).exists())
            self.assertFalse(self.history.audio_path(entry).with_suffix(".json").exists())
        self.assertEqual(TranscriptionHistory(transcript_history.path).entries[0].text, "Bestaande tekst")

    def test_retry_updates_the_same_entry_without_changing_its_age(self) -> None:
        entry = self.history.add(self.source, created_at=1_700_000_000)
        audio = self.history.audio_path(entry).read_bytes()
        self.history.update(entry.id, status="failed", error="First attempt failed")
        self.history.update(entry.id, status="processing")
        self.history.update(entry.id, status="done", text="Groq werkt.")
        loaded = RecordingHistory(self.history.directory)
        self.assertEqual(len(loaded.entries), 1)
        self.assertEqual(loaded.get(entry.id).text, "Groq werkt.")
        self.assertEqual(loaded.get(entry.id).error, "")
        self.assertEqual(loaded.get(entry.id).created_at, entry.created_at)
        self.assertEqual(loaded.audio_path(entry).read_bytes(), audio)

    def test_recovers_audio_when_metadata_is_missing_corrupt_or_incomplete(self) -> None:
        entry = self.history.add(self.source)
        metadata = self.history.audio_path(entry).with_suffix(".json")
        for contents in (None, "{bad json", "[]", '{"status": [], "created_at": "nan"}'):
            if contents is None:
                metadata.unlink(missing_ok=True)
            else:
                metadata.write_text(contents, encoding="utf-8")
            loaded = RecordingHistory(self.history.directory)
            self.assertEqual(loaded.get(entry.id).status, "saved")
            self.assertTrue(loaded.audio_path(entry).exists())

    def test_interrupted_request_can_be_retried_after_restart(self) -> None:
        entry = self.history.add(self.source)
        self.history.update(entry.id, status="processing")
        loaded = RecordingHistory(self.history.directory)
        self.assertEqual(loaded.get(entry.id).status, "failed")
        self.assertIn("onderbroken", loaded.get(entry.id).error)

    def test_metadata_write_failure_does_not_lose_committed_audio(self) -> None:
        from history import os

        original_replace = os.replace

        def replace_unless_metadata(source, target):
            if Path(target).suffix == ".json":
                raise OSError("disk full")
            original_replace(source, target)

        with mock.patch("history.os.replace", side_effect=replace_unless_metadata), self.assertLogs(level="WARNING"):
            entry = self.history.add(self.source)
        loaded = RecordingHistory(self.history.directory)
        self.assertEqual(loaded.get(entry.id).status, "saved")
        self.assertEqual(loaded.audio_path(entry).read_bytes(), self.source.read_bytes())
        self.assertEqual(list(self.history.directory.glob("*.tmp")), [])

    def test_failed_audio_commit_preserves_source_and_existing_history(self) -> None:
        entry = self.history.add(self.source)
        with mock.patch("history.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.history.add(self.source)
        self.assertTrue(self.source.exists())
        self.assertEqual(self.history.entries, (entry,))
        self.assertEqual(list(self.history.directory.glob("*.tmp")), [])

    def test_clear_removes_audio_and_metadata(self) -> None:
        self.history.add(self.source)
        self.history.add(self.source)
        self.history.clear()
        self.assertEqual(RecordingHistory(self.history.directory).entries, ())
        self.assertEqual(list(self.history.directory.iterdir()), [])

    def test_missing_or_expired_recording_cannot_be_retried(self) -> None:
        entry = self.history.add(self.source)
        self.history.audio_path(entry).unlink()
        with self.assertRaisesRegex(RuntimeError, "niet meer beschikbaar"):
            self.history.get(entry.id)
        with self.assertRaises(RuntimeError):
            self.history.get("../outside")


if __name__ == "__main__":
    unittest.main()
