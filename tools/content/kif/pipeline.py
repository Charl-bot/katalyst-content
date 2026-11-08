"""Stage 2-4: download + convert + QA (build), validate, and pack.

Output layout for one pack (D-191):

    <out>/<pack>/<instrument>.kif.json     one per instrument in the pack
    <out>/<pack>/samples/<name>.flac       shared by the pack's instruments
    <out>/<pack>/qa/<instrument>.json      QA measurements
    <out>/<pack>/provenance.json           every source file: path, commit, blob, sha256

Sample PCM is never gain-changed: loudness normalisation is the KIF
instrument ``gain_db`` (applied by the sampler), so velocity layers keep the
exact relationship the upstream recording had (D-194).
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
import os
import re
import struct
import tarfile
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import audio
from . import plan as planmod
from .upstream import ProvenanceError, Upstream, sha256_file

CONVERTER = "katalyst-kif-convert 1.0.0"
TARGET_LUFS = -18.0  # max momentary loudness of a top-velocity single note
PEAK_CEILING_DBFS = -1.0
PITCH_FLAG_CENTS = 10.0
PITCH_MIN_CONFIDENCE = 0.7
PRELOAD_FRAMES = 8192
PACK_PART_LIMIT = 1_900_000_000  # GitHub release assets must be < 2 GiB
# Defaults; instruments.json "release" overrides them (D-191: a public,
# content-only repository, because Charl-bot/katalyst-suite is private and its
# release assets cannot be downloaded anonymously by the app).
RELEASE_REPO = "Charl-bot/katalyst-content"
RELEASE_TAG = "instruments-cc0-v1"


def release_base(repo: str = RELEASE_REPO, tag: str = RELEASE_TAG) -> str:
    return f"https://github.com/{repo}/releases/download/{tag}"


# -- helpers -------------------------------------------------------------------
def out_name(path: str) -> str:
    base = os.path.splitext(os.path.basename(path))[0]
    base = base.replace("#", "s")
    base = re.sub(r"[^A-Za-z0-9_.-]+", "_", base).strip("_")
    return base


def read_smpl_loops(path: Path) -> List[Tuple[int, int]]:
    """Loop points from a WAV 'smpl' chunk (frames, end inclusive)."""
    try:
        with open(path, "rb") as f:
            hdr = f.read(12)
            if hdr[:4] != b"RIFF" or hdr[8:12] != b"WAVE":
                return []
            while True:
                ch = f.read(8)
                if len(ch) < 8:
                    return []
                cid, size = ch[:4], struct.unpack("<I", ch[4:])[0]
                if cid == b"smpl":
                    data = f.read(size)
                    n = struct.unpack("<I", data[28:32])[0]
                    loops = []
                    for i in range(n):
                        o = 36 + 24 * i
                        _id, _type, start, end = struct.unpack("<IIII", data[o : o + 16])
                        loops.append((int(start), int(end)))
                    return loops
                f.seek(size + (size & 1), 1)
    except (OSError, struct.error):
        return []


@dataclass
class SampleOut:
    source: str
    name: str
    blob: str
    src_sha256: str = ""
    src_bytes: int = 0
    src_format: str = ""
    sha256: str = ""
    bytes: int = 0
    frames: int = 0
    channels: int = 0
    sample_rate: int = 0
    bits: int = 0
    cut: int = 0
    peak_dbfs: float = -200.0
    tail_rms_dbfs: float = -200.0
    clip_runs: int = 0
    loudness: Optional[float] = None
    pitch_cents: Optional[float] = None
    pitch_conf: float = 0.0
    pitch_octave: int = 0
    pitch_note: str = ""
    smpl_loops: List[Tuple[int, int]] = field(default_factory=list)


class _UF:
    def __init__(self):
        self.p: Dict[str, str] = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def _db(x: float) -> float:
    return 20 * math.log10(x) if x > 0 else -200.0


# -- build ---------------------------------------------------------------------
def build_pack(pp: planmod.PackPlan, out_root: Path, keep_raw: bool = False, workers: int = 3, log=print) -> dict:
    pack_id = pp.cfg["id"]
    up: Upstream = pp.upstream
    pdir = out_root / pack_id
    sdir = pdir / "samples"
    sdir.mkdir(parents=True, exist_ok=True)
    (pdir / "qa").mkdir(exist_ok=True)

    # Unique, stable output names.
    names: Dict[str, str] = {}
    used: Counter = Counter()
    for src in sorted(pp.sources):
        n = out_name(src)
        if used[n]:
            n = f"{n}_{hashlib.sha1(src.encode()).hexdigest()[:6]}"
        used[out_name(src)] += 1
        names[src] = n

    # Alignment groups (all mic layers of a hit share one trim) and pitch targets.
    uf = _UF()
    pitch_target: Dict[str, Tuple[float, float]] = {}
    pitch_method: Dict[str, str] = {}
    for ip in pp.instruments:
        for z in ip.zones:
            first = z.layers[0][0]
            for src, _ in z.layers:
                uf.union(src, first)
                if ip.pitched and z.root is not None and z.trigger == "attack" and src not in pitch_target:
                    pitch_target[src] = (z.root, z.tune_cents)
                    pitch_method[src] = ip.cfg.get("pitch_method", "yin")
    groups: Dict[str, List[str]] = defaultdict(list)
    for src in sorted(pp.sources):
        groups[uf.find(src)].append(src)

    # Default-preset mic gains for multi-mic loudness.
    mic_gain: Dict[str, Optional[float]] = {}
    for ip in pp.instruments:
        presets = ip.cfg.get("mic_presets")
        if presets:
            d = next((p for p in presets if p.get("default")), presets[0])
            mic_gain.update(d["gains_db"])
    mic_of: Dict[str, str] = {}
    for ip in pp.instruments:
        for z in ip.zones:
            for src, mic in z.layers:
                mic_of[src] = mic

    results: Dict[str, SampleOut] = {}
    group_loudness: Dict[str, Optional[float]] = {}
    group_peak: Dict[str, float] = {}
    lock = threading.Lock()
    done = [0]
    total = len(groups)

    def process(group: List[str]) -> None:
        loaded = []
        for src in group:
            entry = pp.sources[src]
            path = up.fetch(entry)
            pcm = audio.read_pcm(str(path))
            so = SampleOut(source=src, name=names[src], blob=entry.blob)
            so.src_sha256 = sha256_file(path)
            so.src_bytes = path.stat().st_size
            so.src_format = f"{pcm.fmt}/{pcm.subtype}"
            so.smpl_loops = read_smpl_loops(path) if pcm.fmt == "WAV" else []
            loaded.append((src, path, pcm, so))
        mags = [np.max(np.abs(p.data.astype(np.float64)), axis=1) / 2147483648.0 for _, _, p, _ in loaded]
        rates = {p.sample_rate for _, _, p, _ in loaded}
        if len(rates) != 1:
            raise ProvenanceError(f"mic layers {group} have different sample rates {rates}")
        sr = rates.pop()
        cut = audio.trim_cut(mags, sr)
        onset, _ = audio.onset_frame(mags)
        mix = None
        for src, path, pcm, so in loaded:
            data = audio.quantise_to(audio.apply_trim(pcm.data, cut), pcm.bits)
            dest = sdir / f"{so.name}.flac"
            audio.write_flac(str(dest), data, pcm.sample_rate, pcm.bits)
            so.sha256 = sha256_file(dest)
            so.bytes = dest.stat().st_size
            so.frames, so.channels = int(data.shape[0]), int(data.shape[1])
            so.sample_rate, so.bits, so.cut = pcm.sample_rate, pcm.bits, cut
            so.peak_dbfs = audio.peak_dbfs(data)
            so.clip_runs = audio.clip_runs(data, pcm.bits)
            fl = data.astype(np.float64) / 2147483648.0
            tail = fl[-int(0.1 * sr) :]
            so.tail_rms_dbfs = 10 * math.log10(float(np.mean(tail * tail)) + 1e-30)
            so.loudness = audio.max_momentary_lufs(fl, sr)
            if src in pitch_target:
                root, tune = pitch_target[src]
                detector = audio.detect_pitch_spectral if pitch_method.get(src) == "spectral" else audio.detect_pitch
                r = detector(fl, sr, audio.midi_hz(root), onset=max(0, onset - cut))
                so.pitch_cents = None if r.cents is None else r.cents + tune
                so.pitch_conf, so.pitch_octave, so.pitch_note = r.confidence, r.octave, r.note
            g = mic_gain.get(mic_of.get(src, "main"), 0.0) if mic_gain else 0.0
            if g is not None and len(loaded) > 1:
                st = fl if fl.shape[1] == 2 else np.repeat(fl, 2, axis=1)
                st = st * 10 ** (g / 20)
                if mix is None:
                    mix = st
                else:
                    n = max(mix.shape[0], st.shape[0])
                    mix = np.pad(mix, ((0, n - mix.shape[0]), (0, 0))) + np.pad(st, ((0, n - st.shape[0]), (0, 0)))
            if not keep_raw:
                up.drop(pp.sources[src])
        key = uf.find(group[0])
        with lock:
            for src, _, _, so in loaded:
                results[src] = so
            if mix is not None:
                group_loudness[key] = audio.max_momentary_lufs(mix, sr)
                group_peak[key] = _db(float(np.max(np.abs(mix))))
            done[0] += 1
            if done[0] % 100 == 0 or done[0] == total:
                log(f"   [{pack_id}] {done[0]}/{total} groups")

    with ThreadPoolExecutor(workers) as ex:
        for fut in [ex.submit(process, g) for g in groups.values()]:
            fut.result()

    prov = {
        "library": pp.lib["id"],
        "repo": pp.lib["repo"],
        "audio_commit": up.audio_commit,
        "mapping_commit": up.mapping_commit,
        "licence_file": pp.lib["licence_file"],
        "licence_sha256": pp.licence_sha256,
        "files": [
            {"path": s.source, "commit": up.audio_commit, "git_blob": s.blob, "sha256": s.src_sha256, "bytes": s.src_bytes,
             "output": f"samples/{s.name}.flac", "output_sha256": s.sha256}
            for s in sorted(results.values(), key=lambda r: r.source)
        ],
    }
    (pdir / "provenance.json").write_text(json.dumps(prov, indent=1) + "\n")
    (pdir / "LICENSE.upstream.txt").write_text(pp.licence_text)

    summary = {"pack": pack_id, "instruments": []}
    for ip in pp.instruments:
        kif, qa = assemble(ip, pp, results, uf, group_loudness, group_peak)
        (pdir / f"{ip.id}.kif.json").write_text(json.dumps(kif, indent=1) + "\n")
        (pdir / "qa" / f"{ip.id}.json").write_text(json.dumps(qa, indent=1) + "\n")
        summary["instruments"].append(qa)
    summary["pack_bytes"] = sum(s.bytes for s in results.values())
    summary["sample_count"] = len(results)
    summary["source_bytes"] = sum(s.src_bytes for s in results.values())
    (pdir / "qa" / "_pack.json").write_text(json.dumps(summary, indent=1) + "\n")
    return summary


def _rr_groups(zones) -> Dict[int, str]:
    keys: Dict[tuple, str] = {}
    out = {}
    for i, z in enumerate(zones):
        if not z.rr or z.rr["mode"] != "sequence":
            continue
        k = (z.articulation, z.key_lo, z.key_hi, z.vel_lo, z.vel_hi, z.trigger, tuple((c["cc"], c["lo"], c["hi"]) for c in z.cc))
        keys.setdefault(k, f"g{len(keys) + 1}")
        out[i] = keys[k]
    return out


def assemble(ip, pp, results, uf, group_loudness, group_peak):
    inst = ip.cfg
    zones = ip.zones
    multi = bool(inst.get("multi_mic"))
    envs = Counter(json.dumps(z.envelope, sort_keys=True) for z in zones if z.trigger == "attack")
    default_env = json.loads(envs.most_common(1)[0][0])
    rr_group = _rr_groups(zones)
    used_samples: Dict[str, SampleOut] = {}
    kz = []
    notes = list(ip.notes)
    loops = 0
    offset_clamped = 0
    for i, z in enumerate(zones):
        cut = results[z.layers[0][0]].cut
        zo = {"key_lo": z.key_lo, "key_hi": z.key_hi, "root": z.root, "vel_lo": z.vel_lo, "vel_hi": z.vel_hi}
        if len(ip.articulations) > 1:
            zo["articulation"] = z.articulation
        zo["trigger"] = z.trigger
        if z.vel_xfade:
            zo["vel_xfade"] = z.vel_xfade
        if z.rr:
            rr = dict(z.rr)
            if rr["mode"] == "sequence":
                rr["group"] = rr_group[i]
            zo["rr"] = rr
        if z.cc:
            zo["cc"] = z.cc
        if z.tune_cents:
            zo["tune_cents"] = round(z.tune_cents, 3)
        if z.gain_db:
            zo["gain_db"] = round(z.gain_db, 4)
        if z.pan:
            zo["pan"] = round(z.pan, 4)
        if z.amp_veltrack is not None:
            zo["amp_veltrack"] = z.amp_veltrack
        off = z.offset - cut
        if z.offset and off < 0:
            offset_clamped += 1
        if off > 0:
            zo["offset"] = off
        so0 = results[z.layers[0][0]]
        loop = None
        if z.loop_mode in ("loop_continuous", "loop_sustain") or (z.loop_mode is None and so0.smpl_loops):
            if z.loop_start is not None and z.loop_end is not None:
                ls, le = z.loop_start, z.loop_end
            elif so0.smpl_loops:
                ls, le = so0.smpl_loops[0]
            else:
                ls, le = cut, cut + so0.frames - 1  # whole sample, as SFZ does
            ls, le = ls - cut, le - cut
            if ls < 0 or le >= so0.frames or le <= ls:
                raise ProvenanceError(f"{z.origin}: loop {ls}-{le} invalid after trimming {cut} frames")
            loop = {"mode": "sustain" if z.loop_mode == "loop_sustain" else "continuous", "start": int(ls), "end": int(le)}
            loops += 1
        if loop:
            zo["loop"] = loop
        if z.one_shot:
            zo["one_shot"] = True
        if z.release_decay:
            zo["release_decay_db_per_s"] = z.release_decay
        if z.group is not None:
            zo["choke_group"] = z.group
        if z.off_by is not None:
            zo["choked_by"] = z.off_by
            if z.off_time is not None:
                zo["choke_time"] = z.off_time
        if z.envelope != default_env:
            zo["envelope"] = z.envelope
        zo["layers"] = [{"sample": results[s].name, "mic": m} for s, m in z.layers]
        for s, _ in z.layers:
            used_samples[s] = results[s]
        kz.append(zo)
    if offset_clamped:
        notes.append(f"{offset_clamped} zone(s): upstream start offset fell inside the trimmed lead-in; playback starts at the trim point")

    # -- loudness normalisation (one gain for the whole instrument)
    def zone_level(z):
        if multi:
            k = uf.find(z.layers[0][0])
            return group_loudness.get(k), group_peak.get(k, -200.0)
        so = results[z.layers[0][0]]
        return so.loudness, so.peak_dbfs

    top_vel: Dict[tuple, int] = {}
    for z in zones:
        if z.trigger == "attack":
            k = (z.articulation, z.key_lo)
            top_vel[k] = max(top_vel.get(k, 0), z.vel_hi)
    refs, peaks = [], []
    for z in zones:
        if z.trigger != "attack":
            continue
        lv, pk = zone_level(z)
        peaks.append(pk + z.gain_db)
        if z.vel_hi == top_vel[(z.articulation, z.key_lo)] and lv is not None:
            refs.append(lv + z.gain_db)
    ref = median(refs) if refs else None
    gain = (TARGET_LUFS - ref) if ref is not None else 0.0
    max_peak = max(peaks) if peaks else -200.0
    limited = False
    if max_peak + gain > PEAK_CEILING_DBFS:
        gain = PEAK_CEILING_DBFS - max_peak
        limited = True

    samples = []
    for src in sorted(used_samples, key=lambda s: used_samples[s].name):
        so = used_samples[src]
        e = {
            "id": so.name, "file": f"samples/{so.name}.flac", "sha256": so.sha256, "bytes": so.bytes,
            "frames": so.frames, "channels": so.channels, "sample_rate": so.sample_rate, "bit_depth": so.bits,
            "preload_frames": min(so.frames, PRELOAD_FRAMES), "peak_dbfs": round(so.peak_dbfs, 2), "tail_rms_dbfs": round(max(so.tail_rms_dbfs, -199.0), 2),
            "measured_pitch_cents": None if so.pitch_cents is None else round(so.pitch_cents, 2),
            "trimmed_frames": so.cut,
            "source": {"path": so.source, "git_blob": so.blob, "sha256": so.src_sha256, "bytes": so.src_bytes, "format": so.src_format},
        }
        samples.append(e)
    rates = Counter(s["sample_rate"] for s in samples)
    mics = inst.get("mics") or [{"id": "main", "name": "Stereo" if max(s["channels"] for s in samples) == 2 else "Mono"}]
    presets = inst.get("mic_presets") or [{"id": "default", "name": "Default", "default": True, "gains_db": {m["id"]: 0 for m in mics}}]
    kif = {
        "kif": 1,
        "id": f"katalyst.{inst['id']}",
        "version": "1.0.0",
        "name": inst["name"],
        "category": inst["category"],
    }
    if inst.get("description"):
        kif["description"] = inst["description"]
    kif.update({
        "pitched": ip.pitched,
        "pack": pp.cfg["id"],
        "licence": {"spdx": "CC0-1.0", "verdict": "GREEN", "note": f"{pp.lib['title']} ({pp.lib['publisher']}); courtesy credit in CONTENT_CREDITS"},
        "audio": {"format": "flac", "sample_rate": rates.most_common(1)[0][0], "channels": max(s["channels"] for s in samples),
                  "bit_depths": sorted({s["bit_depth"] for s in samples})},
        "gain_db": round(gain, 2),
        "loudness": {"method": "BS.1770-4 max momentary of top-velocity attack zones (median), peak-limited",
                     "target_lufs": TARGET_LUFS, "reference_lufs": None if ref is None else round(ref, 2),
                     "peak_dbfs_after_gain": round(max_peak + gain, 2), "limited_by_peak": limited},
        "envelope": {k: round(v, 4) for k, v in default_env.items()},
        "velocity": {"track": 1.0},
        "polyphony": 128 if multi else 64,
        "mics": mics,
        "mic_presets": presets,
        "articulations": ip.articulations,
    })
    if inst.get("controls"):
        kif["controls"] = inst["controls"]
    if ip.key_labels:
        kif["key_labels"] = ip.key_labels
    if ip.choke_triggers:
        kif["choke_triggers"] = sorted(ip.choke_triggers, key=lambda t: (t["group"], t["key_lo"]))
    kif["zones"] = kz
    kif["samples"] = samples
    kif["provenance"] = {
        "kind": "upstream-sfz", "library": pp.lib["id"], "repo": pp.lib["repo"],
        "audio_commit": pp.upstream.audio_commit, "mapping_commit": pp.upstream.mapping_commit,
        "sfz": ip.sfz_files, "converter": CONVERTER,
        "modifications": [
            "Leading silence trimmed (onset at -50 dB re. peak, 3 ms pre-roll kept, 32-frame fade-in on trimmed samples)",
            "Converted from WAV/FLAC to FLAC at the source bit depth and sample rate",
            "SFZ mapping converted to KIF v1; instrument gain set by loudness normalisation (PCM not gain-changed)",
        ],
    }
    qa = quality_report(ip, kif, results, used_samples, notes, loops, ref, gain, max_peak, limited, pp)
    return kif, qa


def coverage_stats(kif: dict) -> dict:
    """Velocity layers, key range, missing notes and velocity holes from the KIF alone.

    Counted per articulation, per key and per CC state (sustain pedal up/down,
    hi-hat openness, snares on/off are separate sets), so a key with three
    hi-hat states of 12 layers each reads 12, not 36. Round-robin copies of one
    velocity range count once.
    """
    zones = [z for z in kif["zones"] if z["trigger"] == "attack"]
    arts = [a["id"] for a in kif["articulations"]]
    by_art = defaultdict(list)
    for z in zones:
        by_art[z.get("articulation", arts[0])].append(z)
    vel_layers, missing, vel_holes, keys_mapped = {}, {}, {}, {}
    for a, zs in by_art.items():
        per_state = defaultdict(set)
        keys = set()
        for z in zs:
            state = tuple((c["cc"], c["lo"], c["hi"]) for c in z.get("cc", []))
            for k in range(z["key_lo"], z["key_hi"] + 1):
                per_state[(k, state)].add((z["vel_lo"], z["vel_hi"]))
                keys.add(k)
        counts = [len(v) for v in per_state.values()]
        vel_layers[a] = {"max": max(counts), "median": median(counts), "min": min(counts)}
        lo, hi = min(keys), max(keys)
        keys_mapped[a] = [lo, hi, len(keys)]
        if kif["pitched"]:
            missing[a] = [k for k in range(lo, hi + 1) if k not in keys]
        holes = set()
        for (k, _state), ranges in per_state.items():
            cov = set()
            for vlo, vhi in ranges:
                cov.update(range(vlo, vhi + 1))
            if len(cov) < 127:
                holes.add(k)
        vel_holes[a] = len(holes)
    return {"velocity_layers": vel_layers, "keys": keys_mapped, "missing_notes": missing, "keys_with_velocity_holes": vel_holes}


def quality_report(ip, kif, results, used, notes, loops, ref, gain, max_peak, limited, pp):
    zones = kif["zones"]
    att = [z for z in zones if z["trigger"] == "attack"]
    rel = [z for z in zones if z["trigger"] == "release"]
    arts = [a["id"] for a in kif["articulations"]]
    cov = coverage_stats(kif)
    vel_layers, missing, vel_holes, keys_mapped = cov["velocity_layers"], cov["missing_notes"], cov["keys_with_velocity_holes"], cov["keys"]
    rr = Counter()
    for z in att:
        if "rr" in z:
            rr[z["rr"]["mode"] + (":" + str(z["rr"]["length"]) if z["rr"]["mode"] == "sequence" else "")] += 1
    pitch_rows = []
    unmeasured = 0
    octave = []
    attack_sources = {s for z in ip.zones if z.trigger == "attack" for s, _ in z.layers}
    for src, so in used.items():
        if not kif["pitched"] or so.source not in attack_sources:
            continue
        if so.pitch_cents is None or so.pitch_conf < PITCH_MIN_CONFIDENCE:
            unmeasured += 1
            continue
        if so.pitch_octave:
            octave.append({"sample": so.name, "octave": so.pitch_octave})
        pitch_rows.append((so.name, so.pitch_cents, so.pitch_conf))
    flagged = sorted([r for r in pitch_rows if abs(r[1]) > PITCH_FLAG_CENTS], key=lambda r: -abs(r[1]))
    abs_c = [abs(r[1]) for r in pitch_rows]
    clipped = sorted(so.name for so in used.values() if so.clip_runs)
    by_name = {so.name: so for so in used.values()}
    zg = [z.get("gain_db", 0.0) for z in zones]
    tails = [by_name[l["sample"]].tail_rms_dbfs + z.get("gain_db", 0.0) + gain for z in zones for l in z["layers"]]
    cuts_ms = [1000 * so.cut / so.sample_rate for so in used.values()]
    total_bytes = sum(so.bytes for so in used.values())
    return {
        "instrument": kif["id"], "name": kif["name"], "pack": kif["pack"],
        "zones": len(zones), "attack_zones": len(att), "release_zones": len(rel), "samples": len(used),
        "articulations": arts, "velocity_layers": vel_layers, "round_robin": dict(rr),
        "keys": keys_mapped, "missing_notes": missing, "keys_with_velocity_holes": vel_holes,
        "loops": loops,
        "pitch": None if not kif["pitched"] else {
            "measured": len(pitch_rows), "not_measurable": unmeasured,
            "median_abs_cents": round(median(abs_c), 2) if abs_c else None,
            "max_abs_cents": round(max(abs_c), 2) if abs_c else None,
            "flagged_over_10c": len(flagged),
            "flagged": [{"sample": n, "cents": round(c, 1), "confidence": round(cf, 2)} for n, c, cf in flagged],
            "octave_suspects": octave,
        },
        "clipping": {"samples_with_clip_runs": len(clipped), "list": clipped},
        "zone_gain_db": {"min": round(min(zg), 2), "max": round(max(zg), 2), "zones_over_24db": sum(1 for g in zg if g > 24)},
        "tail_noise": {"worst_dbfs_after_gains": round(max(tails), 1),
                       "layers_above_minus60_dbfs": sum(1 for t in tails if t > -60),
                       "note": "RMS of each sample's last 100 ms plus zone and instrument gain. High values mean hiss, or a sample that ends before the sound has decayed (an audible cut-off on long notes)."},
        "trim_ms": {"min": round(min(cuts_ms), 2), "median": round(median(cuts_ms), 2), "max": round(max(cuts_ms), 2),
                    "untrimmed": sum(1 for c in cuts_ms if c == 0)},
        "loudness": {"reference_lufs": None if ref is None else round(ref, 2), "gain_db": round(gain, 2),
                     "peak_dbfs_after_gain": round(max_peak + gain, 2), "limited_by_peak": limited},
        "sample_rates": dict(Counter(so.sample_rate for so in used.values())),
        "bit_depths": dict(Counter(so.bits for so in used.values())),
        "channels": dict(Counter(so.channels for so in used.values())),
        "duration_s": round(sum(so.frames / so.sample_rate for so in used.values()), 1),
        "bytes": total_bytes,
        "source_bytes": sum(so.src_bytes for so in used.values()),
        "skipped_regions": dict(ip.skipped),
        "ignored_opcodes": dict(ip.ignored_opcodes),
        "notes": notes,
    }


# -- commands --------------------------------------------------------------------
def _libs(libs_cfg):
    return {l["id"]: l for l in libs_cfg["libraries"]}


def cmd_build(args, libs_cfg, insts):
    import build as cli  # the CLI module (sys.path has tools/content)
    from .validate import validate_dir

    libs = _libs(libs_cfg)
    for pack in cli.selected_packs(insts, args.pack):
        lib = libs[pack["library"]]
        up = cli.upstream_for(lib, args.work)
        pp = planmod.plan_pack(pack, lib, libs_cfg["global_exclude"], up)
        print(f"== building {pack['id']}: {len(pp.sources)} source files", flush=True)
        s = build_pack(pp, args.out, keep_raw=args.keep_raw, log=lambda m: print(m, flush=True))
        errs = validate_dir(args.out / pack["id"], check_files=True)
        if errs:
            raise SystemExit(f"{pack['id']}: validation failed:\n  " + "\n  ".join(errs[:30]))
        print(f"   ok: {s['sample_count']} samples, {s['pack_bytes'] / 1e6:.1f} MB (source {s['source_bytes'] / 1e6:.1f} MB)", flush=True)


def cmd_validate(args, libs_cfg, insts):
    from .validate import validate_dir

    bad = 0
    for pack in insts["packs"]:
        d = args.out / pack["id"]
        if not d.exists():
            print(f"{pack['id']}: not built")
            bad += 1
            continue
        errs = validate_dir(d, check_files=True)
        print(f"{pack['id']}: {'OK' if not errs else str(len(errs)) + ' error(s)'}")
        for e in errs[:20]:
            print("   ", e)
        bad += bool(errs)
    raise SystemExit(1 if bad else 0)


def _tarinfo(name: str, size: int) -> tarfile.TarInfo:
    ti = tarfile.TarInfo(name)
    ti.size, ti.mtime, ti.mode, ti.uid, ti.gid, ti.uname, ti.gname = size, 0, 0o644, 0, 0, "", ""
    return ti


def write_pack_parts(pdir: Path, dist: Path, pack_id: str, version: str, base_url: str = None) -> List[dict]:
    """Deterministic .tar.gz parts (sorted members, zero mtimes/owners, gzip mtime 0)."""
    kifs = sorted(p.name for p in pdir.glob("*.kif.json"))
    samples = sorted(p.name for p in (pdir / "samples").glob("*.flac"))
    members = [(f"{pack_id}/{k}", pdir / k) for k in kifs] + [(f"{pack_id}/samples/{s}", pdir / "samples" / s) for s in samples]
    parts: List[List[tuple]] = [[]]
    size = 0
    for m in members:
        b = m[1].stat().st_size + 1024
        if size + b > PACK_PART_LIMIT and parts[-1]:
            parts.append([])
            size = 0
        parts[-1].append(m)
        size += b
    dist.mkdir(parents=True, exist_ok=True)
    out = []
    for i, part in enumerate(parts, 1):
        suffix = "" if len(parts) == 1 else f".part{i}of{len(parts)}"
        fname = f"katalyst-{pack_id}-{version}{suffix}.tar.gz"
        dest = dist / fname
        with open(dest, "wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=6) as gz:
                with tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as tar:
                    for name, path in part:
                        with open(path, "rb") as f:
                            tar.addfile(_tarinfo(name, path.stat().st_size), f)
        out.append({"file": fname, "url": f"{base_url or release_base()}/{fname}", "sha256": sha256_file(dest), "bytes": dest.stat().st_size,
                    "members": len(part)})
    return out


def verify_pack_parts(parts: List[dict], dist: Path, pdir: Path) -> None:
    """Re-open each archive and check every member against the KIF hashes."""
    expected: Dict[str, str] = {}
    for k in pdir.glob("*.kif.json"):
        doc = json.loads(k.read_text())
        expected[f"{pdir.name}/{k.name}"] = sha256_file(k)
        for s in doc["samples"]:
            expected[f"{pdir.name}/{s['file']}"] = s["sha256"]
    seen = set()
    for p in parts:
        with tarfile.open(dist / p["file"], "r:gz") as tar:
            for m in tar:
                h = hashlib.sha256()
                f = tar.extractfile(m)
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
                if expected.get(m.name) != h.hexdigest():
                    raise ProvenanceError(f"{p['file']}: member {m.name} does not match its KIF hash")
                seen.add(m.name)
    missing = set(expected) - seen
    if missing:
        raise ProvenanceError(f"packs for {pdir.name} lack {sorted(missing)[:5]}")


def cmd_pack(args, libs_cfg, insts):
    import build as cli

    libs = _libs(libs_cfg)
    manifest_path = cli.REPO / "content" / "instruments" / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"packs": []}
    by_id = {p["id"]: p for p in manifest.get("packs", [])}
    rel = insts.get("release", {})
    repo_slug, tag = rel.get("repo", RELEASE_REPO), rel.get("tag", RELEASE_TAG)
    base = release_base(repo_slug, tag)
    for pack in cli.selected_packs(insts, args.pack):
        pdir = args.out / pack["id"]
        qa = json.loads((pdir / "qa" / "_pack.json").read_text())
        parts = write_pack_parts(pdir, args.dist, pack["id"], insts["version"], base)
        verify_pack_parts(parts, args.dist, pdir)
        lib = libs[pack["library"]]
        entry = {
            "id": pack["id"],
            "version": insts["version"],
            "library": lib["id"],
            "licence": {"spdx": lib["licence_spdx"], "verdict": "GREEN"},
            "source": {"repo": lib["repo"], "audio_commit": lib["audio_commit"], "mapping_commit": lib["mapping_commit"]},
            "installed_bytes": qa["pack_bytes"] + sum(k.stat().st_size for k in pdir.glob("*.kif.json")),
            "sample_count": qa["sample_count"],
            "instruments": [
                {"id": json.loads(k.read_text())["id"], "name": json.loads(k.read_text())["name"],
                 "category": json.loads(k.read_text())["category"], "kif": f"{pack['id']}/{k.name}",
                 "kif_sha256": sha256_file(k), "kif_bytes": k.stat().st_size}
                for k in sorted(pdir.glob("*.kif.json"))
            ],
            "parts": [{k: v for k, v in p.items()} for p in parts],
        }
        by_id[pack["id"]] = entry
        print(f"{pack['id']}: {len(parts)} part(s), {sum(p['bytes'] for p in parts) / 1e6:.1f} MB -> {args.dist}", flush=True)
        if getattr(args, "drop_after_hash", False):
            for p in parts:
                (args.dist / p["file"]).unlink()
    order = [p["id"] for p in insts["packs"]]
    manifest = {
        "schema": "katalyst-instrument-manifest/1",
        "comment": "Generated by tools/content/build.py pack (D-191). The app downloads each part from 'url', checks sha256 and bytes, extracts, then checks every KIF (kif_sha256) and every sample (KIF samples[].sha256) before use.",
        "release_repo": repo_slug,
        "release_tag": tag,
        "base_url": base,
        "kif_version": 1,
        "packs": [by_id[i] for i in order if i in by_id],
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=1) + "\n")


# -- report: QA markdown + per-library provenance into the repository ----------
def _mb(b: int) -> str:
    return f"{b / 1e6:.1f} MB"


def render_qa_md(q: dict) -> str:
    L = [f"# QA: {q['name']} (`{q['instrument']}`)", ""]
    L.append(f"Pack `{q['pack']}`. Measurements only; no listening judgement is implied.")
    L.append("")
    L.append("| Measure | Value |")
    L.append("| --- | --- |")
    L.append(f"| Zones | {q['zones']} ({q['attack_zones']} attack, {q['release_zones']} release) |")
    L.append(f"| Samples | {q['samples']} ({q['duration_s']} s of audio) |")
    L.append(f"| Articulations | {', '.join(q['articulations'])} |")
    vl = "; ".join(f"{a}: max {v['max']}, median {v['median']}" for a, v in q["velocity_layers"].items())
    L.append(f"| Velocity layers per key | {vl} |")
    L.append(f"| Round robin | {', '.join(f'{k} ({n} zones)' for k, n in q['round_robin'].items()) or 'none'} |")
    keys = "; ".join(f"{a}: {v[0]}-{v[1]} ({v[2]} keys)" for a, v in q["keys"].items())
    L.append(f"| Key range | {keys} |")
    if q["missing_notes"]:
        parts = []
        for a, v in q["missing_notes"].items():
            shown = ", ".join(map(str, v[:12])) + ("..." if len(v) > 12 else "")
            parts.append(f"{a}: {len(v)}" + (f" ({shown})" if v else ""))
        L.append(f"| Missing notes inside the range | {'; '.join(parts)} |")
    L.append(f"| Keys with velocity holes | {', '.join(f'{a}: {n}' for a, n in q['keys_with_velocity_holes'].items())} |")
    L.append(f"| Loops (upstream-provided only) | {q['loops']} |")
    p = q.get("pitch")
    if p:
        L.append(f"| Pitch, measured samples | {p['measured']} (not measurable: {p['not_measurable']}) |")
        L.append(f"| Pitch error, median / max abs | {p['median_abs_cents']} / {p['max_abs_cents']} cents |")
        L.append(f"| Pitch, over 10 cents | {p['flagged_over_10c']} |")
        L.append(f"| Octave suspects | {len(p['octave_suspects'])} |")
    else:
        L.append("| Pitch | not applicable (unpitched) |")
    L.append(f"| Samples with clipping runs | {q['clipping']['samples_with_clip_runs']} |")
    t = q["trim_ms"]
    L.append(f"| Lead-in trimmed | median {t['median']} ms, max {t['max']} ms; {t['untrimmed']} untrimmed |")
    lo = q["loudness"]
    lim = " (peak-limited)" if lo["limited_by_peak"] else ""
    L.append(f"| Loudness reference / instrument gain | {lo['reference_lufs']} LUFS / {lo['gain_db']} dB{lim} |")
    L.append(f"| Peak after gain (single note) | {lo['peak_dbfs_after_gain']} dBFS |")
    zg = q["zone_gain_db"]
    L.append(f"| Upstream zone gain | {zg['min']} to {zg['max']} dB ({zg['zones_over_24db']} zones over +24 dB) |")
    tn = q["tail_noise"]
    L.append(f"| Tail level after gains, worst (hiss or early cut-off) | {tn['worst_dbfs_after_gains']} dBFS ({tn['layers_above_minus60_dbfs']} layers above -60 dBFS) |")
    fmt = f"{', '.join(f'{k} Hz' for k in q['sample_rates'])}; {', '.join(f'{k}-bit' for k in q['bit_depths'])}; channels {q['channels']}"
    L.append(f"| Format | {fmt} |")
    L.append(f"| Size (FLAC) | {_mb(q['bytes'])} (source {_mb(q['source_bytes'])}) |")
    L.append("")
    if p and p["flagged"]:
        L += ["## Pitch over 10 cents", "",
              "Error = detected pitch vs the zone root after the upstream `tune` is applied. Reported, not corrected (D-194).", "",
              "| Sample | Cents | Confidence |", "| --- | --- | --- |"]
        for r in p["flagged"][:60]:
            L.append(f"| {r['sample']} | {r['cents']:+.1f} | {r['confidence']} |")
        if len(p["flagged"]) > 60:
            L.append(f"| ... {len(p['flagged']) - 60} more in the JSON | | |")
        L.append("")
    if p and p["octave_suspects"]:
        L += ["## Octave suspects", ""]
        for o in p["octave_suspects"]:
            L.append(f"- {o['sample']}: sounds {'an octave above' if o['octave'] > 0 else 'an octave below'} its root")
        L.append("")
    if q["clipping"]["list"]:
        L += ["## Samples with clipping runs (3+ frames within 1 LSB of full scale)", ""]
        L.append(", ".join(q["clipping"]["list"][:80]) + (" ..." if len(q["clipping"]["list"]) > 80 else ""))
        L.append("")
    if q["skipped_regions"] or q["ignored_opcodes"] or q["notes"]:
        L += ["## Conversion notes", ""]
        for k, n in q["skipped_regions"].items():
            L.append(f"- Skipped {n} region(s): {k}")
        if q["ignored_opcodes"]:
            ig = sorted(q["ignored_opcodes"].items(), key=lambda x: -x[1])
            L.append("- SFZ opcodes not carried into KIF v1 (count): " + ", ".join(f"`{k}` {n}" for k, n in ig))
        for n in q["notes"]:
            L.append(f"- {n}")
        L.append("")
    return "\n".join(L)


def cmd_report(args, libs_cfg, insts):
    import build as cli

    repo = cli.REPO
    qa_dir = repo / "docs" / "content" / "qa"
    qa_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    total_bytes = total_src = 0
    by_lib: Dict[str, dict] = {}
    libs = _libs(libs_cfg)
    for pack in insts["packs"]:
        pdir = args.out / pack["id"]
        pq = json.loads((pdir / "qa" / "_pack.json").read_text())
        total_bytes += pq["pack_bytes"]
        total_src += pq["source_bytes"]
        for q in pq["instruments"]:
            kif_doc = json.loads((pdir / f"{q['instrument'].split('.', 1)[1]}.kif.json").read_text())
            q.update(coverage_stats(kif_doc))  # KIF-derived fields, recomputed from the file on disk
            (qa_dir / f"{q['instrument'].split('.', 1)[1]}.md").write_text(render_qa_md(q) + "\n")
            rows.append((pack["id"], q))
        prov = json.loads((pdir / "provenance.json").read_text())
        lib = libs[prov["library"]]
        e = by_lib.setdefault(lib["id"], {
            "library": lib["id"], "title": lib["title"], "publisher": lib["publisher"], "repo": lib["repo"],
            "source_url": lib["source_url"], "licence_spdx": lib["licence_spdx"],
            "audio_commit": prov["audio_commit"], "audio_ref": lib.get("audio_ref"),
            "mapping_commit": prov["mapping_commit"], "mapping_ref": lib.get("mapping_ref"),
            "licence_file": prov["licence_file"], "licence_sha256": prov["licence_sha256"],
            "excluded_patterns": libs_cfg["global_exclude"] + lib["exclude"], "files": {},
        })
        for f in prov["files"]:
            e["files"][f["path"]] = {"commit": f["commit"], "git_blob": f["git_blob"], "sha256": f["sha256"], "bytes": f["bytes"],
                                     "used_in_pack": pack["id"]}
        lic_dst = repo / "content" / "provenance" / lib["id"]
        lic_dst.mkdir(parents=True, exist_ok=True)
        (lic_dst / "LICENSE").write_text((pdir / "LICENSE.upstream.txt").read_text())
    for lid, e in by_lib.items():
        files = e.pop("files")
        e["file_count"] = len(files)
        e["source_bytes"] = sum(f["bytes"] for f in files.values())
        # One file per line so reviews diff cleanly.
        lines = ["{", f'  "meta": {json.dumps(e)},', '  "files": [']
        items = sorted(files.items())
        for i, (path, f) in enumerate(items):
            lines.append("    " + json.dumps(dict(path=path, **f)) + ("," if i + 1 < len(items) else ""))
        lines += ["  ]", "}"]
        (repo / "content" / "provenance" / lid / "provenance.json").write_text("\n".join(lines) + "\n")
    S = ["# QA summary: CC0 instrument set v1", "",
         "Generated by `tools/content/build.py report` from the build's QA JSON. Measurements only; nothing here is a listening judgement.", "",
         "| Instrument | Pack | Zones (att/rel) | Vel. layers (max) | RR | Pitch >10c / measured | Octave suspects | Clipped samples | FLAC size |",
         "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    summary = {"instruments": [], "packs": []}
    for pid, q in rows:
        p = q.get("pitch")
        vl = max(v["max"] for v in q["velocity_layers"].values())
        rr = ", ".join(sorted(q["round_robin"])) or "-"
        pitch = f"{p['flagged_over_10c']} / {p['measured']}" if p else "n/a"
        octs = str(len(p["octave_suspects"])) if p else "n/a"
        slug = q["instrument"].split(".", 1)[1]
        S.append(f"| [{q['name']}](qa/{slug}.md) | {pid} | {q['zones']} ({q['attack_zones']}/{q['release_zones']}) | {vl} | {rr} | {pitch} | {octs} | {q['clipping']['samples_with_clip_runs']} | {_mb(q['bytes'])} |")
        rec = {k: q[k] for k in ("instrument", "name", "pack", "zones", "attack_zones", "release_zones", "samples", "bytes")}
        rec.update({"velocity_layers_max": vl, "pitch_flagged": p["flagged_over_10c"] if p else None,
                    "pitch_measured": p["measured"] if p else None, "octave_suspects": len(p["octave_suspects"]) if p else None,
                    "clipped": q["clipping"]["samples_with_clip_runs"]})
        summary["instruments"].append(rec)
    S += ["", "Instrument sizes count every sample the instrument uses; instruments in one pack share samples, so the pack table is the download size.", "",
          "| Pack | Samples | FLAC (download size before gzip) | Source |", "| --- | --- | --- | --- |"]
    for pack in insts["packs"]:
        pq = json.loads((args.out / pack["id"] / "qa" / "_pack.json").read_text())
        S.append(f"| {pack['id']} | {pq['sample_count']} | {_mb(pq['pack_bytes'])} | {_mb(pq['source_bytes'])} |")
        summary["packs"].append({"id": pack["id"], "samples": pq["sample_count"], "bytes": pq["pack_bytes"], "source_bytes": pq["source_bytes"]})
    S.append(f"| **Total** | {sum(p['samples'] for p in summary['packs'])} | **{_mb(total_bytes)}** | {_mb(total_src)} |")
    summary["total_bytes"], summary["total_source_bytes"] = total_bytes, total_src
    (repo / "docs" / "content" / "QA_SUMMARY.md").write_text("\n".join(S) + "\n")
    (repo / "content" / "instruments").mkdir(parents=True, exist_ok=True)
    (repo / "content" / "instruments" / "qa-summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    print(f"QA for {len(rows)} instruments; total {_mb(total_bytes)} FLAC from {_mb(total_src)} source")
