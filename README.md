# shaperbox-importer

[![CI](https://github.com/PhillipAmend/shaperbox-importer/actions/workflows/ci.yml/badge.svg)](https://github.com/PhillipAmend/shaperbox-importer/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)
[![License: PolyForm NC 1.0.0](https://img.shields.io/badge/License-PolyForm--NC--1.0.0-orange.svg)](LICENSE)
[![Platform: macOS](https://img.shields.io/badge/platform-macOS-lightgrey.svg)]()

Bulk-import preset packs into Cableguys [ShaperBox 3](https://www.cableguys.com/shaperbox.html) on macOS — without clicking through `Load FXP → Save preset` for every single file.

ShaperBox 3 has no built-in bulk import. This tool hosts ShaperBox via [Pedalboard](https://github.com/spotify/pedalboard) so the plugin itself migrates each preset to its current internal format, then writes the result directly into ShaperBox's local SQLite DB and content-addressed `.dat` store. After running, the presets appear in the `MY PRESETS` tab on next launch.

> **Disclaimer**: Not affiliated with or endorsed by Cableguys. The internal storage format was reverse-engineered. Use at your own risk; always keep the auto-backup until you've verified your presets.

## Features

- Imports hundreds of presets in one go
- Supports `.vstpreset` (VST3 preset files) and `.fst` (FL Studio plugin-state files)
- Uses ShaperBox's own migration code via Pedalboard — version-correct output
- Auto-backups your `~/Library/Cableguys/ShaperBox3/` folder before any writes
- Refuses to run while a DAW is open (avoids DB lock)
- Dry-run mode

## Requirements

- macOS
- Python 3.9+
- ShaperBox 3 installed at `/Library/Audio/Plug-Ins/VST3/ShaperBox 3.vst3`
- A valid ShaperBox 3 license (the plugin must initialize for migration to work)

## Install

```sh
pip install shaperbox-importer
```

Or from source:

```sh
git clone https://github.com/PhillipAmend/shaperbox-importer
cd shaperbox-importer
pip install .
```

## Usage

```sh
# Dry-run first to see what would be imported
shaperbox-import /path/to/preset/folder --dry-run

# Real import (auto-backs up ShaperBox3/ first)
shaperbox-import /path/to/preset/folder
```

Close your DAW first. The tool refuses to run while one is open; override with `--force` if you really need to.

### Flags

| flag           | description                                                |
| -------------- | ---------------------------------------------------------- |
| `--dry-run`    | list what would be imported, no writes                     |
| `--no-backup`  | skip the auto-backup of the Cableguys data folder          |
| `--force`      | proceed even with a DAW open (risks DB lock)               |
| `--version`    | print version                                              |

### Example

```text
$ shaperbox-import ~/Downloads/MyPresetPack
found 200 preset file(s) (200 .vstpreset) under /Users/me/Downloads/MyPresetPack

backing up ShaperBox data folder ...
backup: /Users/me/Library/Cableguys/ShaperBox3.backup-20260522-142425

loading ShaperBox 3 via Pedalboard ...

importing 200 preset(s):
  [   1/200] +  Bass Wobble 01  (4385 B)
  [   2/200] +  Bass Wobble 02  (4586 B)
  ...
imported 200 preset(s); 0 failed.
open ShaperBox in your DAW to see the new presets in MY PRESETS.
```

### Restoring from backup

If something looks wrong, the script prints the backup path. To restore:

```sh
rm -rf ~/Library/Cableguys/ShaperBox3
mv ~/Library/Cableguys/ShaperBox3.backup-<timestamp> ~/Library/Cableguys/ShaperBox3
```

## How it works

1. ShaperBox stores user presets in `~/Library/Cableguys/ShaperBox3/`:
   - `presets.db` — SQLite with `presets`, `queue`, `packs`, `pack_positions`, `files1`, `info` tables.
   - Per-preset `.dat` files in a CAS layout (`<hash[0]>/<hash[1]>/<hash>.dat`), each containing `#zip#\0` + zlib-compressed JUCE `ValueTree`.
2. The current state schema (version 75 in ShaperBox 3.6.x) differs from older saved presets — dropping older `.fxp`/`.vstpreset` bytes in directly doesn't work because internal modules (`LimiterState`, `PitchState`, etc.) have evolved.
3. Hosting ShaperBox via Pedalboard and calling `load_preset()` triggers the plugin's own migration code, which gives us a bit-correct current-format chunk back.
4. The chunk is written as a `.dat` and matching rows are inserted into `presets` + `queue`. **`author` must be empty** for the entry to appear in `MY PRESETS`.

For `.fst` files, the embedded `#zip#` chunk is extracted from FL Studio's container, re-wrapped as a synthetic `.vstpreset`, and fed through the same pipeline.

## FAQ

**Will this work on Windows or Linux?**
No. The Cableguys data folder, plugin path, and DAW-detection heuristics are macOS-specific. PRs welcome.

**Will it break in the next ShaperBox release?**
Possibly. The schema is bumped occasionally. If imports stop showing up, the `CURRENT_DB_VERSION` constant in `cli.py` likely needs to be raised; the rest of the format has been stable for several major versions.

**Does it work for HalfTime / FilterShaper Core / other Cableguys plugins?**
Not today. The data folder layout is similar, but each plugin has its own state schema and `MY PRESETS` flow.

**Can I assign presets to a specific pack instead of `MY PRESETS`?**
Not yet. PRs welcome — see `pack_positions` in the DB.

**Does it sync to my Cableguys cloud account?**
The imported presets land in the local sync queue (`state=0`). What Cableguys' server does with them on next sync isn't documented; we observed that they display correctly locally regardless.

## Acknowledgments

This project was built in collaboration with [Claude](https://claude.ai) (Anthropic). The format reverse-engineering, code, tests, and documentation were paired with AI assistance.

## License

[PolyForm Noncommercial License 1.0.0](LICENSE).

In plain English: anyone can use, copy, fork, modify, and redistribute this tool freely for personal, hobby, educational, research, charity, or government use. **You may not use this tool, or any work derived from it, as part of a product or service that you sell or otherwise commercialize.** If that's restrictive for your use case, open an issue and we can talk.

Note: PolyForm Noncommercial is "source-available" / "fair-source" — it is not an [OSI-approved](https://opensource.org/licenses/) open source license because of the commercial-use restriction.
