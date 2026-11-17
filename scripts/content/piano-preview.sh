#!/usr/bin/env bash
# D-429: the browser preview's piano, made from the built grand-piano pack. Two layers (v5 soft, v12 strong) of the
# thirty recorded notes, full natural decay, 112 kb/s stereo MP3, in a deterministic .tar.gz. The web build of
# Katalyst Suite downloads it by SHA-256 (never committed there); the desktop app plays the full pack.
#
#   scripts/content/piano-preview.sh <out/grand-piano> <dist dir> <version>
set -euo pipefail
SRC="$1"; DIST="$2"; VERSION="$3"
command -v ffmpeg >/dev/null || { echo "piano-preview: ffmpeg is required" >&2; exit 1; }
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
mkdir -p "$work/piano" "$DIST"
names=(C Ds Fs A) # the pack's file names spell sharps "s"
for root in $(seq 21 3 108); do
  n="${names[$(( (root % 12) / 3 ))]}$(( root / 12 - 1 ))"
  for pair in soft:5 strong:12; do
    label="${pair%%:*}"; layer="${pair##*:}"
    in="$SRC/samples/${n}v${layer}.flac"
    [[ -f "$in" ]] || { echo "piano-preview: missing $in" >&2; exit 1; }
    ffmpeg -v error -y -i "$in" -map_metadata -1 -fflags +bitexact -flags:a +bitexact \
      -c:a libmp3lame -b:a 112k -ar 44100 "$work/piano/$(printf %03d "$root")-$label.mp3"
  done
done
out="$DIST/katalyst-grand-piano-preview-$VERSION.tar.gz"
(cd "$work" && find piano -type f | LC_ALL=C sort | tar --owner=0 --group=0 --numeric-owner --mtime=@0 --format=ustar -cf - -T - | gzip -n -9 > "$out")
echo "piano-preview: $(basename "$out") $(wc -c <"$out" | tr -d ' ') bytes sha256 $(sha256sum "$out" | cut -d' ' -f1)"
