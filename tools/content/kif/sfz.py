"""A strict-enough SFZ parser for converting upstream CC0 libraries to KIF.

Supports the subset the pinned upstream files actually use (counted, not
assumed; see the QA report's ``ignored_opcodes``):

* headers ``<control> <global> <master> <group> <region> <curve>``
* ARIA preprocessor ``#define $NAME value`` and ``#include "file"`` (paths
  relative to the *root* SFZ file's folder, as ARIA / sforzando resolve them)
* ``default_path`` (backslashes allowed), comments ``//`` and ``/* */``
* note names (``c4`` = 60, sharps ``#``, flats ``b``)

The parser does no file-system access of its own for samples: it returns
``Region`` objects whose ``sample`` is the joined, normalised relative path.
Resolving that path against the upstream git tree (case-insensitively, with
a recorded warning) is the caller's job.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

NOTE_RE = re.compile(r"^([a-gA-G])([#b]?)(-?\d+)$")
NOTE_BASE = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}
OPCODE_RE = re.compile(r"(?:(?<=\s)|^)([A-Za-z_][A-Za-z0-9_]*)=")
HEADER_RE = re.compile(r"<([a-z]+)>")
DEFINE_RE = re.compile(r"^\s*#define\s+(\$[A-Za-z0-9_]+)\s+(\S+)")
INCLUDE_RE = re.compile(r'^\s*#include\s+"([^"]+)"')
VAR_RE = re.compile(r"\$[A-Za-z0-9_]+")
INLINE_INCLUDE_RE = re.compile(r'#include\s+"[^"]+"')

LEVELS = ("control", "global", "master", "group", "region")


class SfzError(ValueError):
    pass


def _split_inline_includes(raw: str) -> List[str]:
    """A line with ``#include "x"`` after other text becomes [text, '#include "x"', text, ...]; others stay whole."""
    if DEFINE_RE.match(raw) or not INLINE_INCLUDE_RE.search(raw):
        return [raw]
    out: List[str] = []
    pos = 0
    for m in INLINE_INCLUDE_RE.finditer(raw):
        if raw[pos : m.start()].strip():
            out.append(raw[pos : m.start()])
        out.append(m.group(0))
        pos = m.end()
    if raw[pos:].strip():
        out.append(raw[pos:])
    return out


def note_to_midi(value: str) -> int:
    """SFZ key value: an integer or a note name where c4 = 60."""
    v = value.strip()
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    m = NOTE_RE.match(v)
    if not m:
        raise SfzError(f"not a key: {value!r}")
    letter, acc, octave = m.groups()
    n = NOTE_BASE[letter.lower()] + (1 if acc == "#" else -1 if acc == "b" else 0)
    return (int(octave) + 1) * 12 + n


@dataclass
class Region:
    opcodes: Dict[str, str]
    sample: str  # normalised posix path relative to the root SFZ folder
    origin: str  # "file:line" of the <region> header, for error messages

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        return self.opcodes.get(name, default)


@dataclass
class SfzFile:
    path: str
    regions: List[Region] = field(default_factory=list)
    control: Dict[str, str] = field(default_factory=dict)
    includes: List[str] = field(default_factory=list)
    defines: Dict[str, str] = field(default_factory=dict)


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    out = []
    for line in text.splitlines():
        # A '//' starts a comment; sample paths in the pinned files never contain it.
        i = line.find("//")
        out.append(line if i < 0 else line[:i])
    return "\n".join(out)


def _split_opcodes(segment: str) -> List[tuple]:
    """'a=1 b=two words c=3' -> [(a,'1'), (b,'two words'), (c,'3')]."""
    matches = list(OPCODE_RE.finditer(segment))
    pairs = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(segment)
        pairs.append((m.group(1), segment[m.end() : end].strip()))
    return pairs


