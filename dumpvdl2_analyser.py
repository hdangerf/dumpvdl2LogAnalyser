#!/usr/bin/env python3
"""
dumpvdl2 Log Analyser  –  ATN/AVLC format  (full message decode edition)
=========================================================================
Parses daily dumpvdl2 plain-text log files, decodes message content for
all major ACARS/ATN message types, builds per-aircraft conversation
summaries, flags sessions deserving further analysis, and writes a
self-contained HTML report.

Usage:
    python dumpvdl2_analyser.py [options] <logfile> [<logfile2> ...]

Options:
    --output FILE        HTML report path (default: report.html)
    --csv                Also export a CSV summary
    --db                 Also store results in SQLite (dumpvdl2.db)
    --freq-threshold N   Messages/min burst threshold (default: 10)
    --verbose            Print progress to console

Example:
    python dumpvdl2_analyser.py pi21vdl2_20260329.log
    python dumpvdl2_analyser.py --csv --db *.log
"""

import re, sys, csv, sqlite3, argparse, textwrap
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
from html import escape

# ─────────────────────────────────────────────────────────
#  ACARS LABEL REFERENCE
# ─────────────────────────────────────────────────────────
ACARS_LABELS = {
    "H1":"ADS-C / position report","H2":"ADS-C position report (alt)",
    "_d":"ACK / downlink ack","Q0":"Pre-departure clearance (PDC)",
    "Q1":"PDC response","SA":"Media Advisory / link status",
    "MA":"MIAM encoded message (approach/landing performance)",
    "16":"Position report (Ryanair/AOC format)",
    "10":"Free text","15":"D-ATIS (digital ATIS)",
    "B6":"ADS-C position report (ARINC 620)",
    "B9":"ATC message","80":"AOC / position report (free text)",
    "4A":"Cabin crew","49":"ACARS downlink",
    "DL":"Datalink init","1B":"Uplink message","31":"ATC clearance",
    "30":"ATC message","4R":"Fuel data","42":"Flight plan",
    "38":"Weight & balance","36":"Cargo manifest","12":"Company message",
    "HX":"Position/ATC extended","8E":"Engine trend","85":"Engine data / MVA",
    "DF":"ADS-C downlink",
}
FLAG_LABELS = {"80","8E","85","38","4R","42","HX","B6","DF","MA"}

# ─────────────────────────────────────────────────────────
#  BLOCK-LEVEL REGEXES
# ─────────────────────────────────────────────────────────
_HDR  = re.compile(r'^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) [A-Z]{2,4}\]\s+\[([\d.]+)\]\s+\[[^\]]+\]\s+\[([\d.]+) dB\]')
_DIR  = re.compile(r'^([0-9A-Fa-f]{6})\s+\([^)]+\)\s+->\s+([0-9A-Fa-f]+)\s+\([^)]+\):\s+(\w+)')
_AC   = re.compile(r'^AC info:\s*([^,]+),\s*([^,]+),\s*(.*)')
_GS   = re.compile(r'^GS info:\s*([A-Z0-9]{4}),\s*(.*)')
_AVLC = re.compile(r'AVLC type:\s*(.*)')
_AREG = re.compile(r'Reg:\s*\.?([A-Z0-9\-]+)\s+Flight:\s*([A-Z0-9]+)',re.I)
_ALBL = re.compile(r'Mode: \d+ Label:\s*(\S+)')
_ASUB = re.compile(r'Sublabel:\s*(\S+)')
_AMSG = re.compile(r'Msg num:\s*(\S+)')
_ARSM = re.compile(r'Reassembly:\s*(.*)')
_AMSG_BODY = re.compile(r'^\s{3}Message:$')
_PDU  = re.compile(r'(IDRP \w+|ACSE \w+|COTP \w+|X\.224 \w+|X\.225 \w+|X\.227 \w+|CM \w+|CPDLC)')

# ─────────────────────────────────────────────────────────
#  MESSAGE DECODERS
# ─────────────────────────────────────────────────────────

def decode_message(label: str, sublabel: str, raw: str, block_raw: str) -> dict:
    """
    Attempt to decode the message payload into structured fields.
    Returns a dict with 'type', 'summary', and any decoded fields.
    Always falls back gracefully.
    """
    label = label.upper().strip()
    text  = raw.strip()

    # ── CPDLC decoded by dumpvdl2 directly ────────────────
    cpdlc = _decode_cpdlc(block_raw)
    if cpdlc:
        return cpdlc

    # ── ADS-C decoded by dumpvdl2 directly ────────────────
    adsc = _decode_adsc(block_raw)
    if adsc:
        return adsc

    # ── XID Handoff (in raw block, not ACARS message) ─────
    xid = _decode_xid(block_raw)
    if xid:
        return xid

    # ── Media Advisory (SA label) ─────────────────────────
    if label == "SA":
        return _decode_media_advisory(block_raw, text)

    # ── MIAM (label MA or inner MIAM CORE block) ───────────────
    if label == "MA" or "MIAM CORE Data" in block_raw:
        return _decode_miam(block_raw, text)

    # ── D-ATIS (label 15) ─────────────────────────────────
    if label == "15":
        return _decode_atis(text)

    # ── Ryanair AOC position (label 16) ───────────────────
    if label == "16":
        return _decode_label16(text)

    # ── H1/DF — ADS-C or position report ─────────────────
    if label == "H1" and sublabel in ("DF",""):
        return _decode_h1(text)

    # ── B6 — ADS-C ARINC 620 (already decoded above mostly)
    if label == "B6":
        return _decode_b6(text, block_raw)

    # ── Label 80 — AOC free-text position / MVA ───────────
    if label == "80":
        return _decode_label80(text)

    # ── Label 85 — MVA / engine report ───────────────────
    if label == "85":
        return _decode_label85(text)

    # ── Label 38 — Weight & balance ───────────────────────
    if label == "38":
        return _decode_label38(text)

    # ── Label 10 — Free text ──────────────────────────────
    if label == "10":
        return {"type":"Free text","summary":text[:120],"text":text}

    # ── Label Q0 — PDC (empty body common) ────────────────
    if label == "Q0":
        return {"type":"Pre-departure clearance","summary":"PDC request","text":text}

    # ── Fallback ──────────────────────────────────────────
    if text:
        return {"type":ACARS_LABELS.get(label,"Unknown"),"summary":text[:80],"text":text}
    return {"type":ACARS_LABELS.get(label,"Unknown"),"summary":"(no decoded content)","text":""}


def _decode_cpdlc(block_raw: str) -> dict | None:
    """Extract CPDLC message data decoded by dumpvdl2."""
    if "CPDLC" not in block_raw and "FANS-1" not in block_raw:
        return None
    lines = block_raw.splitlines()
    msg_type = ""
    msg_data = []
    msg_id = msg_ref = timestamp = ack = ""
    in_data = False

    for line in lines:
        s = line.strip()
        if "FANS-1/A CPDLC" in s or "CPDLC Downlink" in s or "CPDLC Uplink" in s:
            msg_type = s
        if re.match(r'Msg ID:', s):    msg_id  = s.split(":",1)[1].strip()
        if re.match(r'Msg Ref:', s):   msg_ref = s.split(":",1)[1].strip()
        if re.match(r'Timestamp:', s): timestamp = s.split(":",1)[1].strip()
        if re.match(r'Logical ACK:', s): ack = s.split(":",1)[1].strip()
        if s == "Message data:": in_data = True; continue
        if in_data and s and not s.startswith("Header") and not s.startswith("["):
            msg_data.append(s)

    if not msg_type and not msg_data:
        return None

    payload = " | ".join(msg_data) if msg_data else "(session management)"
    summary_parts = []
    if msg_type: summary_parts.append(msg_type.replace("FANS-1/A ","").replace("CPDLC ",""))
    if msg_data: summary_parts.append(payload[:100])

    return {
        "type": "CPDLC",
        "summary": " — ".join(summary_parts)[:120],
        "cpdlc_type": msg_type,
        "msg_id": msg_id,
        "msg_ref": msg_ref,
        "timestamp": timestamp,
        "ack_required": ack,
        "payload": payload,
    }


