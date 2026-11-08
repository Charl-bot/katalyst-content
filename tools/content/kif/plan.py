"""Stage 1: upstream SFZ -> KIF zone plan, with every provenance gate.

No audio is read here. The plan says which upstream files each pack needs,
how every region became a zone, and what was dropped or ignored and why.
"""

from __future__ import annotations

import posixpath
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import sfz as sfzmod
from .upstream import ProvenanceError, TreeEntry, Upstream

CC0_MARKERS = (
    "CC0 1.0 Universal",
    "Statement of Purpose",
    "Affirmer hereby overtly, fully, permanently, irrevocably and unconditionally waives",
)

# Opcodes the converter maps into KIF. Everything else is counted and reported.
HANDLED = {
    "sample", "lokey", "hikey", "key", "pitch_keycenter", "lovel", "hivel", "volume", "tune", "transpose", "pan",
    "offset", "seq_length", "seq_position", "lorand", "hirand", "trigger", "group", "off_by", "off_mode", "off_time",
    "sw_lokey", "sw_hikey", "sw_last", "sw_default", "sw_label", "ampeg_attack", "ampeg_hold", "ampeg_decay",
    "ampeg_sustain", "ampeg_release", "amp_veltrack", "xfin_lovel", "xfin_hivel", "xfout_lovel", "xfout_hivel",
    "loop_mode", "loop_start", "loop_end", "rt_decay", "pitch_keytrack", "default_path", "group_label",
}
CC_COND_RE = re.compile(r"^(lo|hi)cc(\d+)$")
# Virtuosity gates regions on its mic-mix CCs: 101-111 are mic volume, bleed
# and width faders (bleed layers need CC102/104 >= 43 or 85; upstream default
# is 127, so every bleed layer plays) and 113 is "epic". KIF replaces mic
# faders with mic presets, so these conditions are dropped (D-196).
MIC_FADER_CCS = set(range(101, 114))
NOTE_PREFIX_RE = re.compile(r"^[a-g][#b]?-?\d+\s+", re.I)


@dataclass
class ZonePlan:
    instrument: str
    key_lo: int
    key_hi: int
    root: Optional[int]
    vel_lo: int
    vel_hi: int
    articulation: str
    trigger: str
    layers: List[Tuple[str, str]]  # (source path at audio commit, mic id)
    tune_cents: float = 0.0
    gain_db: float = 0.0
    pan: float = 0.0
    amp_veltrack: Optional[float] = None
    offset: int = 0
    rr: Optional[dict] = None
    cc: List[dict] = field(default_factory=list)
    vel_xfade: Optional[dict] = None
    loop_mode: Optional[str] = None
    loop_start: Optional[int] = None
    loop_end: Optional[int] = None
    one_shot: bool = False
    release_decay: Optional[float] = None
    group: Optional[int] = None
    off_by: Optional[int] = None
    off_time: Optional[float] = None
    envelope: Optional[dict] = None
    origin: str = ""

    def signature(self, stem: str) -> tuple:
        return (
            self.articulation, self.key_lo, self.key_hi, self.vel_lo, self.vel_hi, self.trigger,
            tuple(sorted((self.rr or {}).items())), tuple((c["cc"], c["lo"], c["hi"]) for c in self.cc), stem,
        )


@dataclass
class InstrumentPlan:
    cfg: dict
    zones: List[ZonePlan] = field(default_factory=list)
    articulations: List[dict] = field(default_factory=list)
    ignored_opcodes: Counter = field(default_factory=Counter)
    skipped: Counter = field(default_factory=Counter)
    notes: List[str] = field(default_factory=list)
    sfz_files: List[str] = field(default_factory=list)
    key_labels: List[dict] = field(default_factory=list)
    # SFZ '*silence' regions that exist only to choke other voices (D-196).
    choke_triggers: List[dict] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.cfg["id"]

    @property
    def pitched(self) -> bool:
        return self.cfg.get("pitched", True)


@dataclass
class PackPlan:
    cfg: dict
    lib: dict
    upstream: Upstream
    instruments: List[InstrumentPlan]
    sources: Dict[str, TreeEntry]
    warnings: List[str]
    licence_text: str
    licence_sha256: str


