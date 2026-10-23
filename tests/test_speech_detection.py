"""Exercise the real native VAD with PCM, independently of Groq and the UI."""

import io
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

import speech_detection
from speech_detection import contains_speech, response_is_silence, speech_upload


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
        random = np.random.default_rng(42)
        for samples in (np.zeros(32000), np.full(32000, 4000),
                        random.normal(0, 15, 32000), random.normal(0, 1000, 32000)):
            with self.subTest(kind=str(samples[:3])):
                self.assertFalse(self.check(samples, 16000))

    def test_microphone_startup_transient_with_noise_is_not_speech(self):
        # DC settling at stream startup was counted as speech by the first VAD.
        time = np.arange(48000) / 16000
        transient = 30000 * np.exp(-time / 0.06)
        noise = np.random.default_rng(42).normal(0, 65, len(time))
        self.assertFalse(self.check(transient + noise, 16000))

    def test_each_recording_starts_with_fresh_model_state(self):
        self.assertTrue(self.check(self.speech))
        self.assertFalse(self.check(np.zeros(16000), 16000))
        self.assertTrue(self.check(self.speech.astype(float) * 0.01))

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


class TrailingSilenceTests(unittest.TestCase):
    """Only long silence after the last word is left out of the upload."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "audio.wav"
        self.speech = np.fromfile(Path(__file__).parent / "fixtures" / "webrtc-speech-8k.raw", dtype="<i2")

    def upload(self, *pieces, rate=8000, channels=1):
        samples = np.concatenate(pieces).astype("<i2")
        if channels == 2:
            samples = np.column_stack((samples, samples // 2))
        with wave.open(str(self.path), "wb") as wav:
            wav.setnchannels(channels)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(samples.tobytes())
        before = self.path.read_bytes()
        upload = speech_upload(self.path)
        self.assertEqual(before, self.path.read_bytes())
        return upload, samples

    @staticmethod
    def sent(upload):
        name, data = upload
        with wave.open(io.BytesIO(data), "rb") as wav:
            return name, wav.getparams(), np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")

    def test_short_trailing_silence_sends_the_original_recording(self):
        upload, _samples = self.upload(self.speech, np.zeros(8000))
        self.assertEqual(upload, self.path)

    def test_pauses_and_leading_silence_are_never_cut(self):
        # Cutting inside the dictation broke sentences; leading silence is kept
        # because trimming it made Whisper misdetect the language of "Hallo".
        long = np.zeros(8000 * 6)
        upload, _samples = self.upload(long, self.speech, long, self.speech)
        self.assertEqual(upload, self.path)

    def test_long_trailing_silence_is_left_out(self):
        pause = np.zeros(8000 * 4)
        upload, samples = self.upload(np.zeros(8000 * 3), self.speech, pause, self.speech, np.zeros(8000 * 8))
        name, params, sent = self.sent(upload)
        self.assertEqual(name, self.path.name)
        self.assertEqual((params.nchannels, params.sampwidth, params.framerate), (1, 2, 8000))
        # A prefix of the original samples: start and inner pause intact, speech complete.
        self.assertTrue(np.array_equal(sent, samples[:len(sent)]))
        speech_end = 8000 * 3 + len(self.speech) * 2 + len(pause)
        padding = round(speech_detection.TRAILING_PADDING_SECONDS * 8000)
        self.assertLessEqual(len(sent), speech_end + padding)
        self.assertGreater(len(sent), speech_end - len(self.speech) // 2)
        self.assertTrue(contains_speech_in(sent[-len(self.speech) - padding:], 8000))

    def test_trim_keeps_rate_and_both_stereo_channels(self):
        speech = np.interp(np.arange(len(self.speech) * 6) / 6, np.arange(len(self.speech)), self.speech)
        upload, samples = self.upload(speech, np.zeros(48000 * 5), rate=48000, channels=2)
        _name, params, sent = self.sent(upload)
        self.assertEqual((params.nchannels, params.framerate), (2, 48000))
        self.assertTrue(np.array_equal(sent, samples.reshape(-1)[:len(sent)]))
        self.assertLess(len(sent), samples.size // 2)

    def test_no_speech_means_no_upload(self):
        self.assertIsNone(self.upload(np.zeros(8000 * 6))[0])


def contains_speech_in(samples, rate):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "part.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(np.asarray(samples, dtype="<i2").tobytes())
        return contains_speech(path)


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
