"""The dictation engine without microphone, network or desktop; runs on any OS."""
import dataclasses
import math
import queue
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from unittest import mock

from qt_support import install_missing_stubs

install_missing_stubs()

import numpy as np  # noqa: E402

import engine  # noqa: E402
from config_store import Config  # noqa: E402
from history import RecordingHistory, TranscriptionHistory  # noqa: E402


class EngineTests(unittest.TestCase):
    def test_wav_and_statistics_match_pcm_samples(self) -> None:
        frames = [
            np.array([[0, 1_000], [16_384, -1_000], [-16_384, 2_000]], dtype=np.int16),
            np.array([[32_767, -2_000], [-32_768, 3_000], [0, -3_000]], dtype=np.int16),
        ]
        session = engine.RecordingSession(
            input_device=None,
            sample_rate=16_000,
            channels=2,
            model="whisper-large-v3-turbo",
            language="nl",
            prompt="Vocabulary: Groq.",
            word_replacements=(("Grok", "Groq"),),
            paste_after_transcription=True,
            remove_final_period=False,
            client=mock.Mock(),
        )
        dictation = engine.DictationEngine.__new__(engine.DictationEngine)
        path, duration, peak, rms = dictation.write_wav_and_stats(session, frames)
        reference_path = path.with_name(f"{path.stem}-reference.wav")
        try:
            with wave.open(str(path), "rb") as wav:
                self.assertEqual(wav.getnchannels(), 2)
                self.assertEqual(wav.getframerate(), 16_000)
                self.assertEqual(wav.getnframes(), 6)
            with wave.open(str(reference_path), "wb") as wav:
                wav.setnchannels(2)
                wav.setsampwidth(2)
                wav.setframerate(16_000)
                for frame in frames:
                    wav.writeframes(frame.tobytes())
            self.assertEqual(path.read_bytes(), reference_path.read_bytes())
            expected = np.concatenate(frames).astype(np.float64).reshape(-1)
            self.assertAlmostEqual(duration, 6 / 16_000, places=12)
            self.assertAlmostEqual(peak, float(np.max(np.abs(expected)) / 32768.0), places=12)
            self.assertAlmostEqual(
                rms,
                math.sqrt(float(np.mean(np.square(expected)))) / 32768.0,
                places=12,
            )
        finally:
            path.unlink(missing_ok=True)
            reference_path.unlink(missing_ok=True)

    def test_stop_waits_until_stream_start_publishes_ownership(self) -> None:
        start_entered = threading.Event()
        allow_start = threading.Event()

        class FakeStream:
            def __init__(self, **_kwargs) -> None:
                self.closed = False

            def start(self) -> None:
                start_entered.set()
                self.assert_release()

            def assert_release(self) -> None:
                if not allow_start.wait(2):
                    raise AssertionError("test did not release stream.start")

            def stop(self) -> None:
                pass

            def close(self) -> None:
                self.closed = True

        dictation = engine.DictationEngine(Config(api_key="test"))
        created: list[FakeStream] = []

        def make_stream(**kwargs):
            stream = FakeStream(**kwargs)
            created.append(stream)
            return stream

        start_thread = threading.Thread(target=dictation.start_recording)
        with (
            mock.patch.object(engine.sd, "InputStream", side_effect=make_stream),
            mock.patch.object(engine, "play_sound"),
            mock.patch.object(engine.time, "sleep"),
        ):
            start_thread.start()
            self.assertTrue(start_entered.wait(1))
            stop_thread = threading.Thread(target=dictation.stop_recording)
            stop_thread.start()
            stop_thread.join(0.05)
            self.assertTrue(stop_thread.is_alive())
            allow_start.set()
            start_thread.join(2)
            stop_thread.join(2)
            self.assertFalse(start_thread.is_alive())
            self.assertFalse(stop_thread.is_alive())

        self.assertTrue(created[0].closed)
        self.assertIsNone(dictation.stream)

    def test_transcription_sends_one_combined_prompt(self) -> None:
        client = mock.Mock()
        client.audio.transcriptions.create.return_value.text = "Groq en Clinon"
        session = engine.RecordingSession(
            input_device=None,
            sample_rate=16_000,
            channels=1,
            model="whisper-large-v3-turbo",
            language="nl",
            prompt="Nederlandse vergadering\nVocabulary: Groq, Clinon.",
            word_replacements=(("Grok", "Groq"),),
            paste_after_transcription=True,
            remove_final_period=False,
            client=client,
        )
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as audio:
            audio.write(b"RIFFtest")
            path = Path(audio.name)
        try:
            dictation = engine.DictationEngine.__new__(engine.DictationEngine)
            self.assertEqual(dictation.transcribe(session, path), "Groq en Clinon")
            kwargs = client.audio.transcriptions.create.call_args.kwargs
            self.assertEqual(kwargs["prompt"], session.prompt)
            self.assertEqual(client.audio.transcriptions.create.call_count, 1)
        finally:
            path.unlink(missing_ok=True)



class AudioLevelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dictation = engine.DictationEngine.__new__(engine.DictationEngine)
        self.dictation.state = "recording"
        self.dictation.audio_queue = queue.SimpleQueue()
        self.dictation.audio_warning = None
        self.dictation.audio_level_reading = (0.0, 0.0)
        self.dictation.audio_level_peak = 0.0

    def feed_audio(self, amplitude: int, channels: int = 1) -> "np.ndarray":
        samples = np.full((160, channels), amplitude, dtype=np.int16)
        self.dictation.audio_callback(samples, len(samples), None, None)
        return samples

    def test_silence_and_low_noise_do_not_show_audio_activity(self) -> None:
        for amplitude in (0, 20, -80):
            with self.subTest(amplitude=amplitude):
                self.feed_audio(amplitude)
                self.assertEqual(self.dictation.get_audio_level(), 0.0)
        self.dictation.audio_callback(np.empty((0, 1), dtype=np.int16), 0, None, None)
        self.assertEqual(self.dictation.get_audio_level(), 0.0)

    def test_louder_audio_increases_the_level_without_overflow(self) -> None:
        levels = []
        for amplitude in (300, -3000, -32768):
            self.feed_audio(amplitude, channels=2)
            levels.append(self.dictation.get_audio_level())
        self.assertGreater(levels[0], 0.0)
        self.assertLess(levels[0], levels[1])
        self.assertLess(levels[1], levels[2])
        self.assertEqual(levels[2], 1.0)

    def test_meter_preserves_the_recorded_samples_and_audio_warning(self) -> None:
        samples = np.array([[12000, -32768], [0, 32767]], dtype=np.int16)
        expected = samples.copy()
        self.dictation.audio_callback(samples, len(samples), None, "input overflow")
        samples.fill(0)
        np.testing.assert_array_equal(self.dictation.audio_queue.get_nowait(), expected)
        self.assertEqual(self.dictation.audio_warning, "input overflow")

    def test_missing_audio_and_inactive_recording_clear_the_level(self) -> None:
        with mock.patch.object(engine.time, "monotonic", return_value=10.0):
            self.feed_audio(3000)
            self.assertGreater(self.dictation.get_audio_level(), 0.0)
            for state in ("processing", "idle"):
                self.dictation.state = state
                self.assertEqual(self.dictation.get_audio_level(), 0.0)
        self.dictation.state = "recording"
        with mock.patch.object(engine.time, "monotonic", return_value=11.0):
            self.assertEqual(self.dictation.get_audio_level(), 0.0)

    def test_short_peaks_between_ui_ticks_are_kept(self) -> None:
        with mock.patch.object(engine.time, "monotonic", return_value=10.0):
            self.feed_audio(3000)
            loud = self.dictation.get_audio_level()
            self.feed_audio(3000)
            self.feed_audio(0)
            self.assertEqual(self.dictation.get_audio_level(), loud)
            self.assertEqual(self.dictation.get_audio_level(), 0.0)
            self.feed_audio(300)
            quiet = self.dictation.get_audio_level()
            # No new audio block yet: hold the latest level instead of dropping to silence.
            self.assertEqual(self.dictation.get_audio_level(), quiet)

    def test_level_rises_quickly_and_falls_back_gradually(self) -> None:
        level = engine.smooth_audio_level(0.0, 1.0)
        self.assertGreaterEqual(level, 0.6)
        tail = []
        for _ in range(20):
            level = engine.smooth_audio_level(level, 0.0)
            tail.append(level)
        self.assertGreater(tail[0], 0.3)
        self.assertTrue(all(a >= b for a, b in zip(tail, tail[1:])))
        self.assertEqual(tail[-1], 0.0)



class RecordingRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.history = TranscriptionHistory(self.directory / "history.json")
        self.engine = engine.DictationEngine.__new__(engine.DictationEngine)
        self.engine.config = Config(api_key="test-only", sample_rate=16_000, channels=1)
        self.engine.client = mock.Mock()
        self.engine.recordings = RecordingHistory(self.directory / "recordings")
        self.engine.recording_callback = mock.Mock()
        self.engine.transcript_callback = self.history.add
        self.engine.status_callback = mock.Mock()
        self.engine.state_callback = mock.Mock()
        self.engine.state = "processing"
        self.engine.lock = threading.Lock()
        self.session = engine.RecordingSession(
            input_device=None, sample_rate=16_000, channels=1, model="whisper-large-v3-turbo",
            language="nl", prompt="", word_replacements=(), paste_after_transcription=True,
            remove_final_period=False, client=self.engine.client,
        )
        self.clipboard = mock.patch.object(engine.pyperclip, "copy").start()
        self.paste = mock.patch.object(engine.pyautogui, "hotkey").start()
        mock.patch.object(engine, "play_sound").start()
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

        with mock.patch.object(engine.threading, "Thread", side_effect=start_worker):
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

    def test_long_dictation_is_pasted_as_paragraphs_unless_disabled(self) -> None:
        sentences = [f"Dit is zin nummer {index} van een langer gesproken bericht." for index in range(1, 7)]
        response = " ".join(sentences) + " Groetjes."
        self.engine.client.audio.transcriptions.create.return_value.text = response
        self.engine.transcribe_and_output(self.session, self.frames())
        stored = self.engine.recordings.entries[0].text
        self.assertIn("\n\n\n\nGroetjes.", stored)
        self.assertEqual(stored.split(), response.split())
        self.assertEqual(self.history.entries[0].text, stored)
        self.clipboard.assert_called_once_with(stored.replace("\n", "\r\n") + " ")

        self.clipboard.reset_mock()
        session = dataclasses.replace(self.session, auto_paragraphs=False)
        self.engine.transcribe_and_output(session, self.frames())
        self.assertEqual(self.engine.recordings.entries[0].text, response)
        self.clipboard.assert_called_once_with(response + " ")

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


class QaClientTests(unittest.TestCase):
    def test_offline_transcripts_need_an_isolated_profile(self):
        import os

        with mock.patch.dict(os.environ, {"GROQ_DICTATION_QA_TRANSCRIPTS": "een|twee", "GROQ_DICTATION_PROFILE": ""}):
            self.assertIsNone(engine.make_client(""))
        with mock.patch.dict(os.environ, {"GROQ_DICTATION_QA_TRANSCRIPTS": "een|twee", "GROQ_DICTATION_PROFILE": "qa"}):
            client = engine.make_client("")
            texts = [client.audio.transcriptions.create(model="x").text for _ in range(3)]
        self.assertEqual(texts, ["een", "twee", "twee"])


if __name__ == "__main__":
    unittest.main()
