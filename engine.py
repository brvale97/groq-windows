"""Recording, Windows audio devices, cues and Groq transcription.

UI-independent: the engine reports through plain callbacks, which the Qt app
turns into signals so worker threads never touch widgets.
"""

from __future__ import annotations

import dataclasses
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
# Importing pyautogui makes the process system DPI aware before Qt starts, which
# is how the UI has always been scaled. Pasting lives in windows_paste now.
import pyautogui  # noqa: F401
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
from speech_detection import NoSpeechDetected, prepare_speech_detection, response_is_silence, speech_upload
from text_cleanup import CleanupError, clean_transcript, compose_cleanup_prompt
from windows_paste import paste_clipboard

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
# During a recording: no audio callback for this long means the microphone is
# gone (a Bluetooth headset that dropped, a USB microphone pulled out).
INPUT_STALL_SECONDS = 1.0
INPUT_WATCH_INTERVAL_SECONDS = 0.2
# Every this many watch ticks, also check that Windows still lists the microphone.
INPUT_PRESENCE_EVERY_TICKS = 5
# A higher priority must be listed this many checks in a row before switching
# back to it; if it then does not open, wait this long before trying again.
INPUT_RETURN_CHECKS = 2
INPUT_RETURN_RETRY_SECONDS = 10.0
INPUT_RETURN_ATTEMPTS = 3
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


def default_input_name() -> str | None:
    """Name of the Windows default microphone, or None if there is none."""
    try:
        available = sd.query_devices()
        # MME's "Microsoft Sound Mapper" is an alias. WASAPI exposes the
        # actual Windows default endpoint and its complete friendly name.
        for host_api in sd.query_hostapis():
            if host_api["name"] == "Windows WASAPI":
                index = host_api["default_input_device"]
                return available[index]["name"] if index >= 0 else None
        return sd.query_devices(kind="input")["name"]
    except Exception as exc:
        logging.warning("Could not identify default input device: %s", exc)
        return None


def live_input_names() -> list[str] | None:
    """Names of the microphones Windows has active right now (Core Audio).

    PortAudio only sees devices from its last initialization; this asks
    Windows directly, takes about a millisecond and does not touch any
    stream. None off Windows or on failure.
    """
    if os.name != "nt":
        return None
    try:
        with windows_audio_thread():
            return _core_audio_capture_names()
    except Exception as exc:
        logging.warning("Could not read the live microphone list: %s", exc)
        return None


