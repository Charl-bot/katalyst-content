"""Pinned upstream repositories: tree listing, path resolution, verified fetch.

Provenance chain for every source file:

1. The library is pinned to a full 40-hex git commit (``libraries.json``).
2. The file list and each file's git blob id come from that commit's tree
   (``git ls-tree -r <commit>`` on a blobless clone: trees only, no audio).
3. Each file is downloaded from ``raw.githubusercontent.com/<repo>/<commit>/<path>``
   and its git blob id is recomputed (``sha1("blob <len>\\0" + bytes)``) and
   compared with the tree entry. A mismatch is fatal. The SHA-256 of the
   verified bytes is then recorded in the provenance manifest.

So a manifest entry ties the original bytes to the pinned commit
cryptographically, not by trusting a URL.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


class ProvenanceError(RuntimeError):
    pass


@dataclass
class TreeEntry:
    path: str
    blob: str
    mode: str


def git_blob_id(data: bytes) -> str:
    h = hashlib.sha1()
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Upstream:
    """One library pinned to one or two commits (audio + mapping)."""

    def __init__(self, lib: dict, clone_dir: Path, cache_dir: Path):
        self.lib = lib
        self.id = lib["id"]
        self.repo = lib["repo"]  # owner/name
        self.clone = clone_dir
        self.cache = cache_dir
        self.cache.mkdir(parents=True, exist_ok=True)
        self.audio_commit = lib["audio_commit"]
        self.mapping_commit = lib.get("mapping_commit", self.audio_commit)
        self._trees: Dict[str, Dict[str, TreeEntry]] = {}

    # -- git (trees only; never fetches audio blobs) -------------------------
    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.clone), *args], check=True, capture_output=True, text=True
        ).stdout

    def tree(self, commit: str) -> Dict[str, TreeEntry]:
        if commit not in self._trees:
            out = self._git("ls-tree", "-r", "-z", "--full-tree", commit)
            entries: Dict[str, TreeEntry] = {}
            for rec in out.split("\0"):
                if not rec:
                    continue
                meta, path = rec.split("\t", 1)
                mode, kind, blob = meta.split()
                if kind == "blob":
                    entries[path] = TreeEntry(path, blob, mode)
            self._trees[commit] = entries
        return self._trees[commit]

    def resolve(self, path: str, commit: Optional[str] = None) -> tuple:
        """Exact, else case-insensitive match. Returns (entry, warning|None)."""
        tree = self.tree(commit or self.audio_commit)
        if path in tree:
            return tree[path], None
        low = path.lower()
        hits = [e for p, e in tree.items() if p.lower() == low]
        if len(hits) == 1:
            return hits[0], f"case-insensitive match: {path!r} -> {hits[0].path!r}"
        if len(hits) > 1:
            raise ProvenanceError(f"ambiguous case-insensitive match for {path!r}")
        raise ProvenanceError(f"{path!r} is not in {self.repo}@{(commit or self.audio_commit)[:12]}")

    def text(self, path: str, commit: Optional[str] = None) -> str:
        """Small text files (SFZ, LICENSE) via git (lazy blob fetch)."""
        c = commit or self.mapping_commit
        entry, _ = self.resolve(path, c)
        data = subprocess.run(
            ["git", "-C", str(self.clone), "cat-file", "blob", entry.blob], check=True, capture_output=True
        ).stdout
        if git_blob_id(data) != entry.blob:
            raise ProvenanceError(f"git returned bytes that do not hash to {entry.blob} for {path}")
        return data.decode("utf-8", errors="replace")

    # -- raw download, verified against the tree ----------------------------
    def raw_url(self, entry: TreeEntry, commit: str) -> str:
        return "https://raw.githubusercontent.com/%s/%s/%s" % (
            self.repo,
            commit,
            urllib.parse.quote(entry.path, safe="/"),
        )

    def fetch(self, entry: TreeEntry, commit: Optional[str] = None, retries: int = 3) -> Path:
        commit = commit or self.audio_commit
        dest = self.cache / entry.blob
        if self.lib.get("fetch") == "git":
            # Offline / test mode: read the blob from the clone itself.
            data = subprocess.run(["git", "-C", str(self.clone), "cat-file", "blob", entry.blob], check=True, capture_output=True).stdout
            if git_blob_id(data) != entry.blob:
                raise ProvenanceError(f"{entry.path}: git returned bytes that do not hash to {entry.blob}")
            dest.write_bytes(data)
            return dest
        if dest.exists():
            if git_blob_id(dest.read_bytes()) == entry.blob:
                return dest
            dest.unlink()
        url = self.raw_url(entry, commit)
        last: Optional[Exception] = None
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(url, timeout=120) as r:
                    data = r.read()
                if git_blob_id(data) != entry.blob:
                    raise ProvenanceError(
                        f"{entry.path}: downloaded bytes do not match git blob {entry.blob} at {commit[:12]}"
                    )
                tmp = dest.with_suffix(".part")
                tmp.write_bytes(data)
                os.replace(tmp, dest)
                return dest
            except ProvenanceError:
                raise
            except Exception as e:  # network: retry
                last = e
                time.sleep(2 * (attempt + 1))
        raise ProvenanceError(f"could not download {url}: {last}")

    def drop(self, entry: TreeEntry) -> None:
        p = self.cache / entry.blob
        if p.exists():
            p.unlink()


def load_libraries(path: Path) -> List[dict]:
    return json.loads(path.read_text())["libraries"]