class Excluder:
    def __init__(self, global_patterns: List[str], lib: dict):
        self.patterns = [re.compile(p) for p in global_patterns + lib.get("exclude", [])]
        self.allow = [(re.compile(a["pattern"]), a["reason"]) for a in lib.get("allow", [])]
        self.allowed_hits: Counter = Counter()

    def check(self, path: str) -> None:
        for p in self.patterns:
            if p.search(path):
                for a, reason in self.allow:
                    if a.search(path):
                        self.allowed_hits[reason] += 1
                        return
                raise ProvenanceError(f"EXCLUDED CONTENT: {path!r} matches {p.pattern!r} (D-193); refusing to build")


def verify_licence(up: Upstream) -> Tuple[str, str]:
    import hashlib

    lic_path = up.lib["licence_file"]
    text = up.text(lic_path, up.audio_commit)
    flat = " ".join(text.split())
    for marker in CC0_MARKERS:
        if marker not in flat:
            raise ProvenanceError(f"{up.repo}@{up.audio_commit[:12]} {lic_path} is not the CC0 1.0 text (missing {marker!r})")
    if up.lib["licence_spdx"] != "CC0-1.0":
        raise ProvenanceError(f"{up.id}: only CC0-1.0 libraries are allowed (D-193)")
    # Any other licence-like file in the tree must be reviewed: fail loudly.
    others = [
        p for p in up.tree(up.audio_commit)
        if re.search(r"(?i)(^|/)(licen[cs]e|copying|copyright|notice)[^/]*$", p) and p != lic_path
    ]
    if others:
        raise ProvenanceError(f"{up.id}: unexpected licence files at the pinned commit: {others}")
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def _note(v: Optional[str], default: int) -> int:
    return default if v is None else sfzmod.note_to_midi(v)


def _f(v: Optional[str], default: float = 0.0) -> float:
    return default if v is None else float(v)


def _resolve_articulation(region: sfzmod.Region, part: dict, inst: dict) -> Optional[str]:
    if part.get("articulation"):
        return part["articulation"]
    label = region.get("sw_label")
    if label is None:
        arts = inst["articulations"]
        if len(arts) == 1:
            return arts[0]["id"]
        raise ProvenanceError(f"{region.origin}: no keyswitch label and {inst['id']} has several articulations")
    norm = NOTE_PREFIX_RE.sub("", label.strip()).lower()
    for drop in part.get("drop", []):
        if re.search(drop, norm):
            return None
    hits = [a["id"] for a in inst["articulations"] if a.get("match") and re.search(a["match"], norm)]
    if len(hits) != 1:
        raise ProvenanceError(f"{region.origin}: keyswitch label {label!r} matches {hits or 'no'} articulation(s) of {inst['id']}")
    return hits[0]