def _decode_adsc(block_raw: str) -> dict | None:
    """Extract ADS-C position decoded by dumpvdl2."""
    if "ADS-C message:" not in block_raw:
        return None
    lines = block_raw.splitlines()
    fields = {}
    report_type = ""
    next_wpt = {}
    in_next = False

    for line in lines:
        s = line.strip()
        if "ADS-C message:" in s: continue
        if re.match(r'(Basic report|Waypoint change event|Emergency|Extended projected profile):', s):
            report_type = s.rstrip(":")
        m = re.match(r'Lat:\s*([-\d.]+)', s)
        if m and "lat" not in fields: fields["lat"] = float(m.group(1))
        m = re.match(r'Lon:\s*([-\d.]+)', s)
        if m and "lon" not in fields: fields["lon"] = float(m.group(1))
        m = re.match(r'Alt:\s*([-\d.]+)\s*ft', s)
        if m and "alt_ft" not in fields: fields["alt_ft"] = float(m.group(1))
        m = re.match(r'True track:\s*([\d.]+)', s)
        if m: fields["track_deg"] = float(m.group(1))
        m = re.match(r'Ground speed:\s*([\d.]+)\s*kt', s)
        if m: fields["gs_kt"] = float(m.group(1))
        m = re.match(r'Vertical speed:\s*([-\d.]+)\s*ft/min', s)
        if m: fields["vs_fpm"] = float(m.group(1))
        m = re.match(r'Flight ID:\s*(\S+)', s)
        if m: fields["flight_id"] = m.group(1).strip()
        m = re.match(r'True airspeed:\s*([\d.]+)\s*kt', s)
        if m: fields["tas_kt"] = float(m.group(1))
        m = re.match(r'Heading:\s*([\d.]+)\s*deg', s)
        if m: fields["heading"] = float(m.group(1))
        if "Next waypoint:" in s: in_next = True
        if in_next:
            m = re.match(r'Lat:\s*([-\d.]+)', s)
            if m and "lat" not in next_wpt: next_wpt["lat"] = float(m.group(1))
            m = re.match(r'Lon:\s*([-\d.]+)', s)
            if m and "lon" not in next_wpt: next_wpt["lon"] = float(m.group(1))
            m = re.match(r'ETA:\s*(\d+)\s*sec', s)
            if m: next_wpt["eta_sec"] = int(m.group(1))

    if not fields:
        return None

    parts = []
    if "lat" in fields and "lon" in fields:
        parts.append(f"Pos: {fields['lat']:.4f}°, {fields['lon']:.4f}°")
    if "alt_ft" in fields:
        parts.append(f"Alt: {int(fields['alt_ft'])} ft")
    if "gs_kt" in fields:
        parts.append(f"GS: {fields['gs_kt']:.0f} kt")
    if "track_deg" in fields:
        parts.append(f"Trk: {fields['track_deg']:.0f}°")
    if "vs_fpm" in fields:
        vs = fields["vs_fpm"]
        parts.append(f"VS: {'+' if vs>=0 else ''}{int(vs)} fpm")

    return {
        "type": f"ADS-C {report_type}".strip(),
        "summary": "  ".join(parts)[:120],
        **fields,
        "next_waypoint": next_wpt if next_wpt else None,
    }


def _decode_xid(block_raw: str) -> dict | None:
    """Extract VDL2 XID handoff information."""
    if "XID: Handoff" not in block_raw:
        return None
    lines = block_raw.splitlines()
    fields = {"type": "XID Handoff"}
    for line in lines:
        s = line.strip()
        m = re.match(r'Destination airport:\s*(\S+)', s)
        if m: fields["dest_airport"] = m.group(1)
        m = re.match(r'Aircraft location:\s*(.*)', s)
        if m: fields["ac_location"] = m.group(1).strip()
        m = re.match(r'XID sequencing:\s*seq:\s*(\d+)\s*retry:\s*(\d+)', s)
        if m: fields["xid_seq"] = m.group(1); fields["xid_retry"] = m.group(2)
        if "Alternate ground stations:" in s:
            fields["alt_gs"] = s.split(":",1)[1].strip()
    dest = fields.get("dest_airport","?")
    loc  = fields.get("ac_location","")
    fields["summary"] = f"Handoff → {dest}" + (f"  ({loc})" if loc else "")
    return fields


# MIAM CORE REP report — phase codes
_MIAM_PHASE = {
    "01": "Departure", "02": "Approach/Landing", "03": "Cruise",
    "04": "Diversion", "05": "Ground",
}
_MIAM_SUBPHASE = {
    "01": "Before T/O", "02": "Gear Up", "03": "Flap Retract",
    "04": "Stable", "05": "Gear Down", "06": "Touchdown",
}
_MIAM_REP = {
    "REP001": "Approach stabilisation (5000 ft)",
    "REP002": "Approach stabilisation (alt)",
    "REP004": "Approach sequence",
    "REP015": "Flap/slat config",
    "REP020": "Gear extension",
    "REP024": "Approach snapshot",
    "REP053": "Landing snapshot",
    "REP081": "Takeoff performance",
}


