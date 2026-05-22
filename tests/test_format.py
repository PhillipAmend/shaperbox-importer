"""Unit tests for the pure format helpers.

These tests deliberately avoid importing pedalboard or touching ShaperBox so
they can run in CI without the plugin or a license.
"""

from __future__ import annotations

import struct
import zlib

import pytest

from shaperbox_importer.cli import (
    SHAPERBOX_CID,
    cas_path,
    extract_chunk_from_fst,
    find_presets,
    wrap_chunk_as_vst3preset,
)


def _make_chunk(payload: bytes = b"hello world") -> bytes:
    """Build a fake `#zip#` chunk from arbitrary payload."""
    return b"#zip#\x00" + zlib.compress(payload, 9)


class TestExtractChunkFromFst:
    def test_extracts_chunk_after_arbitrary_prefix(self):
        chunk = _make_chunk(b"x" * 200)
        fst = b"FLhd\x06\x00\x00\x00" + b"\x00" * 32 + chunk + b"trailing junk"
        recovered = extract_chunk_from_fst(fst)
        assert recovered == chunk

    def test_recovers_original_payload(self):
        original = b"PluginState\x00\x01\x03version" * 50
        chunk = _make_chunk(original)
        fst = b"FLhd" + b"\xaa" * 20 + chunk + b"more bytes"
        recovered = extract_chunk_from_fst(fst)
        assert zlib.decompress(recovered[6:]) == original

    def test_raises_when_marker_missing(self):
        with pytest.raises(ValueError, match="no #zip# marker"):
            extract_chunk_from_fst(b"FLhd no chunk here at all")


class TestWrapChunkAsVst3preset:
    def test_header_magic_and_cid(self):
        wrapped = wrap_chunk_as_vst3preset(_make_chunk())
        assert wrapped[:4] == b"VST3"
        assert struct.unpack("<I", wrapped[4:8])[0] == 1
        assert wrapped[8:40] == SHAPERBOX_CID

    def test_declared_body_size_matches_actual(self):
        chunk = _make_chunk(b"y" * 500)
        wrapped = wrap_chunk_as_vst3preset(chunk)
        declared = struct.unpack("<Q", wrapped[40:48])[0]
        assert declared == len(wrapped) - 48

    def test_list_trailer_lists_comp_and_cont(self):
        chunk = _make_chunk()
        wrapped = wrap_chunk_as_vst3preset(chunk)
        # Trailer = 48 bytes at end: "List" + count + 2 entries
        trailer = wrapped[-48:]
        assert trailer[:4] == b"List"
        count = struct.unpack("<I", trailer[4:8])[0]
        assert count == 2
        assert trailer[8:12] == b"Comp"
        comp_off = struct.unpack("<Q", trailer[12:20])[0]
        comp_sz = struct.unpack("<Q", trailer[20:28])[0]
        assert comp_off == 48
        assert comp_sz == len(chunk)
        assert trailer[28:32] == b"Cont"
        cont_off = struct.unpack("<Q", trailer[32:40])[0]
        cont_sz = struct.unpack("<Q", trailer[40:48])[0]
        assert cont_off == 48 + len(chunk)
        assert cont_sz == 8


class TestFindPresets:
    def test_finds_vstpreset_and_fst_recursively(self, tmp_path):
        (tmp_path / "a.vstpreset").write_bytes(b"")
        (tmp_path / "b.fst").write_bytes(b"")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "c.vstpreset").write_bytes(b"")
        (sub / "d.txt").write_bytes(b"")  # unsupported
        out = find_presets(tmp_path)
        names = sorted(p.name for p in out)
        assert names == ["a.vstpreset", "b.fst", "c.vstpreset"]

    def test_returns_empty_for_empty_dir(self, tmp_path):
        assert find_presets(tmp_path) == []


class TestCasPath:
    def test_two_level_hex_prefix(self, tmp_path):
        h = "abcdef0123456789" * 2
        p = cas_path(tmp_path, h)
        assert p == tmp_path / "a" / "b" / f"{h}.dat"
