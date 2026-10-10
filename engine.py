"""Recording, Windows audio devices, cues and Groq transcription.

UI-independent: the engine reports through plain callbacks, which the Qt app
turns into signals so worker threads never touch widgets.
"""

from __future__ import annotations

import logging
import math
import os
import queue
import tempfile
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyautogui
import pyperclip
import sounddevice as sd
import truststore

# Use Windows' trusted certificate store. This keeps TLS verification enabled
# while supporting managed networks that add a trusted inspection CA.
truststore.inject_into_ssl()

from groq import Groq

import config_store
from config_store import Config
from dictation_core import (
    DEFAULT_DEVICE_LABEL,
    append_trailing_space,
    apply_final_period_preference,
    apply_word_replacements,
    clipboard_text,
    compose_transcription_prompt,
    format_paragraphs,
)
from history import RecordingEntry, RecordingHistory
from microphone_test import windows_audio_thread

try:
    import winsound
except ImportError:  # pragma: no cover - Windows-only nicety
    winsound = None


MIN_TRANSCRIPTION_SECONDS = 1.0
MIN_TRANSCRIPTION_BYTES = 32_000
POST_STOP_RECORDING_SECONDS = 0.15
AUDIO_LEVEL_NOISE_FLOOR = 0.003
AUDIO_LEVEL_FULL_SCALE = 0.25
AUDIO_LEVEL_TIMEOUT_SECONDS = 0.25
# Dictaphone-style waveform: each tick adds one bar on the right and older bars
# scroll left, so the last ~0.7 s of speech stays visible.
WAVE_BAR_COUNT = 11
WAVE_TICK_MS = 60
WAVE_ATTACK = 0.7  # Fraction of a rise shown per tick: speech appears almost immediately.
WAVE_RELEASE = 0.6  # Per-tick decay after speech, a soft tail of roughly 0.2 s.
_SOUNDS_READY = False
_SOUNDS_LOCK = threading.Lock()


def make_tone(path: Path, notes: list[float]) -> None:
    sample_rate = 44_100
    note_duration = 0.09
    note_gap = 0.025
    attack_time = 0.015
    max_gain = 0.2
    note_samples = int(note_duration * sample_rate)
    gap_samples = int(note_gap * sample_rate)
    samples: list[int] = []

    for note_index, freq in enumerate(notes):
        decay_duration = note_duration - attack_time
        for i in range(note_samples):
            t = i / sample_rate
            sine = math.sin(2 * math.pi * freq * t)
            if t < attack_time:
                envelope = (t / attack_time) * max_gain
            else:
                decay_progress = (t - attack_time) / decay_duration
                envelope = max_gain * math.pow(0.0001 / max_gain, decay_progress)
            samples.append(int(sine * envelope * 32767))

        if note_index < len(notes) - 1:
            samples.extend([0] * gap_samples)

    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"".join(sample.to_bytes(2, "little", signed=True) for sample in samples))


def ensure_sounds() -> None:
    global _SOUNDS_READY
    if _SOUNDS_READY:
        return

    with _SOUNDS_LOCK:
        if _SOUNDS_READY:
            return
        sounds_dir = config_store.SOUNDS_DIR
        sounds_dir.mkdir(parents=True, exist_ok=True)
        marker = sounds_dir / ".groqandroid-cues-v1"
        sounds = {
            "start.wav": [523.25, 659.25],
            "success.wav": [587.33, 440.0],
            "error.wav": [246.94, 196.00],
        }
        generated = False
        for filename, notes in sounds.items():
            path = sounds_dir / filename
            if not path.exists() or not marker.exists():
                make_tone(path, notes)
                generated = True
        if generated or not marker.exists():
            marker.write_text("Generated from GroqAndroid cue parameters.\n", encoding="utf-8")
        _SOUNDS_READY = True


def play_sound(name: str) -> None:
    if winsound is None:
        return
    ensure_sounds()
    path = config_store.SOUNDS_DIR / name
    try:
        winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
    except RuntimeError as exc:
        logging.warning("Could not play sound %s: %s", name, exc)



