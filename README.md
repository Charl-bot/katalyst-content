# Katalyst content

Downloadable instrument packs for [Katalyst Suite](https://katalyst-suite-sandy.vercel.app). This repository holds **public-domain (CC0) audio only**, plus the scripts that rebuild it reproducibly from pinned upstream sources. It contains no Katalyst application code.

- **Packs** are published as release assets. `content/instruments/manifest.json` lists every file with its size and SHA-256, and the app checks both before it uses anything.
- **Sources and licences** are in `content/provenance/`, one folder per library, each with its LICENSE text and a per-file record. Credits are in [docs/CONTENT_CREDITS.md](docs/CONTENT_CREDITS.md).
- **Rebuild and publish** by running the "Publish instrument packs" workflow (Actions tab).
