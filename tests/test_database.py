"""Tests for database validation and an end-to-end import into temporary data."""

from __future__ import annotations

import sqlite3
import sys
import types
import zlib

import pytest

from shaperbox_importer import cli


def _create_db(path, version=75):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE presets(hash TEXT, name TEXT, author TEXT, liked TEXT, version NUM, custom TEXT)"
    )
    conn.execute(
        "CREATE TABLE queue(command TEXT, table_name TEXT, row_key TEXT, row_subkey TEXT, "
        "column TEXT, value TEXT, state NUM)"
    )
    conn.execute(
        "INSERT INTO presets VALUES ('factory', 'Factory', 'Cableguys', '0', ?, '')",
        (version,),
    )
    conn.commit()
    conn.close()


def _fake_pedalboard(monkeypatch, migrated_chunk):
    class FakePlugin:
        preset_data = cli.wrap_chunk_as_vst3preset(migrated_chunk)

        def load_preset(self, path):
            assert path.endswith(".vstpreset")

    module = types.ModuleType("pedalboard")
    module.load_plugin = lambda path: FakePlugin()
    monkeypatch.setitem(sys.modules, "pedalboard", module)


def _run_import(monkeypatch, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    source_chunk = b"#zip#\x00" + zlib.compress(b"old state")
    (source / "My Preset.fst").write_bytes(b"FLhd" + source_chunk + b"trailer")

    data_dir = tmp_path / "ShaperBox3"
    data_dir.mkdir()
    _create_db(data_dir / "presets.db")
    plugin_path = tmp_path / "ShaperBox 3.vst3"
    plugin_path.write_bytes(b"fake plugin")

    migrated_chunk = b"#zip#\x00" + zlib.compress(b"migrated state")
    _fake_pedalboard(monkeypatch, migrated_chunk)
    monkeypatch.setattr(cli, "check_daw_running", lambda: [])
    monkeypatch.setattr(cli, "extract_visible_modules", lambda chunk: "time,filter")
    result = cli.run(source, skip_backup=True, data_dir=data_dir, plugin_path=plugin_path)
    return result, data_dir, migrated_chunk


def test_validate_db_rejects_newer_preset_version(tmp_path):
    db_path = tmp_path / "presets.db"
    _create_db(db_path, version=cli.CURRENT_DB_VERSION + 1)
    conn = sqlite3.connect(db_path)
    with pytest.raises(RuntimeError, match="newer than supported"):
        cli.validate_db(conn.cursor())
    conn.close()


def test_import_writes_dat_preset_and_queue(monkeypatch, tmp_path):
    result, data_dir, migrated_chunk = _run_import(monkeypatch, tmp_path)
    assert result == 0

    conn = sqlite3.connect(data_dir / "presets.db")
    row = conn.execute(
        "SELECT hash, author, version, custom FROM presets WHERE name='My Preset'"
    ).fetchone()
    assert row is not None
    h, author, version, custom = row
    assert author == ""
    assert version == cli.CURRENT_DB_VERSION
    assert custom == "time,filter"
    assert conn.execute("SELECT COUNT(*) FROM queue WHERE row_key=?", (h,)).fetchone()[0] == 7
    conn.close()
    assert cli.cas_path(data_dir, h).read_bytes() == migrated_chunk


def test_import_rolls_back_and_removes_dat_on_db_failure(monkeypatch, tmp_path):
    original_insert = cli.insert_preset

    def failing_insert(cur, h, name, version, custom):
        original_insert(cur, h, name, version, custom)
        raise sqlite3.OperationalError("simulated failure")

    monkeypatch.setattr(cli, "insert_preset", failing_insert)
    result, data_dir, migrated_chunk = _run_import(monkeypatch, tmp_path)
    assert result == 5

    h = cli.preset_id(migrated_chunk, "My Preset")
    assert not cli.cas_path(data_dir, h).exists()
    conn = sqlite3.connect(data_dir / "presets.db")
    assert conn.execute("SELECT COUNT(*) FROM presets WHERE name='My Preset'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM queue WHERE row_key=?", (h,)).fetchone()[0] == 0
    conn.close()


def test_repair_tags_updates_db_and_pending_queue(monkeypatch, tmp_path):
    data_dir = tmp_path / "ShaperBox3"
    data_dir.mkdir()
    db_path = data_dir / "presets.db"
    _create_db(db_path)
    h = "a" * 32
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO presets VALUES (?, 'Imported', '', '0', 75, ?)",
        (h, cli.ALL_MODULES),
    )
    conn.execute(
        "INSERT INTO queue VALUES ('new__part', 'presets', ?, '', 'custom', ?, 0)",
        (h, cli.ALL_MODULES),
    )
    conn.commit()
    conn.close()
    dat = cli.cas_path(data_dir, h)
    dat.parent.mkdir(parents=True)
    dat.write_bytes(b"fake state")

    monkeypatch.setattr(cli, "check_daw_running", lambda: [])
    monkeypatch.setattr(cli, "extract_visible_modules", lambda chunk: "time,filter")

    assert cli.repair_preset_tags(data_dir, dry_run=True, skip_backup=True) == 0
    conn = sqlite3.connect(db_path)
    assert (
        conn.execute("SELECT custom FROM presets WHERE hash=?", (h,)).fetchone()[0]
        == cli.ALL_MODULES
    )
    conn.close()

    assert cli.repair_preset_tags(data_dir, skip_backup=True) == 0
    conn = sqlite3.connect(db_path)
    assert (
        conn.execute("SELECT custom FROM presets WHERE hash=?", (h,)).fetchone()[0] == "time,filter"
    )
    assert (
        conn.execute(
            "SELECT value FROM queue WHERE row_key=? AND column='custom'", (h,)
        ).fetchone()[0]
        == "time,filter"
    )
    conn.close()
