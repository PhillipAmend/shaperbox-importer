"""Bulk-import preset packs into Cableguys ShaperBox 3 (macOS).

ShaperBox has no built-in bulk import; you must Load FXP + Save preset for each
one. This script bypasses that by hosting ShaperBox via Pedalboard, letting the
plugin migrate each preset to the current internal format, then writing the
resulting state directly into ShaperBox's local SQLite + content-addressed
storage so the presets appear in the MY PRESETS tab on next launch.

Supports .vstpreset (VST3 preset files) and .fst (FL Studio plugin state
files — the chunk is recovered by scanning for the `#zip#` marker).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from collections import Counter

DATA_DIR = pathlib.Path.home() / "Library/Cableguys/ShaperBox3"
DB_PATH = DATA_DIR / "presets.db"
PLUGIN_PATH = pathlib.Path("/Library/Audio/Plug-Ins/VST3/ShaperBox 3.vst3")

# ShaperBox 3 VST3 class ID (32 ASCII hex chars). Used when wrapping a raw
# chunk in a synthetic .vstpreset to feed back through the plugin's loader.
SHAPERBOX_CID = b"ABCDEF019182FAEB4361626C43474C33"

SUPPORTED_EXTS = (".vstpreset", ".fst")

# Lists every Shaper module — used to populate the `custom` filter column in
# the DB. ShaperBox uses this for the module-tag filter in MY PRESETS;
# mismatches are cosmetic, not blocking, so we use the full set.
ALL_MODULES = "time,drive,noise,filter,pan,reverb,compressor,oscilloscope"

# DB schema version of currently-saved presets. Bump when ShaperBox bumps it.
CURRENT_DB_VERSION = 75

DAW_PROCESS_HINTS = (
    "fl studio",
    "ableton",
    "logic",
    "bitwig",
    "reaper",
    "cubase",
    "studio one",
    "shaperbox",
)


# ---------------------------------------------------------------------------
# Format helpers (pure — easy to unit-test without ShaperBox)
# ---------------------------------------------------------------------------


def find_presets(folder: pathlib.Path) -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for ext in SUPPORTED_EXTS:
        out.extend(folder.rglob(f"*{ext}"))
    return sorted(out)


def extract_chunk_from_fst(fst_bytes: bytes) -> bytes:
    """Scan an .fst file for the embedded ShaperBox `#zip#` chunk and return it."""
    idx = fst_bytes.find(b"#zip#\x00")
    if idx < 0:
        raise ValueError("no #zip# marker in .fst")
    # The zlib stream knows its own length. Decompress to discover where it ends.
    d = zlib.decompressobj()
    payload = fst_bytes[idx + 6 :]
    d.decompress(payload)
    while d.unconsumed_tail:
        d.decompress(d.unconsumed_tail)
    consumed = len(payload) - len(d.unused_data)
    return fst_bytes[idx : idx + 6 + consumed]


def wrap_chunk_as_vst3preset(comp_chunk: bytes) -> bytes:
    """Build a valid VST3 .vstpreset around a ShaperBox `#zip#` chunk.

    Layout: 48-byte header + Comp data + 8-byte zero Cont data + 48-byte
    List trailer naming the two segments.
    """
    cont_chunk = b"\x00" * 8
    list_count = 2
    body_size = len(comp_chunk) + len(cont_chunk) + 4 + 4 + 2 * (4 + 8 + 8)
    header = b"VST3" + struct.pack("<I", 1) + SHAPERBOX_CID + struct.pack("<Q", body_size)
    comp_off = 48
    cont_off = comp_off + len(comp_chunk)
    trailer = (
        b"List"
        + struct.pack("<I", list_count)
        + b"Comp"
        + struct.pack("<Q", comp_off)
        + struct.pack("<Q", len(comp_chunk))
        + b"Cont"
        + struct.pack("<Q", cont_off)
        + struct.pack("<Q", len(cont_chunk))
    )
    return header + comp_chunk + cont_chunk + trailer


def cas_path(data_dir: pathlib.Path, h: str) -> pathlib.Path:
    return data_dir / h[0] / h[1] / f"{h}.dat"


# ---------------------------------------------------------------------------
# Plugin + DB operations
# ---------------------------------------------------------------------------


def check_daw_running() -> list[str]:
    proc = subprocess.run(["ps", "ax", "-o", "comm="], capture_output=True, text=True)
    hits = []
    for line in proc.stdout.splitlines():
        low = line.lower()
        for hint in DAW_PROCESS_HINTS:
            if hint in low and "shaperbox-import" not in low:
                hits.append(line.strip())
                break
    return hits


def backup_data_dir(data_dir: pathlib.Path) -> pathlib.Path:
    if not data_dir.exists():
        raise FileNotFoundError(f"ShaperBox data folder not found: {data_dir}")
    ts = time.strftime("%Y%m%d-%H%M%S")
    dest = data_dir.parent / f"{data_dir.name}.backup-{ts}"
    shutil.copytree(data_dir, dest)
    return dest


def migrate_preset(plugin, src: pathlib.Path) -> bytes:
    """Feed a preset file through ShaperBox's own loader and return the
    migrated `#zip#` chunk (the .dat content)."""
    ext = src.suffix.lower()
    if ext == ".vstpreset":
        plugin.load_preset(str(src))
    elif ext == ".fst":
        chunk = extract_chunk_from_fst(src.read_bytes())
        # Pedalboard's preset_data setter does extra validation and rejects the
        # minimal wrapper; writing a temp .vstpreset and using load_preset works.
        with tempfile.NamedTemporaryFile(suffix=".vstpreset", delete=False) as tmp:
            tmp.write(wrap_chunk_as_vst3preset(chunk))
            tmp_path = tmp.name
        try:
            plugin.load_preset(tmp_path)
        finally:
            os.unlink(tmp_path)
    else:
        raise ValueError(f"unsupported extension: {ext}")
    pd = plugin.preset_data
    if pd[:4] != b"VST3":
        raise RuntimeError(f"unexpected preset_data magic for {src.name}")
    chunk_size = struct.unpack("<Q", pd[40:48])[0]
    chunk = pd[48 : 48 + chunk_size]
    if not chunk.startswith(b"#zip#\x00"):
        raise RuntimeError(f"unexpected chunk header for {src.name}")
    return chunk


def insert_preset(cur: sqlite3.Cursor, h: str, name: str) -> None:
    cur.execute(
        "INSERT INTO presets(hash, name, author, liked, version, custom) "
        "VALUES (?, ?, '', '0', ?, ?)",
        (h, name, CURRENT_DB_VERSION, ALL_MODULES),
    )
    parts = [
        ("new__begin", "", ""),
        ("new__part", "author", ""),
        ("new__part", "custom", ALL_MODULES),
        ("new__part", "liked", "0"),
        ("new__part", "name", name),
        ("new__part", "version", str(CURRENT_DB_VERSION)),
        ("new__end", "", ""),
    ]
    for cmd, col, val in parts:
        cur.execute(
            "INSERT INTO queue VALUES (?, 'presets', ?, '', ?, ?, 0)",
            (cmd, h, col, val),
        )


def existing_names(cur: sqlite3.Cursor, names: list[str]) -> set[str]:
    if not names:
        return set()
    qmarks = ",".join("?" * len(names))
    cur.execute(f"SELECT name FROM presets WHERE name IN ({qmarks})", names)
    return {r[0] for r in cur.fetchall()}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run(
    folder: pathlib.Path,
    dry_run: bool = False,
    skip_backup: bool = False,
    force: bool = False,
    data_dir: pathlib.Path = DATA_DIR,
    plugin_path: pathlib.Path = PLUGIN_PATH,
) -> int:
    if not plugin_path.exists():
        print(f"error: ShaperBox 3.vst3 not found at {plugin_path}", file=sys.stderr)
        return 2

    presets_on_disk = find_presets(folder)
    if not presets_on_disk:
        joined = ", ".join(SUPPORTED_EXTS)
        print(f"no {joined} files found under {folder}", file=sys.stderr)
        return 1
    counts = Counter(p.suffix.lower() for p in presets_on_disk)
    summary = ", ".join(f"{n} {ext}" for ext, n in sorted(counts.items()))
    print(f"found {len(presets_on_disk)} preset file(s) ({summary}) under {folder}")

    running = check_daw_running()
    if running and not force:
        print("\nrefusing to run while a DAW / ShaperBox process is open — the DB may be locked:")
        for p in running:
            print(f"  {p}")
        print("close the DAW or re-run with --force")
        return 3

    db_path = data_dir / "presets.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    names = [p.stem for p in presets_on_disk]
    dupes = existing_names(cur, names)
    todo = [p for p in presets_on_disk if p.stem not in dupes]
    for n in sorted(dupes):
        print(f"  skip (already in DB): {n}")
    if not todo:
        print("nothing to import")
        conn.close()
        return 0

    if dry_run:
        print(f"\n[dry-run] would import {len(todo)} preset(s):")
        for p in todo:
            print(f"  + {p.stem}")
        conn.close()
        return 0

    if not skip_backup:
        print("\nbacking up ShaperBox data folder ...")
        backup = backup_data_dir(data_dir)
        print(f"backup: {backup}")

    print("\nloading ShaperBox 3 via Pedalboard ...")
    try:
        from pedalboard import load_plugin
    except ImportError:
        print(
            "error: pedalboard not installed. run: pip3 install pedalboard",
            file=sys.stderr,
        )
        return 4
    plugin = load_plugin(str(plugin_path))

    print(f"\nimporting {len(todo)} preset(s):")
    imported = 0
    failed: list[tuple[str, str]] = []
    for i, src in enumerate(todo, start=1):
        name = src.stem
        try:
            chunk = migrate_preset(plugin, src)
        except Exception as e:
            failed.append((name, str(e)))
            print(f"  [{i:>4}/{len(todo)}] !  {name}: migration failed ({e})")
            continue
        h = hashlib.md5(chunk + name.encode("utf-8")).hexdigest()
        dat = cas_path(data_dir, h)
        dat.parent.mkdir(parents=True, exist_ok=True)
        dat.write_bytes(chunk)
        insert_preset(cur, h, name)
        imported += 1
        print(f"  [{i:>4}/{len(todo)}] +  {name}  ({len(chunk)} B)")

    conn.commit()
    conn.close()
    print(f"\nimported {imported} preset(s); {len(failed)} failed.")
    if failed:
        print("failures:")
        for name, err in failed:
            print(f"  - {name}: {err}")
    print("open ShaperBox in your DAW to see the new presets in MY PRESETS.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="shaperbox-import",
        description=(
            "Bulk-import .vstpreset and .fst preset files into Cableguys ShaperBox 3 (macOS)."
        ),
    )
    p.add_argument(
        "folder",
        type=pathlib.Path,
        help="folder containing preset files (recursively scanned)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be imported, write nothing",
    )
    p.add_argument(
        "--no-backup",
        action="store_true",
        help="skip the auto-backup of the Cableguys data folder",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="proceed even if a DAW is running (risks DB lock)",
    )
    p.add_argument(
        "--version",
        action="version",
        version=_version_string(),
    )
    return p


def _version_string() -> str:
    from . import __version__

    return f"%(prog)s {__version__}"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return run(
        folder=args.folder.expanduser().resolve(),
        dry_run=args.dry_run,
        skip_backup=args.no_backup,
        force=args.force,
    )


if __name__ == "__main__":
    raise SystemExit(main())
