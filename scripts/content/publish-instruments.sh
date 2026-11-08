#!/usr/bin/env bash
# Publish the CC0 instrument packs as GitHub release assets (D-191).
#
#   scripts/content/publish-instruments.sh --build            # rebuild from the pinned upstreams, then upload
#   scripts/content/publish-instruments.sh --dist DIR          # upload packs already in DIR
#   scripts/content/publish-instruments.sh --build --dry-run   # rebuild + verify only, upload nothing
#   scripts/content/publish-instruments.sh --create-repo ...    # first run: create the public content repo
#
# Assets go to the repository named in the manifest ("release_repo",
# default Charl-bot/katalyst-content). It must be PUBLIC: the app downloads
# anonymously, and Charl-bot/katalyst-suite is private (D-191). The content
# repo holds only CC0 audio releases, no code.
#
# Every archive is checked against content/instruments/manifest.json (bytes
# and SHA-256) before upload, and every uploaded asset is downloaded again and
# re-checked after. A rebuild that does not reproduce the committed hashes
# stops here: nothing is uploaded until the new manifest is reviewed and
# committed (run with --accept-new-hashes to write it, then commit it).
#
# Needs: git, python3 (3.13), curl, gh (authenticated: `gh auth login`, one
# time, with write access to the repository). Disk for --build: about 2x the
# total pack size plus ~1 GB (raw downloads are deleted as they are converted).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MANIFEST="$ROOT/content/instruments/manifest.json"
REPO_SLUG="${KATALYST_RELEASE_REPO:-}"
WORK="${KIF_WORK:-$ROOT/build-content/work}"
OUT="${KIF_OUT:-$ROOT/build-content/out}"
DIST="${KIF_DIST:-$ROOT/build-content/dist}"
BUILD=0
DRY=0
CLOBBER=0
ACCEPT=0
CREATE=0

usage() { sed -n '2,25p' "$0"; exit "${1:-0}"; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --build) BUILD=1 ;;
    --dist) DIST="$2"; shift ;;
    --work) WORK="$2"; shift ;;
    --out) OUT="$2"; shift ;;
    --dry-run) DRY=1 ;;
    --clobber) CLOBBER=1 ;;
    --accept-new-hashes) ACCEPT=1 ;;
    --create-repo) CREATE=1 ;;
    -h|--help) usage 0 ;;
    *) echo "unknown argument: $1" >&2; usage 2 ;;
  esac
  shift
done

die() { echo "publish-instruments: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "$1 is required"; }
need git; need python3; need curl
[[ $DRY -eq 1 ]] || need gh

sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }
filesize() { wc -c <"$1" | tr -d ' '; }

[[ -f "$MANIFEST" ]] || die "no manifest at $MANIFEST"
TAG="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["release_tag"])' "$MANIFEST")"
[[ -n "$REPO_SLUG" ]] || REPO_SLUG="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["release_repo"])' "$MANIFEST")"

if [[ $BUILD -eq 1 ]]; then
  VENV="$ROOT/build-content/venv"
  if [[ ! -x "$VENV/bin/python" ]]; then
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install -q -r "$ROOT/tools/content/requirements.txt"
  fi
  mkdir -p "$WORK"
  cp "$MANIFEST" "$WORK/manifest.committed.json"
  "$VENV/bin/python" -I "$ROOT/tools/content/build.py" build --work "$WORK" --out "$OUT"
  "$VENV/bin/python" -I "$ROOT/tools/content/build.py" pack --work "$WORK" --out "$OUT" --dist "$DIST"
  KIF_OUT="$OUT" "$VENV/bin/python" -m unittest discover -s "$ROOT/tools/content/tests" -t "$ROOT/tools/content" >/dev/null
  if ! cmp -s "$MANIFEST" "$WORK/manifest.committed.json"; then
    if [[ $ACCEPT -eq 1 ]]; then
      echo "The rebuild produced new hashes; content/instruments/manifest.json was rewritten. Review and commit it, then rerun without --build."
      exit 3
    fi
    cp "$WORK/manifest.committed.json" "$MANIFEST"
    die "the rebuild did not reproduce the committed manifest (different hashes). Nothing uploaded. Check tools/content/requirements.txt pins (the FLAC encoder version decides the bytes), or rerun with --accept-new-hashes and commit the new manifest."
  fi
