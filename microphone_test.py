"""Local microphone check; audio stays in memory until explicitly played back."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import math
import os
from pathlib import Path
import tempfile
import threading
import time
import wave

import numpy as np


@contextmanager
def windows_audio_thread():
    """WASAPI needs COM initialized on every thread opening or closing a stream."""
    if os.name != "nt":
        yield
        return
    ole = ctypes.WinDLL("ole32")
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    ole.CoInitializeEx.restype = ctypes.c_long
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    result = ole.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
    # An existing STA also provides COM; leave its initialization untouched.
    if result < 0 and result != -2147417850:  # RPC_E_CHANGED_MODE
        raise OSError(f"Windows audio-initialisatie mislukt (0x{result & 0xffffffff:08x}).")
    try:
        yield
    finally:
        if result >= 0:
            ole.CoUninitialize()


@dataclass(frozen=True)
class MicrophoneTestStatus:
    state: str = "idle"
    level: float = 0.0
    elapsed: float = 0.0
    duration: float = 0.0
    error: str = ""
    heard_audio: bool = False


class MicrophoneTest:
    def __init__(self, backend, device, *, seconds=5.0, wasapi=False, finished=lambda: None):
        self.backend = backend
        self.device = device
        self.seconds = seconds
        self.wasapi = wasapi
        self.finished = finished
        self.state = "opening"
        self.error = ""
        self.sample_rate = 0
        self.frames = []
        self.sample_count = 0
        self.peak_rms = 0.0
        self.level = 0.0
        self.updated_at = 0.0
        self.stop_requested = threading.Event()
        self.complete = threading.Event()
        self._directory = None
        self.thread = threading.Thread(target=self._record, name="microphone-test", daemon=True)

    def start(self):
        self.thread.start()

    def snapshot(self):
        elapsed = self.sample_count / self.sample_rate if self.sample_rate else 0.0
        level = self.level if time.monotonic() - self.updated_at < 0.3 else 0.0
        return MicrophoneTestStatus(
            self.state, level, elapsed, self.seconds, self.error, self.peak_rms > 0.003,
        )

    def _callback(self, data, frames, _time, status):
        if status:
            self.error = str(status)
        remaining = max(0, round(self.seconds * self.sample_rate) - self.sample_count)
        samples = data[:remaining].copy()
        if not samples.size:
            return
        self.frames.append(samples)
        self.sample_count += len(samples)
        rms = math.sqrt(float(np.mean(np.square(samples.astype(np.float32))))) / 32768.0
        self.peak_rms = max(self.peak_rms, rms)
        self.level = min(1.0, max(0.0, math.log(max(rms, 0.003) / 0.003) / math.log(0.25 / 0.003)))
        self.updated_at = time.monotonic()
        if self.sample_count >= round(self.seconds * self.sample_rate):
            self.complete.set()

    def _record(self):
        try:
            with windows_audio_thread():
                self._capture()
        except Exception as exc:
            self.error = str(exc)
            self.state = "error"
            self.frames.clear()
        finally:
            self.finished()

    def _capture(self):
        stream = None
        try:
            info = self.backend.query_devices(self.device, "input")
            if info["max_input_channels"] < 1:
                raise RuntimeError("De geselecteerde microfoon is niet beschikbaar.")
            self.sample_rate = round(info["default_samplerate"])
            stream = self.backend.InputStream(
                device=self.device, samplerate=self.sample_rate, channels=1,
                dtype="int16", callback=self._callback,
                **({"extra_settings": self.backend.WasapiSettings(auto_convert=True)} if self.wasapi else {}),
            )
            stream.start()
            self.state = "recording"
            deadline = time.monotonic() + self.seconds + 2
            while not self.complete.wait(0.05):
                if self.stop_requested.is_set():
                    break
                if not stream.active:
                    raise RuntimeError("De microfoon is gestopt of losgekoppeld.")
                if time.monotonic() > deadline:
                    raise RuntimeError("Geen audio ontvangen. Controleer de aansluiting van de microfoon.")
            stream.stop()
            stream.close()
            stream = None
            if self.stop_requested.is_set():
                self.state = "cancelled"
                self.frames.clear()
            elif not self.sample_count:
                raise RuntimeError("Geen audio ontvangen van de microfoon.")
            else:
                self.state = "ready"
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass

    def playback_path(self) -> Path:
        if self.state != "ready" or not self.frames:
            raise RuntimeError("Maak eerst een testopname.")
        if self._directory is None:
            self._directory = tempfile.TemporaryDirectory(prefix="groq-microphone-test-")
        path = Path(self._directory.name) / "test.wav"
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(self.sample_rate)
            for frame in self.frames:
                audio.writeframes(frame.tobytes())
        return path

    def close(self):
        self.stop_requested.set()
        if self.thread.is_alive():
            self.thread.join(timeout=2)
        if self._directory is not None:
            self._directory.cleanup()
            self._directory = None