def resolve_input_device(input_device: str) -> int | None:
    if not input_device:
        return None

    if input_device.isdigit():
        index = int(input_device)
        if sd.query_devices(index)["max_input_channels"] < 1:
            raise RuntimeError("De geselecteerde microfoon is niet beschikbaar.")
        return index

    if input_device.startswith("wasapi:"):
        name = input_device.removeprefix("wasapi:")
        devices = sd.query_devices()
        for host in sd.query_hostapis():
            if host["name"] == "Windows WASAPI":
                for index in host["devices"]:
                    if devices[index]["max_input_channels"] > 0 and devices[index]["name"] == name:
                        return index
        raise RuntimeError(f"Microfoon {name!r} is niet aangesloten of niet beschikbaar.")

    devices = sd.query_devices()
    needle = input_device.lower()
    for index, device in enumerate(devices):
        if device["max_input_channels"] > 0 and needle in device["name"].lower():
            return index

    raise RuntimeError(f"Geen input device gevonden voor {input_device!r}.")


def input_devices() -> list[tuple[str, str]]:
    available = sd.query_devices()
    default_name = None
    try:
        # MME's "Microsoft Sound Mapper" is an alias. WASAPI exposes the
        # actual Windows default endpoint and its complete friendly name.
        for host_api in sd.query_hostapis():
            if host_api["name"] == "Windows WASAPI":
                index = host_api["default_input_device"]
                if index >= 0:
                    default_name = available[index]["name"]
                break
        else:
            default_name = sd.query_devices(kind="input")["name"]
    except Exception as exc:
        logging.warning("Could not identify default input device: %s", exc)
    default_label = (
        f"{DEFAULT_DEVICE_LABEL}: {default_name}"
        if default_name else f"{DEFAULT_DEVICE_LABEL} (geen microfoon beschikbaar)"
    )
    devices = [("", default_label)]
    for host in sd.query_hostapis():
        if host["name"] == "Windows WASAPI":
            for index in host["devices"]:
                device = available[index]
                if device["max_input_channels"] > 0:
                    devices.append((f"wasapi:{device['name']}", device["name"]))
            return devices
    for index, device in enumerate(available):
        if device["max_input_channels"] > 0:
            devices.append((str(index), f"{index}: {device['name']}"))
    return devices


def stable_input_selector(selector: str) -> str:
    """Migrate old PortAudio indices/names before a refresh can renumber them."""
    if not selector or selector.startswith("wasapi:"):
        return selector
    try:
        index = resolve_input_device(selector)
        name = sd.query_devices(index)["name"]
        candidates = [device_id for device_id, label in input_devices() if device_id.startswith("wasapi:") and label.startswith(name)]
        if len(candidates) == 1:
            return candidates[0]
    except Exception:
        pass
    return selector


def smooth_audio_level(previous: float, target: float) -> float:
    """Follow louder input quickly and let quieter input fall back gradually."""
    if target >= previous:
        return previous + (target - previous) * WAVE_ATTACK
    level = max(target, previous * WAVE_RELEASE)
    return level if level >= 0.01 else 0.0


class _QaTranscriptions:
    def __init__(self, texts: list[str]) -> None:
        self.texts = texts
        self.calls = 0

    def create(self, **_kwargs):
        text = self.texts[min(self.calls, len(self.texts) - 1)]
        self.calls += 1
        return type("Transcription", (), {"text": text})()


class QaClient:
    """Offline stand-in for Groq, used only by isolated QA profiles."""

    def __init__(self, texts: list[str]) -> None:
        self.audio = type("Audio", (), {})()
        self.audio.transcriptions = _QaTranscriptions(texts)


def make_client(api_key: str):
    """Groq client for the saved key.

    An isolated QA profile (GROQ_DICTATION_PROFILE) may set
    GROQ_DICTATION_QA_TRANSCRIPTS="first|second" to exercise the packaged app
    end to end without network or a real key. Normal installs ignore it.
    """
    qa = os.getenv("GROQ_DICTATION_QA_TRANSCRIPTS", "")
    if qa and config_store.profile_name():
        return QaClient(qa.split("|"))
    return Groq(api_key=api_key) if api_key else None


