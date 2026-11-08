"""Audio analysis and I/O for the KIF converter.

Everything here works on integer PCM so a 16-bit source written as 16-bit
FLAC stays bit-exact (except the short fade-in on samples that were trimmed).

Rules (one set for every instrument, D-194):

* Trim: onset = first frame whose |x| (max over channels, and over every mic
  of a multi-mic hit) reaches ``peak * 10^(-TRIM_REL_DB/20)``; the cut is
  ``onset - PREROLL_S`` (never negative), so the attack and a few ms before it
  are kept. A trimmed sample gets a FADE_FRAMES raised-cosine fade-in, which
  only touches audio that is below the threshold.
* Pitch: YIN (de Cheveigne & Kawahara 2002) on frames after the attack,
  searched within +-7 semitones of the expected pitch, plus a check of the
  octave above and below so a mis-mapped octave is reported, not hidden.
* Loudness: ITU-R BS.1770-4 K-weighting; the per-sample figure is the
  maximum momentary (400 ms) loudness. Mono counts as dual-mono.
* Clipping: a run of >= 3 consecutive frames within 1 LSB of full scale.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np
import soundfile as sf
from scipy.signal import lfilter, resample_poly

TRIM_REL_DB = 50.0
PREROLL_S = 0.003
FADE_FRAMES = 32
CLIP_RUN = 3

SUBTYPE_BITS = {"PCM_16": 16, "PCM_24": 24, "PCM_32": 32, "PCM_S8": 8, "PCM_U8": 8, "FLOAT": 32, "DOUBLE": 64}


@dataclass
class Pcm:
    data: np.ndarray  # int32, shape (frames, channels), left-justified to 32 bits
    sample_rate: int
    bits: int  # source bit depth (16 or 24 after mapping)
    subtype: str
    fmt: str

    @property
    def frames(self) -> int:
        return int(self.data.shape[0])

    @property
    def channels(self) -> int:
        return int(self.data.shape[1])

    def as_float(self) -> np.ndarray:
        return self.data.astype(np.float64) / 2147483648.0


def read_pcm(path: str) -> Pcm:
    info = sf.info(path)
    sub = info.subtype
    if sub in ("FLOAT", "DOUBLE"):
        x = sf.read(path, dtype="float64", always_2d=True)[0]
        x = np.clip(np.round(x * 8388608.0), -8388608, 8388607).astype(np.int64) << 8
        data = x.astype(np.int32)
        bits = 24
    else:
        data = sf.read(path, dtype="int32", always_2d=True)[0]
        src_bits = SUBTYPE_BITS.get(sub, 24)
        bits = 16 if src_bits <= 16 else 24
        if src_bits == 32:
            # 32-bit integer sources become 24-bit (round, keep the top 24 bits)
            data = (np.clip((data.astype(np.int64) + 128) >> 8, -8388608, 8388607) << 8).astype(np.int32)
    return Pcm(data=data, sample_rate=info.samplerate, bits=bits, subtype=sub, fmt=info.format)


def write_flac(path: str, pcm: np.ndarray, sample_rate: int, bits: int) -> None:
    subtype = "PCM_16" if bits == 16 else "PCM_24"
    sf.write(path, pcm, sample_rate, format="FLAC", subtype=subtype, compression_level=1.0)


def onset_frame(mags: Sequence[np.ndarray]) -> Tuple[int, float]:
    """mags: per-layer |x| max over channels (float, full scale = 1)."""
    peak = max(float(m.max()) if m.size else 0.0 for m in mags)
    if peak <= 0:
        return 0, 0.0
    thr = peak * 10 ** (-TRIM_REL_DB / 20)
    firsts = []
    for m in mags:
        idx = np.flatnonzero(m >= thr)
        if idx.size:
            firsts.append(int(idx[0]))
    return (min(firsts) if firsts else 0), peak


def trim_cut(mags: Sequence[np.ndarray], sample_rate: int) -> int:
    onset, _ = onset_frame(mags)
    return max(0, onset - int(round(PREROLL_S * sample_rate)))


def apply_trim(pcm: np.ndarray, cut: int) -> np.ndarray:
    out = pcm[cut:].copy()
    if cut > 0 and out.shape[0] > 0:
        n = min(FADE_FRAMES, out.shape[0])
        ramp = 0.5 - 0.5 * np.cos(np.pi * (np.arange(n) + 0.5) / n)
        out[:n] = np.round(out[:n].astype(np.float64) * ramp[:, None]).astype(np.int32)
    return out


def quantise_to(pcm: np.ndarray, bits: int) -> np.ndarray:
    """Make the left-justified int32 hold exactly ``bits`` significant bits."""
    shift = 32 - bits
    return ((pcm.astype(np.int64) >> shift) << shift).astype(np.int32)


def clip_runs(x: np.ndarray, bits: int) -> int:
    """Number of runs of >= CLIP_RUN frames within 1 LSB of full scale."""
    lsb = 1 << (32 - bits)
    hi = 2147483647 - lsb
    lo = -2147483648 + lsb
    flags = ((x >= hi) | (x <= lo)).any(axis=1)
    if not flags.any():
        return 0
    d = np.diff(np.concatenate([[0], flags.astype(np.int8), [0]]))
    starts = np.flatnonzero(d == 1)
    ends = np.flatnonzero(d == -1)
    return int(np.sum((ends - starts) >= CLIP_RUN))


def peak_dbfs(x: np.ndarray) -> float:
    p = float(np.max(np.abs(x.astype(np.int64)))) / 2147483648.0 if x.size else 0.0
    return 20 * math.log10(p) if p > 0 else -200.0


# -- BS.1770 -----------------------------------------------------------------
def k_weighting(fs: int):
    f0, G, Q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    K = math.tan(math.pi * f0 / fs)
    Vh = 10 ** (G / 20)
    Vb = Vh**0.4996667741545416
    a0 = 1 + K / Q + K * K
    b1 = [(Vh + Vb * K / Q + K * K) / a0, 2 * (K * K - Vh) / a0, (Vh - Vb * K / Q + K * K) / a0]
    a1 = [1.0, 2 * (K * K - 1) / a0, (1 - K / Q + K * K) / a0]
    f0, Q = 38.13547087602444, 0.5003270373238773
    K = math.tan(math.pi * f0 / fs)
    d = 1 + K / Q + K * K
    b2 = [1.0, -2.0, 1.0]
    a2 = [1.0, 2 * (K * K - 1) / d, (1 - K / Q + K * K) / d]
    return (b1, a1), (b2, a2)


def max_momentary_lufs(x: np.ndarray, fs: int) -> Optional[float]:
    """x: float (frames, channels). Returns max 400 ms loudness, or None if silent."""
    if x.shape[1] == 1:
        x = np.repeat(x, 2, axis=1)  # dual-mono
    (b1, a1), (b2, a2) = k_weighting(fs)
    y = lfilter(b2, a2, lfilter(b1, a1, x, axis=0), axis=0)
    win = int(round(0.4 * fs))
    if y.shape[0] < win:
        y = np.vstack([y, np.zeros((win - y.shape[0], y.shape[1]))])
    power = np.sum(y * y, axis=1)
    c = np.concatenate([[0.0], np.cumsum(power)])
    hop = max(1, int(round(0.1 * fs)))
    starts = np.arange(0, y.shape[0] - win + 1, hop)
    ms = (c[starts + win] - c[starts]) / win
    m = float(ms.max()) if ms.size else 0.0
    return -0.691 + 10 * math.log10(m) if m > 0 else None


# -- pitch -------------------------------------------------------------------
@dataclass
class PitchResult:
    cents: Optional[float]  # error vs expected (positive = sharp)
    confidence: float  # 1 - CMND minimum (1 = perfectly periodic)
    octave: int  # 0, +1 (sounds an octave up) or -1
    note: str = ""


def _cmnd(frame: np.ndarray, max_tau: int) -> np.ndarray:
    # YIN difference d(tau) = sum_{j<W} (x_j - x_{j+tau})^2 = e0 + e_tau - 2 r(tau)
    n = frame.shape[0]
    w = n - max_tau
    nfft = 1 << int(math.ceil(math.log2(n + w)))
    r = np.fft.irfft(np.conj(np.fft.rfft(frame[:w], nfft)) * np.fft.rfft(frame, nfft), nfft)[: max_tau + 1]
    sq = np.concatenate([[0.0], np.cumsum(frame * frame)])
    taus = np.arange(max_tau + 1)
    e0 = sq[w]
    et = sq[taus + w] - sq[taus]
    d = np.maximum(e0 + et - 2 * r, 0.0)
    d[0] = 0.0
    out = np.ones_like(d)
    cs = np.cumsum(d[1:])
    with np.errstate(divide="ignore", invalid="ignore"):
        out[1:] = d[1:] * np.arange(1, max_tau + 1) / np.where(cs > 0, cs, np.inf)
    return out


def _best_in(cmnd: np.ndarray, lo: int, hi: int) -> Tuple[float, float]:
    lo = max(2, lo)
    hi = min(cmnd.shape[0] - 2, hi)
    if hi <= lo:
        return float("nan"), 1.0
    seg = cmnd[lo : hi + 1]
    i = int(np.argmin(seg)) + lo
    # parabolic interpolation
    a, b, c = cmnd[i - 1], cmnd[i], cmnd[i + 1]
    den = a - 2 * b + c
    off = 0.5 * (a - c) / den if den != 0 else 0.0
    return i + float(np.clip(off, -1, 1)), float(b)


def detect_pitch(x: np.ndarray, fs: int, expected_hz: float, onset: int = 0) -> PitchResult:
    mono = x.mean(axis=1) if x.ndim == 2 else x
    # Short periods leave too few lags for accurate interpolation: upsample.
    up = int(min(8, max(1, math.ceil(160 / (fs / expected_hz)))))
    if up > 1:
        mono = resample_poly(mono.astype(np.float64), up, 1)
        onset *= up
        fs *= up
    period = fs / expected_hz
    lo_tau = period / 2 ** (7 / 12)
    hi_tau = period * 2 ** (7 / 12)
    max_tau = int(math.ceil(hi_tau * 2 * 2 ** (7 / 12))) + 2  # room for the octave-below check
    frame_len = max(2048, 2 * max_tau + 256)
    start = onset + int(0.06 * fs)
    avail = mono.shape[0] - start
    if avail < frame_len:
        start = onset + int(0.015 * fs)
        avail = mono.shape[0] - start
    if avail < frame_len:
        return PitchResult(None, 0.0, 0, "too short for detection")
    hop = frame_len // 2
    n_frames = int(min(12, max(1, (avail - frame_len) // hop + 1)))
    est: List[float] = []
    conf: List[float] = []
    oct_votes: List[int] = []
    for k in range(n_frames):
        fr = mono[start + k * hop : start + k * hop + frame_len].astype(np.float64)
        if not np.any(fr):
            continue
        fr = fr - fr.mean()
        c = _cmnd(fr, max_tau)
        t1, v1 = _best_in(c, int(lo_tau), int(math.ceil(hi_tau)))
        t_up, v_up = _best_in(c, int(lo_tau / 2), int(math.ceil(hi_tau / 2)))
        t_dn, v_dn = _best_in(c, int(lo_tau * 2), int(math.ceil(hi_tau * 2)))
        # The octave below always has a dip where the expected period has one
        # (2T is also a period), so it only wins if it is much deeper.
        # Half the expected period being (almost) perfectly periodic means the
        # note sounds an octave up: T is then also a period, so v1 is small too.
        votes = 0
        if v_up < 0.08 and v_up <= v1 + 0.03:
            votes = +1
        elif v_dn < 0.35 * v1 and v_dn < 0.15 and v1 > 0.3:
            votes = -1
        if math.isnan(t1):
            continue
        est.append(t1)
        conf.append(1 - v1)
        oct_votes.append(votes)
    if not est:
        return PitchResult(None, 0.0, 0, "no periodic frames")
    tau = float(np.median(est))
    hz = fs / tau
    cents = 1200 * math.log2(hz / expected_hz)
    octave = int(np.sign(np.sum(oct_votes))) if abs(np.sum(oct_votes)) > len(oct_votes) / 2 else 0
    return PitchResult(cents, float(np.median(conf)), octave)


def detect_pitch_spectral(x: np.ndarray, fs: int, expected_hz: float, onset: int = 0) -> PitchResult:
    """Strongest spectral peak within +-7 semitones of the expected pitch.

    For bars and bells whose fundamental dominates but whose partials are far
    from harmonic (glockenspiel), where YIN's periodicity model is unreliable.
    Hann window, 4x zero padding, parabolic interpolation on log magnitude.
    """
    mono = x.mean(axis=1) if x.ndim == 2 else x
    start = onset + int(0.05 * fs)
    seg = mono[start : start + int(1.5 * fs)].astype(np.float64)
    if seg.shape[0] < 4096:
        return PitchResult(None, 0.0, 0, "too short for detection")
    n = 1 << int(math.ceil(math.log2(seg.shape[0] * 4)))
    mag = np.abs(np.fft.rfft(seg * np.hanning(seg.shape[0]), n))
    freqs = np.fft.rfftfreq(n, 1 / fs)
    lo, hi = expected_hz / 2 ** (7 / 12), expected_hz * 2 ** (7 / 12)
    idx = np.flatnonzero((freqs >= lo) & (freqs <= hi))
    if idx.size < 3:
        return PitchResult(None, 0.0, 0, "out of range")
    i = int(idx[np.argmax(mag[idx])])
    if i <= idx[0] or i >= idx[-1]:
        return PitchResult(None, 0.0, 0, "peak at window edge")
    a, b, c = (20 * math.log10(mag[k] + 1e-30) for k in (i - 1, i, i + 1))
    off = 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) != 0 else 0.0
    hz = (i + off) * fs / n
    prominence = float(mag[i] / (np.median(mag[idx]) + 1e-30))
    # Octave check: a much stronger peak an octave up means the bar sounds there.
    up = np.flatnonzero((freqs >= 2 * lo) & (freqs <= 2 * hi))
    octave = 1 if up.size and float(mag[up].max()) > 4 * float(mag[i]) else 0
    conf = max(0.0, min(1.0, 1 - 3 / prominence))
    return PitchResult(1200 * math.log2(hz / expected_hz), conf, octave)


def midi_hz(note: float, a4: float = 440.0) -> float:
    return a4 * 2 ** ((note - 69) / 12)
