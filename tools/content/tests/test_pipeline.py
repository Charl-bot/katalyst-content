"""End-to-end test of plan -> build -> validate -> pack on a synthetic upstream.

A throw-away git repository stands in for an upstream library: CC0 LICENSE,
an SFZ with two keyswitched articulations, velocity layers, a round robin, a
release sample, a WAV with a 'smpl' loop, one sample deliberately 20 cents
sharp and leading silence on everything. No network is used.
"""

import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from kif import pipeline, plan, validate  # noqa: E402
from kif.upstream import ProvenanceError, Upstream  # noqa: E402

FS = 48000
LEAD = int(0.08 * FS)


def midi_hz(n):
    return 440.0 * 2 ** ((n - 69) / 12)


def write_tone(path, note, cents=0.0, amp=0.5, seconds=0.8, loop=None):
    t = np.arange(int(seconds * FS)) / FS
    hz = midi_hz(note) * 2 ** (cents / 1200)
    x = sum(a * np.sin(2 * math.pi * hz * k * t) for k, a in ((1, 1.0), (2, 0.4), (3, 0.2)))
    x = amp * x / np.max(np.abs(x)) * np.exp(-t / 0.6)
    x = np.concatenate([np.zeros(LEAD), x])
    sf.write(path, np.stack([x, x], 1), FS, subtype="PCM_24")
    if loop:
        # append a RIFF 'smpl' chunk with one forward loop
        start, end = loop
        data = struct.pack("<9I", 0, 0, 0, 60, 0, 0, 0, 1, 0) + struct.pack("<6I", 0, 0, start, end, 0, 0)
        raw = bytearray(Path(path).read_bytes())
        raw += b"smpl" + struct.pack("<I", len(data)) + data
        raw[4:8] = struct.pack("<I", len(raw) - 8)
        Path(path).write_bytes(bytes(raw))


SFZ = """
<control> default_path=Samples\\
<group> sw_lokey=c1 sw_hikey=c#1 sw_last=c1 sw_label=C1 Sustain ampeg_release=0.5
<region> sample=tone_C4_v1.wav lokey=58 hikey=61 pitch_keycenter=60 lovel=1 hivel=63
<region> sample=tone_C4_v2_rr1.wav lokey=58 hikey=61 pitch_keycenter=60 lovel=64 hivel=127 seq_length=2 seq_position=1 volume=-2
<region> sample=tone_C4_v2_rr2.wav lokey=58 hikey=61 pitch_keycenter=60 lovel=64 hivel=127 seq_length=2 seq_position=2 volume=-2
<region> sample=tone_E4_sharp.wav lokey=62 hikey=66 pitch_keycenter=64
<region> sample=tone_A4_loop.wav lokey=67 hikey=72 pitch_keycenter=69 loop_mode=loop_continuous off_by=7
<region> sample=rel_C4.wav lokey=58 hikey=72 pitch_keycenter=60 trigger=release rt_decay=6
<group> sw_last=c#1 sw_label=C#1 Staccato ampeg_release=0.1
<region> sample=tone_C4_v1.wav lokey=58 hikey=72 pitch_keycenter=60
<group> sample=*silence group=7 locc4=100 hicc4=127
<region> key=70
"""


def git(d, *a):
    subprocess.run(["git", "-C", str(d), *a], check=True, capture_output=True)