@dataclass(frozen=True)
class RecordingSession:
    input_device: int | None
    sample_rate: int
    channels: int
    model: str
    language: str
    prompt: str
    word_replacements: tuple[tuple[str, str], ...]
    paste_after_transcription: bool
    remove_final_period: bool
    client: Groq
    auto_paragraphs: bool = True
    wasapi: bool = False



class DictationEngine:
    def __init__(
        self, config: Config, status_callback=None, state_callback=None, transcript_callback=None,
        recording_history: RecordingHistory | None = None, recording_callback=None,
    ) -> None:
        self.config = config
        self.status_callback = status_callback or (lambda message: None)
        self.state_callback = state_callback or (lambda state: None)
        self.transcript_callback = transcript_callback or (lambda text: None)
        self.recordings = recording_history if recording_history is not None else RecordingHistory(config_store.RECORDINGS_DIR)
        self.recording_callback = recording_callback or (lambda: None)
        self.input_device: int | None = None
        self.input_device_error: str | None = None
        self._resolve_device(config)
        self.client = make_client(config.api_key)
        self.audio_queue: queue.SimpleQueue = queue.SimpleQueue()
        self.audio_warning: str | None = None
        self.audio_level_reading = (0.0, 0.0)
        self.audio_level_peak = 0.0
        self.stream: sd.InputStream | None = None
        self.active_session: RecordingSession | None = None
        self.state = "idle"
        self.lock = threading.Lock()
        self.stream_transition_lock = threading.Lock()

        pyautogui.FAILSAFE = False
        pyautogui.PAUSE = 0

    def _resolve_device(self, config: Config) -> None:
        """Never let a vanished microphone block startup; report it when recording."""
        try:
            self.input_device = resolve_input_device(config.input_device)
            self.input_device_error = None
        except Exception as exc:
            self.input_device = None
            self.input_device_error = str(exc)
            logging.warning("Could not resolve input device: %s", exc)

    def update_config(self, config: Config) -> None:
        with self.lock:
            self.config = config
            self._resolve_device(config)
            self.client = make_client(config.api_key)

    def notify(self, message: str) -> None:
        logging.info(message)
        self.status_callback(message)

    def emit_state(self, state: str) -> None:
        self.state_callback(state)

    def on_shortcut(self) -> None:
        """Toggle recording. Returns immediately; the work runs on a worker thread.

        Opening the microphone or flushing the stream can take hundreds of
        milliseconds, which must never block the thread that reports the
        shortcut (Windows silently drops slow keyboard hooks).
        """
        threading.Thread(target=self.toggle_recording, name="dictation-toggle", daemon=True).start()

    def toggle_recording(self) -> None:
        try:
            with self.lock:
                state = self.state

            if state == "idle":
                self.start_recording()
            elif state == "recording":
                self.stop_recording()
            elif state == "testing":
                self.notify("Microfoontest bezig. Stop de test in Instellingen.")
            else:
                self.notify("Nog bezig met transcriberen; shortcut genegeerd.")
        except Exception as exc:
            with self.lock:
                self.state = "idle"
            self.emit_state("idle")
            self.notify(f"Kon opname niet starten/stoppen: {exc}")
            play_sound("error.wav")

    def start_recording(self) -> None:
        with self.stream_transition_lock, windows_audio_thread():
            self._start_recording()

    def _start_recording(self) -> None:
        with self.lock:
            if self.state != "idle":
                return
            config = self.config
            if self.input_device_error:
                self.notify(f"{self.input_device_error} Kies een andere microfoon in Instellingen.")
                play_sound("error.wav")
                return
            if self.client is None or (not config.api_key and not isinstance(self.client, QaClient)):
                session = None
            else:
                session = RecordingSession(
                    input_device=self.input_device,
                    sample_rate=config.sample_rate,
                    channels=config.channels,
                    model=config.model,
                    language=config.language,
                    prompt=compose_transcription_prompt(config.prompt, config.custom_words),
                    word_replacements=config.word_replacements,
                    paste_after_transcription=config.paste_after_transcription,
                    remove_final_period=config.remove_final_period,
                    client=self.client,
                    auto_paragraphs=config.auto_paragraphs,
                    wasapi=config.input_device.startswith("wasapi:"),
                )

            if session is None:
                self.notify("Open Instellingen en vul eerst je Groq API key in.")
                play_sound("error.wav")
                return
            self.state = "recording"
            self.active_session = session
            self.audio_queue = queue.SimpleQueue()
            self.audio_warning = None
            self.audio_level_reading = (0.0, 0.0)
            self.audio_level_peak = 0.0

        stream: sd.InputStream | None = None
        try:
            stream = sd.InputStream(
                device=session.input_device,
                samplerate=session.sample_rate,
                channels=session.channels,
                dtype="int16",
                callback=self.audio_callback,
                **({"extra_settings": sd.WasapiSettings(auto_convert=True)} if session.wasapi else {}),
            )
            stream.start()
            self.stream = stream
        except Exception:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    logging.exception("Could not close failed input stream")
            with self.lock:
                self.state = "idle"
                self.active_session = None
                self.stream = None
            self.emit_state("idle")
            raise

        self.emit_state("recording")
        play_sound("start.wav")
        self.notify("Opname gestart. Gebruik je shortcut opnieuw om te stoppen.")

    def stop_recording(self) -> None:
        with self.stream_transition_lock, windows_audio_thread():
            self._stop_recording()

    def _stop_recording(self) -> None:
        with self.lock:
            if self.state != "recording":
                return
            self.state = "processing"
            stream = self.stream
            self.stream = None
            session = self.active_session
            self.active_session = None
        self.emit_state("processing")

        if stream is not None:
            time.sleep(POST_STOP_RECORDING_SECONDS)
            try:
                stream.stop()
            except Exception as exc:
                logging.warning("Could not stop input stream cleanly: %s", exc)
            finally:
                try:
                    stream.close()
                except Exception as exc:
                    logging.warning("Could not close input stream cleanly: %s", exc)

        captured_frames: list[np.ndarray] = []
        while True:
            try:
                captured_frames.append(self.audio_queue.get_nowait())
            except queue.Empty:
                break

        if self.audio_warning:
            self.notify(f"Audio waarschuwing: {self.audio_warning}")
        self.notify("Opname gestopt. Transcriberen...")
        if session is None:
            raise RuntimeError("Opnamesessie ontbreekt.")
        threading.Thread(
            target=self.transcribe_and_output,
            args=(session, captured_frames),
            daemon=True,
        ).start()

    def audio_callback(self, indata, frames, time_info, status) -> None:
        if status:
            self.audio_warning = str(status)
        self.audio_queue.put(indata.copy())
        # The stream supplies int16 PCM. Cast before squaring to avoid overflow.
        values = np.asarray(indata, dtype=np.float32)
        rms = math.sqrt(float(np.mean(np.square(values)))) / 32768.0 if values.size else 0.0
        level = 0.0
        if rms > AUDIO_LEVEL_NOISE_FLOOR:
            level = min(
                1.0,
                math.log(rms / AUDIO_LEVEL_NOISE_FLOOR)
                / math.log(AUDIO_LEVEL_FULL_SCALE / AUDIO_LEVEL_NOISE_FLOOR),
            )
        # Publish snapshots; the UI timer reads them without queuing UI work
        # from PortAudio's callback thread. The peak keeps short syllables that
        # fall between two UI ticks.
        self.audio_level_peak = max(self.audio_level_peak, level)
        self.audio_level_reading = (level, time.monotonic())

    def get_audio_level(self) -> float:
        """Loudest level since the previous read, or the latest level if no new audio arrived yet."""
        level, updated_at = self.audio_level_reading
        peak, self.audio_level_peak = self.audio_level_peak, 0.0
        if self.state != "recording" or time.monotonic() - updated_at > AUDIO_LEVEL_TIMEOUT_SECONDS:
            return 0.0
        return max(peak, level)

    def write_wav_and_stats(
        self,
        session: RecordingSession,
        frames: list[np.ndarray],
    ) -> tuple[Path, float, float, float]:
        if not frames:
            raise RuntimeError("Geen audio opgenomen.")

        temp = tempfile.NamedTemporaryFile(
            prefix="groq-insert-dictation-",
            suffix=".wav",
            delete=False,
        )
        temp_path = Path(temp.name)
        temp.close()

        frame_count = 0
        sample_count = 0
        peak_value = 0.0
        sum_of_squares = 0.0
        try:
            with wave.open(str(temp_path), "wb") as wav:
                wav.setnchannels(session.channels)
                wav.setsampwidth(2)
                wav.setframerate(session.sample_rate)
                for frame in frames:
                    wav.writeframesraw(frame.tobytes())
                    values = np.asarray(frame, dtype=np.float64)
                    frame_count += len(frame)
                    sample_count += values.size
                    if values.size:
                        peak_value = max(peak_value, float(np.max(np.abs(values))))
                        sum_of_squares += float(np.sum(np.square(values)))
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

        duration = frame_count / session.sample_rate
        peak = peak_value / 32768.0 if sample_count else 0.0
        rms = math.sqrt(sum_of_squares / sample_count) / 32768.0 if sample_count else 0.0
        return temp_path, duration, peak, rms

    def retry_recording(self, recording_id: str) -> None:
        """Retry saved audio with current settings, without pasting into Settings."""
        with self.lock:
            if self.state != "idle":
                raise RuntimeError("Wacht tot de huidige opname of transcriptie klaar is.")
            config = self.config
            if self.client is None or (not config.api_key and not isinstance(self.client, QaClient)):
                raise RuntimeError("Vul eerst je Groq API key in en sla je instellingen op.")
            entry = self.recordings.get(recording_id)
            session = RecordingSession(
                input_device=None, sample_rate=config.sample_rate, channels=config.channels,
                model=config.model, language=config.language,
                prompt=compose_transcription_prompt(config.prompt, config.custom_words),
                word_replacements=config.word_replacements, paste_after_transcription=False,
                remove_final_period=config.remove_final_period, client=self.client,
                auto_paragraphs=config.auto_paragraphs,
            )
            self.state = "processing"
        try:
            self.emit_state("processing")
            threading.Thread(
                target=self.transcribe_and_output, args=(session, [], entry),
                name="dictation-retry", daemon=True,
            ).start()
        except Exception:
            with self.lock:
                self.state = "idle"
            self.emit_state("idle")
            raise

    def update_recording(self, entry: RecordingEntry, *, status: str, text: str | None = None, error: str = "") -> None:
        self.recordings.update(entry.id, status=status, text=text, error=error)
        self.recording_changed()

    def recording_changed(self) -> None:
        try:
            self.recording_callback()
        except Exception:
            logging.exception("Could not refresh recording history")

    def transcribe_and_output(
        self, session: RecordingSession, frames: list[np.ndarray], recording: RecordingEntry | None = None,
    ) -> None:
        wav_path: Path | None = None
        temp_path: Path | None = None
        started_at = time.perf_counter()
        show_idle_bubble = True
        try:
            if recording is None:
                temp_path, duration, peak, rms = self.write_wav_and_stats(session, frames)
                frames.clear()
                # Commit the WAV before any request, including recordings that
                # are too short or receive an empty/error response from Groq.
                try:
                    recording = self.recordings.add(temp_path)
                except Exception as exc:
                    preserved_path = temp_path
                    temp_path = None  # Keep the original as a last-resort backup.
                    raise RuntimeError(
                        f"Opname opslaan in Geschiedenis mislukt. Audio staat nog in {preserved_path}: {exc}"
                    ) from exc
                wav_path = self.recordings.audio_path(recording)
                self.recording_changed()
                self.notify(f"Audio: {duration:.1f}s, piek {peak:.3f}, rms {rms:.3f}")
                if duration < MIN_TRANSCRIPTION_SECONDS or wav_path.stat().st_size < MIN_TRANSCRIPTION_BYTES:
                    self.update_recording(recording, status="failed", error="Opname te kort. Je kunt opnieuw proberen.")
                    self.notify("Transcriptie te kort. Er is niets geplakt.")
                    self.emit_state("too_short")
                    play_sound("error.wav")
                    show_idle_bubble = False
                    return

                if peak < 0.01:
                    self.notify("Waarschuwing: bijna geen inputvolume gemeten. Check microfoon/device.")
            else:
                wav_path = self.recordings.audio_path(recording)

            self.update_recording(recording, status="processing")

            text = apply_word_replacements(
                self.transcribe(session, wav_path).strip(),
                session.word_replacements,
            )
            if session.auto_paragraphs:
                text = format_paragraphs(text)
            text = apply_final_period_preference(
                text,
                remove_final_period=session.remove_final_period,
            )
            elapsed = time.perf_counter() - started_at

            if not text:
                raise RuntimeError("Geen tekst herkend. Opname bewaard; probeer opnieuw via Geschiedenis.")

            if set(text) == {"*"}:
                raise RuntimeError("Groq gaf alleen sterretjes terug. Check je microfoon of probeer opnieuw via Geschiedenis.")

            self.update_recording(recording, status="done", text=text)
            pasted_text = append_trailing_space(text)
            pyperclip.copy(clipboard_text(pasted_text))
            self.notify(f"Transcriptie klaar in {elapsed:.1f}s. Tekst staat op je klembord.")
            try:
                self.transcript_callback(text)
            except Exception:
                logging.exception("Could not record transcription history")

            if session.paste_after_transcription:
                try:
                    pyautogui.hotkey("ctrl", "v")
                    self.notify("Geplakt in het actieve venster.")
                except Exception as exc:
                    self.notify(f"Automatisch plakken mislukte, maar de tekst staat op je klembord: {exc}")

            play_sound("success.wav")
        except Exception as exc:
            if recording is not None:
                self.update_recording(recording, status="failed", error=str(exc))
            self.notify(f"Fout: {exc}")
            play_sound("error.wav")
        finally:
            frames.clear()
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            with self.lock:
                self.state = "idle"
            if show_idle_bubble:
                self.emit_state("idle")
            self.recording_changed()
            self.notify("Klaar. Gebruik je shortcut voor een nieuwe opname.")

    def transcribe(self, session: RecordingSession, wav_path: Path) -> str:
        kwargs = {
            "file": wav_path.open("rb"),
            "model": session.model,
            "response_format": "json",
            "temperature": 0.0,
        }
        if session.language:
            kwargs["language"] = session.language
        if session.prompt:
            kwargs["prompt"] = session.prompt

        with kwargs["file"] as audio_file:
            kwargs["file"] = audio_file
            try:
                transcription = session.client.audio.transcriptions.create(**kwargs)
            except Exception as exc:
                error_text = str(exc).lower()
                if "prompt" in error_text and ("224" in error_text or "token" in error_text):
                    raise RuntimeError(
                        "Prompt en woordenboek zijn samen te lang voor Groq. "
                        "Maak de Prompt korter of verwijder enkele woorden."
                    ) from exc
                raise

        return getattr(transcription, "text", "") or ""

    def shutdown(self) -> None:
        with self.stream_transition_lock, windows_audio_thread():
            self._shutdown()

    def _shutdown(self) -> None:
        with self.lock:
            stream = self.stream
            self.stream = None
            self.active_session = None
            self.state = "idle"
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                try:
                    stream.stop()
                except Exception:
                    pass
            finally:
                try:
                    stream.close()
                except Exception:
                    pass