def parse(
    root_file: str,
    read_text: Callable[[str], str],
    max_depth: int = 16,
) -> SfzFile:
    """Parse ``root_file`` (a posix path inside the upstream tree).

    ``read_text(path)`` returns the text of an upstream file (so the caller can
    serve it from a git tree, a checkout or a test fixture).
    """
    root_dir = posixpath.dirname(root_file)
    result = SfzFile(path=root_file)
    defines: Dict[str, str] = {}

    # Stream of (kind, payload, origin): kind is 'header' or 'op'.
    tokens: List[tuple] = []

    def expand(path: str, depth: int) -> None:
        if depth > max_depth:
            raise SfzError(f"#include nesting deeper than {max_depth} at {path}")
        text = read_text(path)
        if path != root_file:
            result.includes.append(path)
        lines = _strip_comments(text).splitlines()
        for lineno, raw in enumerate(lines, 1):
            origin = f"{path}:{lineno}"
            # An #include may sit inside a line: ``<group> #include "vel.txt" lovel=1 #include "region.txt"``
            # (Salamander Grand). Split there, so each piece runs in order and a #define from the first file
            # is visible to the text after it.
            pieces = _split_inline_includes(raw)
            for piece in pieces:
                d = DEFINE_RE.match(piece)
                if d:
                    defines[d.group(1)] = d.group(2)
                    continue
                inc = INCLUDE_RE.match(piece)
                if inc:
                    target = VAR_RE.sub(lambda m: defines.get(m.group(0), m.group(0)), inc.group(1))
                    expand(posixpath.normpath(posixpath.join(root_dir, target)), depth + 1)
                    continue
                line = VAR_RE.sub(lambda m: _subst(m.group(0), defines, origin), piece)
                pos = 0
                for h in HEADER_RE.finditer(line):
                    for k, v in _split_opcodes(line[pos : h.start()]):
                        tokens.append(("op", (k, v), origin))
                    tokens.append(("header", h.group(1), origin))
                    pos = h.end()
                for k, v in _split_opcodes(line[pos:]):
                    tokens.append(("op", (k, v), origin))

    expand(root_file, 0)
    result.defines = dict(defines)

    scopes: Dict[str, Dict[str, str]] = {lvl: {} for lvl in LEVELS}
    current: Optional[str] = None
    region_origin = ""
    in_curve = False

    def flush_region() -> None:
        if current != "region":
            return
        merged: Dict[str, str] = {}
        for lvl in ("global", "master", "group", "region"):
            merged.update(scopes[lvl])
        if "sample" not in merged:
            raise SfzError(f"region without sample at {region_origin}")
        sample = merged["sample"].replace("\\", "/")
        if sample.startswith("*"):
            # Built-in generators (*silence, *sine): kept so the caller can
            # account for them, never resolved to a file.
            path = sample
        else:
            default_path = scopes["control"].get("default_path", "").replace("\\", "/")
            path = posixpath.normpath(posixpath.join(root_dir, default_path, sample))
        result.regions.append(Region(opcodes=merged, sample=path, origin=region_origin))

    for kind, payload, origin in tokens:
        if kind == "header":
            flush_region()
            in_curve = payload == "curve"
            if in_curve:
                current = None
                continue
            if payload not in LEVELS:
                raise SfzError(f"unsupported header <{payload}> at {origin}")
            idx = LEVELS.index(payload)
            # A header clears its own level and every level below it,
            # except <control>, which persists until the next <control>.
            for lvl in LEVELS[idx:]:
                if lvl == "control" and payload != "control":
                    continue
                scopes[lvl] = {}
            if payload == "control":
                # A new <control> also resets the lower levels (ARIA behaviour).
                for lvl in LEVELS[1:]:
                    scopes[lvl] = {}
            current = payload
            region_origin = origin
        else:
            if in_curve:
                continue
            if current is None:
                raise SfzError(f"opcode {payload[0]} before any header at {origin}")
            k, v = payload
            scopes[current][k] = v
            if current == "control":
                result.control[k] = v
    flush_region()
    return result


def _subst(name: str, defines: Dict[str, str], origin: str) -> str:
    if name not in defines:
        raise SfzError(f"undefined {name} at {origin}")
    return defines[name]


def read_from_dir(base: Path) -> Callable[[str], str]:
    def _read(path: str) -> str:
        return (base / path).read_text(encoding="utf-8", errors="replace")

    return _read
