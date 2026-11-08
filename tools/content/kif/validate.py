"""KIF v1 validator: JSON Schema + semantic rules + (optionally) the files.

    python -I tools/content/kif/validate.py <file.kif.json | pack dir> [--files]

Exit status 0 = valid. Used by the build (every instrument must pass) and
by tests/test_build_output.py.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import List

SCHEMA_PATH = Path(__file__).resolve().parent / "schema" / "kif-v1.schema.json"
_validator = None


def _schema_validator():
    global _validator
    if _validator is None:
        import jsonschema

        schema = json.loads(SCHEMA_PATH.read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        _validator = jsonschema.Draft202012Validator(schema)
    return _validator


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_doc(doc: dict, base: Path = None, check_files: bool = False) -> List[str]:
    errs = [f"schema: {'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in _schema_validator().iter_errors(doc)]
    if errs:
        return errs  # semantic checks assume the shape is right
    samples = {s["id"]: s for s in doc["samples"]}
    if len(samples) != len(doc["samples"]):
        errs.append("duplicate sample ids")
    files = [s["file"] for s in doc["samples"]]
    if len(set(files)) != len(files):
        errs.append("duplicate sample files")
    mics = {m["id"] for m in doc["mics"]}
    if len(mics) != len(doc["mics"]):
        errs.append("duplicate mic ids")
    arts = {a["id"]: a for a in doc["articulations"]}
    defaults = [a for a in doc["articulations"] if a.get("default")]
    if len(defaults) != 1:
        errs.append(f"exactly one default articulation required, found {len(defaults)}")
    if len(arts) > 1 and any(a.get("keyswitch") is None for a in arts.values()):
        errs.append("every articulation needs a keyswitch when there are several")
    ks = [a["keyswitch"] for a in arts.values() if a.get("keyswitch") is not None]
    if len(set(ks)) != len(ks):
        errs.append(f"duplicate keyswitches {ks}")
    pdef = [p for p in doc["mic_presets"] if p.get("default")]
    if len(pdef) != 1:
        errs.append(f"exactly one default mic preset required, found {len(pdef)}")
    for p in doc["mic_presets"]:
        if set(p["gains_db"]) != mics:
            errs.append(f"mic preset {p['id']} must give a gain (or null) for exactly the mics {sorted(mics)}")
        if all(v is None for v in p["gains_db"].values()):
            errs.append(f"mic preset {p['id']} has every mic off")
    used = set()
    rr_len = defaultdict(set)
    rr_pos = defaultdict(set)
    playable = set()
    for i, z in enumerate(doc["zones"]):
        where = f"zones[{i}]"
        if z["key_lo"] > z["key_hi"]:
            errs.append(f"{where}: key_lo > key_hi")
        if z["vel_lo"] > z["vel_hi"]:
            errs.append(f"{where}: vel_lo > vel_hi")
        for c in z.get("cc", []):
            if c["lo"] > c["hi"]:
                errs.append(f"{where}: cc{c['cc']} lo > hi")
        art = z.get("articulation")
        if art is None and len(arts) > 1:
            errs.append(f"{where}: articulation required (instrument has {len(arts)})")
        if art is not None and art not in arts:
            errs.append(f"{where}: unknown articulation {art}")
        if doc["pitched"] and z.get("root") is None and z.get("trigger", "attack") == "attack":
            errs.append(f"{where}: pitched instrument zone without root")
        if not doc["pitched"] and z.get("root") is not None:
            errs.append(f"{where}: unpitched instrument zone with a root")
        if z.get("trigger", "attack") == "attack":
            playable.update(range(z["key_lo"], z["key_hi"] + 1))
        rr = z.get("rr")
        if rr and rr["mode"] == "sequence":
            if "group" not in rr or "position" not in rr or "length" not in rr:
                errs.append(f"{where}: sequence rr needs group, position, length")
            else:
                if rr["position"] > rr["length"]:
                    errs.append(f"{where}: rr position {rr['position']} > length {rr['length']}")
                rr_len[rr["group"]].add(rr["length"])
                rr_pos[rr["group"]].add(rr["position"])
        if rr and rr["mode"] == "random" and not (0 <= rr.get("lo", 0) < rr.get("hi", 1) <= 1):
            errs.append(f"{where}: random rr range invalid")
        seen_mics = set()
        frames = None
        for layer in z["layers"]:
            s = samples.get(layer["sample"])
            if s is None:
                errs.append(f"{where}: unknown sample {layer['sample']}")
                continue
            used.add(layer["sample"])
            if layer["mic"] not in mics:
                errs.append(f"{where}: unknown mic {layer['mic']}")
            if layer["mic"] in seen_mics:
                errs.append(f"{where}: two layers for mic {layer['mic']}")
            seen_mics.add(layer["mic"])
            frames = s["frames"] if frames is None else min(frames, s["frames"])
        if frames is not None:
            if z.get("offset", 0) >= frames:
                errs.append(f"{where}: offset {z['offset']} beyond sample end {frames}")
            lp = z.get("loop")
            if lp and not (0 <= lp["start"] < lp["end"] < frames):
                errs.append(f"{where}: loop {lp['start']}-{lp['end']} outside 0..{frames - 1}")
        if "choked_by" in z and not any(o.get("choke_group") == z["choked_by"] for o in doc["zones"]) and not any(
            t["group"] == z["choked_by"] for t in doc.get("choke_triggers", [])
        ):
            errs.append(f"{where}: choked_by {z['choked_by']} but no zone is in that choke group")
    for g, lens in rr_len.items():
        if len(lens) != 1:
            errs.append(f"rr group {g}: inconsistent lengths {sorted(lens)}")
        elif rr_pos[g] != set(range(1, next(iter(lens)) + 1)):
            errs.append(f"rr group {g}: positions {sorted(rr_pos[g])} do not cover 1..{next(iter(lens))}")
    for k in ks:
        if k in playable:
            errs.append(f"keyswitch {k} collides with a playable key")
    unused = set(samples) - used
    if unused:
        errs.append(f"{len(unused)} sample(s) not referenced by any zone, e.g. {sorted(unused)[:3]}")
    if check_files:
        if base is None:
            errs.append("check_files needs the KIF's folder")
        else:
            import soundfile as sf

            for s in doc["samples"]:
                p = base / s["file"]
                if not p.is_file():
                    errs.append(f"missing file {s['file']}")
                    continue
                if p.stat().st_size != s["bytes"]:
                    errs.append(f"{s['file']}: {p.stat().st_size} bytes, KIF says {s['bytes']}")
                    continue
                if _sha256(p) != s["sha256"]:
                    errs.append(f"{s['file']}: sha256 mismatch")
                    continue
                info = sf.info(str(p))
                bits = {"PCM_16": 16, "PCM_24": 24}.get(info.subtype)
                if info.format != "FLAC" or (info.frames, info.channels, info.samplerate, bits) != (
                    s["frames"], s["channels"], s["sample_rate"], s["bit_depth"]
                ):
                    errs.append(f"{s['file']}: header {info.format} {info.frames}f {info.channels}ch {info.samplerate}Hz {info.subtype} disagrees with KIF")
    return errs


def validate_file(path: Path, check_files: bool = False) -> List[str]:
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return [f"{path}: {e}"]
    return [f"{path.name}: {e}" for e in validate_doc(doc, path.parent, check_files)]


def validate_dir(d: Path, check_files: bool = False) -> List[str]:
    kifs = sorted(d.glob("*.kif.json"))
    if not kifs:
        return [f"{d}: no .kif.json files"]
    errs: List[str] = []
    for k in kifs:
        errs += validate_file(k, check_files)
    return errs


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument("--files", action="store_true", help="also check sample files, sizes, SHA-256 and FLAC headers")
    a = ap.parse_args(argv)
    errs: List[str] = []
    for p in a.paths:
        errs += validate_dir(p, a.files) if p.is_dir() else validate_file(p, a.files)
    for e in errs:
        print(e)
    print("valid" if not errs else f"{len(errs)} error(s)")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
