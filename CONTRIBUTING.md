# Contributing

Thanks for considering a contribution. A few notes:

## Scope

This tool exists for one job: making it easy to import many ShaperBox 3 presets at once on macOS. Issues or PRs that broaden that scope (other DAWs, other plugins, other OSes) are welcome, but please open an issue first so we can discuss the design.

## Dev setup

```sh
git clone https://github.com/PhillipAmend/shaperbox-importer
cd shaperbox-importer
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Before opening a PR

```sh
ruff check .
ruff format --check .
pytest
```

The unit tests run without ShaperBox installed (they exercise only the pure format helpers). End-to-end testing against a real plugin install is manual.

## Licensing of contributions

By submitting a contribution (PR, patch, etc.) you agree that your contribution will be licensed under the same [PolyForm Noncommercial License 1.0.0](LICENSE) as the rest of the project.

## Reporting issues

When reporting an issue, please include:

- macOS version
- ShaperBox 3 version (Help → About in the plugin)
- Python version
- Steps to reproduce
- Full output (run with `-v` if you've added it, otherwise paste the import log)

## Format / schema changes

ShaperBox's internal format is reverse-engineered. If Cableguys ships a new ShaperBox release that changes the storage format, the most likely break points are:

- `CURRENT_DB_VERSION` in `cli.py` — bump when the `presets.version` column moves
- The list of children expected in `PluginState` — currently 27 in v75
- The `SHAPERBOX_CID` constant — unlikely to change but worth checking

If you've reverse-engineered a fix, please document the new behavior in the PR description.
