"""Tray application, status bubble and thread hand-off; Qt offscreen or Windows desktop."""
import tempfile
import threading
import time
import types
import unittest
import wave
from pathlib import Path
from unittest import mock

from qt_support import qt_app

app_qt = qt_app()

import app  # noqa: E402
from audio_player import PlaybackStatus  # noqa: E402
from config_store import Config  # noqa: E402
from PySide6.QtCore import QRect, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


def pump(seconds: float = 0.02) -> None:
    deadline = time.monotonic() + seconds
    while True:
        QApplication.processEvents()
        if time.monotonic() >= deadline:
            return
        time.sleep(0.005)


def bare_tray() -> "app.TrayApp":
    tray = app.TrayApp.__new__(app.TrayApp)
    tray.qt = QApplication.instance()
    tray.startup_finished = False
    tray.fatal_startup_error = None
    tray.splash = mock.Mock(started_at=100.0)
    tray.startup_timer = mock.Mock()
    return tray


class GeometryTests(unittest.TestCase):
    def test_bubble_is_bottom_centered_in_the_work_area_and_clamped(self):
        self.assertEqual(app.bottom_centered_position(220, 72, QRect(0, 0, 1920, 1032)), (850, 932))
        self.assertEqual(app.bottom_centered_position(220, 72, QRect(1920, 0, 1280, 984)), (2450, 884))
        self.assertEqual(app.bottom_centered_position(300, 72, QRect(0, 0, 200, 50)), (0, 0))


class StartupTests(unittest.TestCase):
    def test_splash_waits_for_minimum_time_after_tray_is_ready(self):
        tray = bare_tray()
        tray.tray_ready = lambda: True
        tray.splash.minimum_time_has_elapsed.return_value = False
        with mock.patch.object(app.time, "monotonic", return_value=100.1), \
                mock.patch.object(app.QTimer, "singleShot") as single_shot:
            tray._poll_startup_ready()
        tray.splash.show_ready.assert_not_called()
        single_shot.assert_not_called()
        tray.startup_timer.stop.assert_not_called()

    def test_ready_tray_finishes_splash_once(self):
        tray = bare_tray()
        tray.tray_ready = lambda: True
        tray.splash.minimum_time_has_elapsed.return_value = True
        with mock.patch.object(app.time, "monotonic", return_value=101.0), \
                mock.patch.object(app.QTimer, "singleShot") as single_shot:
            tray._poll_startup_ready()
        tray.splash.show_ready.assert_called_once_with()
        single_shot.assert_called_once_with(app.SPLASH_READY_VISIBLE_MS, tray._finish_startup)
        tray.startup_timer.stop.assert_called_once_with()

    def test_tray_startup_timeout_fails_instead_of_hanging(self):
        tray = bare_tray()
        tray.tray_ready = lambda: False
        tray._fail_startup = mock.Mock()
        with mock.patch.object(app.time, "monotonic", return_value=111.0):
            tray._poll_startup_ready()
        tray._fail_startup.assert_called_once()
        self.assertIn("systeemvak", str(tray._fail_startup.call_args.args[0]))

    def test_synchronous_startup_failure_runs_cleanup(self):
        tray = bare_tray()
        tray.config = Config()
        tray._cleanup_failed_startup = mock.Mock()
        with mock.patch.object(app, "save_config", side_effect=RuntimeError("disk unavailable")):
            with self.assertRaisesRegex(RuntimeError, "disk unavailable"):
                tray.run()
        tray._cleanup_failed_startup.assert_called_once_with()

    def test_settings_open_only_after_the_splash_has_closed(self):
        tray = bare_tray()
        tray.config = Config(api_key="")
        tray.hotkey_error = None
        order = []
        tray.splash.destroy_splash.side_effect = lambda: order.append("splash closed")
        with mock.patch.object(app.QTimer, "singleShot", side_effect=lambda delay, fn: order.append(getattr(fn, "__name__", "callback"))):
            tray._finish_startup()
        self.assertEqual(order[0], "splash closed")
        self.assertIn("open_settings", order)
        tray._finish_startup()  # A second call is ignored.
        self.assertEqual(order.count("splash closed"), 1)


class EngineBridgeTests(unittest.TestCase):
    def test_worker_thread_callbacks_run_on_the_gui_thread(self):
        bridge = app.EngineBridge()
        seen = []
        bridge.state.connect(lambda state: seen.append((state, threading.current_thread() is threading.main_thread())))
        worker = threading.Thread(target=lambda: bridge.state.emit("recording"))
        worker.start()
        worker.join(2)
        deadline = time.monotonic() + 2
        while not seen and time.monotonic() < deadline:
            pump()
        self.assertEqual(seen, [("recording", True)])
        calls = []
        threading.Thread(target=lambda: bridge.run_on_main(lambda: calls.append(threading.current_thread() is threading.main_thread()))).start()
        deadline = time.monotonic() + 2
        while not calls and time.monotonic() < deadline:
            pump()
        self.assertEqual(calls, [True])


