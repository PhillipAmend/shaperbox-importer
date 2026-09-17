"""Bulk-import preset packs into Cableguys ShaperBox 3 (macOS and Windows).

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
import csv
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
from contextlib import suppress

MACOS_DATA_DIR = pathlib.Path.home() / "Library/Cableguys/ShaperBox3"
MACOS_PLUGIN_PATH = pathlib.Path("/Library/Audio/Plug-Ins/VST3/ShaperBox 3.vst3")

# ShaperBox 3 VST3 class ID (32 ASCII hex chars). Used when wrapping a raw
# chunk in a synthetic .vstpreset to feed back through the plugin's loader.
SHAPERBOX_CID = b"ABCDEF019182FAEB4361626C43474C33"

SUPPORTED_EXTS = (".vstpreset", ".fst")

MODULE_ORDER = (
    "time",
    "pitch",
    "drive",
    "noise",
    "filter",
    "liquid",
    "crush",
    "volume",
    "pan",
    "reverb",
    "width",
    "compressor",
    "oscilloscope",
)
ALL_MODULES = ",".join(MODULE_ORDER)

# State IDs mostly match DB tags. DynamicsShaper is called "compressor" by the
# preset browser, while its internal ValueTree state ID is "dynamics".
MODULE_TAG_BY_STATE_ID = {module: module for module in MODULE_ORDER}
MODULE_TAG_BY_STATE_ID["dynamics"] = "compressor"
del MODULE_TAG_BY_STATE_ID["compressor"]

# DB schema version of currently-saved presets. Bump when ShaperBox bumps it.
CURRENT_DB_VERSION = 75

DAW_PROCESS_HINTS = (
    "fl studio",
    "fl64.exe",
    "fl.exe",
    "ableton",
    "logic",
    "bitwig",
    "reaper",
    "cubase",
    "studio one",
    "shaperbox",
)

REQUIRED_DB_COLUMNS = {
    "presets": {"hash", "name", "author", "liked", "version", "custom"},
    "queue": {"command", "table_name", "row_key", "row_subkey", "column", "value", "state"},
}


# ---------------------------------------------------------------------------
# Format helpers (pure — easy to unit-test without ShaperBox)
# ---------------------------------------------------------------------------


def default_data_dir() -> pathlib.Path:
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return pathlib.Path(appdata) / "Cableguys/ShaperBox3"
        return pathlib.Path.home() / "AppData/Roaming/Cableguys/ShaperBox3"
    return MACOS_DATA_DIR


def default_plugin_path() -> pathlib.Path:
    if sys.platform == "win32":
        common_program_files = os.environ.get("COMMONPROGRAMFILES")
        if common_program_files:
            return pathlib.Path(common_program_files) / "VST3/ShaperBox 3.vst3"
        program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        return pathlib.Path(program_files) / "Common Files/VST3/ShaperBox 3.vst3"
    return MACOS_PLUGIN_PATH


def find_presets(folder: pathlib.Path) -> list[pathlib.Path]:
    return sorted(
        p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS
    )


def extract_chunk_from_fst(fst_bytes: bytes) -> bytes:
    """Scan an .fst file for the embedded ShaperBox `#zip#` chunk and return it."""
    idx = fst_bytes.find(b"#zip#\x00")
    if idx < 0:
        raise ValueError("no #zip# marker in .fst")
    # The zlib stream knows its own length. Decompress to discover where it ends.
    payload = fst_bytes[idx + 6 :]
    d = zlib.decompressobj()
    try:
        d.decompress(payload)
        d.flush()
    except zlib.error as exc:
        raise ValueError("invalid zlib stream after #zip# marker") from exc
    if not d.eof:
        raise ValueError("truncated zlib stream after #zip# marker")
    consumed = len(payload) - len(d.unused_data)
    return fst_bytes[idx : idx + 6 + consumed]


def wrap_chunk_as_vst3preset(comp_chunk: bytes) -> bytes:
    """Build a valid VST3 .vstpreset around a ShaperBox `#zip#` chunk.

    Layout: 48-byte header + Comp data + 8-byte zero Cont data + 48-byte
    List trailer naming the two segments.
    """
    cont_chunk = b"\x00" * 8
    list_count = 2
    comp_off = 48
    cont_off = comp_off + len(comp_chunk)
    list_off = cont_off + len(cont_chunk)
    header = b"VST3" + struct.pack("<I", 1) + SHAPERBOX_CID + struct.pack("<Q", list_off)
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


def extract_comp_chunk_from_vst3preset(preset_data: bytes) -> bytes:
    """Extract the ShaperBox component state from a VST3 preset container."""
    if len(preset_data) < 56:
        raise ValueError("VST3 preset is too short")
    if preset_data[:4] != b"VST3":
        raise ValueError("unexpected VST3 preset magic")
    if preset_data[8:40] != SHAPERBOX_CID:
        raise ValueError("unexpected ShaperBox VST3 class ID")

    list_off = struct.unpack_from("<Q", preset_data, 40)[0]
    if list_off < 48 or list_off + 8 > len(preset_data):
        raise ValueError("invalid VST3 chunk-list offset")
    if preset_data[list_off : list_off + 4] != b"List":
        raise ValueError("VST3 chunk list not found")

    count = struct.unpack_from("<I", preset_data, list_off + 4)[0]
    entries_off = list_off + 8
    if count > (len(preset_data) - entries_off) // 20:
        raise ValueError("truncated VST3 chunk list")

    for index in range(count):
        entry_off = entries_off + index * 20
        chunk_id = preset_data[entry_off : entry_off + 4]
        chunk_off, chunk_size = struct.unpack_from("<QQ", preset_data, entry_off + 4)
        chunk_end = chunk_off + chunk_size
        if chunk_off < 48 or chunk_end > list_off:
            raise ValueError(f"invalid {chunk_id!r} VST3 chunk bounds")
        if chunk_id == b"Comp":
            chunk = preset_data[chunk_off:chunk_end]
            if not chunk.startswith(b"#zip#\x00"):
                raise ValueError("unexpected ShaperBox component-state header")
            return chunk
    raise ValueError("VST3 preset has no Comp chunk")


class _JuceValueTreeReader:
    """Minimal reader for the JUCE ValueTree stream stored by ShaperBox."""

    def __init__(self, data: bytes):
        self.data = data
        self.position = 0

    def _read(self, size: int) -> bytes:
        end = self.position + size
        if size < 0 or end > len(self.data):
            raise ValueError("truncated JUCE ValueTree")
        result = self.data[self.position : end]
        self.position = end
        return result

    def _read_cstring(self) -> str:
        try:
            end = self.data.index(0, self.position)
        except ValueError as exc:
            raise ValueError("unterminated JUCE string") from exc
        raw = self.data[self.position : end]
        self.position = end + 1
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("invalid UTF-8 in JUCE ValueTree") from exc

    def _read_compressed_int(self) -> int:
        size_byte = self._read(1)[0]
        size = size_byte & 0x7F
        if size > 4:
            raise ValueError("invalid JUCE compressed integer")
        value = int.from_bytes(self._read(size), "little") if size else 0
        return -value if size_byte & 0x80 else value

    def _read_var(self) -> object:
        size = self._read_compressed_int()
        if size <= 0:
            return None
        end = self.position + size
        marker = self._read(1)[0]
        if marker == 1:
            value: object = struct.unpack("<i", self._read(4))[0]
        elif marker == 2:
            value = True
        elif marker == 3:
            value = False
        elif marker == 4:
            value = struct.unpack("<d", self._read(8))[0]
        elif marker == 5:
            raw = self._read(size - 1)
            try:
                value = raw.rstrip(b"\x00").decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("invalid JUCE variant string") from exc
        elif marker == 6:
            value = struct.unpack("<q", self._read(8))[0]
        elif marker == 7:
            count = self._read_count("variant array")
            value = [self._read_var() for _ in range(count)]
        elif marker == 8:
            value = self._read(size - 1)
        elif marker == 9:
            value = None
        else:
            self.position = end
            return None
        if self.position != end:
            raise ValueError("invalid JUCE variant size")
        return value

    def _skip_var(self) -> None:
        size = self._read_compressed_int()
        if size < 0:
            raise ValueError("invalid JUCE variant size")
        self._read(size)

    def _read_count(self, label: str) -> int:
        count = self._read_compressed_int()
        if count < 0 or count > 100_000:
            raise ValueError(f"invalid JUCE {label} count")
        return count

    def read_tree_header(self) -> tuple[str, dict[str, object], int]:
        node_type = self._read_cstring()
        property_count = self._read_count("property")
        properties = {self._read_cstring(): self._read_var() for _ in range(property_count)}
        return node_type, properties, self._read_count("child")

    def skip_tree(self, depth: int = 0) -> None:
        if depth > 128:
            raise ValueError("JUCE ValueTree nesting is too deep")
        self._read_cstring()
        property_count = self._read_count("property")
        for _ in range(property_count):
            self._read_cstring()
            self._skip_var()
        child_count = self._read_count("child")
        for _ in range(child_count):
            self.skip_tree(depth + 1)


def extract_visible_modules(chunk: bytes) -> str:
    """Return the DB module tags for Shapers visible in the preset's UI."""
    if not chunk.startswith(b"#zip#\x00"):
        raise ValueError("unexpected ShaperBox state header")
    try:
        state = zlib.decompress(chunk[6:])
    except zlib.error as exc:
        raise ValueError("invalid compressed ShaperBox state") from exc

    reader = _JuceValueTreeReader(state)
    root_type, _, root_child_count = reader.read_tree_header()
    if root_type != "PluginState":
        raise ValueError("invalid ShaperBox PluginState ValueTree")

    visible: set[str] = set()
    for _ in range(root_child_count):
        _, properties, child_count = reader.read_tree_header()
        state_id = properties.get("id")
        tag = MODULE_TAG_BY_STATE_ID.get(state_id) if isinstance(state_id, str) else None
        for _ in range(child_count):
            _, child_properties, grandchild_count = reader.read_tree_header()
            if (
                tag is not None
                and child_properties.get("id") == "VISIBLE"
                and child_properties.get("value") is True
            ):
                visible.add(tag)
            for _ in range(grandchild_count):
                reader.skip_tree()
    if reader.position != len(state):
        raise ValueError("invalid ShaperBox PluginState ValueTree")
    return ",".join(module for module in MODULE_ORDER if module in visible)


