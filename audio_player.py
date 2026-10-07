"""Seekable WAV playback through PortAudio for History and the microphone test.

``winsound`` can neither seek nor report progress, so the old player copied
the rest of a WAV into a temporary file to jump and guessed the position from
the clock. This player keeps the PCM in memory, streams it from the requested
frame and reports the frame that was actually handed to the device.
"""

from __future__ import annotations

import logging
import threading
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from microphone_test import windows_audio_thread


@dataclass(frozen=True)
class PlaybackStatus:
    key: str = ""
    position: float = 0.0
    duration: float = 0.0
    playing: bool = False
    finished: bool = False
    error: str = ""


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """Return int16 frames shaped (frames, channels) and the sample rate."""
    with wave.open(str(path), "rb") as audio:
        if audio.getsampwidth() != 2:
            raise RuntimeError("Alleen 16-bit WAV-opnames kunnen worden afgespeeld.")
        channels = audio.getnchannels()
        rate = audio.getframerate()
        data = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2")
    return data.reshape(-1, channels).copy(), rate


class AudioPlayer:
    def __init__(self, backend) -> None:
        self.backend = backend
        self._lock = threading.RLock()
        self._stream = None
        self._frames: np.ndarray | None = None
        self._rate = 0
        self._position = 0
        self._key = ""
        self._finished = False
        self._error = ""

    def play(self, key: str, path: Path, offset: float = 0.0) -> float:
        """Start ``path`` at ``offset`` seconds; returns the duration."""
        frames, rate = read_wav(path)
        with self._lock:
            self._close_stream()
            self._frames = frames
            self._rate = rate
            self._key = key
            self._finished = False
            self._error = ""
            total = len(frames)
            self._position = min(total, max(0, round(offset * rate)))
            if self._position >= total:
                self._finished = True
                return total / rate if rate else 0.0
            with windows_audio_thread():
                stream = self.backend.OutputStream(
                    samplerate=rate, channels=frames.shape[1], dtype="int16",
                    callback=self._callback, finished_callback=self._on_finished,
                )
                try:
                    stream.start()
                except Exception:
                    stream.close()
                    raise
            self._stream = stream
            return total / rate

    def _callback(self, outdata, frame_count, _time, status) -> None:
        if status:
            self._error = str(status)
        frames = self._frames
        if frames is None:
            outdata.fill(0)
            raise self.backend.CallbackStop
        start = self._position
        chunk = frames[start:start + frame_count]
        outdata[:len(chunk)] = chunk
        if len(chunk) < frame_count:
            outdata[len(chunk):] = 0
        self._position = start + len(chunk)
        if self._position >= len(frames):
            raise self.backend.CallbackStop

    def _on_finished(self) -> None:
        frames = self._frames
        if frames is not None and self._position >= len(frames):
            self._finished = True

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            with windows_audio_thread():
                try:
                    stream.abort()
                finally:
                    stream.close()
        except Exception as exc:
            logging.warning("Could not close playback stream: %s", exc)

    def stop(self) -> None:
        """Pause: the position is kept for :meth:`status`."""
        with self._lock:
            self._close_stream()

    def release(self) -> None:
        """Stop and forget the loaded audio."""
        with self._lock:
            self._close_stream()
            self._frames = None
            self._key = ""
            self._position = 0
            self._finished = False

    def status(self) -> PlaybackStatus:
        with self._lock:
            if self._frames is None or not self._rate:
                return PlaybackStatus(error=self._error)
            stream = self._stream
            if self._finished and stream is not None:
                self._close_stream()  # Release the device once the end is reached.
                stream = None
            playing = bool(stream is not None and stream.active and not self._finished)
            return PlaybackStatus(
                key=self._key, position=self._position / self._rate,
                duration=len(self._frames) / self._rate, playing=playing,
                finished=self._finished, error=self._error,
            )
