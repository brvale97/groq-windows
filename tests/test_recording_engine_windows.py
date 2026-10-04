"""Recording recovery through the real engine, without microphone or network."""
import dataclasses
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from history import RecordingHistory, TranscriptionHistory


@unittest.skipUnless(os.name == "nt", "Windows engine integration test")
class RecordingEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        global app, np
        import app
        import numpy as np

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.history = TranscriptionHistory(self.directory / "history.json")
        self.engine = app.DictationEngine.__new__(app.DictationEngine)
        self.engine.config = app.Config(api_key="test-only", sample_rate=16_000, channels=1)
        self.engine.client = mock.Mock()
        self.engine.recordings = RecordingHistory(self.directory / "recordings")
        self.engine.recording_callback = mock.Mock()
        self.engine.transcript_callback = self.history.add
        self.engine.status_callback = mock.Mock()
        self.engine.state_callback = mock.Mock()
        self.engine.state = "processing"
        self.engine.lock = threading.Lock()
        self.session = app.RecordingSession(
            input_device=None, sample_rate=16_000, channels=1, model="whisper-large-v3-turbo",
            language="nl", prompt="", word_replacements=(), paste_after_transcription=True,
            remove_final_period=False, client=self.engine.client,
        )
        self.clipboard = mock.patch.object(app.pyperclip, "copy").start()
        self.paste = mock.patch.object(app.pyautogui, "hotkey").start()
        mock.patch.object(app, "play_sound").start()
        self.addCleanup(mock.patch.stopall)

    def frames(self, duration=1.5):
        return [np.full((int(16_000 * duration), 1), 4_000, dtype=np.int16)]

    def fail_then_reload(self):
        self.engine.client.audio.transcriptions.create.side_effect = RuntimeError("Network unavailable")
        self.engine.transcribe_and_output(self.session, self.frames())
        self.engine.recordings = RecordingHistory(self.engine.recordings.directory)
        return self.engine.recordings.entries[0]

    def run_retry_synchronously(self, recording_id):
        # Exercise the public retry method but run the worker deterministically.
        def start_worker(**kwargs):
            thread = mock.Mock()
            thread.start.side_effect = lambda: kwargs["target"](*kwargs["args"])
            return thread

        with mock.patch.object(app.threading, "Thread", side_effect=start_worker):
            self.engine.retry_recording(recording_id)

    def test_audio_is_on_disk_before_groq_and_survives_failure(self) -> None:
        def request(**kwargs):
            saved = RecordingHistory(self.engine.recordings.directory)
            self.assertEqual(len(saved.entries), 1)
            entry = saved.entries[0]
            self.assertEqual(Path(kwargs["file"].name), saved.audio_path(entry))
            self.assertTrue(saved.audio_path(entry).exists())
            raise RuntimeError("Network unavailable")

        self.engine.client.audio.transcriptions.create.side_effect = request
        frames = self.frames()
        self.engine.transcribe_and_output(self.session, frames)
        loaded = RecordingHistory(self.engine.recordings.directory)
        self.assertEqual(loaded.entries[0].status, "failed")
        self.assertIn("Network unavailable", loaded.entries[0].error)
        self.assertEqual(frames, [])
        self.assertEqual(self.engine.state, "idle")
        self.clipboard.assert_not_called()

    def test_retry_after_restart_uses_current_settings_without_pasting(self) -> None:
        entry = self.fail_then_reload()
        original_audio = self.engine.recordings.audio_path(entry).read_bytes()
        self.engine.config = dataclasses.replace(
            self.engine.config, model="whisper-large-v3", language="en", prompt="Meeting",
            custom_words=("Groq",), word_replacements=(("Grok", "Groq"),), remove_final_period=True,
        )
        self.engine.client = mock.Mock()
        self.engine.client.audio.transcriptions.create.return_value.text = "Grok works."
        self.run_retry_synchronously(entry.id)
        updated = self.engine.recordings.entries[0]
        self.assertEqual(updated.id, entry.id)
        self.assertEqual(updated.created_at, entry.created_at)
        self.assertEqual(updated.text, "Groq works")
        self.assertEqual(updated.status, "done")
        self.assertEqual(updated.error, "")
        self.assertEqual(len(self.engine.recordings.entries), 1)
        self.assertEqual(self.engine.recordings.audio_path(entry).read_bytes(), original_audio)
        kwargs = self.engine.client.audio.transcriptions.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "whisper-large-v3")
        self.assertEqual(kwargs["language"], "en")
        self.assertIn("Vocabulary: Groq.", kwargs["prompt"])
        self.assertEqual(self.history.entries[0].text, "Groq works")
        self.clipboard.assert_called_once_with("Groq works ")
        self.paste.assert_not_called()
        self.assertEqual(self.engine.state, "idle")

    def test_repeated_retry_failure_keeps_original_audio(self) -> None:
        entry = self.fail_then_reload()
        original = self.engine.recordings.audio_path(entry).read_bytes()
        self.run_retry_synchronously(entry.id)
        self.assertEqual(self.engine.recordings.audio_path(entry).read_bytes(), original)
        self.assertEqual(self.engine.recordings.get(entry.id).status, "failed")
        self.assertEqual(len(self.engine.recordings.entries), 1)
        self.assertEqual(self.engine.state, "idle")

    def test_successful_dictation_keeps_audio_and_existing_paste_behavior(self) -> None:
        self.engine.client.audio.transcriptions.create.return_value.text = "Herkenning werkt."
        self.engine.transcribe_and_output(self.session, self.frames())
        entry = RecordingHistory(self.engine.recordings.directory).entries[0]
        self.assertEqual(entry.status, "done")
        self.assertEqual(entry.text, "Herkenning werkt.")
        self.assertEqual(self.history.entries[0].text, entry.text)
        self.clipboard.assert_called_once_with("Herkenning werkt. ")
        self.paste.assert_called_once_with("ctrl", "v")

    def test_empty_and_star_only_responses_leave_retryable_audio(self) -> None:
        for response in ("", "   ", "***"):
            with self.subTest(response=response):
                self.engine.client.audio.transcriptions.create.return_value.text = response
                self.engine.transcribe_and_output(self.session, self.frames())
                entry = self.engine.recordings.entries[0]
                self.assertEqual(entry.status, "failed")
                self.assertTrue(self.engine.recordings.audio_path(entry).exists())
        self.assertEqual(self.history.entries, ())
        self.clipboard.assert_not_called()

    def test_short_audio_is_saved_and_can_be_retried_explicitly(self) -> None:
        self.engine.transcribe_and_output(self.session, self.frames(duration=0.5))
        entry = self.engine.recordings.entries[0]
        self.assertEqual(entry.status, "failed")
        self.engine.client.audio.transcriptions.create.assert_not_called()
        self.engine.client.audio.transcriptions.create.return_value.text = "Hallo"
        self.run_retry_synchronously(entry.id)
        self.assertEqual(self.engine.recordings.get(entry.id).text, "Hallo")

    def test_clipboard_failure_still_keeps_transcript_in_recording_history(self) -> None:
        self.engine.client.audio.transcriptions.create.return_value.text = "Bewaarde tekst"
        self.clipboard.side_effect = RuntimeError("Clipboard unavailable")
        self.engine.transcribe_and_output(self.session, self.frames())
        entry = RecordingHistory(self.engine.recordings.directory).entries[0]
        self.assertEqual(entry.text, "Bewaarde tekst")
        self.assertTrue(self.engine.recordings.audio_path(entry).exists())

    def test_retry_is_rejected_during_recording_or_without_key(self) -> None:
        entry = self.fail_then_reload()
        for state in ("recording", "processing"):
            self.engine.state = state
            with self.assertRaisesRegex(RuntimeError, "Wacht"):
                self.engine.retry_recording(entry.id)
        self.engine.state = "idle"
        self.engine.config = dataclasses.replace(self.engine.config, api_key="")
        with self.assertRaisesRegex(RuntimeError, "API key"):
            self.engine.retry_recording(entry.id)
        self.assertEqual(self.engine.state, "idle")

    def test_audio_storage_failure_keeps_temp_file_and_skips_network(self) -> None:
        path, *_stats = self.engine.write_wav_and_stats(self.session, self.frames())
        self.addCleanup(path.unlink, missing_ok=True)
        with (
            mock.patch.object(self.engine, "write_wav_and_stats", return_value=(path, 1.5, 0.2, 0.1)),
            mock.patch.object(self.engine.recordings, "add", side_effect=OSError("disk full")),
        ):
            self.engine.transcribe_and_output(self.session, self.frames())
        self.assertTrue(path.exists())
        self.engine.client.audio.transcriptions.create.assert_not_called()
        self.assertEqual(self.engine.state, "idle")
        messages = [str(call.args[0]) for call in self.engine.status_callback.call_args_list]
        self.assertTrue(any(str(path) in message for message in messages))


if __name__ == "__main__":
    unittest.main()