def cas_path(data_dir: pathlib.Path, h: str) -> pathlib.Path:
    return data_dir / h[0] / h[1] / f"{h}.dat"


# ---------------------------------------------------------------------------
# Plugin + DB operations
# ---------------------------------------------------------------------------


def check_daw_running() -> list[str]:
    if sys.platform == "win32":
        command = ["tasklist", "/FO", "CSV", "/NH"]
    else:
        command = ["ps", "ax", "-o", "comm="]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise RuntimeError(f"could not list running processes: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or f"exit code {proc.returncode}"
        raise RuntimeError(f"could not list running processes: {detail}")

    if sys.platform == "win32":
        lines = [row[0] for row in csv.reader(proc.stdout.splitlines()) if row]
    else:
        lines = proc.stdout.splitlines()
    hits = []
    for line in lines:
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
    try:
        return extract_comp_chunk_from_vst3preset(plugin.preset_data)
    except ValueError as exc:
        raise RuntimeError(f"invalid migrated state for {src.name}: {exc}") from exc


def insert_preset(cur: sqlite3.Cursor, h: str, name: str, version: int, custom: str) -> None:
    cur.execute(
        "INSERT INTO presets(hash, name, author, liked, version, custom) "
        "VALUES (?, ?, '', '0', ?, ?)",
        (h, name, version, custom),
    )
    parts = [
        ("new__begin", "", ""),
        ("new__part", "author", ""),
        ("new__part", "custom", custom),
        ("new__part", "liked", "0"),
        ("new__part", "name", name),
        ("new__part", "version", str(version)),
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
    found: set[str] = set()
    for start in range(0, len(names), 500):
        batch = names[start : start + 500]
        qmarks = ",".join("?" * len(batch))
        cur.execute(f"SELECT name FROM presets WHERE name IN ({qmarks})", batch)
        found.update(r[0] for r in cur.fetchall())
    return found


def validate_db(cur: sqlite3.Cursor) -> int:
    for table, expected_columns in REQUIRED_DB_COLUMNS.items():
        columns = {row[1] for row in cur.execute(f'PRAGMA table_info("{table}")')}
        missing = expected_columns - columns
        if missing:
            joined = ", ".join(sorted(missing))
            raise RuntimeError(f"incompatible presets.db: {table} is missing {joined}")

    row = cur.execute("SELECT MAX(CAST(version AS INTEGER)) FROM presets").fetchone()
    highest_version = row[0] if row and row[0] is not None else 0
    if highest_version > CURRENT_DB_VERSION:
        raise RuntimeError(
            f"presets.db contains version {highest_version}, newer than supported "
            f"version {CURRENT_DB_VERSION}"
        )
    return CURRENT_DB_VERSION


def _open_db_read_only(db_path: pathlib.Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)


def preset_id(chunk: bytes, name: str) -> str:
    return hashlib.md5(chunk + name.encode("utf-8"), usedforsecurity=False).hexdigest()


def write_dat_file(data_dir: pathlib.Path, h: str, chunk: bytes) -> pathlib.Path | None:
    """Write a new state file atomically; return its path, or None if it existed."""
    dat = cas_path(data_dir, h)
    dat.parent.mkdir(parents=True, exist_ok=True)
    if dat.exists():
        if dat.read_bytes() != chunk:
            raise RuntimeError(f"preset ID collision for {h}")
        return None

    tmp_path: pathlib.Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=dat.parent, prefix=f".{h}.", suffix=".tmp", delete=False
        ) as tmp:
            tmp.write(chunk)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = pathlib.Path(tmp.name)
        os.replace(tmp_path, dat)
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()
    return dat


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def process_check_passes(force: bool) -> bool:
    running: list[str] = []
    try:
        running = check_daw_running()
    except RuntimeError as exc:
        if not force:
            print(f"error: {exc}; re-run with --force to bypass this check", file=sys.stderr)
            return False
        print(f"warning: {exc}", file=sys.stderr)
    if running and not force:
        print("\nrefusing to run while a DAW / ShaperBox process is open — the DB may be locked:")
        for process in running:
            print(f"  {process}")
        print("close the DAW or re-run with --force")
        return False
    return True


def repair_preset_tags(
    data_dir: pathlib.Path | None = None,
    dry_run: bool = False,
    skip_backup: bool = False,
    force: bool = False,
) -> int:
    """Recalculate module tags for existing user presets without migrating them."""
    data_dir = default_data_dir() if data_dir is None else data_dir
    if not process_check_passes(force):
        return 3

    db_path = data_dir / "presets.db"
    if not db_path.is_file():
        print(f"error: ShaperBox presets.db not found at {db_path}", file=sys.stderr)
        return 5

    repairs: list[tuple[str, str, str]] = []
    failed: list[tuple[str, str]] = []
    try:
        read_conn = _open_db_read_only(db_path)
        try:
            validate_db(read_conn.cursor())
            rows = read_conn.execute(
                "SELECT hash, name, custom FROM presets WHERE author=''"
            ).fetchall()
        finally:
            read_conn.close()
        for h, name, old_custom in rows:
            dat = cas_path(data_dir, h)
            try:
                custom = extract_visible_modules(dat.read_bytes())
            except (OSError, ValueError) as exc:
                failed.append((name, str(exc)))
                continue
            if custom != old_custom:
                repairs.append((h, name, custom))
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        print(f"error: could not inspect presets.db: {exc}", file=sys.stderr)
        return 5

    print(f"found {len(repairs)} preset tag(s) to repair; {len(failed)} unreadable")
    if dry_run:
        for _, name, custom in repairs:
            print(f"  ~  {name}: {custom or 'no visible modules'}")
        return 0
    if not repairs:
        return 0 if not failed else 6

    if not skip_backup:
        print("\nbacking up ShaperBox data folder ...")
        try:
            backup = backup_data_dir(data_dir)
        except OSError as exc:
            print(f"error: backup failed: {exc}", file=sys.stderr)
            return 5
        print(f"backup: {backup}")

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        cur = conn.cursor()
        validate_db(cur)
        cur.execute("BEGIN IMMEDIATE")
        for h, _, custom in repairs:
            cur.execute("UPDATE presets SET custom=? WHERE hash=? AND author=''", (custom, h))
            # Keep unsynchronised new-preset queue entries consistent with the DB.
            cur.execute(
                "UPDATE queue SET value=? WHERE table_name='presets' AND row_key=? "
                "AND column='custom'",
                (custom, h),
            )
        conn.commit()
    except (sqlite3.Error, RuntimeError) as exc:
        if conn is not None:
            conn.rollback()
        print(f"error: tag repair rolled back: {exc}", file=sys.stderr)
        return 5
    finally:
        if conn is not None:
            conn.close()

    print(f"repaired module tags for {len(repairs)} preset(s)")
    if failed:
        print("unreadable presets:")
        for name, error in failed:
            print(f"  - {name}: {error}")
    return 0


def run(
    folder: pathlib.Path,
    dry_run: bool = False,
    skip_backup: bool = False,
    force: bool = False,
    data_dir: pathlib.Path | None = None,
    plugin_path: pathlib.Path | None = None,
) -> int:
    data_dir = default_data_dir() if data_dir is None else data_dir
    plugin_path = default_plugin_path() if plugin_path is None else plugin_path

    if not folder.is_dir():
        print(f"error: preset folder not found: {folder}", file=sys.stderr)
        return 1
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

    if not process_check_passes(force):
        return 3

    db_path = data_dir / "presets.db"
    if not db_path.is_file():
        print(f"error: ShaperBox presets.db not found at {db_path}", file=sys.stderr)
        return 5
    names = [p.stem for p in presets_on_disk]
    try:
        read_conn = _open_db_read_only(db_path)
        try:
            validate_db(read_conn.cursor())
            dupes = existing_names(read_conn.cursor(), names)
        finally:
            read_conn.close()
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        print(f"error: could not validate presets.db: {exc}", file=sys.stderr)
        return 5
    todo: list[pathlib.Path] = []
    seen_names = set(dupes)
    duplicate_files: list[pathlib.Path] = []
    for path in presets_on_disk:
        if path.stem in seen_names:
            if path.stem not in dupes:
                duplicate_files.append(path)
            continue
        seen_names.add(path.stem)
        todo.append(path)
    for n in sorted(dupes):
        print(f"  skip (already in DB): {n}")
    for path in duplicate_files:
        print(f"  skip (duplicate input name): {path}")
    if not todo:
        print("nothing to import")
        return 0

    if dry_run:
        print(f"\n[dry-run] would import {len(todo)} preset(s):")
        for p in todo:
            print(f"  + {p.stem}")
        return 0

    print("\nloading ShaperBox 3 via Pedalboard ...")
    try:
        from pedalboard import load_plugin
    except ImportError:
        print(
            "error: pedalboard not installed. run: pip3 install pedalboard",
            file=sys.stderr,
        )
        return 4
    try:
        plugin = load_plugin(str(plugin_path))
    except Exception as exc:
        print(f"error: could not load ShaperBox 3: {exc}", file=sys.stderr)
        return 4

    print(f"\nmigrating {len(todo)} preset(s):")
    failed: list[tuple[str, str]] = []
    migrated: list[tuple[str, bytes, str, str]] = []
    for i, src in enumerate(todo, start=1):
        name = src.stem
        try:
            chunk = migrate_preset(plugin, src)
        except Exception as e:
            failed.append((name, str(e)))
            print(f"  [{i:>4}/{len(todo)}] !  {name}: migration failed ({e})")
            continue
        h = preset_id(chunk, name)
        try:
            custom = extract_visible_modules(chunk)
        except ValueError as exc:
            failed.append((name, str(exc)))
            print(f"  [{i:>4}/{len(todo)}] !  {name}: tag detection failed ({exc})")
            continue
        migrated.append((name, chunk, h, custom))
        tags = custom or "no visible modules"
        print(f"  [{i:>4}/{len(todo)}] ready  {name}  ({tags}; {len(chunk)} B)")

    if not migrated:
        print(f"\nimported 0 preset(s); {len(failed)} failed.")
        return 6

    if not skip_backup:
        print("\nbacking up ShaperBox data folder ...")
        try:
            backup = backup_data_dir(data_dir)
        except OSError as exc:
            print(f"error: backup failed: {exc}", file=sys.stderr)
            return 5
        print(f"backup: {backup}")

    print(f"\nwriting {len(migrated)} preset(s):")
    conn: sqlite3.Connection | None = None
    created_files: list[pathlib.Path] = []
    imported = 0
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        cur = conn.cursor()
        version = validate_db(cur)
        cur.execute("BEGIN IMMEDIATE")
        now_existing = existing_names(cur, [name for name, _, _, _ in migrated])
        for name, chunk, h, custom in migrated:
            if name in now_existing:
                print(f"  skip (added while importing): {name}")
                continue
            created = write_dat_file(data_dir, h, chunk)
            if created is not None:
                created_files.append(created)
            insert_preset(cur, h, name, version, custom)
            imported += 1
            print(f"  +  {name}")
        conn.commit()
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        if conn is not None:
            conn.rollback()
        for path in created_files:
            with suppress(OSError):
                path.unlink(missing_ok=True)
        print(f"error: import rolled back: {exc}", file=sys.stderr)
        return 5
    finally:
        if conn is not None:
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
        description=("Bulk-import .vstpreset and .fst preset files into Cableguys ShaperBox 3."),
    )
    p.add_argument(
        "folder",
        nargs="?",
        type=pathlib.Path,
        help="folder containing preset files (required unless --repair-tags is used)",
    )
    p.add_argument(
        "--data-dir",
        type=pathlib.Path,
        default=default_data_dir(),
        help="ShaperBox 3 data folder (default: platform-specific location)",
    )
    p.add_argument(
        "--plugin-path",
        type=pathlib.Path,
        default=default_plugin_path(),
        help="ShaperBox 3 VST3 path (default: platform-specific location)",
    )
    p.add_argument(
        "--repair-tags",
        action="store_true",
        help="recalculate effect tags for existing user presets without re-importing",
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


def configure_console_output() -> None:
    """Prevent unencodable preset names from aborting Windows CLI output."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="backslashreplace")


def main(argv: list[str] | None = None) -> int:
    configure_console_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    data_dir = args.data_dir.expanduser().resolve()
    if args.repair_tags:
        return repair_preset_tags(
            data_dir=data_dir,
            dry_run=args.dry_run,
            skip_backup=args.no_backup,
            force=args.force,
        )
    if args.folder is None:
        parser.error("folder is required unless --repair-tags is used")
    return run(
        folder=args.folder.expanduser().resolve(),
        dry_run=args.dry_run,
        skip_backup=args.no_backup,
        force=args.force,
        data_dir=data_dir,
        plugin_path=args.plugin_path.expanduser().resolve(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