def _decode_miam(block_raw: str, text: str) -> dict:
    """
    Decode MIAM CORE encoded messages (label MA).
    dumpvdl2 handles decompression/reassembly; we decode the inner
    ACARS payload which is an Airbus/Boeing approach & landing
    performance REP report.

    H01 field layout (comma-separated):
      rep_num, phase, subphase, altitude_ft, distance_nm, reg,
      engine_count, spare, dd, mm, yy, HH, MM, SS, runway_heading
    H02 field layout:
      "ORIG DEST", flight_id, software_ref1, software_ref2, config_ref
    H03: free text event description
    """
    fields = {"type": "MIAM"}

    # ── Check for non-ACARS proprietary payload ───────────────────
    app_m = re.search(r'Application ID:\s*(\S+)', block_raw)
    if app_m:
        app_id = app_m.group(1)
        fields["type"]    = "MIAM (proprietary)"
        fields["app_id"]  = app_id
        fields["summary"] = f"MIAM proprietary payload  App ID: {app_id}  (not decodable)"
        return fields

    # ── Extract inner decoded message from MIAM block ─────────────
    # dumpvdl2 prints the decoded payload after "Message:" inside the MIAM block
    miam_msg = re.search(r'MIAM CORE Data.*?Message:[ \t]*\n[ \t]*([^\n]+)', block_raw, re.DOTALL)
    payload = miam_msg.group(1).strip() if miam_msg else text.strip()

    if not payload:
        fields["summary"] = "MIAM (no decoded payload)"
        return fields

    # ── REP type ──────────────────────────────────────────────────
    rep_m = re.search(r'/(REP\d+)', payload)
    rep   = rep_m.group(1) if rep_m else ""
    fields["rep_type"]  = rep
    fields["rep_label"] = _MIAM_REP.get(rep, rep)

    # ── H01 — identity and event context ─────────────────────────
    h01_m = re.search(r'H01,([^/;\r\n]+)', payload)
    if h01_m:
        h = h01_m.group(1).split(',')
        if len(h) >= 15:
            fields["rep_num"]    = h[0].strip()
            phase_code           = h[1].strip()
            subphase_code        = h[2].strip()
            fields["phase"]      = f"{_MIAM_PHASE.get(phase_code, phase_code)} / {_MIAM_SUBPHASE.get(subphase_code, subphase_code)}"
            fields["phase_code"] = f"{phase_code}/{subphase_code}"
            try: fields["altitude_ft"]  = int(h[3])
            except: pass
            try: fields["distance_nm"]  = int(h[4])
            except: pass
            fields["ac_reg"]     = h[5].strip().lstrip('.')
            try: fields["engine_count"] = int(h[6])
            except: pass
            # Date/time: dd, mm, yy, HH, MM, SS
            try:
                dd,mm,yy,HH,MM,SS = h[8],h[9],h[10],h[11],h[12],h[13]
                fields["event_time"] = f"20{yy}-{mm}-{dd} {HH}:{MM}:{SS}"
            except: pass
            try: fields["runway_hdg"] = int(h[14]) if h[14].strip().isdigit() else h[14].strip()
            except: pass

    # ── H02 — route and flight identity ──────────────────────────
    h02_m = re.search(r'H02,([^/;\r\n]+)', payload)
    if h02_m:
        h2 = h02_m.group(1).split(',')
        if h2:
            route = h2[0].strip()
            if len(route) >= 9:
                fields["origin_icao"] = route[:4]
                fields["dest_icao"]   = route[5:9]
            elif len(route) == 8:
                fields["origin_icao"] = route[:4]
                fields["dest_icao"]   = route[4:]
        if len(h2) >= 2:
            fields["flight_id"] = h2[1].strip()
        if len(h2) >= 5:
            fields["config_ref"] = h2[4].strip()

    # ── H03 — free text event description ────────────────────────
    h03_m = re.search(r'H03,([^/;\r\n]+)', payload)
    if h03_m:
        event_text = h03_m.group(1).strip()
        if event_text:
            fields["event_desc"] = event_text

    # ── Build summary ─────────────────────────────────────────────
    parts = []
    if rep:
        parts.append(f"{rep}  {fields['rep_label']}")
    orig = fields.get("origin_icao","")
    dest = fields.get("dest_icao","")
    flt  = fields.get("flight_id","")
    if orig and dest:
        parts.append(f"{orig} → {dest}")
    if flt:
        parts.append(flt)
    if "altitude_ft" in fields:
        parts.append(f"Alt: {fields['altitude_ft']} ft")
    if "distance_nm" in fields:
        parts.append(f"Dist: {fields['distance_nm']} nm")
    if "runway_hdg" in fields:
        hdg = fields["runway_hdg"]
        if isinstance(hdg, int) and hdg > 0:
            parts.append(f"Rwy: {hdg:03d}°")
    if "event_desc" in fields:
        parts.append(f"[{fields['event_desc']}]")
    if "phase" in fields:
        parts.append(f"Phase: {fields['phase']}")

    fields["summary"] = "  ".join(parts) if parts else "MIAM approach/landing report"
    fields["raw_payload"] = payload[:500]
    return fields


def _decode_media_advisory(block_raw: str, text: str) -> dict:
    """Decode SA label Media Advisory — link status events."""
    lines = block_raw.splitlines()
    events = []
    links  = ""
    for line in lines:
        s = line.strip()
        m = re.match(r'Link (\S.*?) (established|lost) at ([\d:]+)', s, re.I)
        if m: events.append(f"{m.group(2).upper()} {m.group(1)} @ {m.group(3)}")
        m = re.match(r'Available links:\s*(.*)', s)
        if m: links = m.group(1).strip()
    summary = " | ".join(events) if events else text[:80]
    if links: summary += f"  [Links: {links}]"
    return {"type":"Media Advisory","summary":summary,"events":events,"available_links":links}


def _decode_atis(text: str) -> dict:
    """
    Decode D-ATIS label 15 message.
    Format: FST01<origin><airport_icao><lat><lon><wind_dir><wind_spd><vis>...
    Example: FST01FAOREGLLN514777W0004034000 1311373 ...
    """
    fields = {"type":"D-ATIS"}
    m = re.match(r'FST01(\S{4})(\S{4})', text)
    if m:
        fields["origin_icao"] = m.group(1)
        fields["dest_icao"]   = m.group(2)
    # lat/lon embedded
    m = re.search(r'([NS])(\d{2})(\d{4})([EW])(\d{3})(\d{4})', text)
    if m:
        lat = int(m.group(2)) + int(m.group(3))/10000
        if m.group(1)=="S": lat = -lat
        lon = int(m.group(5)) + int(m.group(6))/10000
        if m.group(4)=="W": lon = -lon
        fields["lat"] = lat; fields["lon"] = lon
    dest = fields.get("dest_icao","?")
    orig = fields.get("origin_icao","?")
    fields["summary"] = f"D-ATIS  {orig} → {dest}"
    fields["text"] = text
    return fields


def _decode_label16(text: str) -> dict:
    """
    Ryanair / Buzz AOC position report label 16.
    Format: HHMMSS,FFFFFFF,CCCC,AAAA,N LL.LLL E/W LLL.LLL
    Fields: time, fuel(kg?), course, altitude(ft), lat, lon
    """
    fields = {"type":"AOC Position Report"}
    parts = [p.strip() for p in text.split(",")]
    if len(parts) >= 5:
        try:
            t = parts[0].zfill(6)
            fields["time_utc"] = f"{t[:2]}:{t[2:4]}:{t[4:6]}"
        except: pass
        try: fields["fuel_kg"] = int(parts[1])
        except: pass
        try: fields["course_deg"] = int(parts[2])
        except: pass
        try: fields["alt_ft"] = int(parts[3])
        except: pass
        pos_str = " ".join(parts[4:])
        m = re.search(r'([NS])\s*([\d.]+)\s+([EW])\s*([\d.]+)', pos_str)
        if m:
            lat = float(m.group(2)); lon = float(m.group(4))
            if m.group(1)=="S": lat=-lat
            if m.group(3)=="W": lon=-lon
            fields["lat"]=lat; fields["lon"]=lon
    parts_out = []
    if "lat" in fields and "lon" in fields:
        parts_out.append(f"Pos: {fields['lat']:.3f}°, {fields['lon']:.3f}°")
    if "alt_ft" in fields:
        parts_out.append(f"Alt: {fields['alt_ft']} ft")
    if "course_deg" in fields:
        parts_out.append(f"Course: {fields['course_deg']}°")
    if "fuel_kg" in fields:
        parts_out.append(f"Fuel: {fields['fuel_kg']} kg")
    fields["summary"] = "  ".join(parts_out) if parts_out else text[:80]
    fields["text"] = text
    return fields


def _decode_h1(text: str) -> dict:
    """H1/DF messages — may be ADS-C position or various AOC formats."""
    fields = {"type":"H1 Report","text":text}
    # Try PRG/DTLF... format (progress report)
    m = re.match(r'PRG/(\S+),(\d+)([OC]),(\d+),(\d{10})', text)
    if m:
        fields["type"]    = "Progress Report"
        fields["station"] = m.group(1)
        fields["eta"]     = m.group(4)
        fields["summary"] = f"Progress report via {m.group(1)}"
        return fields
    # APM performance monitoring
    if text.startswith("APM") or "APM" in text[:10]:
        fields["type"]    = "AOC Performance Monitor"
        fields["summary"] = "Aircraft performance monitoring data"
        return fields
    # Generic — show trimmed
    fields["summary"] = text[:100].replace("\r\n"," ").replace("\n"," ") if text else "(empty)"
    return fields