def plan_pack(pack: dict, lib: dict, global_exclude: List[str], up: Upstream) -> PackPlan:
    excl = Excluder(global_exclude, lib)
    licence_text, licence_sha = verify_licence(up)
    warnings: List[str] = []
    sources: Dict[str, TreeEntry] = {}
    plans: List[InstrumentPlan] = []

    for inst in pack["instruments"]:
        ip = InstrumentPlan(cfg=inst)
        ip.notes.extend(inst.get("qa_notes", []))
        arts = {a["id"]: dict(a) for a in inst["articulations"]}
        derived_ks: Dict[str, set] = defaultdict(set)
        defines_prefix = ""
        if inst.get("defines"):
            defines_prefix = '#include "%s"\n' % posixpath.relpath(inst["defines"], "Programs")
        for part in inst["parts"]:
            root = part["sfz"]
            excl.check(root)
            if defines_prefix:
                # Virtuosity: a synthetic root in Programs/ that pulls in the key map
                # and one mic's mapping (so its perc / epic includes are never read).
                virt = "Programs/__katalyst_%s.sfz" % part["mic"]
                text_root = defines_prefix + '#include "%s"\n' % posixpath.relpath(root, "Programs")
            else:
                virt, text_root = root, None

            def read(path: str, _virt=virt, _text=text_root) -> str:
                if path == _virt and _text is not None:
                    return _text
                excl.check(path)
                return up.text(path, up.mapping_commit)

            parsed = sfzmod.parse(virt, read)
            ip.sfz_files.append(root)
            ip.sfz_files.extend(p for p in parsed.includes if p not in ip.sfz_files)
            labelled_keys = set()
            for region in parsed.regions:
                op = region.opcodes
                for k in op:
                    m = CC_COND_RE.match(k)
                    if k not in HANDLED and not (m and int(m.group(2)) not in MIC_FADER_CCS):
                        ip.ignored_opcodes[k] += 1
                if region.sample.startswith("*"):
                    if region.sample == "*silence" and "group" in op:
                        key = op.get("key")
                        cc = []
                        for k, v in op.items():
                            m = CC_COND_RE.match(k)
                            if m and m.group(1) == "lo" and int(m.group(2)) not in MIC_FADER_CCS:
                                n = int(m.group(2))
                                cc.append({"cc": n, "lo": int(float(v)), "hi": int(float(op.get(f"hicc{n}", "127")))})
                        cc.sort(key=lambda c: c["cc"])
                        trig = {"key_lo": _note(op.get("lokey", key), 0), "key_hi": _note(op.get("hikey", key), 127),
                                "group": int(op["group"])}
                        if cc:
                            trig["cc"] = cc
                        if trig not in ip.choke_triggers:
                            ip.choke_triggers.append(trig)
                        ip.skipped["silent choke-trigger region (*silence), kept as choke_triggers"] += 1
                    else:
                        ip.skipped[f"generator sample {region.sample}"] += 1
                    continue
                if any(k.startswith(("on_locc", "on_hicc")) for k in op):
                    ip.skipped["CC-triggered region (on_locc/on_hicc), e.g. pedal noise"] += 1
                    continue
                art = _resolve_articulation(region, part, inst)
                if art is None:
                    ip.skipped[f"articulation dropped by config: {op.get('sw_label')}"] += 1
                    continue
                if "sw_last" in op and not part.get("articulation"):
                    derived_ks[art].add(sfzmod.note_to_midi(op["sw_last"]))
                # -- keys
                key = op.get("key")
                lo = _note(op.get("lokey", key), 0)
                hi = _note(op.get("hikey", key), 127)
                center = _note(op.get("pitch_keycenter", key), 60)
                center -= int(_f(op.get("transpose")))
                shift = int(part.get("key_shift", 0))
                lo, hi, center = lo + shift, hi + shift, center + shift
                if "key_range" in part:
                    klo, khi = part["key_range"]
                    lo, hi = max(lo, klo), min(hi, khi)
                    if lo > hi:
                        ip.skipped[f"outside key split {klo}-{khi} ({posixpath.basename(root)})"] += 1
                        continue
                if not (0 <= lo <= hi <= 127):
                    raise ProvenanceError(f"{region.origin}: key range {lo}-{hi} after shift is invalid")
                if part.get("label"):
                    for k in range(lo, hi + 1):
                        if k not in labelled_keys:
                            labelled_keys.add(k)
                            ip.key_labels.append({"key": k, "label": f"{part['label']} {len([x for x in labelled_keys])}"})
                pitched = inst.get("pitched", True) and op.get("pitch_keytrack", "100") != "0"
                # -- trigger
                trig = op.get("trigger", "attack")
                if trig in ("release", "release_key"):
                    trigger = "release"
                elif trig in ("attack", "first", "legato"):
                    trigger = "attack"
                    if trig != "attack":
                        ip.ignored_opcodes[f"trigger={trig} (treated as attack)"] += 1
                else:
                    raise ProvenanceError(f"{region.origin}: unsupported trigger={trig}")
                # -- sample: resolve at the mapping commit, then require the same bytes at the audio commit
                excl.check(region.sample)
                entry_m, warn = up.resolve(region.sample, up.mapping_commit)
                if warn:
                    warnings.append(f"{region.origin}: {warn}")
                excl.check(entry_m.path)
                entry_a, warn_a = up.resolve(entry_m.path, up.audio_commit)
                if warn_a:
                    warnings.append(f"{region.origin}: {warn_a} (audio commit)")
                if entry_a.blob != entry_m.blob:
                    raise ProvenanceError(
                        f"{entry_m.path}: blob {entry_m.blob[:10]} at mapping commit differs from {entry_a.blob[:10]} at audio commit"
                    )
                sources[entry_a.path] = entry_a
                rr = None
                if "seq_length" in op and int(op["seq_length"]) > 1:
                    rr = {"mode": "sequence", "length": int(op["seq_length"]), "position": int(op.get("seq_position", "1"))}
                elif "lorand" in op or "hirand" in op:
                    rr = {"mode": "random", "lo": _f(op.get("lorand"), 0.0), "hi": _f(op.get("hirand"), 1.0)}
                cc = []
                for k, v in op.items():
                    m = CC_COND_RE.match(k)
                    if m and m.group(1) == "lo" and int(m.group(2)) not in MIC_FADER_CCS:
                        n = int(m.group(2))
                        cc.append({"cc": n, "lo": int(float(v)), "hi": int(float(op.get(f"hicc{n}", "127")))})
                    elif m and m.group(1) == "hi" and int(m.group(2)) not in MIC_FADER_CCS and f"locc{m.group(2)}" not in op:
                        cc.append({"cc": int(m.group(2)), "lo": 0, "hi": int(float(v))})
                cc.sort(key=lambda c: c["cc"])
                xf = None
                if any(k.startswith(("xfin_", "xfout_")) for k in op):
                    xf = {}
                    if "xfin_lovel" in op:
                        xf["in"] = [int(op["xfin_lovel"]), int(op.get("xfin_hivel", op["xfin_lovel"]))]
                    if "xfout_lovel" in op:
                        xf["out"] = [int(op["xfout_lovel"]), int(op.get("xfout_hivel", "127"))]
                env = {
                    "attack": _f(op.get("ampeg_attack"), 0.0),
                    "hold": _f(op.get("ampeg_hold"), 0.0),
                    "decay": _f(op.get("ampeg_decay"), 0.0),
                    "sustain": _f(op.get("ampeg_sustain"), 100.0) / 100.0,
                    "release": _f(op.get("ampeg_release"), 0.001),
                }
                lm = op.get("loop_mode")
                z = ZonePlan(
                    instrument=inst["id"], key_lo=lo, key_hi=hi, root=center if pitched else None,
                    vel_lo=max(1, int(op.get("lovel", "1"))), vel_hi=int(op.get("hivel", "127")),
                    articulation=art, trigger=trigger, layers=[(entry_a.path, part.get("mic", "main"))],
                    tune_cents=_f(op.get("tune")), gain_db=_f(op.get("volume")), pan=_f(op.get("pan")) / 100.0,
                    amp_veltrack=(_f(op["amp_veltrack"]) / 100.0) if "amp_veltrack" in op else None,
                    offset=int(_f(op.get("offset"))), rr=rr, cc=cc, vel_xfade=xf,
                    loop_mode=lm, loop_start=int(op["loop_start"]) if "loop_start" in op else None,
                    loop_end=int(op["loop_end"]) if "loop_end" in op else None,
                    one_shot=(lm == "one_shot"),
                    release_decay=_f(op["rt_decay"]) if "rt_decay" in op else None,
                    group=int(op["group"]) if "group" in op else None,
                    off_by=int(op["off_by"]) if "off_by" in op else None,
                    off_time=_f(op["off_time"]) if "off_time" in op else None,
                    envelope=env, origin=region.origin,
                )
                if z.vel_lo > z.vel_hi:
                    raise ProvenanceError(f"{region.origin}: velocity {z.vel_lo}-{z.vel_hi}")
                ip.zones.append(z)
        # articulations + keyswitches
        out_arts = []
        for a in inst["articulations"]:
            ks = a.get("keyswitch")
            if ks is None and derived_ks.get(a["id"]):
                vals = derived_ks[a["id"]]
                if len(vals) != 1:
                    raise ProvenanceError(f"{inst['id']}/{a['id']}: conflicting upstream keyswitches {sorted(vals)}")
                ks = next(iter(vals))
            used = any(z.articulation == a["id"] for z in ip.zones)
            if not used:
                raise ProvenanceError(f"{inst['id']}: articulation {a['id']} got no zones")
            entry = {"id": a["id"], "name": a["name"], "keyswitch": ks if len(inst["articulations"]) > 1 else None}
            if a.get("default"):
                entry["default"] = True
            out_arts.append(entry)
        if len(out_arts) > 1 and not any(a.get("default") for a in out_arts):
            out_arts[0]["default"] = True
        if len(out_arts) == 1:
            out_arts[0]["default"] = True
        ip.articulations = out_arts
        if inst.get("multi_mic"):
            ip.zones = merge_mics(ip, [p["mic"] for p in inst["parts"]], inst.get("merge_reference_mic"))
        _resolve_chokes(ip)
        plans.append(ip)

    for reason, n in excl.allowed_hits.items():
        warnings.append(f"allowed by reviewed exception ({n} paths): {reason}")
    return PackPlan(pack, lib, up, plans, sources, warnings, licence_text, licence_sha)


