# Katalyst content

Downloadable instrument packs for [Katalyst Suite](https://katalyst-suite-sandy.vercel.app). This repository holds **public-domain (CC0) and Creative Commons Attribution (CC BY) audio only**, plus the scripts that rebuild it reproducibly from pinned upstream sources. It contains no Katalyst application code.

- **Packs** are published as release assets. `content/instruments/manifest.json` lists every file with its size and SHA-256, and the app checks both before it uses anything. The CC0 packs are in release `instruments-cc0-v1`; the CC BY packs, which need attribution, are in `instruments-cc-by-v1`.
- **Sources and licences** are in `content/provenance/`, one folder per library, each with its LICENSE text and a per-file record. Credits, with the attribution and the changes we made to each CC BY work, are in [docs/CONTENT_CREDITS.md](docs/CONTENT_CREDITS.md).
- **Rebuild and publish** by running the "Publish instrument packs" workflow (Actions tab). Give it pack ids to build only those.
- **No locks:** packs are plain `.tar.gz` files of FLAC audio and JSON. The checksums only verify the download.