def _decode_b6(text: str, block_raw: str) -> dict:
    """B6 ADS-C — mostly decoded by dumpvdl2, extract flight ID from raw."""
    adsc = _decode_adsc(block_raw)
    if adsc:
        adsc["type"] = "ADS-C B6"
        return adsc
    # Fallback: parse hex blob partially
    m = re.search(r'ADS\.([A-Z0-9\-]+)', text)
    reg = m.group(1) if m else ""
    return {"type":"ADS-C B6","summary":f"ADS-C report{(' for '+reg) if reg else ''}","text":text}


def _decode_label80(text: str) -> dict:
    """
    Label 80 — free-text AOC/position report.
    Several subtypes seen: 3N01 POSRPT, 3M01 OPNORM, MVA header
    """
    fields = {"type":"AOC Report","text":text}
    # MVA: just header line
    m = re.match(r'MVA\s+([A-Z0-9]+)/(\d{2}[A-Z]{3}\d{2})\s*\.\s*([A-Z0-9\-]+)\s*\.\s*([A-Z]{4})', text)
    if m:
        fields["type"]    = "MVA Report"
        fields["flight"]  = m.group(1)
        fields["date"]    = m.group(2)
        fields["reg"]     = m.group(3)
        fields["airport"] = m.group(4)
        fields["summary"] = f"MVA  {m.group(1)}  {m.group(3)}  {m.group(4)}  {m.group(2)}"
        return fields
    # 3N01 POSRPT
    m = re.match(r'3N01 POSRPT\s+(\S+)/(\S+)', text)
    if m:
        pos_m = re.search(r'POS\s+([NS]\d+[EW]\d+)', text)
        alt_m = re.search(r'ALT\s+(\d+)', text)
        fob_m = re.search(r'FOB\s+(\d+)', text)
        parts = [f"Route: {m.group(1)}/{m.group(2)}"]
        if pos_m: parts.append(f"Pos: {pos_m.group(1)}")
        if alt_m: parts.append(f"Alt: {alt_m.group(1)} ft")
        if fob_m: parts.append(f"Fuel: {fob_m.group(1)} kg")
        fields["type"]    = "AOC Position Report"
        fields["summary"] = "  ".join(parts)
        return fields
    # 3M01 OPNORM
    m = re.match(r'3M01 OPNORM\s+(\S+)', text)
    if m:
        fields["type"]    = "AOC Normal Ops"
        fields["summary"] = f"Normal ops report  {m.group(1)}"
        return fields
    fields["summary"] = text[:100].replace("\r\n"," ")
    return fields


def _decode_label85(text: str) -> dict:
    """Label 85 — MVA / engine data report."""
    fields = {"type":"Engine/MVA Report","text":text}
    m = re.match(r'MVA\s+([A-Z0-9/]+)/(\d{2}[A-Z]{3}\d{2})\s*\.\s*([A-Z0-9\-]+)\s*\.\s*([A-Z]{4})', text.strip())
    if m:
        fields["type"]    = "MVA"
        fields["flight"]  = m.group(1)
        fields["date"]    = m.group(2)
        fields["reg"]     = m.group(3)
        fields["airport"] = m.group(4)
        fields["summary"] = f"MVA  {m.group(1)}  {m.group(3)}  {m.group(4)}  {m.group(2)}"
    else:
        fields["summary"] = text[:80].replace("\r\n"," ")
    return fields


def _decode_label38(text: str) -> dict:
    """
    Label 38 — Weight & Balance.
    Finnair format: /AY1335/290326/W   .284/N 51.482/1993/.../30M
    Other format:   SXS7LK/EGCC/1601/  51/30M
    """
    fields = {"type":"Weight & Balance","text":text}
    # Finnair /AYnnnn/date/ format
    m = re.match(r'/([A-Z]{2}\d+)/(\d{6})/', text)
    if m:
        fields["flight"] = m.group(1)
        d = m.group(2)
        fields["date"]   = f"20{d[4:6]}-{d[2:4]}-{d[:2]}"
    wm = re.search(r'W\s+([\d.]+)', text)
    nm = re.search(r'N\s+([\d.]+)', text)
    if wm: fields["wind_speed"] = wm.group(1)
    if nm: fields["lat_approx"] = nm.group(1)
    # Gross weight / ZFW
    nums = re.findall(r'\b(\d{4,6})\b', text)
    if len(nums) >= 2:
        fields["gw_kg"]  = nums[-2] if len(nums)>1 else ""
        fields["zfw_kg"] = nums[-1]
    fl = fields.get("flight","")
    fields["summary"] = f"W&B  {fl}  " + ("  ".join(f"{k}:{v}" for k,v in fields.items()
                         if k in ("gw_kg","zfw_kg","date")))
    return fields


# ─────────────────────────────────────────────────────────
#  LOG PARSER
# ─────────────────────────────────────────────────────────

def parse_log(path, verbose=False):
    records, current = [], []
    def flush(lines):
        if lines:
            r = _parse_block(lines)
            if r: records.append(r)
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if _HDR.match(line):
                flush(current); current = [line]
            else:
                current.append(line)
    flush(current)
    if verbose: print(f"  {len(records)} records from {Path(path).name}")
    return records


def _parse_block(lines):
    hm = _HDR.match(lines[0])
    if not hm: return None
    ts_str, freq, snr = hm.group(1), hm.group(2), hm.group(3)
    try: ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
    except ValueError: return None
    r = dict(timestamp=ts, freq_mhz=freq, snr_db=float(snr),
             src_icao="", dst_addr="", frame_type="",
             ac_reg="", ac_type="", ac_callsign="",
             gs_icao="", gs_name="", avlc_type="",
             has_acars=False, acars_reg="", acars_flight="",
             acars_label="", acars_sublabel="", acars_msg_num="",
             acars_reassembly="", acars_message="",
             atn_protocols=[], decoded=None, raw="\n".join(lines))
    in_msg, msg_lines = False, []
    for line in lines[1:]:
        s = line.strip()
        dm = _DIR.match(line)
        if dm:
            r["src_icao"]=dm.group(1).upper(); r["dst_addr"]=dm.group(2).upper()
            r["frame_type"]=dm.group(3); in_msg=False; continue
        am = _AC.match(line)
        if am:
            r["ac_reg"]=am.group(1).strip().lstrip(".")
            r["ac_type"]=am.group(2).strip()
            r["ac_callsign"]=am.group(3).strip().lstrip("x").strip("-")
            in_msg=False; continue
        gm = _GS.match(line)
        if gm:
            r["gs_icao"]=gm.group(1).strip(); r["gs_name"]=gm.group(2).strip()
            in_msg=False; continue
        vm = _AVLC.match(s)
        if vm: r["avlc_type"]=vm.group(1).strip(); in_msg=False; continue
        if s=="ACARS:": r["has_acars"]=True; in_msg=False; continue
        rm = _AREG.search(s)
        if rm: r["acars_reg"]=rm.group(1); r["acars_flight"]=rm.group(2); in_msg=False; continue
        lm = _ALBL.search(s)
        if lm and r["has_acars"]: r["acars_label"]=lm.group(1); in_msg=False; continue
        sl = _ASUB.search(s)
        if sl: r["acars_sublabel"]=sl.group(1); continue
        mn = _AMSG.search(s)
        if mn: r["acars_msg_num"]=mn.group(1); continue
        rsm = _ARSM.search(s)
        if rsm: r["acars_reassembly"]=rsm.group(1).strip(); continue
        if _AMSG_BODY.match(line): in_msg=True; continue
        if in_msg: msg_lines.append(line.rstrip()); continue
        pm = _PDU.search(s)
        if pm:
            proto = pm.group(1).split()[0]
            if proto not in r["atn_protocols"]: r["atn_protocols"].append(proto)
    r["acars_message"] = "\n".join(l for l in msg_lines if l.strip())
    if not r["acars_reg"] and r["ac_reg"] not in ("","-",".NO-REG"):
        r["acars_reg"] = r["ac_reg"]

    # Decode message content
    if r["acars_label"] or r["atn_protocols"] or "XID: Handoff" in r["raw"] or "ADS-C" in r["raw"]:
        r["decoded"] = decode_message(
            r["acars_label"], r["acars_sublabel"],
            r["acars_message"], r["raw"]
        )
    return r


