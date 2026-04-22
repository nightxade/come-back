#!/usr/bin/env python3
"""Decompile compiled Go binaries via Ghidra headless (PyGhidra).

Reads metadata.json to discover successfully compiled binaries, then uses
PyGhidra's modern API (open_project / program_context / analyze) to run
Ghidra's decompiler on each one.  Produces one .c file per binary under
data/decomps/<owner__repo>/<variant>/<binary>.c
"""

import argparse
import atexit
import json
import multiprocessing as mp
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from proj261.util import DATA_DIR, METADATA_PATH, BINARIES_DIR, DECOMPS_DIR, safe_name

from tqdm import tqdm

# --------------------------------------------------------------------------- #
#  Configuration
# --------------------------------------------------------------------------- #

GHIDRA_INSTALL = Path(os.environ.get("GHIDRA_INSTALL_DIR", "/opt/ghidra"))

MAX_STRING_DISPLAY_LEN = 200   # truncate long packed strings in annotations

# Regex to find string-related symbol references in decompiled C code.
_STRING_SYM_RE = re.compile(r"\b((?:PTR_)?s_[A-Za-z0-9_]+)\b")

# Regex to extract the trailing hex address from a Ghidra auto-generated label.
# e.g. PTR_s_systemStringFormat__bytestringAc_007e0ce0 -> 007e0ce0
_HEX_ADDR_SUFFIX_RE = re.compile(r"_([0-9a-fA-F]{6,16})$")

# Regex to find DAT_<hex> symbols (Ghidra labels for unclassified data).
_DAT_SYM_RE = re.compile(r"\b(DAT_[0-9a-fA-F]{6,16})\b")

# Regex to find raw hex address constants (5-16 hex digits).
# Excludes negated values (preceded by '-') and identifiers.
_HEX_LITERAL_RE = re.compile(r"(?<![\w-])(0x[0-9a-fA-F]{5,16})\b")

# Regex to match local_XX = VALUE; assignments (for length pairing).
_LOCAL_ASSIGN_RE = re.compile(
    r"\b(local_[0-9a-fA-F]+)\s*=\s*(0x[0-9a-fA-F]+|\d+)\s*;"
)

# Minimum string length for heuristic (DAT / hex) resolution.
_MIN_STRING_LEN = 4


def load_metadata() -> dict:
    return json.loads(METADATA_PATH.read_text())


# --------------------------------------------------------------------------- #
#  GoReSym integration (stripped Go binary symbol recovery)
# --------------------------------------------------------------------------- #

