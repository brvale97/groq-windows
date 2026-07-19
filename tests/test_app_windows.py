import json
import math
import os
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from unittest import mock


@unittest.skipUnless(os.name == "nt", "Windows integration test")
class WindowsAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        global app, np
        import app
        import numpy as np

    def test_startup_splash_geometry_is_centered_and_clamped(self) -> None:
        self.assertEqual(app.centered_window_geometry(380, 170, 1920, 1080), "380x170+770+455")
        self.assertEqual(app.centered_window_geometry(380, 170, 200, 100), "380x170+0+0")

    def test_tray_setup_failure_always_signals_completion(self) -> None:
        tray_app = app.TrayApp.__new__(app.TrayApp)
        tray_app.tray_startup_complete = threading.Event()
        tray_app.tray_startup_error = None

        class FailingIcon:
            @property
            def visible(self):
                return False

            @visible.setter
            def visible(self, _value):
                raise RuntimeError("tray unavailable")

        tray_app._setup_tray(FailingIcon())
        self.assertTrue(tray_app.tray_startup_complete.is_set())
        self.assertIsInstance(tray_app.tray_startup_error, RuntimeError)

    def test_splash_waits_for_minimum_time_after_tray_is_ready(self) -> None:
        tray_app = app.TrayApp.__new__(app.TrayApp)
        tray_app.startup_finished = False
        tray_app.tray_startup_complete = threading.Event()
        tray_app.tray_startup_complete.set()
        tray_app.tray_startup_error = None
        tray_app.splash = mock.Mock(started_at=100.0)
        tray_app.splash.minimum_time_has_elapsed.return_value = False
        tray_app.root = mock.Mock()

        with mock.patch.object(app.time, "monotonic", return_value=100.1):
            tray_app._poll_startup_ready()

        tray_app.splash.show_ready.assert_not_called()
        tray_app.root.after.assert_called_once_with(25, tray_app._poll_startup_ready)

    def test_ready_tray_finishes_splash_once(self) -> None:
        tray_app = app.TrayApp.__new__(app.TrayApp)
        tray_app.startup_finished = False
        tray_app.tray_startup_complete = threading.Event()
        tray_app.tray_startup_complete.set()
        tray_app.tray_startup_error = None
        tray_app.splash = mock.Mock(started_at=100.0)
        tray_app.splash.minimum_time_has_elapsed.return_value = True
        tray_app.root = mock.Mock()

        with mock.patch.object(app.time, "monotonic", return_value=101.0):
            tray_app._poll_startup_ready()

        tray_app.splash.show_ready.assert_called_once_with()
        tray_app.root.after.assert_called_once_with(app.SPLASH_READY_VISIBLE_MS, tray_app._finish_startup)

    def test_tray_startup_timeout_fails_instead_of_hanging(self) -> None:
        tray_app = app.TrayApp.__new__(app.TrayApp)
        tray_app.startup_finished = False
        tray_app.tray_startup_complete = threading.Event()
        tray_app.tray_startup_error = None
        tray_app.splash = mock.Mock(started_at=100.0)
        tray_app.root = mock.Mock()
        tray_app._fail_startup = mock.Mock()

        with mock.patch.object(app.time, "monotonic", return_value=111.0):
            tray_app._poll_startup_ready()

        tray_app._fail_startup.assert_called_once()
        tray_app.root.after.assert_not_called()

    def test_synchronous_startup_failure_runs_cleanup(self) -> None:
        tray_app = app.TrayApp.__new__(app.TrayApp)
        tray_app.config = app.Config()
        tray_app._cleanup_failed_startup = mock.Mock()
        with mock.patch.object(app, "save_config", side_effect=RuntimeError("disk unavailable")):
            with self.assertRaisesRegex(RuntimeError, "disk unavailable"):
                tray_app.run()
        tray_app._cleanup_failed_startup.assert_called_once_with()

    def test_config_roundtrip_preserves_dictionary_replacements_and_audio_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            config = app.Config(
                api_key="secret",
                prompt="Nederlandse vergadering",
                custom_words=("Groq", "Clinon"),
                word_replacements=(("Grok", "Groq"), ("Grog", "Groq")),
                sample_rate=48_000,
                channels=2,
            )
            with (
                mock.patch.object(app, "APP_DIR", Path(directory)),
                mock.patch.object(app, "SETTINGS_PATH", settings_path),
                mock.patch.object(app, "try_read_api_key_from_keyring", return_value=(True, "secret")),
            ):
                app.save_config(config)
                stored = json.loads(settings_path.read_text(encoding="utf-8"))
                self.assertEqual(stored["custom_words"], ["Groq", "Clinon"])
                self.assertEqual(stored["word_replacements"], [["Grok", "Groq"], ["Grog", "Groq"]])
                self.assertEqual(stored["sample_rate"], 48_000)
                self.assertEqual(stored["channels"], 2)
                self.assertNotIn("api_key", stored)

    def test_legacy_settings_load_with_an_empty_dictionary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            settings_path.write_text(json.dumps({"prompt": "Bestaande prompt"}), encoding="utf-8")
            with (
                mock.patch.object(app, "SETTINGS_PATH", settings_path),
                mock.patch.object(app, "try_read_api_key_from_keyring", return_value=(True, "")),
                mock.patch.object(app, "load_dotenv_values", return_value={}),
            ):
                config = app.load_config()
            self.assertEqual(config.prompt, "Bestaande prompt")
            self.assertEqual(config.custom_words, ())
            self.assertEqual(config.word_replacements, ())

    def test_clearing_api_key_attempts_keyring_delete_after_read_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            with (
                mock.patch.object(app, "APP_DIR", Path(directory)),
                mock.patch.object(app, "SETTINGS_PATH", settings_path),
                mock.patch.object(app, "try_read_api_key_from_keyring", return_value=(False, "")),
                mock.patch.object(app, "write_api_key_to_keyring", return_value=True) as write_key,
            ):
                app.save_config(app.Config(api_key=""))
            write_key.assert_called_once_with("")

    def test_startup_save_never_deletes_secret_after_temporary_read_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            with (
                mock.patch.object(app, "APP_DIR", Path(directory)),
                mock.patch.object(app, "SETTINGS_PATH", settings_path),
                mock.patch.object(app, "load_dotenv_values", return_value={}),
                mock.patch.object(app, "try_read_api_key_from_keyring", return_value=(False, "")) as read_key,
                mock.patch.object(app, "write_api_key_to_keyring") as write_key,
            ):
                config = app.load_config()
                app.save_config(config, allow_keyring_mutation=config.keyring_read_succeeded)
            write_key.assert_not_called()
            self.assertEqual(read_key.call_count, 1)

    def test_startup_save_never_overwrites_secret_with_stale_fallback_after_read_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            settings_path.write_text(json.dumps({"api_key": "old-fallback"}), encoding="utf-8")
            with (
                mock.patch.object(app, "APP_DIR", Path(directory)),
                mock.patch.object(app, "SETTINGS_PATH", settings_path),
                mock.patch.object(app, "load_dotenv_values", return_value={}),
                mock.patch.object(app, "try_read_api_key_from_keyring", return_value=(False, "")) as read_key,
                mock.patch.object(app, "write_api_key_to_keyring") as write_key,
            ):
                config = app.load_config()
                app.save_config(config, allow_keyring_mutation=config.keyring_read_succeeded)
            self.assertEqual(config.api_key, "old-fallback")
            write_key.assert_not_called()
            self.assertEqual(read_key.call_count, 1)
            self.assertEqual(json.loads(settings_path.read_text(encoding="utf-8"))["api_key"], "old-fallback")

    def test_confirmed_frozen_start_removes_update_backup_without_callback_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "GroqInsertDictation.exe"
            backup = Path(f"{executable}.bak")
            backup.write_bytes(b"old executable")
            with (
                mock.patch.object(app.sys, "frozen", True, create=True),
                mock.patch.object(app, "current_exe_path", return_value=executable),
            ):
                app.cleanup_confirmed_update_backup()
            self.assertFalse(backup.exists())

    def test_wav_and_statistics_match_pcm_samples(self) -> None:
        frames = [
            np.array([[0, 1_000], [16_384, -1_000], [-16_384, 2_000]], dtype=np.int16),
            np.array([[32_767, -2_000], [-32_768, 3_000], [0, -3_000]], dtype=np.int16),
        ]
        session = app.RecordingSession(
            input_device=None,
            sample_rate=16_000,
            channels=2,
            model="whisper-large-v3-turbo",
            language="nl",
            prompt="Vocabulary: Groq.",
            word_replacements=(("Grok", "Groq"),),
            paste_after_transcription=True,
            client=mock.Mock(),
        )
        engine = app.DictationEngine.__new__(app.DictationEngine)
        path, duration, peak, rms = engine.write_wav_and_stats(session, frames)
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

        engine = app.DictationEngine(app.Config(api_key="test"))
        created: list[FakeStream] = []

        def make_stream(**kwargs):
            stream = FakeStream(**kwargs)
            created.append(stream)
            return stream

        start_thread = threading.Thread(target=engine.start_recording)
        with (
            mock.patch.object(app.sd, "InputStream", side_effect=make_stream),
            mock.patch.object(app, "play_sound"),
            mock.patch.object(app.time, "sleep"),
        ):
            start_thread.start()
            self.assertTrue(start_entered.wait(1))
            stop_thread = threading.Thread(target=engine.stop_recording)
            stop_thread.start()
            stop_thread.join(0.05)
            self.assertTrue(stop_thread.is_alive())
            allow_start.set()
            start_thread.join(2)
            stop_thread.join(2)
            self.assertFalse(start_thread.is_alive())
            self.assertFalse(stop_thread.is_alive())

        self.assertTrue(created[0].closed)
        self.assertIsNone(engine.stream)

    def test_transcription_sends_one_combined_prompt(self) -> None:
        client = mock.Mock()
        client.audio.transcriptions.create.return_value.text = "Groq en Clinon"
        session = app.RecordingSession(
            input_device=None,
            sample_rate=16_000,
            channels=1,
            model="whisper-large-v3-turbo",
            language="nl",
            prompt="Nederlandse vergadering\nVocabulary: Groq, Clinon.",
            word_replacements=(("Grok", "Groq"),),
            paste_after_transcription=True,
            client=client,
        )
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as audio:
            audio.write(b"RIFFtest")
            path = Path(audio.name)
        try:
            engine = app.DictationEngine.__new__(app.DictationEngine)
            self.assertEqual(engine.transcribe(session, path), "Groq en Clinon")
            kwargs = client.audio.transcriptions.create.call_args.kwargs
            self.assertEqual(kwargs["prompt"], session.prompt)
            self.assertEqual(client.audio.transcriptions.create.call_count, 1)
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
