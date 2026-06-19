# MIAM Decoder — Help File

`miam_decoder.py` extracts and decodes MIAM CORE REP messages from
dumpvdl2 plain-text log files. These are Airbus A350 Flight Operations
Quality Assurance (FOQA) style performance reports, transmitted over
VDL2 using the MIAM (Media Independent Aircraft Messaging) encoding.

dumpvdl2 itself handles the deflate decompression and reassembly of
the MIAM envelope. This script decodes the inner ACARS payload that
dumpvdl2 leaves as plain text in the log.

---

## Basic usage

```bash
python miam_decoder.py <logfile> [<logfile2> ...]
```

At least one log file is required. Multiple files are merged before
decoding — useful for combining several days or several hourly log
rotations into a single report.

```bash
python miam_decoder.py pi21vdl2_20260329.log
python miam_decoder.py pi21vdl2_202603*.log
```

---

## Command line options

### `--output FILE`
Path for the HTML report. Defaults to `miam_report.html` in the
current directory.

```bash
python miam_decoder.py --output /home/pi/reports/miam_20260329.html pi21vdl2_20260329.log
```

### `--text`
Prints full decoded detail for every message to the console, not just
the per-aircraft REP type summary. Useful for a quick terminal-only
look without opening the HTML report.

```bash
python miam_decoder.py --text pi21vdl2_20260329.log
```

### `--aircraft REG`
Filters output to a single aircraft registration. Case-insensitive; a
leading dot (as sometimes seen in raw log fields) is stripped
automatically.

```bash
python miam_decoder.py --aircraft 9M-MAD pi21vdl2_20260329.log
```

### `--rep TYPE`
Filters output to a single REP message type (see reference table
below). Case-insensitive.

```bash
python miam_decoder.py --rep REP020 pi21vdl2_20260329.log
```

### `--verbose`
Prints a per-file count of MIAM blocks found while parsing, useful for
confirming files are being read correctly when processing many at
once.

```bash
python miam_decoder.py --verbose *.log
```

### Combining options

```bash
python miam_decoder.py \
    --aircraft G-XWBD \
    --rep REP001 \
    --text \
    --output gxwbd_approach.html \
    pi21vdl2_202603*.log
```

---

## REP message type reference

| Type | Name | What it captures |
|---|---|---|
| `REP001` | Approach Stabilisation (5000 ft gate) | Flight parameters snapshot at the 5000 ft stabilisation gate |
| `REP002` | Approach Stabilisation (alternate gate) | Same as REP001 but at the operator's alternate gate altitude |
| `REP004` | Approach Sequence | Rolling snapshot of approach parameters through final approach |
| `REP015` | Flap / Slat Configuration | Flap and slat positions requested vs achieved at each change |
| `REP020` | Gear Extension Event | Flight loads and hydraulic data at landing gear extension |
| `REP024` | Approach Control Surface Snapshot | Control surface deflections during approach |
| `REP053` | Landing / Touchdown Snapshot | Structural and flight parameters at touchdown |
| `REP081` | Takeoff Performance (EDR) | Engine Data Recorder takeoff report |

These have all been observed across multiple airlines (British
Airways, Virgin Atlantic, Malaysia Airlines, Cathay Pacific, China
Southern, Turkish Airlines, Ethiopian, Air India, Asiana, Finnair) on
the A350-900 and A350-1000, so the REP format itself appears to be an
Airbus standard rather than airline-specific.

---

## What is verified vs. unverified

This is the most important section. Be clear-eyed about what this
script can and can't tell you.

**Verified and reliable:**
- REP type and its general purpose
- Route (origin/destination ICAO) and flight number
- Aircraft registration
- Event timestamp (UTC)
- Phase / sub-phase classification
- Free-text event notes (e.g. "Normal Landing Gear Extension", "No
  Stable Frame")

These fields were cross-checked against other data in the same log
block (e.g. the registration in H01 matches the `AC info:` line
elsewhere in the same record) and behave consistently across all 32+
aircraft and 78 messages sampled during development.

**Not decoded — shown as raw values only:**
The data sections (`A10`, `B10`, `C10`, `D10`, etc.) inside each REP
message contain genuine flight parameters — weight, speed, control
surface deflections, gear pressures, structural loads — but their
exact field-to-unit mapping is not publicly documented.

An earlier version of this script guessed at these mappings and
produced numbers that looked plausible at first but were nonsense once
checked against a wider sample (e.g. an "approach speed" of 21,441 kt,
a landing gear pressure of 439,460 psi). Those guesses have been
removed. The raw section values are preserved in the HTML report's
"Raw section data" panel for each message, so if you have access to
Airbus or airline-specific ACMS/FDIMU documentation, you can map them
yourself with confidence in the underlying data being correct.

If you do have access to such documentation, or can correlate a
specific REP message against known real-world values (e.g. a known
landing weight or wind reading for a specific flight), I can fold the
verified mapping back into the decoder properly.

---

## Output

**Console:** a per-aircraft summary showing registration, type,
flight, route, and a count of each REP type received. With `--text`,
full per-message detail is also printed.

**HTML report:** a dark-themed, searchable report with one section per
aircraft, each containing a card per REP message. Each card shows the
verified header fields, the event note, and an expandable "Raw section
data" panel with every decoded data section displayed in full. Use the
search box at the top to filter by registration, flight, route, or REP
type.

---

## Troubleshooting

**"No MIAM REP blocks found"** — the log file doesn't contain any
label `MA` ACARS messages, or dumpvdl2 wasn't built/configured to
decode MIAM payloads. Check with:

```bash
grep -c "MIAM CORE Data" yourlogfile.log
```

If this returns `0`, there's nothing for the decoder to extract from
that file.

**"No records after filtering"** — your `--aircraft` or `--rep` filter
didn't match anything in the supplied log files. Check the
registration spelling/case, or run without filters first to see what's
actually present.

**Garbled or missing route/flight info** — some REP messages
(particularly `REP081` takeoff reports) use a different internal
structure and may not populate all header fields. This is expected and
shown as `—` in the report rather than guessed at.

---

## Related tool

`miam_decoder.py` is a standalone companion to `dumpvdl2_analyser.py`,
which produces the full per-aircraft conversation report covering all
ACARS/ATN traffic (not just MIAM). Use the analyser for an overall
daily overview, and this decoder when you specifically want to drill
into A350 FOQA performance reporting.