def run_goresym(binary_path: Path) -> dict | None:
    """Run GoReSym on a binary and return parsed JSON, or None on failure."""
    try:
        result = subprocess.run(
            ["goresym", "-d", "-p", str(binary_path)],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        tqdm.write("    WARNING: goresym not found on PATH; skipping symbol recovery")
        return None
    except subprocess.TimeoutExpired:
        tqdm.write("    WARNING: goresym timed out; skipping symbol recovery")
        return None

    if result.returncode != 0:
        tqdm.write(f"    WARNING: goresym exited with code {result.returncode}; skipping symbol recovery")
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        tqdm.write("    WARNING: goresym produced invalid JSON; skipping symbol recovery")
        return None


def get_module_path(binary_path: Path) -> str | None:
    """Extract the Go module path from a compiled binary via `go version -m`."""
    try:
        r = subprocess.run(
            ["go", "version", "-m", str(binary_path)],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "mod":
            return parts[1]
    return None


def is_user_func(name: str, module_path: str) -> bool:
    """Check if a function name belongs to the user's module."""
    if name.startswith("main."):
        return True
    return name.startswith(module_path + "/") or name.startswith(module_path + ".")


def _sanitize_ghidra_name(name: str) -> str:
    """Replace characters that Ghidra rejects in symbol names.

    Go generic type parameters produce names like
    ``HashTrieMap[go.shape.interface {},go.shape.interface {}]``
    which contain ``[]{}`` — invalid for Ghidra symbols.
    """
    return (name
            .replace("[", "<")
            .replace("]", ">")
            .replace("{", "(")
            .replace("}", ")")
            .replace(" ", "_"))


def inject_goresym_symbols(program, goresym_data: dict) -> tuple[int, int]:
    """Inject GoReSym-recovered symbols into a Ghidra program.

    Iterates UserFunctions and StdFunctions from GoReSym JSON. For each:
    - If a function exists at the address with a FUN_ name, rename it.
    - If no function exists, create one with the correct boundaries.

    Must run inside an explicit transaction since this modifies the program
    database before Ghidra's auto-analysis.

    Returns (renamed_count, created_count).
    """
    from ghidra.program.model.symbol import SourceType
    from ghidra.program.model.address import AddressSet

    addr_space = program.getAddressFactory().getDefaultAddressSpace()
    func_mgr = program.getFunctionManager()

    renamed = 0
    created = 0
    errors = 0

    all_funcs = []
    for key in ("UserFunctions", "StdFunctions"):
        all_funcs.extend(goresym_data.get(key, []) or [])

    tid = program.startTransaction("GoReSym symbol injection")
    try:
        for entry in all_funcs:
            try:
                name = entry.get("FullName") or entry.get("PackageName", "")
                start = entry.get("Start")
                end = entry.get("End")
                if not name or start is None:
                    continue
                name = _sanitize_ghidra_name(name)

                start_addr = addr_space.getAddress(start)
                existing = func_mgr.getFunctionAt(start_addr)

                if existing is not None:
                    if existing.getName().startswith("FUN_"):
                        existing.setName(name, SourceType.USER_DEFINED)
                        renamed += 1
                else:
                    if end is not None and end > start:
                        end_addr = addr_space.getAddress(end - 1)  # GoReSym End is exclusive
                        body = AddressSet(start_addr, end_addr)
                        func_mgr.createFunction(name, start_addr, body, SourceType.USER_DEFINED)
                        created += 1
            except Exception as e:
                errors += 1
                if errors <= 3:
                    tqdm.write(f"    GoReSym inject error: {e}")
                continue
    finally:
        program.endTransaction(tid, True)

    if errors > 3:
        tqdm.write(f"    GoReSym: ... and {errors - 3} more errors")

    return renamed, created


# --------------------------------------------------------------------------- #
#  String data collection
# --------------------------------------------------------------------------- #

def _read_le64(mem, addr):
    """Read an 8-byte little-endian integer from program memory."""
    val = 0
    for i in range(8):
        b = mem.getByte(addr.add(i)) & 0xFF
        val |= b << (i * 8)
    return val


def _read_string_at(mem, addr, max_len=MAX_STRING_DISPLAY_LEN):
    """Read a run of printable ASCII from program memory at *addr*.

    Go stores string literals as packed byte runs in .rodata; this reads
    from a given address until it hits a non-printable byte or *max_len*.
    """
    chars = []
    try:
        for i in range(max_len):
            b = mem.getByte(addr.add(i)) & 0xFF
            if b < 0x20 or b > 0x7E:
                break
            chars.append(chr(b))
    except Exception:
        pass
    return "".join(chars) if chars else None


def _read_string_with_len(mem, addr, length):
    """Read exactly *length* bytes from memory; return string if all printable.

    Unlike :func:`_read_string_at` which reads until a non-printable byte,
    this uses the Go string length to properly delimit the value — essential
    for packed string blocks where multiple strings are contiguous.
    """
    chars = []
    try:
        for i in range(length):
            b = mem.getByte(addr.add(i)) & 0xFF
            if b >= 0x20 and b <= 0x7E:
                chars.append(chr(b))
            elif b in (0x09, 0x0A, 0x0D):  # tab, newline, carriage return
                chars.append(chr(b))
            else:
                return None
    except Exception:
        return None
    return "".join(chars) if chars else None


# Maximum number of Go string (ptr, len) pairs to scan at a PTR_s_* address.
_MAX_STRING_ARRAY_ENTRIES = 64
# Stop scanning after this many consecutive non-string pairs.
_MAX_CONSECUTIVE_FAILS = 8


def collect_string_data(program):
    """Build ``{symbol_name: string_value}`` for all string data in *program*.

    Collects two kinds of items from Ghidra's listing:

    * Direct string data (``s_*`` symbols) — the value is read directly.
    * Pointer-to-string data (``PTR_s_*`` symbols) — the pointer target
      is followed to retrieve the string.  If the pointer lands in the
      middle of a packed string block (common in Go binaries), the bytes
      are read directly from memory.
    """
    from ghidra.program.model.data import AbstractStringDataType

    listing = program.getListing()
    sym_table = program.getSymbolTable()
    mem = program.getMemory()

    # Pass 1: collect direct string data items, keyed by address string.
    addr_to_str: dict[str, str] = {}
    result: dict[str, str] = {}
    ptrs: list[tuple[str, object]] = []  # (symbol_name, target Address)

    data_iter = listing.getDefinedData(True)
    while data_iter.hasNext():
        data = data_iter.next()
        addr = data.getAddress()
        sym = sym_table.getPrimarySymbol(addr)
        if sym is None:
            continue
        name = sym.getName()
        dt = data.getDataType()

        if isinstance(dt, AbstractStringDataType):
            try:
                val = data.getValue()
                if val is not None:
                    s = str(val)[:MAX_STRING_DISPLAY_LEN]
                    addr_to_str[str(addr)] = s
                    result[name] = s
            except Exception:
                pass
        elif data.isPointer() and name.startswith("PTR_"):
            try:
                target = data.getValue()  # returns an Address
                if target is not None:
                    ptrs.append((name, target))
            except Exception:
                pass

    # Pass 2: resolve pointers to their target strings.
    for name, target_addr in ptrs:
        target_key = str(target_addr)
        if target_key in addr_to_str:
            result[name] = addr_to_str[target_key]
        else:
            # Pointer may land in the middle of a packed string block —
            # read directly from memory.
            s = _read_string_at(mem, target_addr)
            if s:
                result[name] = s

    return result


def _resolve_from_addr(mem, addr_factory, name, addr):
    """Try to resolve a single symbol to string value(s) given its memory address.

    For ``PTR_s_*`` symbols, first attempts to read a sequence of Go string
    ``(ptr, len)`` pairs starting at *addr*.  Each pair is 16 bytes: an
    8-byte pointer to the string data followed by an 8-byte length.  When
    multiple valid strings are found the result is a ``list[str]``.  If the
    pair reading finds only one string, a plain ``str`` is returned.  If pair
    reading fails entirely, falls back to following the single pointer and
    reading until a non-printable byte.

    For direct ``s_*`` symbols, reads the string directly at *addr*.

    Returns ``str``, ``list[str]``, or ``None``.
    """
    try:
        if name.startswith("PTR_"):
            addr_space = addr_factory.getDefaultAddressSpace()

            # Try reading as an array of Go string (ptr, len) pairs.
            # Go struct arrays interleave strings with other fields
            # (nil slice headers, integers, etc.), so we skip zero entries
            # and tolerate a few consecutive non-string entries before
            # giving up.
            strings = []
            consecutive_fails = 0
            for i in range(_MAX_STRING_ARRAY_ENTRIES):
                if consecutive_fails > _MAX_CONSECUTIVE_FAILS:
                    break

                pair_addr = addr.add(i * 16)
                try:
                    ptr_val = _read_le64(mem, pair_addr)
                    str_len = _read_le64(mem, pair_addr.add(8))
                except Exception:
                    break

                # Skip zero-initialized fields (nil slices, nil pointers)
                if ptr_val == 0:
                    continue

                # Try to read a Go string from this (ptr, len) pair
                s = None
                if 0 < str_len <= MAX_STRING_DISPLAY_LEN:
                    try:
                        target = addr_space.getAddress(ptr_val)
                        if mem.contains(target):
                            s = _read_string_with_len(mem, target, str_len)
                    except Exception:
                        pass

                if s is not None:
                    consecutive_fails = 0
                    strings.append(s)
                else:
                    consecutive_fails += 1

            if len(strings) > 1:
                return strings
            if len(strings) == 1:
                return strings[0]

            # Fallback: follow single pointer, read until non-printable.
            ptr_val = _read_le64(mem, addr)
            target = addr_space.getAddress(ptr_val)
            if mem.contains(target):
                return _read_string_at(mem, target)
            return None
        else:
            return _read_string_at(mem, addr)
    except Exception:
        return None


def _resolve_missing_strings(program, c_code, string_data, attempted):
    """Resolve unresolved ``PTR_s_*``/``s_*`` symbols referenced in *c_code*.

    Ghidra auto-generates labels like ``PTR_s_foo_007e0ce0`` where the
    trailing hex digits are the memory address of the data item.  These
    labels often don't appear in the symbol table as formal global
    symbols, so a symbol-table lookup alone misses them.

    Resolution strategy (tried in order for each symbol):

    1. **Address-from-name** — parse the trailing hex suffix from the
       symbol name (e.g. ``_007e0ce0`` → ``0x007e0ce0``), construct an
       Address, and read directly from memory.
    2. **Symbol table lookup** — fall back to ``getGlobalSymbols(name)``
       for symbols that don't have a hex suffix or whose suffix doesn't
       yield a valid address.

    *string_data* is updated in-place; *attempted* tracks symbols that
    have already been looked up (whether successfully or not) so we
    don't repeat work across functions.
    """
    refs = set(_STRING_SYM_RE.findall(c_code))
    missing = refs - set(string_data.keys()) - attempted

    if not missing:
        return

    mem = program.getMemory()
    addr_factory = program.getAddressFactory()
    addr_space = addr_factory.getDefaultAddressSpace()
    sym_table = program.getSymbolTable()

    for name in missing:
        attempted.add(name)

        # Strategy 1: parse hex address from the symbol name suffix.
        m = _HEX_ADDR_SUFFIX_RE.search(name)
        if m:
            try:
                addr = addr_space.getAddress(int(m.group(1), 16))
                if mem.contains(addr):
                    s = _resolve_from_addr(mem, addr_factory, name, addr)
                    if s:
                        string_data[name] = s
                        continue
            except Exception:
                pass

        # Strategy 2: symbol table lookup.
        syms = list(sym_table.getGlobalSymbols(name))
        if not syms:
            continue
        addr = syms[0].getAddress()
        if not addr.isMemoryAddress():
            continue
        s = _resolve_from_addr(mem, addr_factory, name, addr)
        if s:
            string_data[name] = s


def _get_rodata_ranges(program):
    """Return ``list[(int, int)]`` of read-only, initialized, non-executable memory ranges.

    Used to filter hex constants so we only attempt string resolution for
    addresses that live in ``.rodata`` or similar sections.
    """
    ranges = []
    mem = program.getMemory()
    for block in mem.getBlocks():
        if (block.isInitialized()
                and not block.isExecute()
                and (block.isRead() and not block.isWrite())):
            start = block.getStart().getOffset()
            end = block.getEnd().getOffset()
            ranges.append((start, end))
    return ranges


def _addr_in_rodata(addr_int, ranges):
    """Return True if *addr_int* falls within any of the rodata *ranges*."""
    for start, end in ranges:
        if start <= addr_int <= end:
            return True
    return False


def _resolve_dat_as_string(mem, addr_factory, addr):
    """Try to resolve a ``DAT_*`` address to a string value.

    Strategies (tried in order):

    1. Read 16 bytes as a Go ``(ptr, len)`` pair; follow the pointer and
       read *len* bytes.
    2. Follow as a single pointer (without length).
    3. Read directly as string bytes at the address.

    All strategies require ``len(result) >= _MIN_STRING_LEN``.
    """
    addr_space = addr_factory.getDefaultAddressSpace()

    # Strategy 1: Go (ptr, len) pair
    try:
        ptr_val = _read_le64(mem, addr)
        str_len = _read_le64(mem, addr.add(8))
        if 0 < str_len <= MAX_STRING_DISPLAY_LEN:
            target = addr_space.getAddress(ptr_val)
            if mem.contains(target):
                s = _read_string_with_len(mem, target, str_len)
                if s and len(s) >= _MIN_STRING_LEN:
                    return s
    except Exception:
        pass

    # Strategy 2: follow as single pointer
    try:
        ptr_val = _read_le64(mem, addr)
        target = addr_space.getAddress(ptr_val)
        if mem.contains(target):
            s = _read_string_at(mem, target)
            if s and len(s) >= _MIN_STRING_LEN:
                return s
    except Exception:
        pass

    # Strategy 3: read directly at the address
    try:
        s = _read_string_at(mem, addr)
        if s and len(s) >= _MIN_STRING_LEN:
            return s
    except Exception:
        pass

    return None


def _find_dat_paired_length(lines, dat_name):
    """Find a Go string length paired with a ``DAT_*`` symbol in decompiled C.

    Similar to :func:`_find_paired_length` but searches all lines for the
    *dat_name* token.  Two patterns are checked:

    1. **Same-line comma** — ``func(DAT_xxx, LEN)`` or
       ``func(&DAT_xxx, LEN)`` where *LEN* is a small integer after a comma.
    2. **Adjacent stack locals** — ``local_X = ...DAT_xxx...;`` on one line
       and ``local_Y = LEN;`` on an adjacent line, where
       ``|offset(X) - offset(Y)| == 8`` (Go string header layout).

    Returns ``int | None``.
    """
    for line_idx, line in enumerate(lines):
        pos = line.find(dat_name)
        if pos < 0:
            continue

        # Pattern 1: same-line comma after DAT symbol
        after = line[pos + len(dat_name):]
        m_comma = re.match(r"[^,;\n]{0,20},\s*(0x[0-9a-fA-F]+|\d+)", after)
        if m_comma:
            try:
                val = int(m_comma.group(1), 0)
                if 0 < val <= MAX_STRING_DISPLAY_LEN:
                    return val
            except ValueError:
                pass

        # Pattern 2: adjacent stack local assignments
        m_local = re.search(r"\b(local_([0-9a-fA-F]+))\s*=", line)
        if m_local:
            try:
                var_offset = int(m_local.group(2), 16)
            except ValueError:
                continue

            for delta in (-1, 1):
                adj_idx = line_idx + delta
                if adj_idx < 0 or adj_idx >= len(lines):
                    continue
                m_adj = _LOCAL_ASSIGN_RE.search(lines[adj_idx])
                if not m_adj:
                    continue
                adj_hex = m_adj.group(1).split("_", 1)[1]
                try:
                    adj_offset = int(adj_hex, 16)
                except ValueError:
                    continue
                if abs(var_offset - adj_offset) == 8:
                    try:
                        val = int(m_adj.group(2), 0)
                        if 0 < val <= MAX_STRING_DISPLAY_LEN:
                            return val
                    except ValueError:
                        continue
    return None


def _resolve_dat_symbols(program, c_code, string_data, attempted):
    """Resolve ``DAT_*`` references in *c_code* to string values.

    Parses the hex address from the symbol name (e.g. ``DAT_007e0ce0``),
    constructs a Ghidra address, and first tries to find a paired length
    from the C code context for precise extraction.  Falls back to
    :func:`_resolve_dat_as_string` heuristics when no length is found.
    Updates *string_data* in-place.  (Approach A)
    """
    refs = set(_DAT_SYM_RE.findall(c_code))
    missing = refs - set(string_data.keys()) - attempted
    if not missing:
        return

    mem = program.getMemory()
    addr_factory = program.getAddressFactory()
    addr_space = addr_factory.getDefaultAddressSpace()
    lines = c_code.splitlines()

    for name in missing:
        attempted.add(name)
        # Extract hex address from DAT_<hex>
        hex_str = name[4:]  # strip "DAT_"
        try:
            addr = addr_space.getAddress(int(hex_str, 16))
        except Exception:
            continue
        if not mem.contains(addr):
            continue

        # Try to find a paired length from the C code context first.
        # This avoids _read_string_at over-reading packed .rodata.
        length = _find_dat_paired_length(lines, name)
        if length is not None:
            s = _read_string_with_len(mem, addr, length)
            if s and len(s) >= _MIN_STRING_LEN:
                string_data[name] = s
                continue

        # Fall back to heuristic strategies (ptr/len pair, pointer, direct)
        s = _resolve_dat_as_string(mem, addr_factory, addr)
        if s:
            string_data[name] = s


def _find_paired_length(lines, line_idx, hex_match):
    """Heuristic to find a Go string length paired with a pointer constant.

    Two patterns are checked:

    1. **Same-line comma** — ``func(0xADDR, LEN)`` where *LEN* is a small
       integer following a comma after the hex literal.
    2. **Adjacent stack locals** — ``local_X = 0xADDR;`` on one line and
       ``local_Y = LEN;`` on an adjacent line, where
       ``|offset(X) - offset(Y)| == 8`` (Go string header layout).

    Returns ``int | None``.
    """
    line = lines[line_idx]

    # Pattern 1: same-line comma  e.g. func(0x7e0ce0, 0xd) or func(0x7e0ce0, 13)
    hex_end = hex_match.end()
    after = line[hex_end:]
    m = re.match(r"\s*,\s*(0x[0-9a-fA-F]+|\d+)", after)
    if m:
        try:
            val = int(m.group(1), 0)
            if 0 < val <= MAX_STRING_DISPLAY_LEN:
                return val
        except ValueError:
            pass

    # Pattern 2: adjacent stack local assignments
    m_local = _LOCAL_ASSIGN_RE.search(line)
    if m_local and hex_match.group(1) == m_local.group(2):
        var_name = m_local.group(1)           # e.g. "local_38"
        var_hex = var_name.split("_", 1)[1]   # e.g. "38"
        try:
            var_offset = int(var_hex, 16)
        except ValueError:
            return None

        # Check the line above and below for a paired length assignment
        for delta in (-1, 1):
            adj_idx = line_idx + delta
            if adj_idx < 0 or adj_idx >= len(lines):
                continue
            m_adj = _LOCAL_ASSIGN_RE.search(lines[adj_idx])
            if not m_adj:
                continue
            adj_name = m_adj.group(1)
            adj_hex = adj_name.split("_", 1)[1]
            try:
                adj_offset = int(adj_hex, 16)
            except ValueError:
                continue
            # Go string header: ptr at offset N, len at offset N+8 (or N-8)
            if abs(var_offset - adj_offset) == 8:
                try:
                    val = int(m_adj.group(2), 0)
                    if 0 < val <= MAX_STRING_DISPLAY_LEN:
                        return val
                except ValueError:
                    continue
    return None


def _resolve_hex_string_constants(program, c_code, string_data, rodata_ranges, attempted):
    """Resolve bare hex address constants that point into ``.rodata``.

    Finds hex literals in *c_code* whose values fall within *rodata_ranges*,
    pairs them with a length when possible, and reads the string from memory.
    Falls back to :func:`_read_string_at` when no length is found.
    Keys in *string_data* are the hex literal strings (e.g. ``"0x7e0ce0"``).
    (Approach B)
    """
    if not rodata_ranges:
        return

    mem = program.getMemory()
    addr_factory = program.getAddressFactory()
    addr_space = addr_factory.getDefaultAddressSpace()

    lines = c_code.splitlines()

    for line_idx, line in enumerate(lines):
        for m in _HEX_LITERAL_RE.finditer(line):
            hex_str = m.group(1)
            if hex_str in string_data or hex_str in attempted:
                continue
            attempted.add(hex_str)

            try:
                addr_int = int(hex_str, 16)
            except ValueError:
                continue
            if not _addr_in_rodata(addr_int, rodata_ranges):
                continue

            try:
                addr = addr_space.getAddress(addr_int)
            except Exception:
                continue
            if not mem.contains(addr):
                continue

            # Try to find a paired length for precise extraction
            length = _find_paired_length(lines, line_idx, m)
            if length is not None:
                s = _read_string_with_len(mem, addr, length)
                if s and len(s) >= _MIN_STRING_LEN:
                    string_data[hex_str] = s
                    continue

            # Fallback: read until non-printable byte
            s = _read_string_at(mem, addr)
            if s and len(s) >= _MIN_STRING_LEN:
                string_data[hex_str] = s


def _find_referenced_strings(c_code, string_data):
    """Return ``{name: value}`` for string symbols referenced in *c_code*."""
    refs = set(_STRING_SYM_RE.findall(c_code))
    refs |= set(_DAT_SYM_RE.findall(c_code))
    refs |= set(_HEX_LITERAL_RE.findall(c_code))
    return {name: string_data[name] for name in refs if name in string_data}


def _escape_annotation(s: str) -> str:
    """Escape a string value for use in a ``// Strings:`` annotation."""
    return (s
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t"))


# --------------------------------------------------------------------------- #
#  Core
# --------------------------------------------------------------------------- #

def decompile_program(program, output_path: Path, module_path: str | None = None) -> bool:
    """Decompile all functions in an already-analyzed program.

    Uses DecompInterface directly — no script runner overhead.
    Each function is annotated with a ``// Strings:`` comment block
    listing the resolved values of any ``PTR_s_*`` / ``s_*`` symbols
    referenced in its decompiled C code.

    If *module_path* is provided, only functions belonging to the user's
    module (or ``main.*``) are decompiled — stdlib, runtime, and external
    dependency functions are skipped.

    Writes a sidecar ``<binary>.meta.json`` next to the output with
    decompilation statistics and any functions that hit the timeout.

    Returns True on success.
    """
    import pyghidra
    from ghidra.app.decompiler import DecompInterface

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Collect resolved string data before decompiling.
    string_data = collect_string_data(program)
    resolve_attempted: set[str] = set()
    rodata_ranges = _get_rodata_ranges(program)

    decompiler = DecompInterface()
    decompiler.openProgram(program)
    monitor = pyghidra.task_monitor()

    failed_funcs = []
    decompiled_count = 0
    skipped_count = 0
    total_count = 0

    try:
        functions = list(program.getFunctionManager().getFunctions(True))
        total_count = len(functions)
        with open(output_path, "w") as f:
            for func in functions:
                fname = func.getName()

                if module_path and not is_user_func(fname, module_path):
                    skipped_count += 1
                    continue

                result = decompiler.decompileFunction(
                    func, 600, monitor,
                )

                if not result.decompileCompleted():
                    fname = func.getName()
                    addr = func.getEntryPoint().toString()
                    err = str(result.getErrorMessage() or "unknown").strip()
                    is_timeout = "timeout" in err.lower() or "Didn't finish" in err
                    reason = "timeout" if is_timeout else err
                    failed_funcs.append({
                        "name": fname,
                        "address": addr,
                        "reason": reason,
                    })
                    label = "TIMEOUT" if is_timeout else "DECOMP FAIL"
                    tqdm.write(f"    {label}: {fname} @ {addr} -- {reason}")
                    continue

                decomp = result.getDecompiledFunction()
                if decomp is not None:
                    c_code = decomp.getC()
                    if c_code:
                        c_str = str(c_code)
                        decompiled_count += 1
                        f.write(f"// Function: {func.getName()}\n")

                        # Resolve any symbols the data-item pass missed
                        _resolve_missing_strings(
                            program, c_str, string_data, resolve_attempted,
                        )
                        # Resolve DAT_* symbols (Approach A)
                        _resolve_dat_symbols(
                            program, c_str, string_data, resolve_attempted,
                        )
                        # Resolve bare hex address constants (Approach B)
                        _resolve_hex_string_constants(
                            program, c_str, string_data, rodata_ranges,
                            resolve_attempted,
                        )

                        # Annotate with resolved string values
                        referenced = _find_referenced_strings(c_str, string_data)
                        if referenced:
                            f.write("// Strings:\n")
                            for sname in sorted(referenced):
                                val = referenced[sname]
                                if isinstance(val, list):
                                    parts = []
                                    for s in val:
                                        parts.append('"' + _escape_annotation(s) + '"')
                                    f.write(f'//   {sname} = [{", ".join(parts)}]\n')
                                else:
                                    f.write(f'//   {sname} = "{_escape_annotation(val)}"\n')

                        f.write(c_str)
                        f.write("\n")
    finally:
        decompiler.dispose()

    # Write sidecar metadata
    meta_path = output_path.with_suffix(".meta.json")
    timeouts = [f for f in failed_funcs if f["reason"] == "timeout"]
    errors = [f for f in failed_funcs if f["reason"] != "timeout"]
    meta_path.write_text(json.dumps({
        "module_path": module_path,
        "total_functions": total_count,
        "decompiled": decompiled_count,
        "skipped": skipped_count,
        "timed_out": timeouts,
        "errors": errors,
    }, indent=2) + "\n")

    if skipped_count:
        tqdm.write(f"    Skipped {skipped_count} non-user functions")
    if timeouts:
        tqdm.write(f"    {len(timeouts)} function(s) hit decompilation timeout")
    if errors:
        tqdm.write(f"    {len(errors)} function(s) failed decompilation")

    return output_path.exists() and output_path.stat().st_size > 0


def process_binary(project, binary_path: Path, output_path: Path, variant: str = "default") -> bool:
    """Import, analyze, and decompile a single binary within an open project.

    The program is loaded into the project, analyzed, decompiled, then the
    project file is deleted to keep disk use low.

    For stripped binaries, GoReSym is used to recover function names and
    boundaries from Go's pclntab before Ghidra's auto-analysis runs.
    """
    import pyghidra

    program_name = binary_path.name
    monitor = pyghidra.task_monitor()

    try:
        # Load the binary into the project
        loader = pyghidra.program_loader().project(project)
        loader = loader.source(str(binary_path)).name(program_name)

        with loader.load() as load_results:
            load_results.save(monitor)

        # Get module path for user-function filtering
        mod_path = get_module_path(binary_path)
        if mod_path:
            tqdm.write(f"    Module: {mod_path}")
        else:
            tqdm.write("    WARNING: could not determine module path; decompiling all functions")

        # Open, analyze, decompile
        with pyghidra.program_context(project, f"/{program_name}") as program:
            goresym_data = run_goresym(binary_path)
            if goresym_data is not None:
                renamed, created = inject_goresym_symbols(program, goresym_data)
                tqdm.write(f"    GoReSym: renamed {renamed}, created {created} functions")
            pyghidra.analyze(program, monitor)
            partial_marker = output_path.with_suffix(".partial")
            partial_marker.touch()
            ok = decompile_program(program, output_path, module_path=mod_path)
            partial_marker.unlink(missing_ok=True)

        # Remove program from project to free disk / memory
        domain_file = project.getProjectData().getFile(f"/{program_name}")
        if domain_file is not None:
            domain_file.delete()

        return ok

    except Exception as e:
        tqdm.write(f"    ERROR: {e}")
        return False


def collect_binaries(meta: dict, repo_filter: list[str] | None) -> list[dict]:
    """Build a flat list of binaries to decompile from metadata."""
    entries = []
    for repo_name, info in meta["repos"].items():
        if repo_filter and repo_name not in repo_filter:
            continue
        if not info.get("cloned") or not info.get("compiled_at"):
            continue

        sname = safe_name(repo_name)
        for variant, bin_list in info.get("binaries", {}).items():
            for bin_name in bin_list:
                binary_path = BINARIES_DIR / sname / variant / bin_name
                output_path = DECOMPS_DIR / sname / variant / f"{bin_name}.c"
                entries.append({
                    "repo": repo_name,
                    "variant": variant,
                    "binary": bin_name,
                    "binary_path": binary_path,
                    "output_path": output_path,
                })
    return entries


# --------------------------------------------------------------------------- #
#  Multiprocessing worker (each process gets its own JVM + Ghidra project)
# --------------------------------------------------------------------------- #

_g_project = None
_g_tmpdir = None


def _worker_init(ghidra_install_dir):
    """Called once per worker process — starts a JVM and opens a Ghidra project."""
    import pyghidra as _pyghidra

    global _g_project, _g_tmpdir

    pid = os.getpid()
    tqdm.write(f"    [worker {pid}] Starting JVM...")
    _pyghidra.start(install_dir=Path(ghidra_install_dir))
    tqdm.write(f"    [worker {pid}] JVM started, opening Ghidra project...")
    _g_tmpdir = tempfile.mkdtemp(prefix="ghidra_worker_")
    _g_project_cm = _pyghidra.open_project(_g_tmpdir, "decomp", create=True)
    _g_project = _g_project_cm.__enter__()
    tqdm.write(f"    [worker {pid}] Ready.")

    def _cleanup():
        try:
            _g_project_cm.__exit__(None, None, None)
        except Exception:
            pass
        shutil.rmtree(_g_tmpdir, ignore_errors=True)

    atexit.register(_cleanup)


def _worker_task(entry_ser: dict) -> tuple[str, bool]:
    """Process a single binary inside a worker process.

    Takes and returns plain serialisable types so it works with spawn.
    """
    binary_path = Path(entry_ser["binary_path"])
    output_path = Path(entry_ser["output_path"])
    label = f"  {entry_ser['repo']}  {entry_ser['variant']}/{entry_ser['binary']}"

    if not binary_path.exists():
        return label + " (binary missing)", False

    tqdm.write(label)
    ok = process_binary(_g_project, binary_path, output_path, variant=entry_ser["variant"])
    return label, ok


def _serialise_entries(entries: list[dict]) -> list[dict]:
    """Convert Path objects to strings so entries survive spawn pickling."""
    return [
        {**e, "binary_path": str(e["binary_path"]), "output_path": str(e["output_path"])}
        for e in entries
    ]


# --------------------------------------------------------------------------- #
#  Main
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(
        description="Decompile compiled Go binaries using Ghidra headless.",
    )
    parser.add_argument("--repo", type=str, nargs="*", default=None,
                        help="Decompile binaries for specific repo(s) only (e.g. ollama/ollama)")
    parser.add_argument("--variant", type=str, nargs="+", default=None,
                        help="Only decompile specific variant(s) (default, debug, stripped)")
    parser.add_argument("--binaries", type=str, nargs="+", default=None,
                        help="Only decompile specific binary names (requires exactly one --repo)")
    parser.add_argument("--force", action="store_true",
                        help="Re-decompile even if output already exists")
    parser.add_argument("--max-repos", type=int, default=None,
                        help="Maximum number of repos to decompile")
    parser.add_argument("--max-size", type=int, default=None,
                        help="Skip binaries larger than this size in MB")
    parser.add_argument("--threads", type=int, default=1,
                        help="Number of parallel worker processes (default: 1)")
    parser.add_argument("--ghidra-dir", type=str, default=None,
                        help="Path to Ghidra installation (overrides GHIDRA_INSTALL_DIR env var)")
    args = parser.parse_args()

    if args.ghidra_dir:
        global GHIDRA_INSTALL
        GHIDRA_INSTALL = Path(args.ghidra_dir)

    if args.binaries and (not args.repo or len(args.repo) != 1):
        parser.error("--binaries requires exactly one --repo")

    meta = load_metadata()
    entries = collect_binaries(meta, args.repo)

    if args.variant:
        entries = [e for e in entries if e["variant"] in args.variant]

    if args.binaries:
        entries = [e for e in entries if e["binary_path"].name in args.binaries]

    if not args.force:
        entries = [
            e for e in entries
            if not (e["output_path"].with_suffix(".meta.json").exists()
                    or (e["output_path"].exists() and e["output_path"].stat().st_size > 0
                        and not e["output_path"].with_suffix(".partial").exists()))
        ]

    if args.max_size:
        max_bytes = args.max_size * 1_000_000
        entries = [
            e for e in entries
            if e["binary_path"].exists() and e["binary_path"].stat().st_size <= max_bytes
        ]

    if args.max_repos:
        seen_repos: dict[str, None] = {}
        filtered = []
        for e in entries:
            if e["repo"] not in seen_repos:
                if len(seen_repos) >= args.max_repos:
                    continue
                seen_repos[e["repo"]] = None
            filtered.append(e)
        entries = filtered

    if not entries:
        print("Nothing to decompile (all outputs exist or no binaries found).")
        return

    n_threads = max(1, args.threads)
    print(f"Decompiling {len(entries)} binaries ({n_threads} worker(s))...")

    succeeded = 0
    failed = 0

    if n_threads == 1:
        # Sequential — single JVM, single project, no spawn overhead
        import pyghidra
        pyghidra.start(install_dir=GHIDRA_INSTALL)

        with tempfile.TemporaryDirectory(prefix="ghidra_proj_") as tmpdir:
            with pyghidra.open_project(tmpdir, "decomp", create=True) as project:
                for entry in tqdm(entries, desc="Decompiling", unit="bin"):
                    binary_path = entry["binary_path"]
                    output_path = entry["output_path"]

                    if not binary_path.exists():
                        tqdm.write(f"  SKIP {binary_path} (binary missing)")
                        failed += 1
                        continue

                    tqdm.write(f"  {entry['repo']}  {entry['variant']}/{entry['binary']}")
                    ok = process_binary(project, binary_path, output_path, variant=entry["variant"])
                    if ok:
                        succeeded += 1
                    else:
                        failed += 1
    else:
        # Parallel — spawn separate processes, each with its own JVM
        ctx = mp.get_context("spawn")
        ser_entries = _serialise_entries(entries)

        with ctx.Pool(processes=n_threads, initializer=_worker_init, initargs=(str(GHIDRA_INSTALL),)) as pool:
            results = pool.imap_unordered(_worker_task, ser_entries)
            for label, ok in tqdm(results, total=len(entries), desc="Decompiling", unit="bin"):
                tqdm.write(f"[DONE] {label}")
                if ok:
                    succeeded += 1
                else:
                    failed += 1

    print(f"\n{'='*60}")
    print(f"  Succeeded:  {succeeded}")
    print(f"  Failed:     {failed}")
    print(f"  Output dir: {DECOMPS_DIR}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