class StatusBubbleTests(unittest.TestCase):
    def setUp(self):
        self.levels = []
        self.bubble = app.StatusBubble(lambda: self.clicks.append(True))
        self.clicks = []
        self.bubble.audio_level_provider = lambda: self.levels.pop(0) if self.levels else 0.0
        self.addCleanup(self.bubble.destroy_bubble)

    def test_window_never_takes_focus(self):
        flags = self.bubble.windowFlags()
        for flag in (Qt.WindowType.WindowDoesNotAcceptFocus, Qt.WindowType.Tool,
                     Qt.WindowType.FramelessWindowHint, Qt.WindowType.WindowStaysOnTopHint):
            self.assertTrue(flags & flag, flag)
        self.assertTrue(self.bubble.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        self.assertTrue(self.bubble.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
        self.assertEqual(self.bubble.focusPolicy(), Qt.FocusPolicy.NoFocus)

    def test_states_resize_show_and_hide(self):
        self.bubble.set_state("recording")
        self.assertTrue(self.bubble.isVisible())
        self.assertLess(self.bubble.pill_size.width(), 196)
        self.assertTrue(self.bubble.wave_timer.isActive())
        self.bubble.set_state("processing")
        self.assertFalse(self.bubble.wave_timer.isActive())
        self.assertTrue(self.bubble.spinner_timer.isActive())
        self.assertIn("Transcriptie", self.bubble.windowTitle())
        self.bubble.set_state("idle")
        self.assertFalse(self.bubble.isVisible())
        self.assertFalse(self.bubble.spinner_timer.isActive())

    def test_notice_hides_after_a_few_seconds(self):
        self.bubble.show_notice("Transcriptie te kort")
        self.assertTrue(self.bubble.isVisible())
        self.assertTrue(self.bubble.hide_timer.isActive())
        self.assertEqual(self.bubble.hide_timer.interval(), app.NOTICE_VISIBLE_MS)
        self.assertIn("Transcriptie te kort", self.bubble.windowTitle())
        self.bubble.grab()  # Paints without errors.

    def test_waveform_scrolls_with_speech_and_settles_after_silence(self):
        bubble = self.bubble
        with mock.patch.object(app.time, "perf_counter", return_value=100.0) as clock:
            bubble.set_state("recording")
            silent = bubble.bar_heights()
            self.assertEqual(len(set(silent)), 1)
            clock.return_value = 101.0
            bubble.wave_tick()
            self.assertEqual(bubble.bar_heights(), silent)
            self.assertEqual(bubble.elapsed_label(), "00:01")
            self.levels[:] = [0.2]
            bubble.wave_tick()
            quiet = bubble.bar_heights()
            self.assertGreater(quiet[-1], silent[-1])
            self.levels[:] = [0.9]
            bubble.wave_tick()
            loud = bubble.bar_heights()
            self.assertGreater(loud[-1], quiet[-1])
            self.assertEqual(loud[-2], quiet[-1])  # Earlier speech scrolls left.
            bubble.wave_tick()
            tail = bubble.bar_heights()
            self.assertGreater(tail[-1], silent[-1])
            self.assertLess(tail[-1], loud[-1])
            for _ in range(app.WAVE_BAR_COUNT + 15):
                bubble.wave_tick()
            self.assertEqual(bubble.bar_heights(), silent)
            clock.return_value = 162.0
            self.assertEqual(bubble.elapsed_label(), "01:02")
            bubble.grab()

    def test_oldest_bars_fade_into_the_pill(self):
        self.assertLess(app.StatusBubble.bar_alpha(0), app.StatusBubble.bar_alpha(app.WAVE_BAR_COUNT - 1))
        self.assertEqual(app.StatusBubble.bar_alpha(app.WAVE_BAR_COUNT - 1), 1.0)

    def test_click_reports_to_the_app(self):
        from PySide6.QtTest import QTest

        self.bubble.set_state("recording")
        QTest.mouseClick(self.bubble, Qt.MouseButton.LeftButton)
        self.assertEqual(self.clicks, [True])


class TrayBehaviourTests(unittest.TestCase):
    def tray_with_engine(self, state="idle"):
        tray = bare_tray()
        tray.engine = types.SimpleNamespace(
            state=state, lock=threading.Lock(), stream_transition_lock=threading.Lock(),
            _resolve_device=mock.Mock(), on_shortcut=mock.Mock(),
        )
        tray.config = Config()
        tray.player = mock.Mock()
        tray.player.status.return_value = PlaybackStatus()
        tray.settings_window = None
        tray.bubble = mock.Mock()
        tray.open_settings = mock.Mock()
        return tray

    def test_bubble_click_stops_a_recording_or_opens_settings(self):
        tray = self.tray_with_engine("recording")
        tray.on_bubble_click()
        tray.engine.on_shortcut.assert_called_once_with()
        tray.open_settings.assert_not_called()
        tray.engine.state = "processing"
        tray.on_bubble_click()
        tray.open_settings.assert_not_called()
        tray.engine.state = "idle"
        tray.on_bubble_click()
        tray.open_settings.assert_called_once_with()

    def test_dictation_start_stops_playback_and_short_audio_shows_a_notice(self):
        tray = self.tray_with_engine()
        tray.set_engine_state("recording")
        tray.player.stop.assert_called_once_with()
        tray.bubble.set_state.assert_called_with("recording")
        tray.set_engine_state("too_short")
        tray.bubble.show_notice.assert_called_once_with("Transcriptie te kort")

    def test_refresh_releases_playback_before_reinitializing_portaudio(self):
        tray = self.tray_with_engine()
        order = []
        devices = [{"name": "TONOR", "max_input_channels": 1}]
        tray.player.release.side_effect = lambda: order.append("release")

        def initialize():
            order.append("initialize")
            devices[0] = {"name": "Realtek", "max_input_channels": 1}  # The USB microphone was unplugged.

        with (
            mock.patch.object(app.sd, "_terminate", side_effect=lambda: order.append("terminate"), create=True),
            mock.patch.object(app.sd, "_initialize", side_effect=initialize, create=True),
            mock.patch.object(app.engine.sd, "query_devices", return_value=devices, create=True),
            mock.patch.object(app.engine.sd, "query_hostapis", create=True, return_value=[
                {"name": "Windows WASAPI", "default_input_device": 0, "devices": [0]},
            ]),
        ):
            options = tray.list_input_devices(refresh=True)
            self.assertIn("Realtek", options[0][1])
            self.assertNotIn("TONOR", str(options))
            self.assertEqual(order, ["release", "terminate", "initialize"])
            tray.engine.state = "recording"
            with self.assertRaisesRegex(RuntimeError, "Stop eerst"):
                tray.list_input_devices(refresh=True)
            # Without an explicit refresh a busy engine keeps its streams.
            self.assertIn("Realtek", tray.list_input_devices()[0][1])
        self.assertEqual(order.count("terminate"), 1)

    def test_saved_recording_plays_through_the_seekable_player(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.wav"
            with wave.open(str(source), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(16000)
                audio.writeframes(b"\0\0" * 8000)
            tray = self.tray_with_engine()
            tray.recordings = app.RecordingHistory(Path(directory) / "recordings")
            entry = tray.recordings.add(source)
            tray.player.play.return_value = 0.5
            self.assertEqual(tray.play_recording(entry.id, 0.25), 0.5)
            tray.player.play.assert_called_once_with(entry.id, tray.recordings.audio_path(entry), 0.25)
            tray.stop_playback()
            tray.player.stop.assert_called_once_with()
            with self.assertRaises(RuntimeError):
                tray.play_recording("c" * 32)

    def test_clearing_history_is_refused_while_busy_and_releases_audio(self):
        tray = self.tray_with_engine("processing")
        tray.recordings = mock.Mock()
        tray.history = mock.Mock()
        with self.assertRaisesRegex(RuntimeError, "Wacht"):
            tray.clear_history()
        tray.recordings.clear.assert_not_called()
        tray.engine.state = "idle"
        tray.clear_history()
        tray.player.release.assert_called_once_with()
        tray.recordings.clear.assert_called_once_with()
        tray.history.clear.assert_called_once_with()

    def test_microphone_test_marks_the_engine_busy_and_frees_it_when_done(self):
        tray = self.tray_with_engine()
        tray.microphone_test = None
        created = []

        class FakeTest:
            def __init__(self, backend, device, *, wasapi, finished):
                self.device, self.wasapi, self.finished = device, wasapi, finished
                created.append(self)

            def start(self):
                pass

            def close(self):
                self.closed = True

        with (
            mock.patch.object(app, "MicrophoneTest", FakeTest),
            mock.patch.object(app, "resolve_input_device", return_value=7),
            mock.patch.object(app.sd, "_terminate", create=True),
            mock.patch.object(app.sd, "_initialize", create=True),
        ):
            tray.start_microphone_test("wasapi:TONOR")
        self.assertEqual(tray.engine.state, "testing")
        self.assertEqual((created[0].device, created[0].wasapi), (7, True))
        created[0].finished()
        self.assertEqual(tray.engine.state, "idle")
        tray.player.release.reset_mock()
        tray.player.status.return_value = PlaybackStatus("microphone-test", 1.0, 5.0, True)
        tray.stop_microphone_test()
        tray.player.release.assert_called_once_with()
        self.assertTrue(created[0].closed)
        self.assertIsNone(tray.microphone_test)

    def test_rejected_shortcut_falls_back_and_keeps_other_settings(self):
        tray = self.tray_with_engine()
        tray.config = Config(shortcut="insert", language="nl")
        tray.engine.update_config = mock.Mock()
        tray.hotkeys = mock.Mock()
        tray.hotkeys.set_hotkey.side_effect = [app.HotkeyError("bezet"), None]
        saved = []
        with mock.patch.object(app, "save_config", side_effect=lambda config: saved.append(config)), \
                mock.patch.object(app, "set_autostart"):
            with self.assertRaises(app.HotkeyError):
                tray.apply_settings(Config(shortcut="alt+z", language="en"))
        self.assertEqual(tray.config.shortcut, "insert")
        self.assertEqual(tray.config.language, "en")
        self.assertEqual(saved[-1].shortcut, "insert")


if __name__ == "__main__":
    unittest.main()
