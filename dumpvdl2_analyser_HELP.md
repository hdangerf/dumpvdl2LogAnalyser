# dumpvdl2 Analyser — Help File

`dumpvdl2_analyser.py` parses daily dumpvdl2 plain-text log files
(ATN/AVLC format), builds a per-aircraft conversation summary, decodes
message content where the structure is verifiable, flags sessions
that deserve closer attention, and produces a searchable HTML report
with optional CSV and SQLite outputs.

---

## Basic usage

```bash
python dumpvdl2_analyser.py <logfile> [<logfile2> ...]
```

At least one log file is required. Multiple files are merged before
analysis — useful when dumpvdl2 rotates logs hourly or you want a
multi-day view.

```bash
python dumpvdl2_analyser.py pi21vdl2_20260329.log
python dumpvdl2_analyser.py pi21vdl2_202603*.log
```

---

## Command line options

### `--output FILE`
Path for the HTML report. Defaults to `report.html` in the current
directory.

```bash
python dumpvdl2_analyser.py --output /home/pi/reports/20260329.html pi21vdl2_20260329.log
```

### `--csv`
Exports a flat CSV summary alongside the HTML report, written to the
same path/name as `--output` but with a `.csv` extension. Rows are
sorted by flag count descending, so the most interesting aircraft
appear first.

Columns: `icao, registration, flight, ac_type, first_seen, last_seen,
duration_min, msg_count, acars_count, avg_snr_db, positions,
flag_count, flags, labels, gs_contacts`

```bash
python dumpvdl2_analyser.py --csv pi21vdl2_20260329.log
# produces report.html + report.csv
```

### `--db`
Writes all records and analysis results to a SQLite database, saved
alongside your `--output` file (same name, `.db` extension). Three
tables are created or appended to:

| Table | Contents |
|---|---|
| `aircraft` | One row per ICAO — summary stats and flags |
| `messages` | Every parsed record with all decoded fields |
| `flags` | One row per flag per aircraft |
| `positions` | Decoded lat/lon from ADS-C and AOC position reports |

Running this repeatedly across multiple days accumulates data, making
it easy to query trends over time with `sqlite3` directly:

```bash
python dumpvdl2_analyser.py --db --output 20260329.html pi21vdl2_20260329.log
python dumpvdl2_analyser.py --db --output 20260330.html pi21vdl2_20260330.log
sqlite3 20260330.db "SELECT registration, COUNT(*) FROM messages GROUP BY registration ORDER BY 2 DESC LIMIT 20"
```

If a database from an older version of the script already exists, the
analyser automatically adds any missing columns rather than failing —
no need to delete old databases when upgrading the script.

### `--freq-threshold N`
Sets the messages-per-minute burst detection threshold. Default is
`10`. The `HIGH_FREQ_BURST` flag triggers the first time any 60-second
sliding window contains N or more records for a single aircraft.

Lower values catch more marginal bursts; higher values only flag
genuinely unusual activity.

```bash
python dumpvdl2_analyser.py --freq-threshold 20 pi21vdl2_20260329.log
```

### `--verbose`
Prints a per-file record count to the console while parsing. Useful
when processing many files to confirm they're being read and to spot
any producing zero records.

```bash
python dumpvdl2_analyser.py --verbose *.log
```

### Combining options

```bash
python dumpvdl2_analyser.py \
    --output /data/reports/weekly.html \
    --csv --db \
    --freq-threshold 15 \
    --verbose \
    pi21vdl2_202603*.log
```

---

## Flag reference

These are the conditions the analyser detects and raises against each
aircraft's session:

| Flag | Trigger | Why it matters |
|---|---|---|
| `UNREGISTERED` | No registration found for the ICAO address | Could be military, test aircraft, or simply a callsign the log never captured |
| `NOTABLE_LABEL` | ACARS labels `80` `8E` `85` `38` `4R` `42` `HX` `B6` `DF` `MA` seen | Engine data, fuel, weight & balance, flight plans, MIAM/FOQA reports — generally higher-value payloads worth a closer look |
| `HIGH_FREQ_BURST` | ≥ N messages within any 1-minute window (see `--freq-threshold`) | Often ATN session establishment, an active CPDLC exchange, or a data burst |
| `HAS_ACARS_PAYLOAD` | At least one ACARS message body was present | Actual readable content exists in this session — worth reading |
| `ATN_DATALINK` | ATN protocols ACSE, CPDLC, CM, or X.227 observed | An active Air Traffic datalink session — real controller–pilot communication |
| `REASSEMBLY_ISSUES` | 3+ out-of-sequence or incomplete message fragments | The aircraft may have been at the edge of VHF range or switching ground stations mid-transfer |
| `WEAK_SIGNAL` | Average SNR below 18 dB across 5+ records | Distant or marginal reception |
| `MULTI_GS` | 3+ different ground stations contacted in one session | Aircraft transiting coverage boundaries — common during climb or descent |

---

## ACARS label reference

The analyser recognises the following ACARS labels and attempts to
decode their content where the structure is verifiable:

| Label | Meaning |
|---|---|
| `H1` | ADS-C / position report |
| `H2` | ADS-C position report (alternate format) |
| `_d` | ACK / downlink acknowledgement |
| `Q0` | Pre-departure clearance (PDC) request |
| `Q1` | PDC response |
| `SA` | Media Advisory / datalink status |
| `MA` | MIAM encoded message (A350 approach/landing FOQA report) |
| `16` | Position report (Ryanair/Buzz AOC format) |
| `10` | Free text |
| `15` | D-ATIS (digital ATIS) |
| `B6` | ADS-C position report (ARINC 620 format) |
| `B9` | ATC message |
| `80` | AOC / position report (free text variants) |
| `4A` | Cabin crew message |
| `49` | ACARS downlink |
| `DL` | Datalink initiation |
| `1B` | Uplink message |
| `31` | ATC clearance |
| `30` | ATC message |
| `4R` | Fuel data |
| `42` | Flight plan |
| `38` | Weight & balance |
| `36` | Cargo manifest |
| `12` | Company message |
| `HX` | Position/ATC extended |
| `8E` | Engine trend monitoring |
| `85` | Engine data / MVA report |
| `DF` | ADS-C downlink |

---

## What gets decoded — and how reliably

The analyser includes dedicated decoders for several message types.
Confidence varies, and it's worth knowing which is which:

**Reliable — built from dumpvdl2's own decoded output:**
- **CPDLC** — message type, ID/reference, timestamp, ACK requirement.
  dumpvdl2 already decodes the CPDLC PDU; the analyser just extracts
  it into a clean summary.
- **ADS-C** — position, altitude, ground speed, track, vertical
  speed, next waypoint. Same situation — dumpvdl2 decodes the ADS-C
  PDU natively.
- **XID Handoff** — destination airport, aircraft location at handoff,
  alternate ground stations.

**Reasonably reliable — pattern-matched from plain text fields:**
- **Media Advisory (SA)** — link established/lost events and
  available link types.
- **D-ATIS (label 15)** — origin and destination ICAO from the header.
- **AOC position reports (label 16, label 80 "3N01")** — time, fuel,
  course, altitude, lat/lon parsed from comma-delimited fields with a
  consistent, recognisable structure.
- **MVA / engine reports (label 80/85)** — flight, registration,
  airport, date from a consistent header format.
- **Weight & Balance (label 38)** — flight number and date; weight
  values are approximate, parsed heuristically from the available
  numeric fields.

**MIAM (label MA) — header only, by design:**
The MIAM decoder extracts the route, flight number, REP type, phase,
and any free-text event note, all of which are verified against other
fields in the same log block. It deliberately does **not** attempt to
decode the underlying flight parameter sections (gross weight,
approach speed, gear pressure, etc.), because the field-to-unit
mapping for those is not publicly documented and earlier attempts to
guess it produced clearly wrong values once checked against a wider
sample. If you want the full breakdown of MIAM data sections, use the
companion `miam_decoder.py` script, which shows the same raw values in
a dedicated report.

---

## Output

**Console:** total aircraft, total records, flag count, a list of
every flagged aircraft with its flag details, and a bar-chart style
list of the most active aircraft by message count.

**HTML report:** dark-themed and searchable. A "Flagged for Review"
panel sits at the top listing every flag across all aircraft. Below
that, a sortable summary table of all aircraft, and below that, a
per-aircraft detail section with decoded message highlights and a full
expandable message timeline including ground station, frequency, SNR,
ACARS label, reassembly status, and ATN protocol information.

**CSV (`--csv`):** one row per aircraft, suitable for spreadsheet
analysis or quick filtering.

**SQLite (`--db`):** full relational data for ad-hoc querying or
building your own dashboards across multiple days.

---

## Troubleshooting

**Record count looks far lower than expected** — this usually means
the header timezone regex isn't matching. Older versions of this
script hardcoded `GMT` in the timestamp regex, which silently dropped
every record after a BST/GMT clock change (caught a 99.5% data loss
this way in March 2026). The current version matches any 2–4 letter
timezone abbreviation, so this shouldn't recur, but if you see a
similar drop-off, check:

```bash
grep -oP '\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \K[A-Z]+' yourlog.log | sort -u
```

This shows every distinct timezone label in the file — if the script
predates support for one of them, that's the cause.

**`--db` fails with a database error** — if you have a `.db` file from
a very old version of the script with an incompatible schema, the
auto-migration should handle most cases, but if it doesn't, delete the
old `.db` file and re-run; a fresh one will be created.

**No records parsed at all** — confirm the log file actually matches
the expected header format:

```bash
head -1 yourlog.log
```

Should look like:
`[2026-03-29 06:04:46 BST] [136.850] [-2.5/-27.7 dBFS] [25.2 dB] [-10.5 ppm]`

If your dumpvdl2 build outputs a different format (e.g. JSON), this
script will need adapting — let me know and I can extend it.

---

## Related tool

`miam_decoder.py` is a standalone companion script focused entirely on
decoding label `MA` MIAM/REP messages (A350 FOQA performance reports)
in much greater structural detail than this analyser provides inline.
Use this analyser for the full daily traffic overview, and the MIAM
decoder when you want to drill into approach/landing performance
reporting specifically.
