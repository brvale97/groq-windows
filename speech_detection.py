"""Local speech gate; never alter the recording that is saved or uploaded."""

from __future__ import annotations

import wave
from functools import lru_cache
from pathlib import Path

import numpy as np


FRAME_MS = 32
MIN_VOICED_FRAMES = 4  # 128 ms; a short word must not need a long recording.
SPEECH_THRESHOLD = 0.5
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


def contains_speech(path: Path) -> bool:
    """Run the bundled Silero model locally with independent channel state.

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
        detector_rate = 8000 if rate == 8000 else 16000
        source_size = round(rate * FRAME_MS / 1000)
        target_size = detector_rate * FRAME_MS // 1000
        context_size = 32 if detector_rate == 8000 else 64
        model = _model_session()
        state = np.zeros((2, channels, 128), dtype=np.float32)
        context = np.zeros((channels, context_size), dtype=np.float32)
        voiced = np.zeros(channels, dtype=np.int32)
        sample_rate = np.array(detector_rate, dtype=np.int64)
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
            # Require a short continuous speech interval, so isolated glitches
            # scattered throughout a long recording cannot accumulate.
            voiced = np.where(probabilities.reshape(-1) >= SPEECH_THRESHOLD, voiced + 1, 0)
            if np.any(voiced >= MIN_VOICED_FRAMES):
                return True
    return False


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