def _core_audio_capture_names() -> list[str]:
    import ctypes
    from ctypes import HRESULT, POINTER, byref, c_void_p, wintypes

    ole = ctypes.oledll.ole32

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                    ("Data4", ctypes.c_ubyte * 8)]

        def __init__(self, text: str) -> None:
            super().__init__()
            ole.CLSIDFromString(text, byref(self))

    class PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]

    class PROPVARIANT(ctypes.Structure):
        _fields_ = [("vt", ctypes.c_ushort), ("reserved1", ctypes.c_ushort), ("reserved2", ctypes.c_ushort),
                    ("reserved3", ctypes.c_ushort), ("value", ctypes.c_wchar_p), ("padding", ctypes.c_void_p)]

    def method(obj, index, *argtypes):
        vtable = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
        return ctypes.WINFUNCTYPE(HRESULT, c_void_p, *argtypes)(vtable[index])

    def release(obj) -> None:
        if obj:
            vtable = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
            ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(vtable[2])(obj)

    E_CAPTURE, DEVICE_STATE_ACTIVE, CLSCTX_INPROC_SERVER, VT_LPWSTR = 1, 1, 1, 31
    friendly_name = PROPERTYKEY(GUID("{A45C254E-DF1C-4EFD-8020-67D146A850E0}"), 14)
    enumerator, collection = c_void_p(), c_void_p()
    names: list[str] = []
    try:
        ole.CoCreateInstance(byref(GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")), None, CLSCTX_INPROC_SERVER,
                             byref(GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")), byref(enumerator))
        # IMMDeviceEnumerator::EnumAudioEndpoints
        method(enumerator, 3, ctypes.c_int, wintypes.DWORD, POINTER(c_void_p))(
            enumerator, E_CAPTURE, DEVICE_STATE_ACTIVE, byref(collection))
        count = wintypes.UINT()
        method(collection, 3, POINTER(wintypes.UINT))(collection, byref(count))  # GetCount
        for index in range(count.value):
            device, store = c_void_p(), c_void_p()
            try:
                method(collection, 4, wintypes.UINT, POINTER(c_void_p))(collection, index, byref(device))  # Item
                method(device, 4, wintypes.DWORD, POINTER(c_void_p))(device, 0, byref(store))  # OpenPropertyStore
                value = PROPVARIANT()
                method(store, 5, POINTER(PROPERTYKEY), POINTER(PROPVARIANT))(store, byref(friendly_name), byref(value))
                if value.vt == VT_LPWSTR and value.value:
                    names.append(value.value)
                ole.PropVariantClear(byref(value))
            finally:
                release(store)
                release(device)
    finally:
        release(collection)
        release(enumerator)
    return names


def input_devices() -> list[tuple[str, str]]:
    available = sd.query_devices()
    default_name = default_input_name()
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
class InputChoice:
    """The microphone a recording uses and the preferred ones it had to skip."""

    device: int | None
    name: str
    # Place in the priority list, starting at 1.
    priority: int
    wasapi: bool = False
    skipped: tuple[str, ...] = ()
    # True when every skipped microphone was missing, False when one was
    # present but could not be opened.
    skipped_missing: bool = True
    # During a recording: the microphone it lost when it switched to this one,
    # or whether it switched back because a higher priority returned.
    lost: str = ""
    returned: bool = False
    # The list entry "Windows-standaard": whatever Windows uses.
    windows_default: bool = False

    @property
    def fallback(self) -> bool:
        return bool(self.skipped or self.lost)


class NoMicrophoneError(RuntimeError):
    def __init__(self, *, found: bool) -> None:
        self.found = found
        super().__init__(
            "Geen microfoon kon worden geopend. Kies een andere microfoon in Instellingen." if found
            else "Geen microfoon gevonden. Verbind je headset of sluit een microfoon aan."
        )


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
    # Empty model: no AI cleanup, the literal transcript is used.
    cleanup_model: str = ""
    cleanup_prompt: str = ""



def cleanup_session_settings(config: Config) -> dict[str, str]:
    if not config.cleanup_enabled or not config.cleanup_model.strip():
        return {}
    return {
        "cleanup_model": config.cleanup_model.strip(),
        "cleanup_prompt": compose_cleanup_prompt(config.cleanup_prompt, config.custom_words),
    }


class DictationEngine:
    def __init__(
        self, config: Config, status_callback=None, state_callback=None, transcript_callback=None,
        recording_history: RecordingHistory | None = None, recording_callback=None,
        input_callback=None, refresh_inputs=None,
    ) -> None:
        self.config = config
        self.status_callback = status_callback or (lambda message: None)
        self.state_callback = state_callback or (lambda state: None)
        self.transcript_callback = transcript_callback or (lambda text: None)
        self.recordings = recording_history if recording_history is not None else RecordingHistory(config_store.RECORDINGS_DIR)
        self.recording_callback = recording_callback or (lambda: None)
        # Receives the InputChoice of every recording just before "recording".
        self.input_callback = input_callback or (lambda choice: None)
        # Re-reads the Windows audio devices; the app resets PortAudio here.
        self.refresh_inputs = refresh_inputs or (lambda: None)
        self.client = make_client(config.api_key)
        self.audio_queue: queue.SimpleQueue = queue.SimpleQueue()
        self.audio_warning: str | None = None
        self.audio_level_reading = (0.0, 0.0)
        self.audio_level_peak = 0.0
        self.stream: sd.InputStream | None = None
        self.active_session: RecordingSession | None = None
        self.active_input: InputChoice | None = None
        self.active_selectors: tuple[str, ...] = ()
        # Microphones lost during the current recording; reopened only once
        # Windows lists them again.
        self.lost_inputs: set[str] = set()
        self.return_retry_after: dict[str, float] = {}
        self.return_attempts: dict[str, int] = {}
        self.last_audio_at = 0.0
        self.state = "idle"
        self.lock = threading.Lock()
        self.stream_transition_lock = threading.Lock()

        prepare_speech_detection()
        logging.info("Spraakdetectie klaar: Silero VAD, lokaal op CPU")

    def update_config(self, config: Config) -> None:
        with self.lock:
            self.config = config
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
            if self.client is None or (not config.api_key and not isinstance(self.client, QaClient)):
                session = None
            else:
                session = RecordingSession(
                    input_device=None,
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
                    **cleanup_session_settings(config),
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
            self.active_selectors = config.input_devices or ("",)
            self.lost_inputs = set()
            self.return_retry_after = {}
            self.return_attempts = {}

        try:
            stream, choice = self._open_input(self.active_selectors, session)
        except Exception as exc:
            with self.lock:
                self.state = "idle"
                self.active_session = None
                self.stream = None
            if not isinstance(exc, NoMicrophoneError):
                self.emit_state("idle")
                raise
            self.emit_state("microphone_unavailable" if exc.found else "no_microphone")
            play_sound("error.wav")
            self.notify(str(exc))
            return
        with self.lock:
            self.stream = stream
            self.active_session = dataclasses.replace(session, input_device=choice.device, wasapi=choice.wasapi)
            self.active_input = choice
            self.last_audio_at = time.monotonic()

        self.input_callback(choice)
        self.emit_state("recording")
        play_sound("start.wav")
        self.notify("Opname gestart. Gebruik je shortcut opnieuw om te stoppen.")
        self._watch_input(stream)

    def _watch_input(self, stream) -> None:
        threading.Thread(target=self._input_watchdog, args=(stream,), name="microphone-watch", daemon=True).start()

    def _input_watchdog(self, stream) -> None:
        """Follow the microphones during the recording.

        The microphone in use counts as lost when its stream stops, no audio
        arrives for a second, or Windows no longer lists it (some drivers keep
        delivering silence; only checked if it was listed at the start). A
        higher priority that Windows lists again for two checks in a row (about
        two seconds) takes over again.
        """
        choice = self.active_input
        name = choice.name if choice is not None else ""
        priority = choice.priority if choice is not None else 1
        names = live_input_names()
        check_presence = bool(name) and names is not None and name in names
        # Only a higher priority that comes back counts: one that was listed
        # but not usable when this stream started is not retried, except
        # after a failed switch back (a headset still connecting).
        baseline = set(names or ())
        better = [selector.removeprefix("wasapi:") for selector in self.active_selectors[:priority - 1]
                  if selector.startswith("wasapi:")]
        better = [b for b in better if b not in baseline or b in self.return_retry_after]
        seen_back: dict[str, int] = {}
        ticks = 0
        while True:
            time.sleep(INPUT_WATCH_INTERVAL_SECONDS)
            # Stop and switch close streams under the transition lock; asking
            # PortAudio about a stream that is being closed could crash.
            with self.stream_transition_lock:
                with self.lock:
                    if self.state != "recording" or self.stream is not stream:
                        return
                try:
                    active = bool(stream.active)
                except Exception:
                    active = False
            ticks += 1
            reason, returned = "", ""
            if not active:
                reason = "stream gestopt"
            elif time.monotonic() - self.last_audio_at >= INPUT_STALL_SECONDS:
                reason = "geen audio meer"
            elif (check_presence or better) and ticks % INPUT_PRESENCE_EVERY_TICKS == 0:
                names = live_input_names()
                if names is not None:
                    if check_presence and name not in names:
                        reason = "niet meer in de Windows-lijst"
                    else:
                        now = time.monotonic()
                        listed = [b for b in better if b in names and now >= self.return_retry_after.get(b, 0.0)]
                        seen_back = {b: seen_back.get(b, 0) + 1 for b in listed}
                        returned = next((b for b in listed if seen_back[b] >= INPUT_RETURN_CHECKS), "")
            if not reason and not returned:
                continue
            if reason:
                logging.warning("Microfoon %s: %s.", name or "onbekend", reason)
            else:
                logging.info("Microfoon %s is terug; terugschakelen.", returned)
            try:
                self.switch_input(stream, lost=bool(reason), returned=returned)
            except Exception:
                logging.exception("Could not switch microphones during the recording")
            return

    def switch_input(self, old_stream, *, lost: bool, returned: str = "") -> None:
        """Continue the same recording on another microphone of the priority list.

        After a loss it takes the next one that works; when a higher priority
        is back it takes that one. Audio recorded so far stays; only what was
        said while switching is missing (about a second after a loss, a split
        second when switching back). Without any microphone left, the
        recording stops and what was captured is transcribed as usual.
        """
        with self.stream_transition_lock, windows_audio_thread():
            with self.lock:
                if self.state != "recording" or self.stream is not old_stream:
                    return
                session = self.active_session
                previous = self.active_input
                selectors = self.active_selectors
                self.stream = None
            previous_name = previous.name if previous is not None else ""
            for action in (old_stream.stop, old_stream.close):
                try:
                    action()
                except Exception as exc:
                    logging.info("Old input stream: %s", exc)
            # Microphones lost earlier in this recording that Windows lists
            # again may be used again (also lower ones than the one returning).
            listed = live_input_names()
            if listed is not None:
                self.lost_inputs = {name for name in self.lost_inputs if name not in listed}
            if lost and previous_name:
                self.lost_inputs.add(previous_name)
            if returned:
                self.lost_inputs.discard(returned)
            try:
                stream, choice = self._open_input(selectors, session, exclude=frozenset(self.lost_inputs))
            except NoMicrophoneError:
                play_sound("error.wav")
                self.notify("Microfoon weggevallen. De opname tot dat moment wordt verwerkt.")
                self._stop_recording()
                return
            changed = choice.name != previous_name
            if lost:
                choice = dataclasses.replace(choice, lost=previous_name)
            elif changed:
                choice = dataclasses.replace(choice, returned=True)
            elif returned:
                # Listed again but not usable yet (a headset still connecting): retry a few times.
                attempts = self.return_attempts.get(returned, 0) + 1
                self.return_attempts[returned] = attempts
                self.return_retry_after[returned] = (
                    time.monotonic() + INPUT_RETURN_RETRY_SECONDS if attempts < INPUT_RETURN_ATTEMPTS else math.inf
                )
            if choice.returned:
                self.return_retry_after.pop(choice.name, None)
            with self.lock:
                self.stream = stream
                self.active_input = choice
                self.last_audio_at = time.monotonic()
        if lost or changed:
            self.input_callback(choice)
            verb = "Terug naar" if choice.returned else "Overgeschakeld naar"
            self.notify(f"{verb} {choice.name}; de opname loopt door.")
        self._watch_input(stream)

    def _open_input(
        self, selectors: tuple[str, ...], session: RecordingSession, exclude: frozenset[str] = frozenset(),
    ) -> tuple[sd.InputStream, InputChoice]:
        """Open the first microphone of the priority list that works.

        "" in the list is Windows-standaard: whatever microphone Windows uses.
        PortAudio lists devices only when it initializes, so the list is read
        again first: a headset that connected or disconnected since the last
        recording counts. Microphones named in ``exclude`` (lost during this
        recording) are skipped. Raises NoMicrophoneError if nothing can be opened.
        """
        try:
            self.refresh_inputs()
        except Exception:
            logging.exception("Could not refresh the microphone list")
        skipped: list[str] = []
        skipped_missing = True
        for priority, selector in enumerate(selectors or ("",), start=1):
            if selector:
                name = selector.removeprefix("wasapi:")
            else:
                name = default_input_name() or ""
                if not name:
                    logging.info("Microfoon %d overgeslagen: Windows heeft geen standaardmicrofoon.", priority)
                    skipped.append(DEFAULT_DEVICE_LABEL)
                    continue
            if name in exclude:
                skipped.append(name)
                continue
            if selector:
                try:
                    device = resolve_input_device(selector)
                except Exception as exc:
                    logging.info("Microfoon %d overgeslagen: %s", priority, exc)
                    skipped.append(name)
                    continue
            else:
                device = None  # PortAudio's default input follows Windows.
            wasapi = selector.startswith("wasapi:")
            try:
                stream = self._open_stream(session, device, wasapi=wasapi)
            except Exception as exc:
                logging.warning("Microfoon %d (%s) kon niet worden geopend: %s", priority, name, exc)
                skipped.append(name)
                skipped_missing = False
                continue
            if selector and not wasapi:
                try:
                    name = sd.query_devices(device)["name"]
                except Exception:
                    pass
            choice = InputChoice(device, name, priority, wasapi, tuple(skipped), skipped_missing,
                                 windows_default=not selector)
            break
        else:
            raise NoMicrophoneError(found=not skipped_missing)
        logging.info(
            "Opname met %s (prioriteit %d%s)%s", choice.name, choice.priority,
            ", Windows-standaard" if choice.windows_default else "",
            f"; niet beschikbaar: {', '.join(choice.skipped)}" if choice.skipped else "",
        )
        return stream, choice

    def _open_stream(self, session: RecordingSession, device: int | None, *, wasapi: bool) -> sd.InputStream:
        stream = sd.InputStream(
            device=device,
            samplerate=session.sample_rate,
            channels=session.channels,
            dtype="int16",
            callback=self.audio_callback,
            **({"extra_settings": sd.WasapiSettings(auto_convert=True)} if wasapi else {}),
        )
        try:
            stream.start()
        except Exception:
            try:
                stream.close()
            except Exception:
                logging.exception("Could not close failed input stream")
            raise
        return stream

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
            self.active_input = None
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
        self.last_audio_at = time.monotonic()
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
                auto_paragraphs=config.auto_paragraphs, **cleanup_session_settings(config),
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
        new_recording = recording is None
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
            else:
                wav_path = self.recordings.audio_path(recording)

            upload = None
            if not isinstance(session.client, QaClient):
                upload = speech_upload(wav_path)
                if upload is None:
                    raise NoSpeechDetected()

            if new_recording and (duration < MIN_TRANSCRIPTION_SECONDS or wav_path.stat().st_size < MIN_TRANSCRIPTION_BYTES):
                self.update_recording(recording, status="failed", error="Opname te kort. Je kunt opnieuw proberen.")
                self.notify("Transcriptie te kort. Er is niets geplakt.")
                self.emit_state("too_short")
                play_sound("error.wav")
                show_idle_bubble = False
                return

            self.update_recording(recording, status="processing")
            if isinstance(upload, tuple):
                self.notify("Lange stilte aan het eind niet meegestuurd naar Groq.")

            text = apply_word_replacements(
                self.transcribe(session, wav_path, upload).strip(),
                session.word_replacements,
            )
            cleanup_wanted = bool(text and session.cleanup_model and not isinstance(session.client, QaClient))
            cleaned = False
            if cleanup_wanted:
                text, cleaned = self.clean_up(session, text)
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
            done = "Transcriptie opgeschoond" if cleaned else "Transcriptie klaar"
            self.notify(f"{done} in {elapsed:.1f}s. Tekst staat op je klembord.")
            try:
                self.transcript_callback(text)
            except Exception:
                logging.exception("Could not record transcription history")

            if session.paste_after_transcription:
                try:
                    paste_clipboard()
                    self.notify("Geplakt in het actieve venster.")
                except Exception as exc:
                    self.notify(f"Automatisch plakken mislukte, maar de tekst staat op je klembord: {exc}")

            play_sound("success.wav")
            if cleanup_wanted and not cleaned:
                self.emit_state("cleanup_skipped")
                show_idle_bubble = False
        except NoSpeechDetected as exc:
            if recording is not None:
                self.update_recording(recording, status="failed", error=str(exc))
            self.notify(str(exc))
            self.emit_state("no_speech")
            show_idle_bubble = False
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

    def clean_up(self, session: RecordingSession, text: str) -> tuple[str, bool]:
        """AI cleanup, or the literal transcript when it fails. Never loses the dictation."""
        started_at = time.perf_counter()
        try:
            cleaned = clean_transcript(
                session.client, text, model=session.cleanup_model, system_prompt=session.cleanup_prompt,
            )
        except CleanupError as exc:
            logging.warning("AI cleanup skipped: %s", exc.__cause__ or exc)
            self.notify(f"{exc}. Letterlijke tekst gebruikt.")
            return text, False
        logging.info("AI cleanup with %s in %.2fs", session.cleanup_model, time.perf_counter() - started_at)
        return cleaned, True

    def transcribe(
        self, session: RecordingSession, wav_path: Path, upload: tuple[str, bytes] | Path | None = None,
    ) -> str:
        """Transcribe the recording, or the in-memory copy without its trailing silence."""
        if isinstance(upload, tuple):
            return self._request_transcription(session, upload)
        with wav_path.open("rb") as audio_file:
            return self._request_transcription(session, audio_file)

    def _request_transcription(self, session: RecordingSession, audio_file) -> str:
        kwargs = {
            "file": audio_file,
            "model": session.model,
            "response_format": "verbose_json",
            "temperature": 0.0,
        }
        if session.language:
            kwargs["language"] = session.language
        if session.prompt:
            kwargs["prompt"] = session.prompt

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

        if response_is_silence(getattr(transcription, "segments", None)):
            raise NoSpeechDetected()
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
