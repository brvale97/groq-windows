"""Exercise the real native VAD with PCM, independently of Groq and the UI."""

import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from speech_detection import contains_speech, response_is_silence


class SpeechDetectionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "audio.wav"
        self.speech = np.fromfile(Path(__file__).parent / "fixtures" / "webrtc-speech-8k.raw", dtype="<i2")

    def check(self, samples, rate=8000):
        samples = np.asarray(samples, dtype="<i2")
        with wave.open(str(self.path), "wb") as wav:
            wav.setnchannels(1 if samples.ndim == 1 else samples.shape[1])
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(samples.tobytes())
        before = self.path.read_bytes()
        result = contains_speech(self.path)
        self.assertEqual(before, self.path.read_bytes())
        return result

    def test_silence_dc_offset_and_low_noise_are_rejected(self):
        noise = np.random.default_rng(42).normal(0, 15, 32000)
        for samples in (np.zeros(32000), np.full(32000, 4000), noise):
            with self.subTest(kind=str(samples[:3])):
                self.assertFalse(self.check(samples, 16000))

    def test_speech_and_quiet_speech_pass(self):
        self.assertTrue(self.check(self.speech))
        self.assertTrue(self.check(self.speech.astype(float) * 0.01))

    def test_short_word_surrounded_by_silence_passes(self):
        samples = np.concatenate((np.zeros(8000), self.speech[:2400], np.zeros(8000 * 8)))
        self.assertTrue(self.check(samples))

    def test_supported_rates_and_legacy_44100_pass(self):
        for rate in (8000, 16000, 32000, 44100, 48000):
            with self.subTest(rate=rate):
                samples = np.interp(np.arange(int(len(self.speech) * rate / 8000)) * 8000 / rate,
                                    np.arange(len(self.speech)), self.speech)
                self.assertTrue(self.check(samples, rate))

    def test_stereo_speech_cannot_cancel_itself(self):
        self.assertTrue(self.check(np.column_stack((self.speech, -self.speech))))
        self.assertTrue(self.check(np.column_stack((np.zeros(len(self.speech)), self.speech))))

    def test_start_cue_and_single_tone_do_not_count_as_speech(self):
        # Match make_tone's two-note start cue, without importing the GUI engine.
        rate = 44100
        time = np.arange(int(0.09 * rate)) / rate
        envelope = np.where(time < 0.015, time / 0.015 * 0.2,
                            0.2 * np.power(0.0001 / 0.2, (time - 0.015) / 0.075))
        notes = [np.sin(2 * np.pi * frequency * time) * envelope * 32767 for frequency in (523.25, 659.25)]
        cue = np.concatenate((notes[0], np.zeros(int(0.025 * rate)), notes[1], np.zeros(rate)))
        self.assertFalse(self.check(cue, rate))
        self.assertFalse(self.check(np.sin(2 * np.pi * 523.25 * np.arange(rate) / rate) * 6000, rate))


class SilenceMetadataTests(unittest.TestCase):
    def test_all_segments_must_indicate_uncertain_non_speech(self):
        silence = {"no_speech_prob": 0.95, "avg_logprob": -1.5}
        self.assertTrue(response_is_silence([silence, silence]))
        for segments in (None, [], [{}], [silence, {}],
                         [silence, {"no_speech_prob": 0.1, "avg_logprob": -1.5}],
                         [{"no_speech_prob": 0.95, "avg_logprob": -0.2}],
                         [{"no_speech_prob": "0.95", "avg_logprob": -1.5}]):
            with self.subTest(segments=segments):
                self.assertFalse(response_is_silence(segments))
