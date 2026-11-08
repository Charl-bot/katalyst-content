"""Checks a real build: every KIF validates and every referenced file hashes right.

    KIF_OUT=<build dir> python -m unittest tests.test_build_output

Skipped when KIF_OUT is not set (CI has no instrument audio; it runs
test_pipeline.py on a synthetic library instead).
"""

import hashlib
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kif import validate  # noqa: E402

OUT = os.environ.get("KIF_OUT")
CONFIG = json.loads((Path(__file__).resolve().parent.parent / "instruments.json").read_text())


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@unittest.skipUnless(OUT, "set KIF_OUT to a build output folder")
class BuildOutput(unittest.TestCase):
    def test_every_configured_instrument_was_built(self):
        for pack in CONFIG["packs"]:
            for inst in pack["instruments"]:
                p = Path(OUT) / pack["id"] / f"{inst['id']}.kif.json"
                self.assertTrue(p.is_file(), f"missing {p}")
                self.assertTrue((Path(OUT) / pack["id"] / "qa" / f"{inst['id']}.json").is_file(), f"no QA report for {inst['id']}")

    def test_every_kif_validates_and_every_file_hashes(self):
        kifs = sorted(Path(OUT).glob("*/*.kif.json"))
        self.assertGreater(len(kifs), 0)
        checked = 0
        for k in kifs:
            doc = json.loads(k.read_text())
            self.assertEqual(validate.validate_doc(doc), [], k.name)
            for s in doc["samples"]:
                f = k.parent / s["file"]
                self.assertTrue(f.is_file(), f"{k.name}: {s['file']} missing")
                self.assertEqual(f.stat().st_size, s["bytes"], f"{k.name}: {s['file']} size")
                self.assertEqual(sha256(f), s["sha256"], f"{k.name}: {s['file']} sha256")
                checked += 1
        print(f"\n  {len(kifs)} KIF files, {checked} sample references hashed", file=sys.stderr)

    def test_no_excluded_content_in_provenance(self):
        import re

        libs = json.loads((Path(__file__).resolve().parent.parent / "libraries.json").read_text())
        pats = [re.compile(p) for p in libs["global_exclude"]]
        per_lib = {l["id"]: [re.compile(p) for p in l["exclude"]] for l in libs["libraries"]}
        for prov in Path(OUT).glob("*/provenance.json"):
            doc = json.loads(prov.read_text())
            for f in doc["files"]:
                for p in pats + per_lib[doc["library"]]:
                    self.assertIsNone(p.search(f["path"]), f"{prov.parent.name}: {f['path']} matches {p.pattern}")


if __name__ == "__main__":
    unittest.main()
