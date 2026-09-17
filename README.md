# shaperbox-importer

> If this saved you time, consider giving it a ⭐ — it helps others find the tool!

[![PyPI version](https://img.shields.io/pypi/v/shaperbox-importer.svg)](https://pypi.org/project/shaperbox-importer/)
[![Downloads](https://static.pepy.tech/badge/shaperbox-importer/month)](https://pepy.tech/project/shaperbox-importer)
[![CI](https://github.com/PhillipAmend/shaperbox-importer/actions/workflows/ci.yml/badge.svg)](https://github.com/PhillipAmend/shaperbox-importer/actions/workflows/ci.yml)
[![Python](https://img.shields.io/pypi/pyversions/shaperbox-importer.svg)](https://pypi.org/project/shaperbox-importer/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Platform: macOS + Windows](https://img.shields.io/badge/platform-macOS%20%7C%20Windows-lightgrey.svg)]()

Bulk-import preset packs into Cableguys [ShaperBox 3](https://www.cableguys.com/shaperbox.html) on macOS and Windows — without clicking through `Load FXP → Save preset` for every single file.

ShaperBox 3 has no built-in bulk import. This tool hosts ShaperBox via [Pedalboard](https://github.com/spotify/pedalboard) so the plugin itself migrates each preset to its current internal format, then writes the result directly into ShaperBox's local SQLite DB and content-addressed `.dat` store. After running, the presets appear in the `MY PRESETS` tab on next launch.

> **Disclaimer**: Not affiliated with or endorsed by Cableguys. The internal storage format was reverse-engineered. Use at your own risk; always keep the auto-backup until you've verified your presets.

## Features

- Imports hundreds of presets in one go
- Supports `.vstpreset` (VST3 preset files) and `.fst` (FL Studio plugin-state files)
- Uses ShaperBox's own migration code via Pedalboard — version-correct output
- Auto-backs up the complete ShaperBox 3 data folder before any writes
- Refuses to run while a DAW is open (avoids DB lock)
- Dry-run mode

## Requirements

- macOS or 64-bit Windows
- Python 3.10+
- ShaperBox 3 VST3 installed in its standard location:
  - macOS: `/Library/Audio/Plug-Ins/VST3/ShaperBox 3.vst3`
  - Windows: `%CommonProgramFiles%\VST3\ShaperBox 3.vst3`
- A valid ShaperBox 3 license (the plugin must initialize for migration to work)

## Install

```sh
pip install shaperbox-importer
```

On Windows, the equivalent explicit command is:

```powershell
py -m pip install shaperbox-importer
```

Or run it once without installing, using [uv](https://docs.astral.sh/uv/):

```sh
uvx shaperbox-import /path/to/preset/folder
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
| `--data-dir`   | override the platform-specific ShaperBox data folder        |
| `--plugin-path`| override the platform-specific ShaperBox VST3 path          |
| `--repair-tags`| repair effect dots for existing user presets without re-importing |
| `--version`    | print version                                              |

If presets imported by an older version show every effect dot, close your DAW
and repair their metadata without reprocessing the source files:

```powershell
shaperbox-import --repair-tags --dry-run
shaperbox-import --repair-tags
```

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

On Windows, close every DAW and ShaperBox instance, then rename the current
`%APPDATA%\Cableguys\ShaperBox3` folder and rename the selected
`ShaperBox3.backup-<timestamp>` folder back to `ShaperBox3`.

## How it works

1. ShaperBox stores user presets in `~/Library/Cableguys/ShaperBox3/` on macOS and `%APPDATA%\Cableguys\ShaperBox3` on Windows:
   - `presets.db` — SQLite with `presets`, `queue`, `packs`, `pack_positions`, `files1`, `info` tables.
   - Per-preset `.dat` files in a CAS layout (`<hash[0]>/<hash[1]>/<hash>.dat`), each containing `#zip#\0` + zlib-compressed JUCE `ValueTree`.
2. The current state schema (version 75 in ShaperBox 3.6.x) differs from older saved presets — dropping older `.fxp`/`.vstpreset` bytes in directly doesn't work because internal modules (`LimiterState`, `PitchState`, etc.) have evolved.
3. Hosting ShaperBox via Pedalboard and calling `load_preset()` triggers the plugin's own migration code, which gives us a bit-correct current-format chunk back.
4. The chunk is written as a `.dat` and matching rows are inserted into `presets` + `queue`. **`author` must be empty** for the entry to appear in `MY PRESETS`.

For `.fst` files, the embedded `#zip#` chunk is extracted from FL Studio's container, re-wrapped as a synthetic `.vstpreset`, and fed through the same pipeline.

## FAQ

**Will this work on Windows or Linux?**
Windows and macOS are supported. Linux is not currently supported because ShaperBox 3 does not provide a native Linux VST3 build.

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

[MIT](LICENSE) — an [OSI-approved](https://opensource.org/licenses/MIT) permissive open source license. Use it for anything (personal, commercial, in your own product), just keep the copyright notice.