def _stem(path: str, mic_dirs: set) -> str:
    parts = path.split("/")
    # Samples/<micdir>/<piece>/<micdir>_<name>
    if len(parts) >= 4 and parts[0] == "Samples" and parts[1] in mic_dirs:
        name = parts[-1]
        prefix = parts[1] + "_"
        if name.startswith(prefix):
            name = name[len(prefix):]
        return "/".join(parts[2:-1] + [name])
    return path


def merge_mics(ip: InstrumentPlan, mics: List[str], reference: Optional[str] = None) -> List[ZonePlan]:
    """Combine one-zone-per-mic into zones with one aligned layer per mic.

    Every zone parameter must agree across mics, except the amp envelope:
    where it differs, the ``reference`` mic's mapping wins and the count is
    reported (Virtuosity's snare-roll envelopes differ per mic upstream).
    """
    mic_dirs = {posixpath.basename(posixpath.dirname(posixpath.dirname(z.layers[0][0]))) for z in ip.zones}
    merged: Dict[tuple, ZonePlan] = {}
    order: List[tuple] = []
    compare = ("root", "tune_cents", "gain_db", "pan", "amp_veltrack", "offset", "loop_mode", "one_shot",
               "release_decay", "off_time", "envelope", "vel_xfade")
    diffs: Counter = Counter()
    resolved: Counter = Counter()
    if reference:
        ip.zones.sort(key=lambda z: 0 if z.layers[0][1] == reference else 1)
    for z in ip.zones:
        sig = z.signature(_stem(z.layers[0][0], mic_dirs))
        if sig not in merged:
            merged[sig] = z
            order.append(sig)
            continue
        base = merged[sig]
        if any(m == z.layers[0][1] for _, m in base.layers):
            raise ProvenanceError(f"{z.origin}: two {z.layers[0][1]} samples for one hit ({sig})")
        for attr in compare:
            if getattr(base, attr) != getattr(z, attr):
                if attr == "envelope" and reference and base.layers[0][1] == reference:
                    resolved[attr] += 1  # keep the reference mic's envelope
                else:
                    diffs[attr] += 1
        base.layers.append(z.layers[0])
    if diffs:
        raise ProvenanceError(f"{ip.id}: mic mappings disagree on {dict(diffs)}; cannot merge into aligned layers")
    out = [merged[s] for s in order]
    if resolved:
        ip.notes.append(f"mic mappings disagreed on {dict(resolved)} layer(s); used the {reference} mapping's values")
    # Restore the upstream (key-map) order for readability of the output.
    out.sort(key=lambda z: (z.key_lo, z.articulation, z.trigger, z.vel_lo))
    counts = Counter(len(z.layers) for z in out)
    ip.notes.append(f"mic layers per zone: {dict(sorted(counts.items()))} (of {len(mics)} mics)")
    return out


def _resolve_chokes(ip: InstrumentPlan) -> None:
    """SFZ 'group' is also used as a polyphony counter; keep it only where it chokes."""
    choking = {z.off_by for z in ip.zones if z.off_by is not None}
    emitted = {z.group for z in ip.zones if z.group is not None} | {t["group"] for t in ip.choke_triggers}
    dangling = choking - emitted
    if dangling:
        # Upstream no-op (e.g. Virtuosity's pedal hat has off_by=20 and nothing is in group 20).
        n = sum(1 for z in ip.zones if z.off_by in dangling)
        for z in ip.zones:
            if z.off_by in dangling:
                z.off_by = None
        choking -= dangling
        ip.notes.append(f"{n} zone(s) had off_by {sorted(dangling)}, a group no region emits upstream (a no-op there); dropped")
    ip.choke_triggers = [t for t in ip.choke_triggers if t["group"] in choking]
    for z in ip.zones:
        if z.group not in choking:
            z.group = None
