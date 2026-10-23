"""Local speech gate and trailing-silence trim; the saved recording is never altered."""

from __future__ import annotations

import io
import wave
from functools import lru_cache
from pathlib import Path

import numpy as np


FRAME_MS = 32
MIN_VOICED_FRAMES = 4  # 128 ms; a short word must not need a long recording.
SPEECH_THRESHOLD = 0.5
SPEECH_END_THRESHOLD = 0.35  # Hysteresis: a speech region continues until clearly below this.
# Whisper tends to invent a closing line ("Dank u wel.") for long silence after
# the last word, so that silence is not uploaded. Pauses inside the dictation and
# silence before it stay: cutting there broke sentences and misdetected the
# language of short pieces in tests with Bram's own recordings (2026-10-23).
TRAILING_SILENCE_SECONDS = 2.0
TRAILING_PADDING_SECONDS = 0.4  # Audio kept after the last word, so its tail survives.
MODEL_PATH = Path(__file__).resolve().parent / "assets" / "silero_vad.onnx"


@lru_cache(maxsize=1)
def _model_session():
    import onnxruntime

    options = onnxruntime.SessionOptions()
    options.inter_op_num_threads = 1
    options.intra_op_num_threads = 1
    return onnxruntime.InferenceSession(
        str(MODEL_PATH), sess_options=options, providers=["CPUExecutionProvider"],
    )


def prepare_speech_detection() -> None:
    """Warm up and verify the bundled model before the app is ready to record."""
    _model_session().run(None, {
        "input": np.zeros((1, 576), dtype=np.float32),
        "state": np.zeros((2, 1, 128), dtype=np.float32),
        "sr": np.array(16000, dtype=np.int64),
    })


class NoSpeechDetected(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Geen spraak gedetecteerd. Er is niets geplakt. Opname bewaard in Geschiedenis.")


def _frame_probabilities(path: Path) -> tuple[np.ndarray, int, int, int]:
    """Silero speech probability per 32 ms frame and channel, plus the WAV layout.

    Microphone startup transients, room noise and distorted/reverberant cues can
    fool an energy/spectral VAD. Use learned speech probabilities instead.
    Only detector input is resampled; the saved/uploaded WAV stays untouched.
    Model state and context start empty on every recording, including retries.
    """
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise ValueError("Spraakdetectie vereist 16-bit PCM-audio.")
        rate = wav.getframerate()
        channels = wav.getnchannels()
        total = wav.getnframes()
        detector_rate = 8000 if rate == 8000 else 16000
        source_size = round(rate * FRAME_MS / 1000)
        target_size = detector_rate * FRAME_MS // 1000
        context_size = 32 if detector_rate == 8000 else 64
        model = _model_session()
        state = np.zeros((2, channels, 128), dtype=np.float32)
        context = np.zeros((channels, context_size), dtype=np.float32)
        sample_rate = np.array(detector_rate, dtype=np.int64)
        rows: list[np.ndarray] = []
        while block := wav.readframes(source_size):
            samples = np.frombuffer(block, dtype="<i2").reshape(-1, channels).T.astype(np.float32) / 32768
            if samples.shape[1] < source_size:
                samples = np.pad(samples, ((0, 0), (0, source_size - samples.shape[1])))
            if rate != detector_rate:
                samples = np.asarray([
                    np.interp(
                        np.arange(target_size) * rate / detector_rate,
                        np.arange(source_size), channel,
                    ) for channel in samples
                ], dtype=np.float32)
            model_input = np.concatenate((context, samples), axis=1)
            probabilities, state = model.run(None, {
                "input": model_input, "state": state, "sr": sample_rate,
            })
            context = model_input[:, -context_size:]
            rows.append(probabilities.reshape(-1))
    return np.asarray(rows, dtype=np.float32).reshape(-1, channels), source_size, total, rate


def speech_regions(path: Path) -> tuple[list[tuple[int, int]], int, int]:
    """Sample ranges with speech, the total frame count and the sample rate.

    A region needs a short continuous speech interval in one channel, so
    isolated glitches scattered throughout a long recording cannot accumulate
    and stereo channels cannot cancel each other out.
    """
    probabilities, frame_size, total, rate = _frame_probabilities(path)
    voiced = probabilities >= SPEECH_THRESHOLD
    active = (probabilities >= SPEECH_END_THRESHOLD).any(axis=1)
    run = np.zeros(probabilities.shape[1], dtype=np.int32)
    regions: list[tuple[int, int]] = []
    start: int | None = None
    anchored = False
    for index, is_active in enumerate(active):
        run = np.where(voiced[index], run + 1, 0)
        if is_active:
            if start is None:
                start, anchored = index, False
            anchored = anchored or bool(np.any(run >= MIN_VOICED_FRAMES))
            continue
        if start is not None and anchored:
            regions.append((start, index))
        start = None
    if start is not None and anchored:
        regions.append((start, len(active)))
    return [(begin * frame_size, min(end * frame_size, total)) for begin, end in regions], total, rate


def contains_speech(path: Path) -> bool:
    return bool(speech_regions(path)[0])


def upload_frames(regions: list[tuple[int, int]], total: int, rate: int) -> int:
    """How many frames to upload: all, or up to shortly after the last word before a long silence."""
    end = regions[-1][1]
    if total - end < round(TRAILING_SILENCE_SECONDS * rate):
        return total
    return min(total, end + round(TRAILING_PADDING_SECONDS * rate))


def leading_wav_bytes(path: Path, frames: int) -> bytes:
    """A new in-memory WAV with the first frames of the original samples."""
    with wave.open(str(path), "rb") as source:
        channels, width, rate = source.getnchannels(), source.getsampwidth(), source.getframerate()
        samples = source.readframes(frames)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(width)
        target.setframerate(rate)
        target.writeframes(samples)
    return buffer.getvalue()


def speech_upload(path: Path) -> Path | tuple[str, bytes] | None:
    """What to send to Groq: None without speech, else the original file or a copy without the trailing silence."""
    regions, total, rate = speech_regions(path)
    if not regions:
        return None
    frames = upload_frames(regions, total, rate)
    if frames == total:
        return path
    return (path.name, leading_wav_bytes(path, frames))


def response_is_silence(segments: object) -> bool:
    """Use Whisper's conservative silence rule only with complete metadata.

    A confident speech segment keeps the response. Missing metadata also keeps
    it; the local gate is the primary check, not a blacklist of transcript text.
    """
    if not isinstance(segments, list) or not segments:
        return False
    for segment in segments:
        if not isinstance(segment, dict):
            return False
        probability = segment.get("no_speech_prob")
        log_probability = segment.get("avg_logprob")
        if not isinstance(probability, (int, float)) or not isinstance(log_probability, (int, float)):
            return False
        if not (probability > 0.6 and log_probability <= -1.0):
            return False
    return True