# ─────────────────────────────────────────────────────────
#  ANALYSIS
# ─────────────────────────────────────────────────────────

def analyse(records, freq_threshold=10):
    by_icao = defaultdict(list)
    for r in records:
        by_icao[r["src_icao"] or "UNKNOWN"].append(r)
    aircraft = {}
    for icao, recs in by_icao.items():
        recs.sort(key=lambda x: x["timestamp"])
        first_ts, last_ts = recs[0]["timestamp"], recs[-1]["timestamp"]
        duration = (last_ts - first_ts).total_seconds()
        reg     = next((r["acars_reg"]  for r in reversed(recs) if r["acars_reg"]  not in ("","-",".NO-REG")), "")
        if not reg: reg = next((r["ac_reg"] for r in reversed(recs) if r["ac_reg"] not in ("","-",".NO-REG")), "")
        flight  = next((r["acars_flight"] for r in reversed(recs) if r["acars_flight"] not in ("","-")), "")
        ac_type = next((r["ac_type"]     for r in reversed(recs) if r["ac_type"]  not in ("","-")), "")
        freqs   = sorted({r["freq_mhz"] for r in recs if r["freq_mhz"]})
        gs_set  = {(r["gs_icao"],r["gs_name"]) for r in recs if r["gs_icao"]}
        acars_recs = [r for r in recs if r["acars_label"]]
        label_counts = defaultdict(int)
        for r in acars_recs: label_counts[r["acars_label"]] += 1
        atn_used = set()
        for r in recs: atn_used.update(r["atn_protocols"])
        snrs = [r["snr_db"] for r in recs]
        avg_snr = sum(snrs)/len(snrs) if snrs else 0
        reasm_issues = [r for r in recs if r["acars_reassembly"] in ("out of sequence","incomplete")]

        # Collect decoded highlights
        decoded_highlights = []
        positions = []
        for r in recs:
            d = r.get("decoded")
            if not d: continue
            dtype = d.get("type","")
            if "ADS-C" in dtype or dtype=="AOC Position Report":
                if "lat" in d and "lon" in d:
                    positions.append({"lat":d["lat"],"lon":d["lon"],"alt":d.get("alt_ft"),
                                      "ts":r["timestamp"].strftime("%H:%M:%S")})
            if d.get("summary") and dtype not in ("","ACK / downlink ack"):
                decoded_highlights.append({
                    "ts": r["timestamp"].strftime("%H:%M:%S"),
                    "type": dtype,
                    "summary": d.get("summary",""),
                })

        flags = []
        if not reg:
            flags.append(("UNREGISTERED","No registration found for this ICAO"))
        unusual = [l for l in label_counts if l in FLAG_LABELS]
        if unusual:
            flags.append(("NOTABLE_LABEL","Labels: "+" ".join(f"{l}({ACARS_LABELS.get(l,'?')})" for l in unusual)))
        for i,r in enumerate(recs):
            win = r["timestamp"]+timedelta(minutes=1)
            cnt = sum(1 for s in recs[i:] if s["timestamp"]<=win)
            if cnt>=freq_threshold:
                flags.append(("HIGH_FREQ_BURST",f"{cnt} msgs in 1 min at {r['timestamp'].strftime('%H:%M:%S')}")); break
        with_body = [r for r in acars_recs if r["acars_message"].strip()]
        if with_body:
            flags.append(("HAS_ACARS_PAYLOAD",f"{len(with_body)} ACARS message(s) with decoded content"))
        atn_int = atn_used & {"ACSE","CPDLC","CM","X.227"}
        if atn_int:
            flags.append(("ATN_DATALINK",f"ATN protocols: {', '.join(sorted(atn_int))}"))
        if len(reasm_issues)>=3:
            flags.append(("REASSEMBLY_ISSUES",f"{len(reasm_issues)} out-of-sequence/incomplete fragments"))
        if avg_snr<18 and len(recs)>=5:
            flags.append(("WEAK_SIGNAL",f"Avg SNR {avg_snr:.1f} dB"))
        if len(gs_set)>=3:
            flags.append(("MULTI_GS",f"Contacted {len(gs_set)} ground stations: {' '.join(g[0] for g in sorted(gs_set))}"))

        timeline = []
        for r in recs:
            d = r.get("decoded") or {}
            timeline.append({
                "ts":r["timestamp"].strftime("%H:%M:%S"),
                "freq":r["freq_mhz"],"snr":f"{r['snr_db']:.1f}",
                "gs":f"{r['gs_icao']} {r['gs_name']}".strip(),
                "frame_type":r["frame_type"],
                "label":r["acars_label"],"sublabel":r["acars_sublabel"],
                "msg_num":r["acars_msg_num"],"reassembly":r["acars_reassembly"],
                "atn_protos":", ".join(r["atn_protocols"]),
                "decoded_type":d.get("type",""),
                "decoded_summary":d.get("summary",""),
            })

        aircraft[icao]=dict(
            icao=icao,registration=reg,flight=flight,ac_type=ac_type,
            frequencies=freqs,gs_contacts=sorted(gs_set),
            first_seen=first_ts,last_seen=last_ts,duration_s=duration,
            msg_count=len(recs),acars_count=len(acars_recs),
            label_counts=dict(label_counts),atn_protocols=sorted(atn_used),
            avg_snr=avg_snr,flags=flags,timeline=timeline,
            positions=positions,decoded_highlights=decoded_highlights)
    return aircraft


# ─────────────────────────────────────────────────────────
#  EXPORTS
# ─────────────────────────────────────────────────────────

