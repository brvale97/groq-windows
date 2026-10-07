"""Seekable playback against a fake PortAudio backend; runs on any OS."""
import tempfile
import threading
import unittest
import wave
from pathlib import Path

import numpy as np

from audio_player import AudioPlayer, read_wav


class CallbackStop(Exception):
    pass


class FakeOutputStream:
    def __init__(self, backend, *, samplerate, channels, dtype, callback, finished_callback):
        self.backend = backend
        self.samplerate = samplerate
        self.channels = channels
        self.dtype = dtype
        self.callback = callback
        self.finished_callback = finished_callback
        self.active = False
        self.closed = False
        self.played = []
        backend.streams.append(self)

    def start(self):
        if self.backend.fail_start:
            raise RuntimeError("Geen uitvoerapparaat")
        self.active = True

    def pull(self, frames: int) -> np.ndarray:
        """Act like the device: ask for one block of audio."""
        out = np.full((frames, self.channels), 7, dtype=np.int16)
        try:
            self.callback(out, frames, None, None)
        except CallbackStop:
            self.active = False
            self.played.append(out.copy())
            self.finished_callback()
            return out
        self.played.append(out.copy())
        return out

    def abort(self):
        self.active = False

    def close(self):
        self.closed = True
        self.active = False


class FakeBackend:
    CallbackStop = CallbackStop

    def __init__(self):
        self.streams = []
        self.fail_start = False

    def OutputStream(self, **kwargs):
        return FakeOutputStream(self, **kwargs)


def write_wav(path: Path, samples: np.ndarray, rate: int = 1000) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(samples.shape[1])
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(samples.astype("<i2").tobytes())


class AudioPlayerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "clip.wav"
        self.samples = np.arange(2000, dtype=np.int16).reshape(-1, 1)  # 2 s at 1 kHz
        write_wav(self.path, self.samples)
        self.backend = FakeBackend()
        self.player = AudioPlayer(self.backend)

    def test_reads_pcm_exactly(self):
        frames, rate = read_wav(self.path)
        self.assertEqual(rate, 1000)
        np.testing.assert_array_equal(frames, self.samples)

    def test_plays_from_an_offset_without_temporary_copies(self):
        duration = self.player.play("a", self.path, offset=0.5)
        self.assertEqual(duration, 2.0)
        stream = self.backend.streams[-1]
        block = stream.pull(100)
        np.testing.assert_array_equal(block[:, 0], np.arange(500, 600))
        status = self.player.status()
        self.assertEqual((status.key, status.playing), ("a", True))
        self.assertAlmostEqual(status.position, 0.6)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_pause_keeps_position_and_releases_the_device(self):
        self.player.play("a", self.path)
        stream = self.backend.streams[-1]
        stream.pull(250)
        self.player.stop()
        self.assertTrue(stream.closed)
        status = self.player.status()
        self.assertFalse(status.playing)
        self.assertAlmostEqual(status.position, 0.25)

    def test_end_of_audio_is_reported_and_the_stream_closed(self):
        self.player.play("a", self.path, offset=1.95)
        stream = self.backend.streams[-1]
        block = stream.pull(100)
        np.testing.assert_array_equal(block[:50, 0], np.arange(1950, 2000))
        self.assertTrue((block[50:] == 0).all())  # Silence after the end, never stale data.
        status = self.player.status()
        self.assertTrue(status.finished)
        self.assertFalse(status.playing)
        self.assertTrue(stream.closed)

    def test_starting_another_clip_stops_the_previous_one(self):
        self.player.play("a", self.path)
        first = self.backend.streams[-1]
        self.player.play("b", self.path, offset=1.0)
        self.assertTrue(first.closed)
        self.assertEqual(self.player.status().key, "b")

    def test_stereo_and_seek_beyond_the_end(self):
        stereo = np.stack([np.arange(100), -np.arange(100)], axis=1).astype(np.int16)
        write_wav(self.path, stereo, rate=100)
        self.player.play("s", self.path, offset=0.2)
        block = self.backend.streams[-1].pull(10)
        np.testing.assert_array_equal(block, stereo[20:30])
        self.player.play("s", self.path, offset=5.0)
        self.assertTrue(self.player.status().finished)

    def test_device_failure_is_raised_and_leaves_no_open_stream(self):
        self.backend.fail_start = True
        with self.assertRaisesRegex(RuntimeError, "uitvoerapparaat"):
            self.player.play("a", self.path)
        self.assertTrue(self.backend.streams[-1].closed)
        self.assertFalse(self.player.status().playing)

    def test_release_forgets_audio(self):
        self.player.play("a", self.path)
        self.player.release()
        self.assertEqual(self.player.status().key, "")

    def test_status_is_safe_while_the_device_thread_plays(self):
        self.player.play("a", self.path)
        stream = self.backend.streams[-1]
        stop = threading.Event()

        def device():
            while not stop.is_set() and stream.active:
                stream.pull(10)

        thread = threading.Thread(target=device)
        thread.start()
        for _ in range(200):
            status = self.player.status()
            self.assertLessEqual(status.position, status.duration)
        stop.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
