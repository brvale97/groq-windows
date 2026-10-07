import threading
import unittest
import wave
from unittest import mock

import numpy as np

from microphone_test import MicrophoneTest


class FakeAudio:
    def __init__(self, value=2000, *, disconnected=False, continuous=False):
        self.value = value
        self.disconnected = disconnected
        self.continuous = continuous
        self.started = threading.Event()
        self.stream = None

    def query_devices(self, device, kind):
        self.selected_device = device
        return {'max_input_channels': 1, 'default_samplerate': 8000}

    def InputStream(self, **kwargs):
        backend = self
        if self.disconnected:
            raise RuntimeError('Microfoon is losgekoppeld')

        class Stream:
            active = True
            closed = False

            def start(self):
                backend.started.set()
                if not backend.continuous:
                    samples = np.full((9000, 1), backend.value, dtype=np.int16)
                    kwargs['callback'](samples, len(samples), None, None)

            def stop(self):
                self.active = False

            def close(self):
                self.closed = True
                self.active = False

        self.stream = Stream()
        return self.stream


class MicrophoneCaptureTests(unittest.TestCase):
    def test_capture_is_bounded_and_playback_matches_selected_input(self):
        backend = FakeAudio()
        finished = []
        capture = MicrophoneTest(backend, 7, seconds=1, finished=lambda: finished.append(True))
        try:
            capture.start()
            capture.thread.join(2)
            status = capture.snapshot()
            self.assertEqual(status.state, 'ready')
            self.assertEqual(status.elapsed, 1)
            self.assertTrue(status.heard_audio)
            self.assertEqual(backend.selected_device, 7)
            self.assertTrue(backend.stream.closed)
            self.assertEqual(finished, [True])
            path = capture.playback_path()
            with wave.open(str(path), 'rb') as audio:
                self.assertEqual((audio.getnchannels(), audio.getframerate(), audio.getnframes()), (1, 8000, 8000))
                self.assertEqual(audio.readframes(1), np.array([2000], dtype=np.int16).tobytes())
        finally:
            capture.close()
        self.assertFalse(path.exists())

    def test_silence_is_reported_without_claiming_the_microphone_works(self):
        capture = MicrophoneTest(FakeAudio(0), None, seconds=1)
        try:
            capture.start()
            capture.thread.join(2)
            self.assertEqual(capture.snapshot().state, 'ready')
            self.assertFalse(capture.snapshot().heard_audio)
            self.assertEqual(capture.snapshot().level, 0)
        finally:
            capture.close()

    def test_disconnect_releases_audio_ownership_and_cannot_be_played(self):
        finished = threading.Event()
        capture = MicrophoneTest(FakeAudio(disconnected=True), 4, finished=finished.set)
        try:
            capture.start()
            capture.thread.join(2)
            self.assertEqual(capture.snapshot().state, 'error')
            self.assertIn('losgekoppeld', capture.snapshot().error)
            self.assertTrue(finished.is_set())
            with self.assertRaises(RuntimeError):
                capture.playback_path()
        finally:
            capture.close()

    def test_audio_initialization_failure_releases_owner_and_reports_the_error(self):
        finished = threading.Event()
        capture = MicrophoneTest(FakeAudio(), 4, finished=finished.set)
        with mock.patch('microphone_test.windows_audio_thread', side_effect=OSError('Windows audio niet beschikbaar')):
            capture.start()
            capture.thread.join(2)
        self.assertEqual(capture.snapshot().state, 'error')
        self.assertIn('Windows audio niet beschikbaar', capture.snapshot().error)
        self.assertTrue(finished.is_set())

    def test_cancelling_closes_stream_and_discards_audio(self):
        backend = FakeAudio(continuous=True)
        capture = MicrophoneTest(backend, 2)
        capture.start()
        self.assertTrue(backend.started.wait(2))
        capture.close()
        self.assertFalse(capture.thread.is_alive())
        self.assertEqual(capture.snapshot().state, 'cancelled')
        self.assertTrue(backend.stream.closed)
        self.assertEqual(capture.frames, [])


if __name__ == '__main__':
    unittest.main()
