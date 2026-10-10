"""Local speech gate; never alter the recording that is saved or uploaded."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import webrtcvad


FRAME_MS = 30
MIN_VOICED_FRAMES = 4  # 120 ms; a short word must not need a long recording.
MIN_FRAME_RMS = 0.0002  # About -74 dBFS, below ordinary quiet speech.


class NoSpeechDetected(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Geen spraak gedetecteerd. Er is niets geplakt. Opname bewaard in Geschiedenis.")


def contains_speech(path: Path) -> bool:
    """Require actual active VAD frames, rather than its silence hangover.

    Check channels separately so opposite-polarity stereo cannot cancel speech.
    Pure tones are excluded: the app's short start cue can enter the microphone.
    Read in small blocks and stop as soon as enough speech is found.
    """
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise ValueError("Spraakdetectie vereist 16-bit PCM-audio.")
        rate = wav.getframerate()
        channels = wav.getnchannels()
        detector_rate = rate if rate in (8000, 16000, 32000, 48000) else 16000
        source_size = round(rate * FRAME_MS / 1000)
        target_size = detector_rate * FRAME_MS // 1000
        detectors = [webrtcvad.Vad(2) for _ in range(channels)]
        voiced = [0] * channels
        window = np.hanning(target_size)
        while block := wav.readframes(source_size):
            samples = np.frombuffer(block, dtype="<i2").reshape(-1, channels)
            for channel, detector in enumerate(detectors):
                values = samples[:, channel].astype(np.float64)
                if len(values) < source_size:
                    values = np.pad(values, (0, source_size - len(values)))
                if rate != detector_rate:
                    values = np.interp(
                        np.arange(target_size) * rate / detector_rate,
                        np.arange(source_size), values,
                    )
                values -= values.mean()  # A DC offset is not sound.
                pcm = np.clip(np.rint(values), -32768, 32767).astype("<i2")
                is_voice = detector.is_speech(pcm.tobytes(), detector_rate)
                if not is_voice or np.sqrt(np.mean(values ** 2)) / 32768 < MIN_FRAME_RMS:
                    continue
                power = np.abs(np.fft.rfft(values * window)) ** 2
                peak = int(np.argmax(power))
                tone_power = power[max(0, peak - 1):peak + 2].sum()
                if tone_power >= 0.90 * power.sum():
                    continue
                voiced[channel] += 1
                if voiced[channel] >= MIN_VOICED_FRAMES:
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