class SyntheticUpstream(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        repo = cls.tmp / "upstream"
        (repo / "Samples").mkdir(parents=True)
        shutil.copy(HERE / "fixtures" / "CC0-1.0.txt", repo / "LICENSE")
        write_tone(repo / "Samples/tone_C4_v1.wav", 60, amp=0.15)
        write_tone(repo / "Samples/tone_C4_v2_rr1.wav", 60, amp=0.6)
        write_tone(repo / "Samples/tone_C4_v2_rr2.wav", 60, amp=0.55)
        write_tone(repo / "Samples/tone_E4_sharp.wav", 64, cents=20.0)
        write_tone(repo / "Samples/tone_A4_loop.wav", 69, loop=(LEAD + 9600, LEAD + 19199))
        write_tone(repo / "Samples/rel_C4.wav", 60, amp=0.05, seconds=0.3)
        (repo / "Test.sfz").write_text(SFZ)
        (repo / "Bad.sfz").write_text("<region> sample=Samples/TX81Z/piano.wav\n")
        git(repo, "init", "-q")
        git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
        git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "fixture")
        commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        cls.lib = {
            "id": "synthetic", "title": "Synthetic", "publisher": "Test", "repo": "test/synthetic", "fetch": "git",
            "audio_commit": commit, "mapping_commit": commit, "licence_spdx": "CC0-1.0", "licence_file": "LICENSE",
            "exclude": [], "allow": [],
        }
        cls.global_exclude = json.loads((HERE.parent / "libraries.json").read_text())["global_exclude"]
        cls.up = Upstream(cls.lib, repo, cls.tmp / "raw")
        cls.pack = {
            "id": "synth", "library": "synthetic",
            "instruments": [{
                "id": "synth-tone", "name": "Synth Tone", "category": "other", "parts": [{"sfz": "Test.sfz"}],
                "articulations": [
                    {"id": "sustain", "name": "Sustain", "match": "^sustain$", "default": True},
                    {"id": "staccato", "name": "Staccato", "match": "^staccato$"},
                ],
            }],
        }
        cls.pp = plan.plan_pack(cls.pack, cls.lib, cls.global_exclude, cls.up)
        cls.out = cls.tmp / "out"
        cls.summary = pipeline.build_pack(cls.pp, cls.out, log=lambda *_: None, workers=2)
        cls.pdir = cls.out / "synth"
        cls.kif = json.loads((cls.pdir / "synth-tone.kif.json").read_text())
        cls.qa = json.loads((cls.pdir / "qa" / "synth-tone.json").read_text())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_validator_passes_with_files(self):
        self.assertEqual(validate.validate_dir(self.pdir, check_files=True), [])

    def test_articulations_and_keyswitches(self):
        arts = {a["id"]: a for a in self.kif["articulations"]}
        self.assertEqual(arts["sustain"]["keyswitch"], 24)
        self.assertEqual(arts["staccato"]["keyswitch"], 25)
        self.assertTrue(arts["sustain"]["default"])

    def test_round_robin_and_release(self):
        rr = [z["rr"] for z in self.kif["zones"] if "rr" in z]
        self.assertEqual(sorted(r["position"] for r in rr), [1, 2])
        self.assertEqual(len({r["group"] for r in rr}), 1)
        rel = [z for z in self.kif["zones"] if z["trigger"] == "release"]
        self.assertEqual(len(rel), 1)
        self.assertEqual(rel[0]["release_decay_db_per_s"], 6.0)
        self.assertEqual(self.qa["release_zones"], 1)

    def test_trim_keeps_the_attack(self):
        for s in self.kif["samples"]:
            self.assertGreater(s["trimmed_frames"], 0)
            self.assertLessEqual(s["trimmed_frames"], LEAD)
            self.assertGreaterEqual(LEAD - s["trimmed_frames"], int(0.003 * FS) - 1)

    def test_pcm_is_bit_exact_after_the_trim_point(self):
        s = next(x for x in self.kif["samples"] if x["id"] == "tone_C4_v2_rr1")
        src = sf.read(self.tmp / "upstream" / "Samples" / "tone_C4_v2_rr1.wav", dtype="int32")[0]
        out = sf.read(self.pdir / s["file"], dtype="int32")[0]
        cut = s["trimmed_frames"]
        # D-422: a 24-bit source ships at 16 bits with TPDF dither, so after the trim point each frame is the
        # source within 1 LSB at 16 bits (dither spans +-1 LSB, rounding adds half), and the low 16 bits are zero.
        self.assertEqual(s["bit_depth"], 16)
        self.assertFalse(np.any(out.astype(np.int64) & 0xFFFF))
        err = np.abs(out[64:].astype(np.int64) - src[cut + 64 :].astype(np.int64))
        self.assertLessEqual(int(err.max()), int(1.5 * 65536))
        self.assertIn("16-bit with TPDF dither", " ".join(self.kif["provenance"]["modifications"]))

    def test_pitch_flags_the_sharp_sample_only(self):
        p = self.qa["pitch"]
        self.assertEqual(p["flagged_over_10c"], 1)
        self.assertEqual(p["flagged"][0]["sample"], "tone_E4_sharp")
        self.assertAlmostEqual(p["flagged"][0]["cents"], 20.0, delta=1.0)
        self.assertEqual(p["octave_suspects"], [])

    def test_loop_from_smpl_chunk_is_shifted_by_the_trim(self):
        z = next(z for z in self.kif["zones"] if z["layers"][0]["sample"] == "tone_A4_loop")
        s = next(x for x in self.kif["samples"] if x["id"] == "tone_A4_loop")
        self.assertEqual(z["loop"]["start"], LEAD + 9600 - s["trimmed_frames"])
        self.assertEqual(z["loop"]["end"], LEAD + 19199 - s["trimmed_frames"])
        self.assertEqual(self.qa["loops"], 1)
        self.assertFalse(any("loop" in z for z in self.kif["zones"] if z is not None and z["layers"][0]["sample"] != "tone_A4_loop"))

    def test_loudness_gain_keeps_layer_relationship(self):
        # one instrument gain; zone gains (upstream volume) are carried as-is
        z2 = [z for z in self.kif["zones"] if z.get("rr")]
        self.assertTrue(all(z["gain_db"] == -2.0 for z in z2))
        ref = self.kif["loudness"]["reference_lufs"]
        self.assertAlmostEqual(ref + self.kif["gain_db"], pipeline.TARGET_LUFS, delta=0.01)

    def test_provenance_records_every_source(self):
        prov = json.loads((self.pdir / "provenance.json").read_text())
        self.assertEqual(len(prov["files"]), 6)
        for f in prov["files"]:
            self.assertEqual(len(f["sha256"]), 64)
            self.assertEqual(f["commit"], self.lib["audio_commit"])

    def test_silence_regions_become_choke_triggers(self):
        self.assertEqual(self.kif["choke_triggers"], [{"key_lo": 70, "key_hi": 70, "group": 7, "cc": [{"cc": 4, "lo": 100, "hi": 127}]}])
        z = next(z for z in self.kif["zones"] if z["layers"][0]["sample"] == "tone_A4_loop")
        self.assertEqual(z["choked_by"], 7)

    def test_excluded_path_fails_loudly(self):
        bad = dict(self.pack, instruments=[dict(self.pack["instruments"][0], parts=[{"sfz": "Bad.sfz", "articulation": "sustain"}],
                                                articulations=[{"id": "sustain", "name": "S"}])])
        with self.assertRaisesRegex(ProvenanceError, "EXCLUDED CONTENT"):
            plan.plan_pack(bad, self.lib, self.global_exclude, self.up)

    def test_non_cc0_licence_fails(self):
        lib = dict(self.lib, licence_file="Test.sfz")
        with self.assertRaisesRegex(ProvenanceError, "not the CC0"):
            plan.plan_pack(self.pack, lib, self.global_exclude, Upstream(lib, self.up.clone, self.tmp / "raw2"))

    def test_cc_by_needs_its_text_attribution_and_uri(self):
        """D-391: a CC BY library passes only with the CC BY text at the pin, an attribution line and the licence URI;
        its KIF carries the attribution, the licence URI and the statement of changes."""
        by_text = (self.tmp / "upstream" / "BY-LICENSE.txt")
        by_text.write_text("Creative Commons Legal Code\n\nAttribution 3.0 Unported\n\n"
                           "Subject to the terms and conditions of this License, Licensor hereby grants You a worldwide, "
                           "royalty-free, non-exclusive, perpetual (for the duration of the applicable copyright) license.\n")
        git(self.tmp / "upstream", "add", ".")
        git(self.tmp / "upstream", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "by")
        commit = subprocess.run(["git", "-C", str(self.tmp / "upstream"), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        # the extra licence-like file is a second licence file for the CC0 case; point the BY library at it
        base = dict(self.lib, audio_commit=commit, mapping_commit=commit, licence_spdx="CC-BY-3.0", licence_file="BY-LICENSE.txt")
        up = lambda lib, n: Upstream(lib, self.up.clone, self.tmp / n)
        with self.assertRaisesRegex(ProvenanceError, "unexpected licence files|attribution"):
            plan.plan_pack(self.pack, base, self.global_exclude, up(base, "raw3"))
        with self.assertRaisesRegex(ProvenanceError, "not allowed"):
            lib = dict(base, licence_spdx="CC-BY-NC-4.0")
            plan.plan_pack(self.pack, lib, self.global_exclude, up(lib, "raw4"))
        with self.assertRaisesRegex(ProvenanceError, "licence_uri"):
            lib = dict(base, attribution="Work by A.", licence_uri="https://example.com/")
            plan.verify_licence(up(lib, "raw5"))
        lib = dict(base, attribution="Work by A. Author.", licence_uri="https://creativecommons.org/licenses/by/3.0/")
        with self.assertRaisesRegex(ProvenanceError, "unexpected licence files"):
            plan.verify_licence(up(lib, "raw6"))  # LICENSE (the CC0 text) is still in the tree: review it
        from kif import pipeline

        class _P:  # only the licence block is under test
            pass
        pp = _P()
        pp.lib = lib
        block = pipeline._kif_licence(pp)
        self.assertEqual(block["spdx"], "CC-BY-3.0")
        for needle in ("Work by A. Author.", "https://creativecommons.org/licenses/by/3.0/", "Changes by Katalyst Suite"):
            self.assertIn(needle, block["note"])
        self.assertEqual(pipeline._manifest_licence(lib)["uri"], "https://creativecommons.org/licenses/by/3.0/")

    def test_validator_catches_tampering(self):
        doc = json.loads(json.dumps(self.kif))
        doc["samples"][0]["sha256"] = "0" * 64
        self.assertTrue(any("sha256 mismatch" in e for e in validate.validate_doc(doc, self.pdir, check_files=True)))
        doc = json.loads(json.dumps(self.kif))
        doc["articulations"][1]["keyswitch"] = 60
        self.assertTrue(any("collides" in e for e in validate.validate_doc(doc)))
        doc = json.loads(json.dumps(self.kif))
        doc["zones"][0]["layers"][0]["sample"] = "nope"
        self.assertTrue(any("unknown sample" in e for e in validate.validate_doc(doc)))

    def test_pack_is_deterministic_and_verified(self):
        d1, d2 = self.tmp / "dist1", self.tmp / "dist2"
        p1 = pipeline.write_pack_parts(self.pdir, d1, "synth", "1.0.0")
        p2 = pipeline.write_pack_parts(self.pdir, d2, "synth", "1.0.0")
        self.assertEqual([p["sha256"] for p in p1], [p["sha256"] for p in p2])
        pipeline.verify_pack_parts(p1, d1, self.pdir)
        with tarfile.open(d1 / p1[0]["file"]) as t:
            names = t.getnames()
        self.assertEqual(names[0], "synth/synth-tone.kif.json")

    def test_flac_encoding_is_deterministic(self):
        s = self.kif["samples"][0]
        data = sf.read(self.pdir / s["file"], dtype="int32", always_2d=True)[0]
        from kif import audio

        p = self.tmp / "again.flac"
        audio.write_flac(str(p), data, s["sample_rate"], s["bit_depth"])
        from kif.upstream import sha256_file

        self.assertEqual(sha256_file(p), s["sha256"])


if __name__ == "__main__":
    unittest.main()


class MultiMicMixdown(unittest.TestCase):
    """D-423: a "mixdown" pack ships one stereo file per hit, mixed at the default mic preset, with muted mics left
    out and a zone gain that restores each file's level, so the instrument sounds as the multi-mic default did."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        repo = cls.tmp / "upstream"
        for mic in ("close", "room", "lofi"):
            (repo / "Samples" / mic / "snare").mkdir(parents=True)
            for v, amp in ((1, 0.1), (2, 0.6)):
                write_tone(repo / f"Samples/{mic}/snare/{mic}_snare_v{v}.wav", 50, amp=amp * (0.5 if mic == "room" else 1.0))
            (repo / f"{mic}.sfz").write_text(
                f"<region> sample=Samples/{mic}/snare/{mic}_snare_v1.wav key=38 lovel=1 hivel=63\n"
                f"<region> sample=Samples/{mic}/snare/{mic}_snare_v2.wav key=38 lovel=64 hivel=127\n")
        shutil.copy(HERE / "fixtures" / "CC0-1.0.txt", repo / "LICENSE")
        git(repo, "init", "-q")
        git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
        git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "fixture")
        commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        lib = {"id": "kit", "title": "Kit", "publisher": "Test", "repo": "test/kit", "fetch": "git", "audio_commit": commit,
               "mapping_commit": commit, "licence_spdx": "CC0-1.0", "licence_file": "LICENSE", "exclude": [], "allow": []}
        gex = json.loads((HERE.parent / "libraries.json").read_text())["global_exclude"]
        inst = {"id": "kit", "name": "Kit", "category": "drums", "pitched": False, "multi_mic": True, "merge_reference_mic": "close",
                "parts": [{"sfz": f"{m}.sfz", "mic": m} for m in ("close", "room", "lofi")],
                "mics": [{"id": m, "name": m} for m in ("close", "room", "lofi")], "articulations": [{"id": "normal", "name": "Normal"}],
                "mic_presets": [{"id": "full", "name": "Full", "default": True, "gains_db": {"close": 0, "room": -6, "lofi": None}}]}
        cls.multi = cls._build(cls, lib, gex, {"id": "kit", "library": "kit", "instruments": [inst]}, "multi")
        cls.mixed = cls._build(cls, lib, gex, {"id": "kit", "library": "kit", "mixdown": True, "instruments": [inst]}, "mixed")

    def _build(self, lib, gex, pack, name):
        up = Upstream(lib, self.tmp / "upstream", self.tmp / f"raw-{name}")
        pp = plan.plan_pack(pack, lib, gex, up)
        pipeline.build_pack(pp, self.tmp / name, log=lambda *_: None, workers=1)
        d = self.tmp / name / "kit"
        return d, json.loads((d / "kit.kif.json").read_text())

    def test_one_stereo_file_per_hit_and_one_mic(self):
        d, kif = self.mixed
        self.assertEqual([m["id"] for m in kif["mics"]], ["main"])
        self.assertEqual(len(kif["samples"]), 2)
        self.assertTrue(all(len(z["layers"]) == 1 and z["layers"][0]["mic"] == "main" for z in kif["zones"]))
        self.assertEqual(validate.validate_dir(d, check_files=True), [])
        self.assertLess(sum(s["bytes"] for s in kif["samples"]), sum(s["bytes"] for s in self.multi[1]["samples"]) / 2)

    def test_mix_matches_the_default_preset(self):
        """Played back (file x zone gain x instrument gain) the mix equals close + room at -6 dB, lofi muted."""
        (dm, km), (dx, kx) = self.multi, self.mixed
        self.assertAlmostEqual(km["gain_db"], kx["gain_db"], delta=0.01)
        for vel in (1, 64):
            zx = next(z for z in kx["zones"] if z["vel_lo"] == vel)
            out = sf.read(dx / f"samples/{zx['layers'][0]['sample']}.flac")[0] * 10 ** (zx.get("gain_db", 0) / 20)
            zm = next(z for z in km["zones"] if z["vel_lo"] == vel)
            ref = 0
            for layer in zm["layers"]:
                g = {"close": 0, "room": -6}.get(layer["mic"])
                if g is None:
                    continue
                x = sf.read(dm / f"samples/{layer['sample']}.flac")[0]
                ref = ref + x * 10 ** ((g + zm.get("gain_db", 0)) / 20)
            n = min(len(out), len(ref))
            err = np.max(np.abs(out[:n] - ref[:n]))
            self.assertLess(err, 2 * 2 ** -15 * 10 ** (zx.get("gain_db", 0) / 20) + 1e-4, f"vel {vel}")
            self.assertGreater(np.max(np.abs(out)), 0.05)


class FreezeCc(unittest.TestCase):
    """D-424: CC-driven mic faders, pans and CC conditions are baked at the program's set_cc positions."""

    def test_amplitude_multiplies_and_volume_adds(self):
        adj = plan.freeze_ccs({"amplitude_cc100": "70", "volume_oncc7": "-6"}, {100: 40.0, 7: 127.0}, set())
        self.assertAlmostEqual(adj["gain_db"], 20 * math.log10(0.7 * 40 / 127) - 6.0, places=6)

    def test_condition_outside_the_frozen_value_drops_the_region(self):
        self.assertIsNone(plan.freeze_ccs({"locc100": "1"}, {100: 0.0}, set()))
        self.assertIsNotNone(plan.freeze_ccs({"locc100": "1"}, {100: 40.0}, set()))
        self.assertIsNone(plan.freeze_ccs({"hicc100": "126"}, {100: 127.0}, set()))

    def test_exposed_controls_are_not_frozen(self):
        adj = plan.freeze_ccs({"locc64": "64", "amplitude_cc64": "50"}, {}, {64})
        self.assertEqual(adj["gain_db"], 0.0)

    def test_zero_amplitude_is_silent(self):
        self.assertIsNone(plan.freeze_ccs({"amplitude_oncc100": "100"}, {100: 0.0}, set()))
