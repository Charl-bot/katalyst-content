# Katalyst Suite: sound content credits

Katalyst's downloadable sampled instruments are built from the sample libraries below. Two sets exist:

- **The CC0 set** (release `instruments-cc0-v1`). These libraries are dedicated to the public domain under **CC0 1.0 Universal**. No attribution is legally required. We credit them anyway as a courtesy and to document where every sound came from.
- **The CC BY set** (release `instruments-cc-by-v1`, D-391). These libraries are licensed under **Creative Commons Attribution** (3.0 or 4.0). Attribution **is** required, and this file, the in-app Credits screen and each instrument's KIF `licence.note` carry it. Only CC BY is accepted; NonCommercial, NoDerivatives and ShareAlike material is refused by the build (`tools/content/kif/plan.py`).

CC0 does not grant trademark rights, so our instrument names describe the instrument and use no brand names.

Machine-readable twin: [`content/credits/third_party_content.json`](../content/credits/third_party_content.json), for the in-app Credits screen.

Per-file provenance: `content/provenance/<library>/provenance.json` records the source path, pinned commit, git blob id and SHA-256 of every original file. The CC0 text is in `content/provenance/<library>/LICENSE`.

## VS Chamber Orchestra 2: Community Edition (VSCO 2 CE)

- **By:** Versilian Studios. Recorded by Sam Gossner and Simon Dalzell; sample cutting by Elan Hickler / Soundemote.
- **Licence:** CC0 1.0 Universal (<https://creativecommons.org/publicdomain/zero/1.0/legalcode>)
- **Source:** <https://github.com/sgossner/VSCO-2-CE>, branch `SFZ`, commit `6dd651d55dde97fd4028699be9d4481f26917891`
- **Used in:**
  - Orchestral Strings, Violins, Violas, Cellos, Solo Violin, Solo Double Bass
  - French Horn, Trumpet, Trombone, Tuba
  - Flute, Oboe, Clarinet, Bassoon

## Versilian Community Sample Library (VCSL)

- **By:** Versilian Studios LLC. The SFZ mappings were generated with Peter Eastman's CC0 utility.
- **Licence:** CC0 1.0 Universal
- **Source:** <https://github.com/sgossner/VCSL>
  - audio and LICENSE: `master`, commit `c1ea7bcc3c7309650ab0da9d15c9cd1fbc4a4c7e`
  - SFZ mappings: branch `sfz`, commit `dfcf4a4918771eee884b96ad4493de82ef84daf6`
  - Every sample we use is byte-identical at both commits.
- **Used in:**
  - Concert Grand, Studio Upright, Bright Grand, Flemish Harpsichord, Pipe Organ
  - Marimba, Vibraphone, Xylophone, Glockenspiel, Tubular Bells
  - Timpani, Orchestral Percussion, Hand Percussion
- **Not used:** the TX81Z recordings (recordings of a hardware synthesizer's factory patches) and the other `Electrophones`.

## Virtuosity Drums

- **By:** Versilian Studios. Performed by Austin McMahon; recorded at Virtuosity Musical Instruments, Boston, MA.
- **Licence:** CC0 1.0 Universal
- **Source:** <https://github.com/sfzinstruments/virtuosity_drums>, commit `9f04cf9a734527edfbb0a4eee1f674e45bbf71bc`
- **Used in:** Jazz Club Kit (kick, snare, toms, hi-hat, rides and crash; six mic positions).
- **Not used:**
  - the auxiliary percussion (`Samples/perc`). The upstream README credits it to VSCO 2 Pro and Karoryfer, which are outside this CC0-only set.
  - the GUI artwork (`GUI/`) and the plugin bank file.

## Salamander Grand Piano V3 (CC BY 3.0, attribution required)

- **By:** Alexander Holm (recording). The SFZ remap used here is by kinwie (the `sfzinstruments` project).
- **Title and source of the work:** "Salamander Grand Piano V3", <https://archive.org/details/SalamanderGrandPianoV3>; the remap is at <https://github.com/sfzinstruments/SalamanderGrandPiano>, commit `3382bf9496bba2486f5ab0de55a264d1dfc38404`.
- **Licence:** Creative Commons Attribution 3.0 Unported, <https://creativecommons.org/licenses/by/3.0/>. The licence text at the pinned commit is stored in `content/provenance/salamander-grand/LICENSE`.
- **Used in:** Salamander Grand.
- **Changes we made (as the licence asks us to say):** the samples were converted to FLAC, trimmed of leading silence and loudness-normalised by an instrument gain; the SFZ mapping was converted to the Katalyst Instrument Format with the natural tuning only (the upstream's retuned set is not used); the hammer-noise and string-resonance release samples are kept and the pedal-noise samples are left out. The recordings are otherwise unaltered.
- **What this does not mean:** the author and the sfzinstruments project do not endorse Katalyst Suite or this adaptation.
- **Technical measures:** the pack is downloaded as an ordinary `.tar.gz` and unpacked to plain FLAC and JSON files in the content folder. The SHA-256 checks only verify the download (integrity); nothing is encrypted or locked, so a person who has the pack can use the recordings as CC BY 3.0 section 4(a) allows. The licence for the recordings stays CC BY 3.0 whatever the licence of Katalyst Suite itself. The end-user licence agreement has to say so; it is not written yet (KI-1305).

## Verified but not built: DrumGizmo kits

Both kits are CC BY 4.0 and were checked on their project pages (11 Nov 2026): the **DRSKit** (13 microphones; the DrumGizmo team with Jes Eiler of DRSDrums) and the **MuldjordKit** (Lars Muldjord, 15 microphones). They are published in the DrumGizmo XML format, not SFZ, so they need a converter before they can become KIF packs (KI-1306). The MuldjordKit asks that credits or liner notes say "Drum samples provided by DrumGizmo.org". If either kit is ever shipped, that line goes in this file, the Credits screen and the release notes. Nothing from them is in a build.

## What we changed

The same changes apply to every library:

- Leading silence was trimmed. The cut is 3 ms before the first sample at -50 dB relative to the sample's peak, so the attack is kept, and a 32-frame fade-in is applied.
- Files were converted to FLAC at the original sample rate and bit depth.
- The SFZ mappings were converted to the Katalyst Instrument Format (KIF v1). Instrument gain was set by loudness normalisation. The audio itself is not gain-changed.
- Some SFZ features are not carried over. The QA report of each instrument lists them (`docs/content/qa/`).
