#!/usr/bin/env python3
"""Katalyst CC0 instrument pipeline (D-190 .. D-199).

  python -I tools/content/build.py plan  [--pack ID]          parse + provenance gates, no audio
  python -I tools/content/build.py build [--pack ID]          download, convert, QA, KIF
  python -I tools/content/build.py validate                   schema + semantic + file checks
  python -I tools/content/build.py pack  [--pack ID]          deterministic .tar.gz + manifest

Environment / flags:
  --work DIR     scratch: blobless clones and the raw-download cache (deleted per sample)
  --out DIR      build output: <out>/<pack>/<instrument>.kif.json and <out>/<pack>/samples/
Clones are made on first use with `git clone --filter=blob:none --no-checkout`.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from kif import plan as planmod  # noqa: E402
from kif.upstream import Upstream  # noqa: E402

REPO = HERE.parent.parent


def load_config():
    libs = json.loads((HERE / "libraries.json").read_text())
    insts = json.loads((HERE / "instruments.json").read_text())
    return libs, insts


def ensure_clone(lib: dict, work: Path) -> Path:
    d = work / "clones" / lib["id"]
    if not (d / ".git").exists():
        d.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, GIT_LFS_SKIP_SMUDGE="1")
        subprocess.run(
            ["git", "clone", "-q", "--filter=blob:none", "--no-checkout", f"https://github.com/{lib['repo']}", str(d)],
            check=True, env=env,
        )
    for ref, commit in ((lib.get("audio_ref"), lib["audio_commit"]), (lib.get("mapping_ref"), lib["mapping_commit"])):
        has = subprocess.run(["git", "-C", str(d), "cat-file", "-e", commit + "^{commit}"], capture_output=True)
        if has.returncode != 0:
            subprocess.run(["git", "-C", str(d), "fetch", "-q", "--filter=blob:none", "origin", ref], check=True)
    return d


def upstream_for(lib: dict, work: Path) -> Upstream:
    return Upstream(lib, ensure_clone(lib, work), work / "raw" / lib["id"])


def selected_packs(insts: dict, only):
    packs = insts["packs"]
    if only:
        packs = [p for p in packs if p["id"] in only]
        if not packs:
            sys.exit(f"no pack named {only}")
    return packs


def cmd_plan(args):
    libs_cfg, insts = load_config()
    libs = {l["id"]: l for l in libs_cfg["libraries"]}
    for pack in selected_packs(insts, args.pack):
        lib = libs[pack["library"]]
        pp = planmod.plan_pack(pack, lib, libs_cfg["global_exclude"], upstream_for(lib, args.work))
        print(f"== pack {pack['id']} ({lib['id']}): {len(pp.sources)} source files, licence sha256 {pp.licence_sha256[:16]}")
        for w in pp.warnings[:5]:
            print("   warn:", w)
        if len(pp.warnings) > 5:
            print(f"   ... {len(pp.warnings) - 5} more warnings")
        for ip in pp.instruments:
            n_att = sum(1 for z in ip.zones if z.trigger == "attack")
            print(f"   {ip.id}: zones {len(ip.zones)} (attack {n_att}), arts {[a['id'] + ':' + str(a.get('keyswitch')) for a in ip.articulations]}")
            if ip.skipped:
                print("      skipped:", dict(ip.skipped))
            if ip.ignored_opcodes:
                print("      ignored:", dict(ip.ignored_opcodes.most_common(12)))
            for n in ip.notes:
                print("      note:", n)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["plan", "build", "validate", "pack", "report"])
    ap.add_argument("--pack", action="append")
    ap.add_argument("--work", type=Path, default=Path(os.environ.get("KIF_WORK", "/tmp/kif-work")))
    ap.add_argument("--out", type=Path, default=Path(os.environ.get("KIF_OUT", "/tmp/kif-out")))
    ap.add_argument("--dist", type=Path, default=Path(os.environ.get("KIF_DIST", "/tmp/kif-dist")))
    ap.add_argument("--keep-raw", action="store_true", help="keep downloaded originals (default: delete after converting)")
    ap.add_argument("--drop-after-hash", action="store_true", help="pack: delete each archive after hashing (low-disk machines)")
    args = ap.parse_args(argv)
    if args.command == "plan":
        cmd_plan(args)
    else:
        from kif import pipeline

        getattr(pipeline, "cmd_" + args.command)(args, *load_config())


if __name__ == "__main__":
    main()
