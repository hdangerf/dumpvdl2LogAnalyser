#!/usr/bin/env python3
"""
extract_vdl2.py
---------------
Extract all records for a specific aircraft from a dumpvdl2 log file,
searching by Registration (REG) or ICAO hex address.

Usage:
    python extract_vdl2.py <date4> <REG_or_ICAO>

Examples:
    python extract_vdl2.py 0323 G-TUMH
    python extract_vdl2.py 0323 407739

Input file  : pi21vdl2_20260323.log  (only the 4-digit MMDD date portion is needed)
Output file : 0323_G-TUMH.txt   (REG used if known, otherwise ICAO hex)

Log format (dumpvdl2):
    [2026-03-23 01:17:27 GMT] [136.975] ...          <- block start (timestamp)
    407739 (Aircraft, Airborne) -> 1097AA (...): ... <- ICAO is first token
    AC info: G-TUMH, B38M, x521526028               <- REG is first field after "AC info: "
    ...
    Reg: .G-TUMH Flight: BY0699                      <- REG also here (leading dot stripped)
"""

import sys
import os
import re
import glob


# ---------------------------------------------------------------------------
# Regex patterns derived from the actual log format
# ---------------------------------------------------------------------------

# Timestamp line that starts every message block
RE_BLOCK_START = re.compile(r'^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}')

# ICAO: first hex token on the Aircraft line, e.g. "407739 (Aircraft..."
RE_ICAO_LINE = re.compile(r'^([0-9A-Fa-f]{6})\s+\(Aircraft')

# AC info line, e.g. "AC info: G-TUMH, B38M, x521526028"
# REG is the first comma-delimited field; a bare '-' means unknown
RE_AC_INFO = re.compile(r'^AC info:\s+([^,]+)')

# ACARS Reg line, e.g. "  Reg: .G-TUMH Flight: BY0699"  (leading dot stripped)
RE_ACARS_REG = re.compile(r'\bReg:\s+\.?([A-Z0-9][A-Z0-9\-]+)', re.IGNORECASE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def find_logfile(date4: str) -> str:
    """Locate *<date4>*.log in the current directory."""
    pattern = f"*{date4}*.log"
    matches = glob.glob(pattern)
    if not matches:
        raise FileNotFoundError(
            f"No log file matching '{pattern}' found in {os.getcwd()}"
        )
    if len(matches) > 1:
        print(f"[WARNING] Multiple files match '{pattern}': {matches}")
        print(f"[WARNING] Using: {matches[0]}")
    return matches[0]


def extract_block_fields(block: list) -> tuple:
    """
    Return (icao, reg) found anywhere in a message block.
    Both are upper-case strings, or '' if not found.
    """
    icao = ""
    reg  = ""

    for line in block:
        stripped = line.strip()

        if not icao:
            m = RE_ICAO_LINE.match(stripped)
            if m:
                icao = m.group(1).upper()

        if not reg:
            # AC info line takes priority
            m = RE_AC_INFO.match(stripped)
            if m:
                candidate = m.group(1).strip().upper()
                if candidate and candidate != '-':
                    reg = candidate
                continue

            # ACARS Reg line as fallback
            m = RE_ACARS_REG.search(stripped)
            if m:
                reg = m.group(1).upper()

    return icao, reg


def block_matches(block: list, search: str, is_icao: bool) -> tuple:
    """
    Return (matched, icao_found, reg_found).
    search must already be upper-cased.
    """
    icao, reg = extract_block_fields(block)
    matched = (icao == search) if is_icao else (reg == search)
    return matched, icao, reg


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)

    date4      = sys.argv[1].strip()
    search_raw = sys.argv[2].strip().upper()

    # A 6-char hex string is treated as an ICAO address; everything else is a REG
    is_icao = bool(re.fullmatch(r'[0-9A-F]{6}', search_raw))
    kind    = "ICAO" if is_icao else "REG"
    print(f"Searching for {kind}: {search_raw}")

    # -- Locate the log file -------------------------------------------------
    try:
        logfile = find_logfile(date4)
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}")
        sys.exit(1)

    print(f"Reading : {logfile}")

    # -- Split into message blocks and filter --------------------------------
    matched_blocks = []
    discovered_icao = ""
    discovered_reg  = ""

    current_block = []

    with open(logfile, "r", encoding="utf-8", errors="replace") as fh:
        for raw_line in fh:
            line = raw_line.rstrip("\n")

            if RE_BLOCK_START.match(line):
                # Process the completed previous block
                if current_block:
                    hit, b_icao, b_reg = block_matches(current_block, search_raw, is_icao)
                    if hit:
                        matched_blocks.append(current_block)
                        if b_icao and not discovered_icao:
                            discovered_icao = b_icao
                        if b_reg and not discovered_reg:
                            discovered_reg = b_reg
                current_block = [line]
            else:
                current_block.append(line)

        # Final block
        if current_block:
            hit, b_icao, b_reg = block_matches(current_block, search_raw, is_icao)
            if hit:
                matched_blocks.append(current_block)
                if b_icao and not discovered_icao:
                    discovered_icao = b_icao
                if b_reg and not discovered_reg:
                    discovered_reg = b_reg

    # -- Build output filename -----------------------------------------------
    # Always prefer REG in the filename; fall back to ICAO hex
    label = (discovered_reg if discovered_reg else search_raw) if is_icao else search_raw
    out_filename = f"{date4}_{label}.txt"

    # -- Write results -------------------------------------------------------
    if not matched_blocks:
        print(f"[INFO]  No records found for {kind} = {search_raw}")
        sys.exit(0)

    with open(out_filename, "w", encoding="utf-8") as out:
        for i, block in enumerate(matched_blocks):
            out.write("\n".join(block))
            out.write("\n")
            if i < len(matched_blocks) - 1:
                out.write("\n")   # blank line separator between blocks

    print(f"[OK]    {len(matched_blocks)} message block(s) written to: {out_filename}")
    if is_icao and discovered_reg:
        print(f"[INFO]  Registration discovered in log: {discovered_reg}")


if __name__ == "__main__":
    main()