fi

# 1. Every part present, right size, right hash.
mapfile -t PARTS < <(python3 - "$MANIFEST" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
for p in m["packs"]:
    for part in p["parts"]:
        print("\t".join([part["file"], str(part["bytes"]), part["sha256"], part["url"]]))
PY
)
[[ ${#PARTS[@]} -gt 0 ]] || die "the manifest lists no parts"
total=0
for line in "${PARTS[@]}"; do
  IFS=$'\t' read -r file bytes sha url <<<"$line"
  f="$DIST/$file"
  [[ -f "$f" ]] || die "missing $f (run with --build, or point --dist at the packs)"
  [[ "$(filesize "$f")" == "$bytes" ]] || die "$file: size $(filesize "$f") != manifest $bytes"
  [[ "$(sha256 "$f")" == "$sha" ]] || die "$file: sha256 does not match the manifest"
  (( bytes < 2147483648 )) || die "$file is over GitHub's 2 GiB asset limit"
  total=$((total + bytes))
  echo "verified  $file  ($((bytes / 1000000)) MB)"
done
echo "all ${#PARTS[@]} parts verified, $((total / 1000000)) MB in total"
[[ $DRY -eq 1 ]] && { echo "dry run: nothing uploaded"; exit 0; }

# 2. Public content repository and release (created once; the release is a
#    pre-release with the credits as its notes).
if ! gh repo view "$REPO_SLUG" >/dev/null 2>&1; then
  [[ $CREATE -eq 1 ]] || die "$REPO_SLUG does not exist; rerun with --create-repo to create it (public)"
  # --add-readme: a release needs a commit to tag; an empty repository has none.
  gh repo create "$REPO_SLUG" --public --add-readme --description "Katalyst Suite downloadable instrument content (CC0 sample packs). No code."
fi
[[ "$(gh repo view "$REPO_SLUG" --json visibility --jq .visibility)" == "PUBLIC" ]] \
  || die "$REPO_SLUG is not public: the app could not download its release assets"
if ! gh release view "$TAG" --repo "$REPO_SLUG" >/dev/null 2>&1; then
  gh release create "$TAG" --repo "$REPO_SLUG" --prerelease \
    --title "Katalyst instruments (CC0 set v1)" \
    --notes-file "$ROOT/docs/CONTENT_CREDITS.md"
fi
existing="$(gh release view "$TAG" --repo "$REPO_SLUG" --json assets --jq '.assets[].name')"

# 3. Upload (skip assets already there unless --clobber).
for line in "${PARTS[@]}"; do
  IFS=$'\t' read -r file bytes sha url <<<"$line"
  if grep -qxF "$file" <<<"$existing" && [[ $CLOBBER -eq 0 ]]; then
    echo "present   $file (kept; verified below)"
    continue
  fi
  extra=()
  [[ $CLOBBER -eq 1 ]] && extra=(--clobber)
  gh release upload "$TAG" "$DIST/$file" --repo "$REPO_SLUG" "${extra[@]}"
  echo "uploaded  $file"
done

# 4. Download each asset back through its public URL and check it.
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
for line in "${PARTS[@]}"; do
  IFS=$'\t' read -r file bytes sha url <<<"$line"
  curl -fsSL --retry 3 -o "$tmp/$file" "$url" || die "$url did not download (is the repository public? see docs/content/DISTRIBUTION.md)"
  [[ "$(sha256 "$tmp/$file")" == "$sha" ]] || die "$url: downloaded bytes do not match the manifest"
  rm -f "$tmp/$file"
  echo "online    $file"
done
echo "published ${#PARTS[@]} parts to $REPO_SLUG release $TAG"
