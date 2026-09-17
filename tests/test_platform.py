"""Tests for platform-specific paths and process detection."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from shaperbox_importer import cli


def test_windows_default_paths(monkeypatch, tmp_path):
    appdata = tmp_path / "Roaming"
    common = tmp_path / "Common Files"
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("CommonProgramFiles", str(common))

    assert cli.default_data_dir() == appdata / "Cableguys/ShaperBox3"
    assert cli.default_plugin_path() == common / "VST3/ShaperBox 3.vst3"


def test_windows_process_detection_uses_tasklist(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    result = SimpleNamespace(
        returncode=0,
        stdout='"explorer.exe","1","Console","1","10 K"\n"FL64.exe","2","Console","1","20 K"\n',
        stderr="",
    )

    def fake_run(command, **kwargs):
        assert command == ["tasklist", "/FO", "CSV", "/NH"]
        return result

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli.check_daw_running() == ["FL64.exe"]


def test_process_detection_failure_is_reported(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    result = SimpleNamespace(returncode=1, stdout="", stderr="access denied")
    monkeypatch.setattr(cli.subprocess, "run", lambda *args, **kwargs: result)

    with pytest.raises(RuntimeError, match="access denied"):
        cli.check_daw_running()


def test_force_continues_when_process_detection_fails(monkeypatch, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "preset.fst").write_bytes(b"not read during preflight")
    plugin_path = tmp_path / "ShaperBox 3.vst3"
    plugin_path.write_bytes(b"fake")
    data_dir = tmp_path / "missing-data-dir"
    monkeypatch.setattr(
        cli, "check_daw_running", lambda: (_ for _ in ()).throw(RuntimeError("unavailable"))
    )

    assert cli.run(source, force=True, data_dir=data_dir, plugin_path=plugin_path) == 5


def test_console_output_uses_safe_encoding_errors(monkeypatch):
    stdout = Mock()
    stderr = Mock()
    monkeypatch.setattr(cli.sys, "stdout", stdout)
    monkeypatch.setattr(cli.sys, "stderr", stderr)

    cli.configure_console_output()

    stdout.reconfigure.assert_called_once_with(errors="backslashreplace")
    stderr.reconfigure.assert_called_once_with(errors="backslashreplace")