def export_csv(aircraft, out_path):
    rows=[dict(icao=a["icao"],registration=a["registration"],flight=a["flight"],
               ac_type=a["ac_type"],first_seen=a["first_seen"],last_seen=a["last_seen"],
               duration_min=round(a["duration_s"]/60,1),msg_count=a["msg_count"],
               acars_count=a["acars_count"],avg_snr_db=round(a["avg_snr"],1),
               positions=len(a["positions"]),
               flag_count=len(a["flags"]),flags="|".join(f[0] for f in a["flags"]),
               labels="|".join(f"{k}:{v}" for k,v in a["label_counts"].items()),
               gs_contacts="|".join(g[0] for g in a["gs_contacts"]))
          for a in aircraft.values()]
    rows.sort(key=lambda r:-r["flag_count"])
    if not rows: return
    with open(out_path,"w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    print(f"[CSV] Written: {out_path}")


def export_db(aircraft, records, db_path):
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()

        # Schema migration: add columns introduced in newer versions
        existing_cols = {row[1] for row in c.execute("PRAGMA table_info(messages)")}
        if existing_cols:
            for col, typedef in [("decoded_type","TEXT"),("decoded_summary","TEXT"),
                                  ("avlc_type","TEXT"),("atn_protocols","TEXT")]:
                if col not in existing_cols:
                    c.execute(f"ALTER TABLE messages ADD COLUMN {col} {typedef}")

        c.executescript("""
            CREATE TABLE IF NOT EXISTS aircraft(
                icao TEXT PRIMARY KEY,registration TEXT,flight TEXT,ac_type TEXT,
                first_seen TEXT,last_seen TEXT,duration_s REAL,msg_count INTEGER,
                acars_count INTEGER,avg_snr REAL,flag_count INTEGER,flags TEXT);
            CREATE TABLE IF NOT EXISTS messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                src_icao TEXT,timestamp TEXT,freq_mhz TEXT,snr_db REAL,
                frame_type TEXT,ac_reg TEXT,ac_type TEXT,
                gs_icao TEXT,gs_name TEXT,avlc_type TEXT,
                acars_label TEXT,acars_flight TEXT,acars_reassembly TEXT,
                acars_message TEXT,atn_protocols TEXT,
                decoded_type TEXT,decoded_summary TEXT);
            CREATE TABLE IF NOT EXISTS flags(icao TEXT,flag_type TEXT,detail TEXT);
            CREATE TABLE IF NOT EXISTS positions(
                icao TEXT,registration TEXT,flight TEXT,
                timestamp TEXT,lat REAL,lon REAL,alt_ft REAL);
        """)

        for a in aircraft.values():
            c.execute("INSERT OR REPLACE INTO aircraft VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(
                a["icao"],a["registration"],a["flight"],a["ac_type"],
                str(a["first_seen"]),str(a["last_seen"]),a["duration_s"],
                a["msg_count"],a["acars_count"],a["avg_snr"],
                len(a["flags"]),"|".join(f[0] for f in a["flags"])))
            for ft,det in a["flags"]:
                c.execute("INSERT INTO flags VALUES(?,?,?)",(a["icao"],ft,det))

        for r in records:
            d = r.get("decoded") or {}
            try:
                c.execute("""INSERT INTO messages(
                    src_icao,timestamp,freq_mhz,snr_db,frame_type,
                    ac_reg,ac_type,gs_icao,gs_name,avlc_type,
                    acars_label,acars_flight,acars_reassembly,acars_message,
                    atn_protocols,decoded_type,decoded_summary)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
                    r["src_icao"],str(r["timestamp"]),r["freq_mhz"],r["snr_db"],
                    r["frame_type"],r["ac_reg"],r["ac_type"],
                    r["gs_icao"],r["gs_name"],r["avlc_type"],
                    r["acars_label"],r["acars_flight"],r["acars_reassembly"],
                    r["acars_message"],", ".join(r["atn_protocols"]),
                    d.get("type",""),d.get("summary","")))
            except sqlite3.Error as e:
                print(f"  [DB] Warning: skipped message row: {e}",file=sys.stderr)
                continue

            if d.get("lat") and d.get("lon"):
                icao = r["src_icao"] or "UNKNOWN"
                ac   = aircraft.get(icao,{})
                try:
                    c.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?)",(
                        icao,ac.get("registration",""),ac.get("flight",""),
                        str(r["timestamp"]),d["lat"],d["lon"],d.get("alt_ft")))
                except sqlite3.Error as e:
                    print(f"  [DB] Warning: skipped position row: {e}",file=sys.stderr)

        conn.commit()
        conn.close()
        print(f"[DB]  Written: {db_path}")

    except sqlite3.Error as e:
        print(f"[DB]  ERROR: {e}",file=sys.stderr)
        print(f"      Tip: if the DB exists from an older version, delete it and re-run.",file=sys.stderr)
        raise


# ─────────────────────────────────────────────────────────
#  HTML REPORT
# ─────────────────────────────────────────────────────────

FLAG_STYLE={"UNREGISTERED":("#f59e0b","⚠"),"NOTABLE_LABEL":("#ef4444","🔴"),
    "HIGH_FREQ_BURST":("#8b5cf6","⚡"),"HAS_ACARS_PAYLOAD":("#10b981","📋"),
    "ATN_DATALINK":("#3b82f6","🛰"),"REASSEMBLY_ISSUES":("#f97316","🔧"),
    "WEAK_SIGNAL":("#6b7280","📶"),"MULTI_GS":("#06b6d4","🌐")}

DECODE_COLOURS={
    "CPDLC":"#3b82f6","ADS-C":"#10b981","ADS-C B6":"#10b981",
    "XID Handoff":"#8b5cf6","Media Advisory":"#f59e0b",
    "AOC Position Report":"#06b6d4","AOC Report":"#6b7280",
    "D-ATIS":"#f97316","MVA":"#ef4444","MVA Report":"#ef4444",
    "Weight & Balance":"#ec4899","Free text":"#a3e635",
    "Engine/MVA Report":"#ef4444","AOC Normal Ops":"#6b7280",
    "AOC Performance Monitor":"#6b7280","H1 Report":"#6b7280",
    "Progress Report":"#06b6d4",
}

def _dc(dtype):
    for k,v in DECODE_COLOURS.items():
        if k in dtype: return v
    return "#6b7280"

def _badges(flags):
    return "".join(f'<span class="badge" style="background:{FLAG_STYLE.get(f[0],("#6b7280","ℹ"))[0]}" title="{escape(f[1])}">'
                   f'{FLAG_STYLE.get(f[0],("#6b7280","ℹ"))[1]} {escape(f[0])}</span> ' for f in flags)

def _lchip(label,count=None,extra=""):
    desc=ACARS_LABELS.get(label,"")
    sup=f"<sup>{count}</sup>" if count else ""
    return f'<span class="lchip{extra}" title="{escape(desc)}">{escape(label)}{sup}</span>'

def _dtype_chip(dtype):
    if not dtype: return ""
    col = _dc(dtype)
    return f'<span class="dtype" style="border-color:{col};color:{col}">{escape(dtype)}</span>'


def build_html(aircraft, source_files, date_str):
    all_ac=sorted(aircraft.values(),key=lambda a:-a["msg_count"])
    flagged=[a for a in all_ac if a["flags"]]
    total=sum(a["msg_count"] for a in all_ac)
    acars_t=sum(a["acars_count"] for a in all_ac)
    pos_t=sum(len(a["positions"]) for a in all_ac)

    def sum_row(a):
        hl=' class="fr"' if a["flags"] else ""
        dur=str(timedelta(seconds=int(a["duration_s"])))
        lbls="".join(_lchip(l,v) for l,v in sorted(a["label_counts"].items(),key=lambda x:-x[1]))
        gs=" ".join(f'<span class="lchip gs">{escape(g[0])}</span>' for g in a["gs_contacts"])
        pos_icon = f'<span title="{len(a["positions"])} positions decoded">📍{len(a["positions"])}</span>' if a["positions"] else ""
        return (f'<tr{hl}><td><a href="#ac-{a["icao"]}" class="il">{escape(a["icao"])}</a></td>'
                f'<td>{escape(a["registration"])}</td><td>{escape(a["flight"])}</td>'
                f'<td>{escape(a["ac_type"])}</td>'
                f'<td class="mono">{a["first_seen"].strftime("%H:%M")}</td>'
                f'<td class="mono">{a["last_seen"].strftime("%H:%M")}</td>'
                f'<td>{dur}</td><td class="c">{a["msg_count"]}</td>'
                f'<td class="c">{a["acars_count"] or "—"}</td>'
                f'<td class="c">{pos_icon}</td>'
                f'<td class="c">{a["avg_snr"]:.1f}</td>'
                f'<td>{lbls}</td><td>{gs}</td><td>{_badges(a["flags"])}</td></tr>')

    summary_rows="".join(sum_row(a) for a in all_ac)

    flagged_panel=""
    if flagged:
        rows=""
        for a in flagged:
            for ftype,detail in a["flags"]:
                col,icon=FLAG_STYLE.get(ftype,("#6b7280","ℹ"))
                rows+=(f'<tr><td><a href="#ac-{a["icao"]}">{escape(a["icao"])}</a></td>'
                       f'<td>{escape(a["registration"])}</td><td>{escape(a["flight"])}</td>'
                       f'<td>{escape(a["ac_type"])}</td>'
                       f'<td><span class="badge" style="background:{col}">{icon} {escape(ftype)}</span></td>'
                       f'<td>{escape(detail)}</td></tr>')
        flagged_panel=(f'<h2>⚑ Flagged for Review <span class="cnt">{len(flagged)}</span></h2>'
                       f'<div class="tw"><table><thead><tr><th>ICAO</th><th>Reg</th><th>Flight</th>'
                       f'<th>Type</th><th>Flag</th><th>Detail</th></tr></thead>'
                       f'<tbody>{rows}</tbody></table></div>')

    def tl_rows(a):
        out=""
        for t in a["timeline"]:
            rs=t["reassembly"]
            rs_html=(f'<span class="rs-bad">{escape(rs)}</span>' if rs in ("out of sequence","incomplete")
                     else (f'<span class="rs-ok">{escape(rs)}</span>' if rs else ""))
            lbl=_lchip(t["label"]) if t["label"] else ""
            sub=f' <small class="muted">{escape(t["sublabel"])}</small>' if t["sublabel"] else ""
            atn=f'<span class="atn">{escape(t["atn_protos"])}</span>' if t["atn_protos"] else ""
            dchip=_dtype_chip(t["decoded_type"])
            dsumm=f'<span class="dsumm">{escape(t["decoded_summary"][:120])}</span>' if t["decoded_summary"] else ""
            out+=(f'<tr>'
                  f'<td class="mono sm">{t["ts"]}</td>'
                  f'<td class="mono sm">{t["freq"]}</td>'
                  f'<td class="sm">{t["snr"]}</td>'
                  f'<td class="sm">{escape(t["gs"])}</td>'
                  f'<td class="sm">{escape(t["frame_type"])}</td>'
                  f'<td>{lbl}{sub}</td>'
                  f'<td class="mono sm">{escape(t["msg_num"])}</td>'
                  f'<td>{rs_html}</td>'
                  f'<td>{atn}</td>'
                  f'<td>{dchip}</td>'
                  f'<td class="dsumm-cell">{dsumm}</td>'
                  f'</tr>')
        return out

    def highlights_html(a):
        if not a["decoded_highlights"]: return ""
        rows=""
        for h in a["decoded_highlights"]:
            col=_dc(h["type"])
            rows+=(f'<tr><td class="mono sm">{h["ts"]}</td>'
                   f'<td><span class="dtype" style="border-color:{col};color:{col}">{escape(h["type"])}</span></td>'
                   f'<td class="dsumm-cell"><span class="dsumm">{escape(h["summary"][:160])}</span></td></tr>')
        return (f'<details open><summary class="muted">▶ Decoded message highlights ({len(a["decoded_highlights"])})</summary>'
                f'<div class="tw"><table>'
                f'<thead><tr><th>Time</th><th>Type</th><th>Decoded Content</th></tr></thead>'
                f'<tbody>{rows}</tbody></table></div></details>')

    details=""
    for a in all_ac:
        gs_list=" ".join(f'<span class="lchip gs" title="{escape(g[1])}">{escape(g[0])}</span>' for g in a["gs_contacts"])
        atn_list=(" ".join(f'<span class="lchip atn">{escape(p)}</span>' for p in a["atn_protocols"])
                  if a["atn_protocols"] else "—")
        pos_str=f'  📍 {len(a["positions"])} positions' if a["positions"] else ""
        details+=(f'<section class="acd" id="ac-{a["icao"]}">'
                  f'<div class="ach"><div class="act">'
                  f'<span class="ib">{escape(a["icao"])}</span>'
                  f'<span class="rg">{escape(a["registration"])}</span>'
                  f'<span class="fl">{escape(a["flight"])}</span>'
                  f'<span class="tp muted">{escape(a["ac_type"])}</span></div>'
                  f'<div class="acm muted">SNR {a["avg_snr"]:.1f} dB{pos_str}'
                  f' &nbsp;|&nbsp; {a["msg_count"]} msgs ({a["acars_count"]} ACARS)'
                  f' &nbsp;|&nbsp; {gs_list} &nbsp;|&nbsp; ATN: {atn_list}</div>'
                  f'<div>{_badges(a["flags"])}</div></div>'
                  f'{highlights_html(a)}'
                  f'<details><summary class="muted">▶ Full message timeline ({a["msg_count"]})</summary>'
                  f'<div class="tw"><table class="tlt"><thead><tr>'
                  f'<th>Time</th><th>Freq</th><th>SNR</th><th>Ground Stn</th>'
                  f'<th>Frame</th><th>Label</th><th>Msg#</th>'
                  f'<th>Reassembly</th><th>ATN</th><th>Decoded Type</th><th>Decoded Content</th>'
                  f'</tr></thead><tbody>{tl_rows(a)}</tbody></table></div>'
                  f'</details></section>')

    CSS="""
:root{--bg:#0d0f18;--sf:#161929;--sf2:#1e2235;--bd:#272b40;
      --tx:#dde1f5;--mt:#7880a0;--ac:#5b8ef5;--ok:#10b981;--er:#ef4444;--wa:#f59e0b;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font:14px/1.6 'Segoe UI',system-ui,sans-serif}
a{color:var(--ac);text-decoration:none}a:hover{text-decoration:underline}
header{background:var(--sf);border-bottom:1px solid var(--bd);padding:18px 28px;
       display:flex;align-items:center;gap:14px;flex-wrap:wrap}
header h1{font-size:1.35rem;font-weight:700;color:#fff}
.sub{color:var(--mt);font-size:.82rem;margin-top:2px}
.pills{margin-left:auto;display:flex;gap:10px;flex-wrap:wrap}
.pill{background:var(--sf2);border:1px solid var(--bd);border-radius:20px;padding:3px 13px;font-size:.8rem}
.pill strong{color:var(--ac)}
main{max-width:1600px;margin:0 auto;padding:20px 28px}
h2{font-size:1rem;font-weight:700;color:#fff;margin:28px 0 10px;padding-bottom:5px;border-bottom:1px solid var(--bd)}
.cnt{background:var(--er);color:#fff;border-radius:10px;padding:1px 8px;font-size:.78rem;font-weight:700}
.tw{overflow-x:auto;border-radius:8px;border:1px solid var(--bd);margin-bottom:12px}
table{width:100%;border-collapse:collapse}
th{background:var(--sf2);color:var(--mt);font-weight:600;font-size:.74rem;text-transform:uppercase;
   letter-spacing:.04em;padding:9px 10px;text-align:left;white-space:nowrap}
td{padding:7px 10px;border-top:1px solid var(--bd);vertical-align:top}
tr:hover td{background:var(--sf2)}
.fr td{background:rgba(239,68,68,.05)}.fr:hover td{background:rgba(239,68,68,.10)}
.c{text-align:center}
.badge{display:inline-block;padding:2px 7px;border-radius:4px;font-size:.72rem;font-weight:700;color:#fff;white-space:nowrap;cursor:default}
.lchip{display:inline-block;background:var(--sf2);border:1px solid var(--bd);border-radius:4px;padding:1px 5px;font-size:.72rem;font-family:monospace;cursor:default}
.lchip.gs{border-color:#3b82f6;color:#93c5fd}.lchip.atn{border-color:#8b5cf6;color:#c4b5fd;font-size:.7rem}
.dtype{display:inline-block;border:1px solid;border-radius:4px;padding:1px 6px;font-size:.7rem;font-weight:600;white-space:nowrap;cursor:default}
.dsumm{font-family:monospace;font-size:.75rem;color:var(--mt)}
.dsumm-cell{max-width:480px;word-break:break-word}
.atn{font-size:.72rem;color:#c4b5fd}
.il{font-family:monospace;font-weight:700;color:var(--ac)}
.mono{font-family:monospace}.sm{font-size:.8rem}.muted{color:var(--mt)}
.rs-bad{color:var(--wa);font-size:.75rem}.rs-ok{color:var(--ok);font-size:.75rem}
.acd{background:var(--sf);border:1px solid var(--bd);border-radius:10px;margin-bottom:12px;overflow:hidden}
.ach{padding:12px 16px;display:flex;flex-wrap:wrap;gap:8px;align-items:center;border-bottom:1px solid var(--bd)}
.act{display:flex;align-items:center;gap:9px;flex:1}
.ib{background:var(--ac);color:#fff;font-family:monospace;font-weight:800;font-size:.95rem;padding:3px 9px;border-radius:6px}
.rg{font-weight:700;font-size:.95rem}.fl{color:var(--mt);font-size:.85rem}.tp{font-size:.82rem}.acm{font-size:.78rem}
details summary{padding:8px 16px;cursor:pointer;font-size:.82rem;user-select:none}
details summary:hover{color:var(--tx)}details[open] summary{border-bottom:1px solid var(--bd)}
.tlt td{font-size:.78rem}
#sb{width:100%;max-width:380px;background:var(--sf);border:1px solid var(--bd);color:var(--tx);
    padding:7px 12px;border-radius:6px;font-size:.88rem;margin-bottom:14px}
#sb:focus{outline:none;border-color:var(--ac)}
footer{text-align:center;color:var(--mt);font-size:.75rem;padding:20px;border-top:1px solid var(--bd);margin-top:28px}
"""

    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>dumpvdl2 — {escape(date_str)}</title><style>{CSS}</style></head><body>'
            f'<header><div><h1>📡 dumpvdl2 Log Analysis</h1>'
            f'<div class="sub">{escape(date_str)} &nbsp;|&nbsp; {escape(", ".join(source_files))}</div></div>'
            f'<div class="pills">'
            f'<div class="pill">Aircraft: <strong>{len(all_ac)}</strong></div>'
            f'<div class="pill">Records: <strong>{total}</strong></div>'
            f'<div class="pill">ACARS: <strong>{acars_t}</strong></div>'
            f'<div class="pill">Positions: <strong>{pos_t}</strong></div>'
            f'<div class="pill">Flagged: <strong>{len(flagged)}</strong></div>'
            f'</div></header><main>'
            f'{flagged_panel}'
            f'<h2>📋 All Aircraft</h2>'
            f'<input id="sb" type="search" placeholder="Filter ICAO, reg, flight, type…" oninput="ft(this.value)">'
            f'<div class="tw"><table id="st"><thead><tr>'
            f'<th>ICAO</th><th>Reg</th><th>Flight</th><th>Type</th>'
            f'<th>First</th><th>Last</th><th>Duration</th>'
            f'<th>Msgs</th><th>ACARS</th><th>Pos</th><th>SNR</th>'
            f'<th>Labels</th><th>Ground Stns</th><th>Flags</th>'
            f'</tr></thead><tbody id="sb2">{summary_rows}</tbody></table></div>'
            f'<h2>🔍 Aircraft Detail</h2>{details}</main>'
            f'<footer>Generated {datetime.now().strftime("%Y-%m-%d %H:%M")} by dumpvdl2_analyser.py</footer>'
            f'<script>function ft(q){{q=q.toLowerCase();'
            f'document.querySelectorAll("#sb2 tr").forEach(r=>{{r.style.display=r.textContent.toLowerCase().includes(q)?"":"none";}});}}</script>'
            f'</body></html>')


# ─────────────────────────────────────────────────────────
#  CONSOLE SUMMARY
# ─────────────────────────────────────────────────────────

def print_console(aircraft):
    flagged=[a for a in aircraft.values() if a["flags"]]
    all_ac=sorted(aircraft.values(),key=lambda a:-a["msg_count"])
    total=sum(a["msg_count"] for a in all_ac)
    pos_total=sum(len(a["positions"]) for a in all_ac)
    print(f"\n{'═'*64}\n  dumpvdl2 Analysis  –  ATN/AVLC  (full decode)\n{'═'*64}")
    print(f"  Aircraft : {len(aircraft)}  |  Records : {total}  |  Positions : {pos_total}  |  Flagged : {len(flagged)}\n")
    if flagged:
        print("  ⚑ FLAGGED:")
        for a in sorted(flagged,key=lambda x:-len(x["flags"])):
            print(f"    [{a['icao']} {a['registration']} {a['flight']} {a['ac_type']}]".strip())
            for ft,det in a["flags"]: print(f"      • {ft}: {det}")
        print()
    print("  MOST ACTIVE:")
    for a in all_ac[:15]:
        bar="█"*min(a["msg_count"]//2,35)
        print(f"    {(a['registration'] or a['icao']):10}  {a['flight']:8}  {a['ac_type']:5}  {bar} {a['msg_count']}")
    print(f"{'═'*64}\n")


# ─────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────

def main():
    ap=argparse.ArgumentParser(description="Analyse dumpvdl2 ATN/AVLC log files (full decode)",
        formatter_class=argparse.RawDescriptionHelpFormatter,epilog=textwrap.dedent(__doc__))
    ap.add_argument("logs",nargs="+")
    ap.add_argument("--output",default="report.html")
    ap.add_argument("--csv",action="store_true")
    ap.add_argument("--db",action="store_true")
    ap.add_argument("--freq-threshold",type=int,default=10,dest="freq_threshold",metavar="N")
    ap.add_argument("--verbose",action="store_true")
    args=ap.parse_args()
    all_records,source_names=[],[]
    for p in args.logs:
        path=Path(p)
        if not path.exists(): print(f"WARNING: not found: {path}",file=sys.stderr); continue
        recs=parse_log(path,verbose=args.verbose)
        all_records.extend(recs); source_names.append(path.name)
    if not all_records: print("No records parsed.",file=sys.stderr); sys.exit(1)
    aircraft=analyse(all_records,freq_threshold=args.freq_threshold)
    dates=sorted({r["timestamp"].date() for r in all_records})
    date_str=", ".join(str(d) for d in dates)
    print_console(aircraft)
    out=Path(args.output)
    out.write_text(build_html(aircraft,source_names,date_str),encoding="utf-8")
    print(f"[HTML] Written: {out}")
    if args.csv: export_csv(aircraft,out.with_suffix(".csv"))
    if args.db: export_db(aircraft,all_records,out.with_suffix(".db"))

if __name__=="__main__":
    main()
