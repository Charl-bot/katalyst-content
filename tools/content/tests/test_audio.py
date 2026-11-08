"""Known-answer tests for the converter's analysis (python -m unittest)."""

import math
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from kif import audio  # noqa: E402

FS = 48000


def tone(hz, seconds=1.0, partials=(1.0, 0.5, 0.3, 0.2), fs=FS, amp=0.5, inharm=0.0, decay=1.5):
    t = np.arange(int(seconds * fs)) / fs
    x = np.zeros_like(t)
    for k, a in enumerate(partials, 1):
        fk = hz * k * math.sqrt(1 + inharm * k * k)
        x += a * np.sin(2 * math.pi * fk * t)
    x *= amp / np.max(np.abs(x)) * np.exp(-t / decay)
    return np.stack([x, x], axis=1)


def to_int(x):
    return np.round(np.clip(x, -1, 1 - 2**-31) * 2147483648.0).astype(np.int64).clip(-(2**31), 2**31 - 1).astype(np.int32)


class PitchTests(unittest.TestCase):
    def check(self, note, detune_cents, **kw):
        hz = audio.midi_hz(note) * 2 ** (detune_cents / 1200)
        r = audio.detect_pitch(tone(hz, **kw), FS, audio.midi_hz(note))
        self.assertIsNotNone(r.cents)
        self.assertAlmostEqual(r.cents, detune_cents, delta=1.0, msg=f"note {note}")
        self.assertEqual(r.octave, 0)
        return r

    def test_in_tune_across_range(self):
        for note in (28, 40, 57, 69, 81, 96, 105):
            self.check(note, 0.0)

    def test_detuned_reported_with_sign(self):
        self.check(60, +15.0)
        self.check(45, -12.0)
        self.check(84, +35.0)

    def test_low_piano_like_inharmonic(self):
        # Weak fundamental, stretched partials: must still land near f0.
        hz = audio.midi_hz(29)
        r = audio.detect_pitch(tone(hz, seconds=1.5, partials=(0.2, 1.0, 0.8, 0.6, 0.5), inharm=0.0004), FS, hz)
        self.assertLess(abs(r.cents), 10)
        self.assertEqual(r.octave, 0, "a weak fundamental is not an octave error")

    def test_octave_mismatch_is_reported(self):
        # Recording sounds an octave above the mapped root (a mis-mapped glock).
        r = audio.detect_pitch(tone(audio.midi_hz(84), partials=(1.0, 0.1)), FS, audio.midi_hz(72))
        self.assertEqual(r.octave, +1)

    def test_noise_is_low_confidence(self):
        rng = np.random.default_rng(1)
        x = rng.standard_normal((FS, 2)) * 0.1
        r = audio.detect_pitch(x, FS, 440.0)
        self.assertLess(r.confidence, 0.6)


class SpectralPitchTests(unittest.TestCase):
    def test_bar_like_inharmonic_tone(self):
        # Glockenspiel-like: strong fundamental, partials at 2.76x and 5.40x (free bar modes).
        for note, cents in ((84, 0.0), (91, 14.0), (100, -9.0)):
            hz = audio.midi_hz(note) * 2 ** (cents / 1200)
            x = tone(hz, seconds=1.5, partials=(1.0,), decay=0.8)
            t = np.arange(x.shape[0]) / FS
            x[:, 0] += 0.3 * np.sin(2 * math.pi * 2.76 * hz * t) * np.exp(-t / 0.3)
            x[:, 1] = x[:, 0]
            r = audio.detect_pitch_spectral(x, FS, audio.midi_hz(note))
            self.assertAlmostEqual(r.cents, cents, delta=1.0)
            self.assertGreater(r.confidence, 0.7)
            self.assertEqual(r.octave, 0)


class LoudnessTests(unittest.TestCase):
    def test_bs1770_reference_tone(self):
        # BS.1770: a 0 dBFS 997 Hz sine in one channel reads -3.01 LKFS.
        t = np.arange(3 * FS) / FS
        x = np.stack([np.sin(2 * math.pi * 997 * t), np.zeros_like(t)], axis=1)
        self.assertAlmostEqual(audio.max_momentary_lufs(x, FS), -3.01, delta=0.05)
        x44 = np.stack([np.sin(2 * math.pi * 997 * np.arange(3 * 44100) / 44100)] * 2, axis=1) * 0.5
        # both channels at -6.02 dBFS: -3.01 - 6.02 + 3.01 = -6.02
        self.assertAlmostEqual(audio.max_momentary_lufs(x44, 44100), -6.02, delta=0.05)

    def test_mono_counts_as_dual_mono(self):
        t = np.arange(FS) / FS
        mono = np.sin(2 * math.pi * 997 * t)[:, None] * 0.5
        stereo = np.repeat(mono, 2, axis=1)
        self.assertAlmostEqual(audio.max_momentary_lufs(mono, FS), audio.max_momentary_lufs(stereo, FS), places=6)


class TrimTests(unittest.TestCase):
    def test_keeps_attack_and_preroll(self):
        lead = int(0.2 * FS)
        x = np.zeros((lead + FS, 2))
        rng = np.random.default_rng(0)
        x[:lead] = rng.standard_normal((lead, 2)) * 1e-5  # -100 dB noise floor
        hit = tone(220, seconds=1.0, decay=0.3)
        x[lead:] = hit
        pcm = to_int(x)
        mag = np.max(np.abs(pcm.astype(np.float64)), axis=1) / 2**31
        cut = audio.trim_cut([mag], FS)
        onset, _ = audio.onset_frame([mag])
        self.assertLessEqual(onset, lead + 5)
        self.assertEqual(cut, onset - int(0.003 * FS))
        out = audio.apply_trim(pcm, cut)
        # every frame from the onset on is untouched
        np.testing.assert_array_equal(out[onset - cut :], pcm[onset:])
        # the fade only touches sub-threshold frames
        self.assertLess(audio.FADE_FRAMES, onset - cut)

    def test_multi_mic_uses_one_cut(self):
        a = np.zeros(1000)
        b = np.zeros(1000)
        a[600:] = 0.5
        b[500:] = 0.01  # the close mic speaks first, quieter overall
        cut = audio.trim_cut([a, b], FS)
        self.assertEqual(cut, 500 - int(0.003 * FS))

    def test_no_trim_when_sound_starts_immediately(self):
        x = to_int(tone(440, seconds=0.5))
        mag = np.max(np.abs(x.astype(np.float64)), axis=1) / 2**31
        self.assertEqual(audio.trim_cut([mag], FS), 0)


class ClipAndIoTests(unittest.TestCase):
    def test_clip_runs(self):
        x = np.zeros((100, 1), dtype=np.int32)
        x[10:13] = 2147483647 - 100  # within 1 LSB at 16 and 24 bit
        x[50:52] = -2147483648  # only 2 frames: not a run
        self.assertEqual(audio.clip_runs(x, 16), 1)
        self.assertEqual(audio.clip_runs(x, 24), 1)

    def test_flac_round_trip_is_bit_exact(self):
        rng = np.random.default_rng(3)
        for bits in (16, 24):
            x = audio.quantise_to(rng.integers(-(2**31), 2**31 - 1, size=(5000, 2), dtype=np.int64).astype(np.int32), bits)
            with tempfile.TemporaryDirectory() as d:
                p = os.path.join(d, "t.flac")
                audio.write_flac(p, x, FS, bits)
                back = audio.read_pcm(p)
                self.assertEqual(back.bits, bits)
                np.testing.assert_array_equal(back.data, x)


if __name__ == "__main__":
    unittest.main()
