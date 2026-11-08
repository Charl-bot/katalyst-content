# Katalyst Suite: sound content credits

Katalyst's downloadable sampled instruments (the "CC0 set v1") are built from the public-domain sample libraries below.

Those libraries are dedicated to the public domain under **CC0 1.0 Universal**. No attribution is legally required. We credit them anyway as a courtesy and to document where every sound came from.

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
  - Concert Grand, Studio Upright, Flemish Harpsichord, Pipe Organ
  - Marimba, Vibraphone, Xylophone, Glockenspiel
  - Timpani, Orchestral Percussion
- **Not used:** the TX81Z recordings (recordings of a hardware synthesizer's factory patches) and the other `Electrophones`.

## Virtuosity Drums

- **By:** Versilian Studios. Performed by Austin McMahon; recorded at Virtuosity Musical Instruments, Boston, MA.
- **Licence:** CC0 1.0 Universal
- **Source:** <https://github.com/sfzinstruments/virtuosity_drums>, commit `9f04cf9a734527edfbb0a4eee1f674e45bbf71bc`
- **Used in:** Jazz Club Kit (kick, snare, toms, hi-hat, rides and crash; six mic positions).
- **Not used:**
  - the auxiliary percussion (`Samples/perc`). The upstream README credits it to VSCO 2 Pro and Karoryfer, which are outside this CC0-only set.
  - the GUI artwork (`GUI/`) and the plugin bank file.

## What we changed

The same changes apply to every library:

- Leading silence was trimmed. The cut is 3 ms before the first sample at -50 dB relative to the sample's peak, so the attack is kept, and a 32-frame fade-in is applied.
- Files were converted to FLAC at the original sample rate and bit depth.
- The SFZ mappings were converted to the Katalyst Instrument Format (KIF v1). Instrument gain was set by loudness normalisation. The audio itself is not gain-changed.
- Some SFZ features are not carried over. The QA report of each instrument lists them (`docs/content/qa/`).
