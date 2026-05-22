# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0]

### Added
- Initial release.
- Bulk-import `.vstpreset` files into ShaperBox 3 on macOS.
- Support for `.fst` (FL Studio plugin-state) files by extracting the embedded `#zip#` chunk.
- Auto-backup of `~/Library/Cableguys/ShaperBox3/` before any writes.
- DAW-running detection (refuses to proceed unless `--force`).
- Dry-run mode.
- Console entry point: `shaperbox-import`.

[Unreleased]: https://github.com/phillipamend/shaperbox-importer/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/phillipamend/shaperbox-importer/releases/tag/v0.1.0
