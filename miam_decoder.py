#!/usr/bin/env python3
"""
MIAM Decoder for dumpvdl2 logs
================================
Extracts and decodes MIAM CORE REP (approach & landing performance)
messages from dumpvdl2 plain-text log files and produces a
self-contained HTML report plus a console summary.

These are Airbus A350 Flight Operations Quality Assurance (FOQA)
style reports transmitted via VDL2 using the MIAM (Media Independent
Aircraft Messaging) encoding.  dumpvdl2 handles the deflate
decompression; this script decodes the inner ACARS payload.

REP types decoded:
  REP001  Approach stabilisation at 5000 ft gate
  REP002  Approach stabilisation at alternate gate
  REP004  Approach sequence snapshot
  REP015  Flap / slat configuration report
  REP020  Gear extension event
  REP024  Approach control surface snapshot
  REP053  Landing / touchdown snapshot
  REP081  Takeoff performance (EDR format)

Usage:
    python miam_decoder.py [options] <logfile> [<logfile2> ...]

Options:
    --output FILE    HTML report path (default: miam_report.html)
    --text           Also print full decoded detail to console
    --aircraft REG   Filter to a specific registration (e.g. 9M-MAD)
    --rep TYPE       Filter to a specific REP type (e.g. REP020)
    --verbose        Show parsing progress

Example:
    python miam_decoder.py pi21vdl2_20260329.log
    python miam_decoder.py --aircraft 9M-MAD --rep REP001 *.log
"""

import re
import sys
import argparse
import textwrap
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from html import escape


# ─────────────────────────────────────────────────────────
#  REFERENCE DATA
# ─────────────────────────────────────────────────────────

REP_DESCRIPTIONS = {
    "REP001": ("Approach Stabilisation (5000 ft gate)",
               "Flight parameters snapshot at 5000 ft on approach"),
    "REP002": ("Approach Stabilisation (alternate gate)",
               "Same as REP001 but at the operator's alternate gate altitude"),
    "REP004": ("Approach Sequence",
               "Rolling snapshot of key approach parameters during final approach"),
    "REP015": ("Flap / Slat Configuration",
               "Flap and slat positions requested vs achieved during approach"),
    "REP020": ("Gear Extension Event",
               "Flight loads and hydraulic pressures at landing gear extension"),
    "REP024": ("Approach Control Surface Snapshot",
               "Control surface deflections during the approach"),
    "REP053": ("Landing / Touchdown Snapshot",
               "Structural and flight parameters at touchdown"),
    "REP081": ("Takeoff Performance (EDR)",
               "Engine Data Recorder takeoff report"),
}

PHASE_CODES = {
    "01": "Departure", "02": "Approach / Landing",
    "03": "Cruise", "04": "Diversion", "05": "Ground",
}
SUBPHASE_CODES = {
    "01": "Before Takeoff", "02": "Gear Up", "03": "Flap Retract",
    "04": "Stable Approach", "05": "Gear Down", "06": "Touchdown",
}

SECTION_NAMES = {
    "A10": "Flight parameters at gate", "A11": "Config flags",
    "B10": "Left wing loads",           "B11": "Right wing loads",
    "C10": "ILS deviations (Capt)",     "C11": "ILS deviations (FO)",
    "C12": "Combined ILS deviation",
    "D10": "Capt pitch control",        "E10": "Roll control (Capt)",
    "F10": "Capt sidestick / brakes",   "G10": "Left MLG loads",
    "H10": "Structural symmetry",       "J10": "Autopilot status",
    "K10": "Wind at gate",
    "A20": "Nose gear loads",           "A01": "Landing event",
    "A02": "Autoland",                  "B01": "MLG loads L/R",
    "B03": "NLG loads",                 "D01": "MLG strut FL",
}


# ─────────────────────────────────────────────────────────
#  LOG PARSER — extract MIAM blocks
# ─────────────────────────────────────────────────────────

_HDR = re.compile(
    r'^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) [A-Z]{2,4}\]'
    r'\s+\[([\d.]+)\]'
    r'\s+\[[^\]]+\]'
    r'\s+\[([\d.]+) dB\]'
)


def extract_miam_blocks(path, verbose=False):
    """Parse a dumpvdl2 log file and return decoded MIAM REP records."""
    results = []
    current = []

    def flush(lines):
        if lines and 'MIAM CORE Data' in "\n".join(lines):
            rec = _parse_miam_block(lines)
            if rec:
                results.append(rec)

    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.rstrip('\n')
            if _HDR.match(line):
                flush(current)
                current = [line]
            else:
                current.append(line)
    flush(current)

    if verbose:
        print(f"  Found {len(results)} MIAM blocks in {Path(path).name}")
    return results


def _parse_miam_block(lines):
    raw = "\n".join(lines)
    hm = _HDR.match(lines[0])
    if not hm:
        return None
    ts_str, freq, snr = hm.group(1), hm.group(2), hm.group(3)
    try:
        ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None

    ac_m = re.search(r'AC info:\s*\.?([^,]+),\s*([^,\n]+)', raw)
    gs_m = re.search(r'GS info:\s*([A-Z0-9]+),\s*([^\n]+)', raw)
    reg   = ac_m.group(1).strip().lstrip('.') if ac_m else ""
    atype = ac_m.group(2).strip()             if ac_m else ""
    gs    = gs_m.group(1).strip()             if gs_m else ""
    gs_name = gs_m.group(2).strip()           if gs_m else ""

    flight_m = re.search(r'Reg:\s*\.?([A-Z0-9\-]+)\s+Flight:\s*([A-Z0-9]+)', raw)
    flight = flight_m.group(2) if flight_m else ""

    payload_m = re.search(
        r'MIAM CORE Data.*?Message:[ \t]*\n[ \t]*([^\n]+)', raw, re.DOTALL)
    if not payload_m:
        return None
    payload = payload_m.group(1).strip()

    decoded = _decode_rep_payload(payload)
    if not decoded:
        return None

    if not reg and decoded.get('h01', {}).get('ac_reg'):
        reg = decoded['h01']['ac_reg']
    if not flight and decoded.get('h02', {}).get('flight_id'):
        flight = decoded['h02']['flight_id']

    return {
        'timestamp': ts, 'freq_mhz': freq, 'snr_db': float(snr),
        'gs_icao': gs, 'gs_name': gs_name,
        'registration': reg, 'ac_type': atype, 'flight': flight,
        'payload': payload, 'decoded': decoded, 'raw': raw,
    }


# ─────────────────────────────────────────────────────────
#  REP PAYLOAD DECODER
# ─────────────────────────────────────────────────────────

def _decode_rep_payload(payload):
    if not payload or len(payload) < 10:
        return None
    result = {}

    hdr_m = re.match(r'^([A-Z0-9]+),(\d+),(\d+),(\d+),(\S+)', payload)
    if hdr_m:
        result['airframe']     = hdr_m.group(1)
        result['report_seq']   = int(hdr_m.group(2))
        result['software_ver'] = hdr_m.group(5)

    rep_m = re.search(r'/(REP\d+),(\d+),(\d+);', payload)
    if not rep_m:
        return None
    rep_type, phase_code, subphase_code = rep_m.group(1), rep_m.group(2), rep_m.group(3)
    result['rep_type']   = rep_type
    result['rep_desc']   = REP_DESCRIPTIONS.get(rep_type, ("Unknown REP", ""))[0]
    result['phase']      = PHASE_CODES.get(phase_code, phase_code)
    result['subphase']   = SUBPHASE_CODES.get(subphase_code, subphase_code)
    result['phase_code'] = f"{phase_code}/{subphase_code}"

    h01_m = re.search(r'H01,([^/\r\n]+)', payload)
    if h01_m:
        result['h01'] = _parse_h01(h01_m.group(1))

    h02_m = re.search(r'H02,([^/\r\n]+)', payload)
    if h02_m:
        result['h02'] = _parse_h02(h02_m.group(1))

    h03_m = re.search(r'H03,([^/\r\n]+)', payload)
    if h03_m:
        result['h03'] = h03_m.group(1).strip()

    sections = {}
    for sec_m in re.finditer(r'/([A-Z]\d{2}),([^/\r\n]+)', payload):
        code = sec_m.group(1)
        values = [v.strip() for v in sec_m.group(2).split(',')]
        sections[code] = values
    result['sections'] = sections

    if rep_type == 'REP020':
        result['gear_extension'] = _decode_rep020(sections)
    elif rep_type in ('REP001', 'REP002'):
        result['approach'] = _decode_approach(sections)
    elif rep_type == 'REP053':
        result['touchdown'] = _decode_rep053(sections)
    elif rep_type == 'REP081':
        result['takeoff'] = _decode_rep081(payload)
    elif rep_type == 'REP015':
        result['flap_config'] = _decode_rep015(sections)

    return result


def _parse_h01(h01_str):
    """
    H01 field layout — VERIFIED fields only:
      [0]  rep_num        confirmed: matches the REP sequence number
      [8:14] dd,mm,yy,HH,MM,SS   confirmed: matches message receive date/time (UTC)
      [5]  ac_reg          confirmed: matches registration seen elsewhere in same log line

    The following fields appear at consistent positions across all samples
    but their semantic meaning could NOT be verified against ground truth
    (cross-checking against actual flight data was not possible from the
    log alone), so they are returned as raw values rather than labelled
    with assumed units:
      [3]  often a round number (4000/5000/6000) -- possibly a report
           trigger altitude/gate rather than current aircraft altitude
      [4]  varies widely (18 to 1800) -- does not behave like a sensible
           "distance to runway" value, so not labelled as such here
      [14] often matches a value seen elsewhere as a route/runway-like
           code, but not confirmed as compass heading
    Raw values for all fields are preserved in 'raw_fields' for your own
    cross-referencing against airline documentation if available.
    """
    f = h01_str.split(',')
    h = {'raw_fields': f}
    try: h['rep_num'] = int(f[0])
    except: pass
    h['ac_reg'] = f[5].strip().lstrip('.') if len(f) > 5 else ""
    try:
        dd, mm, yy = f[8], f[9], f[10]
        HH, MM, SS = f[11], f[12], f[13]
        h['event_dt'] = datetime.strptime(f"20{yy}-{mm}-{dd} {HH}:{MM}:{SS}", "%Y-%m-%d %H:%M:%S")
        h['event_time_str'] = h['event_dt'].strftime("%Y-%m-%d %H:%M:%S")
    except: pass
    # Unverified fields - kept but not asserted as fact
    try: h['field_3_unverified'] = int(f[3])
    except: pass
    try: h['field_4_unverified'] = int(f[4])
    except: pass
    try: h['field_14_unverified'] = f[14].strip()
    except: pass
    return h


def _parse_h02(h02_str):
    f = h02_str.split(',')
    h = {}
    if f:
        route = f[0].strip()
        if len(route) >= 9:
            h['origin_icao'] = route[:4]
            h['dest_icao']   = route[5:9]
        elif len(route) == 8:
            h['origin_icao'] = route[:4]
            h['dest_icao']   = route[4:]
    if len(f) >= 2: h['flight_id']  = f[1].strip()
    if len(f) >= 3: h['sw_ref1']    = f[2].strip()
    if len(f) >= 5: h['config_ref'] = f[4].strip()
    return h


def _decode_approach(sections):
    """
    NOTE: Only the H01/H02/H03 header fields (rep type, phase, altitude,
    distance, registration, event timestamp, route, flight ID) have a
    confirmed, verifiable field layout based on consistent patterns across
    all observed REP variants and aircraft types.

    The data sections (A10, B10, C10, etc.) contain flight parameters but
    their exact field-to-unit mapping is NOT publicly documented and could
    not be reliably verified against ground truth in this log sample.
    Earlier attempts to map these to specific values (airspeed, weight,
    wind, gear pressure) produced implausible results (e.g. "21441 kt"
    airspeed) when checked against a wider sample, so no engineering unit
    conversion is applied here. The raw section values are preserved and
    shown in the report's "Raw section data" panel for reference, where
    you can cross-check them against any airline-specific documentation
    you may have access to.
    """
    return {}


def _decode_rep020(sections):
    """
    NOTE: As with _decode_approach, no verified unit mapping exists for
    the A10/B10/C10 etc. data sections in REP020. Earlier guesses produced
    implausible hydraulic pressures (e.g. "439460 psi") so no engineering
    conversion is applied. Raw values are preserved in the report's
    "Raw section data" panel.
    """
    return {}


def _decode_rep053(sections):
    """
    NOTE: No verified unit mapping exists for the A01/A02/B01/B03/D01 data
    sections in REP053. Raw values are preserved in the report's
    "Raw section data" panel rather than presented as decoded engineering
    values, since the exact field semantics could not be confirmed.
    """
    return {}


def _decode_rep015(sections):
    configs = []
    for code in sorted(sections.keys()):
        if not code.startswith('C'):
            continue
        vals = sections[code]
        if len(vals) >= 2:
            try:
                configs.append({
                    'section': code,
                    'requested': int(vals[0]),
                    'achieved': int(vals[1]),
                    'match': vals[0].strip() == vals[1].strip(),
                })
            except: pass
    return {'configurations': configs}


def _decode_rep081(payload):
    t = {}
    nx_m = re.search(r'NX,([A-Z]{4}) ([A-Z]{4})', payload)
    if nx_m:
        t['origin_icao'] = nx_m.group(1)
        t['dest_icao']   = nx_m.group(2)
    engines = []
    for row_m in re.finditer(
        r'^(\d),(\d+),([-+]\d+),([-+]\d+),(\d+),(\d+),([-+]\d+),(\d+)',
        payload, re.MULTILINE):
        try:
            engines.append({
                'engine': int(row_m.group(1)),
                'lat': int(row_m.group(3)) / 1000000,
                'lon': int(row_m.group(4)) / 1000000,
                'alt_ft': int(row_m.group(5)),
                'hdg_deg': int(row_m.group(6)),
                'vs_fpm': int(row_m.group(7)),
                'tas_kt': int(row_m.group(8)),
            })
        except: pass
    if engines:
        t['engines'] = engines
    return t


# ─────────────────────────────────────────────────────────
#  SUMMARY LINE
# ─────────────────────────────────────────────────────────

def build_summary_line(rec):
    d = rec['decoded']
    h01 = d.get('h01', {}); h02 = d.get('h02', {})
    rep = d.get('rep_type', '?')
    orig = h02.get('origin_icao', ''); dest = h02.get('dest_icao', '')
    flt = h02.get('flight_id', rec.get('flight', ''))
    et = h01.get('event_time_str', '')
    evt = d.get('h03', '').strip()

    parts = [rep]
    if orig and dest: parts.append(f"{orig}\u2192{dest}")
    if flt: parts.append(flt.strip())
    if et: parts.append(f"Event:{et}")
    if evt: parts.append(f"[{evt}]")

    appr = d.get('approach', {})
    if appr:
        if 'vapp_kt' in appr: parts.append(f"Vapp:{appr['vapp_kt']}kt")
        if 'vs_fpm' in appr: parts.append(f"VS:{appr['vs_fpm']}fpm")
        if 'airspeed_kt' in appr: parts.append(f"IAS:{appr['airspeed_kt']}kt")
        if 'loc_dev_dot' in appr: parts.append(f"LOC:{appr['loc_dev_dot']:+.2f}dot")
        if 'gs_dev_dot' in appr: parts.append(f"GS:{appr['gs_dev_dot']:+.2f}dot")
        if 'wind_dir_deg' in appr and 'wind_spd_kt' in appr:
            parts.append(f"Wind:{appr['wind_dir_deg']:03d}/{appr['wind_spd_kt']}kt")

    td = d.get('touchdown', {})
    if td and 'mlg_vload_l' in td:
        parts.append(f"MLG-L:{td['mlg_vload_l']}  MLG-R:{td['mlg_vload_r']}")

    return "  ".join(str(p) for p in parts)


# ─────────────────────────────────────────────────────────
#  CONSOLE OUTPUT
# ─────────────────────────────────────────────────────────

def print_console_summary(records, verbose=False):
    by_ac = defaultdict(list)
    for r in records:
        reg = r['registration'] or r['decoded'].get('h01', {}).get('ac_reg', '?')
        by_ac[reg].append(r)

    print(f"\n{'='*70}")
    print(f"  MIAM Decoder  --  {len(records)} REP messages from {len(by_ac)} aircraft")
    print(f"{'='*70}")
    print("  NOTE: Route, flight, and event time are verified from message structure.")
    print("  Raw data sections (A10/B10/C10 etc.) are NOT decoded to engineering units")
    print("  -- their field mapping is not publicly documented. See HTML report for")
    print("  raw section values if you wish to cross-reference them yourself.")
    print(f"{'='*70}\n")

    for reg in sorted(by_ac.keys()):
        recs = sorted(by_ac[reg], key=lambda r: r['timestamp'])
        r0 = recs[0]; d0 = r0['decoded']; h02 = d0.get('h02', {})
        atype = r0.get('ac_type', ''); flt = h02.get('flight_id', r0.get('flight', ''))
        orig = h02.get('origin_icao', ''); dest = h02.get('dest_icao', '')
        route = f"{orig}->{dest}" if orig and dest else ""
        print(f"  -- {reg}  {atype}  {flt}  {route}")

        rep_types = defaultdict(int)
        for r in recs:
            rep_types[r['decoded'].get('rep_type', '?')] += 1
        for rep, cnt in sorted(rep_types.items()):
            desc = REP_DESCRIPTIONS.get(rep, ("Unknown",))[0]
            print(f"     {rep} x{cnt:2d}  {desc}")

        if verbose:
            print()
            for r in recs:
                ts = r['timestamp'].strftime('%H:%M:%S')
                gs = r.get('gs_icao', ''); snr = r['snr_db']
                print(f"    [{ts}] SNR:{snr:.1f}dB  GS:{gs}")
                print(f"      {build_summary_line(r)}")
                appr = r['decoded'].get('approach', {})
                if appr:
                    if 'gross_weight_kg' in appr:
                        print(f"      GW: {appr['gross_weight_kg']:,} kg  ZFW: {appr.get('zfw_kg',0):,} kg  "
                              f"CG: {appr.get('cg_mac_pct',0):.1f}% MAC  Flap:{appr.get('flap_pos','?')}")
                    if 'vapp_kt' in appr:
                        print(f"      Vapp: {appr['vapp_kt']} kt  Vls: {appr.get('vls_kt','?')} kt  "
                              f"VS: {appr.get('vs_fpm','?')} fpm  IAS: {appr.get('airspeed_kt','?')} kt")
                    if 'loc_dev_dot' in appr:
                        print(f"      LOC: {appr['loc_dev_dot']:+.2f} dot  GS: {appr.get('gs_dev_dot',0):+.2f} dot")
                    if 'wind_dir_deg' in appr:
                        print(f"      Wind: {appr['wind_dir_deg']:03d} deg / {appr.get('wind_spd_kt',0)} kt")
                gear = r['decoded'].get('gear_extension', {})
                if gear and 'mlg_l_press_psi' in gear:
                    print(f"      MLG-L: {gear['mlg_l_press_psi']} psi (ref {gear['mlg_l_ref_press_psi']})  "
                          f"MLG-R: {gear['mlg_r_press_psi']} psi (ref {gear['mlg_r_ref_press_psi']})")
                td = r['decoded'].get('touchdown', {})
                if td:
                    print(f"      Landing: {'NORMAL' if td.get('normal_landing') else 'ABNORMAL'}  "
                          f"{'Autoland' if td.get('autoland') else 'Manual'}")
                    if 'mlg_vload_l' in td:
                        print(f"      MLG vertical: L={td['mlg_vload_l']}  R={td['mlg_vload_r']}")
        print()
    print(f"{'='*70}\n")


# ─────────────────────────────────────────────────────────
#  HTML REPORT
# ─────────────────────────────────────────────────────────

REP_COLOURS = {
    "REP001": "#3b82f6", "REP002": "#6366f1", "REP004": "#8b5cf6",
    "REP015": "#f59e0b", "REP020": "#10b981", "REP024": "#06b6d4",
    "REP053": "#ef4444", "REP081": "#f97316",
}

def _rep_badge(rep):
    col = REP_COLOURS.get(rep, "#6b7280")
    return f'<span class="rbadge" style="background:{col}">{escape(rep)}</span>'


def _kv_table(rows):
    if not rows:
        return ""
    cells = "".join(
        f'<tr><td class="kk">{escape(str(k))}</td><td class="kv">{escape(str(v))}</td></tr>'
        for k, v in rows if v not in ("", None))
    return f'<table class="kvt"><tbody>{cells}</tbody></table>'


def _section_table(code, values):
    if not values:
        return ""
    name = SECTION_NAMES.get(code, code)
    cells = "".join(
        f'<tr><td class="kk">[{i}]</td><td class="kv mono">{escape(str(v))}</td></tr>'
        for i, v in enumerate(values))
    return (f'<div class="sec-block"><div class="sec-hdr">{escape(code)} - {escape(name)}</div>'
            f'<table class="kvt"><tbody>{cells}</tbody></table></div>')


def _render_record(r, idx):
    d = r['decoded']; h01 = d.get('h01', {}); h02 = d.get('h02', {})
    rep = d.get('rep_type', '?'); col = REP_COLOURS.get(rep, '#6b7280')
    ts = r['timestamp'].strftime('%H:%M:%S')
    summ = build_summary_line(r)

    meta_rows = []
    if h01.get('event_time_str'):
        meta_rows.append(("Event time (UTC)", h01['event_time_str']))
    meta_rows.append(("Aircraft", f"{r['registration']}  {r['ac_type']}"))
    meta_rows.append(("Flight", h02.get('flight_id', r.get('flight', '-'))))
    meta_rows.append(("Route",
        f"{h02.get('origin_icao','')} -> {h02.get('dest_icao','')}" if h02.get('origin_icao') else '-'))
    if h01.get('field_3_unverified') is not None:
        meta_rows.append(("Field [3] (unverified)", str(h01['field_3_unverified'])))
    if h01.get('field_4_unverified') is not None:
        meta_rows.append(("Field [4] (unverified)", str(h01['field_4_unverified'])))
    if h01.get('field_14_unverified'):
        meta_rows.append(("Field [14] (unverified)", str(h01['field_14_unverified'])))
    meta_rows.append(("Phase", f"{d.get('phase','')} / {d.get('subphase','')}"))
    if d.get('h03', '').strip():
        meta_rows.append(("Event note", d['h03'].strip()))
    meta_rows.append(("Ground station", f"{r['gs_icao']} {r['gs_name']}"))
    meta_rows.append(("Frequency", f"{r['freq_mhz']} MHz  SNR: {r['snr_db']:.1f} dB"))
    if h02.get('config_ref'):
        meta_rows.append(("Config ref", h02['config_ref']))
    meta_html = _kv_table(meta_rows)

    decoded_html = ""
    appr = d.get('approach', {})
    if appr:
        arows = []
        if 'gross_weight_kg' in appr: arows.append(("Gross weight", f"{appr['gross_weight_kg']:,} kg"))
        if 'zfw_kg' in appr: arows.append(("Zero fuel weight", f"{appr['zfw_kg']:,} kg"))
        if 'cg_mac_pct' in appr: arows.append(("CG (%MAC)", f"{appr['cg_mac_pct']:.1f}%"))
        if 'flap_pos' in appr: arows.append(("Flap position", str(appr['flap_pos'])))
        if 'vapp_kt' in appr: arows.append(("Vapp", f"{appr['vapp_kt']} kt"))
        if 'vls_kt' in appr: arows.append(("Vls", f"{appr['vls_kt']} kt"))
        if 'vs_fpm' in appr: arows.append(("Vertical speed", f"{appr['vs_fpm']:+,} fpm"))
        if 'ra_ft' in appr: arows.append(("Radio alt", f"{appr['ra_ft']} ft"))
        if 'airspeed_kt' in appr: arows.append(("Airspeed (IAS)", f"{appr['airspeed_kt']} kt"))
        if 'loc_dev_dot' in appr: arows.append(("LOC deviation", f"{appr['loc_dev_dot']:+.2f} dot"))
        if 'gs_dev_dot' in appr: arows.append(("G/S deviation", f"{appr['gs_dev_dot']:+.2f} dot"))
        if 'wind_dir_deg' in appr and 'wind_spd_kt' in appr:
            arows.append(("Wind", f"{appr['wind_dir_deg']:03d} deg / {appr['wind_spd_kt']} kt"))
        if 'headwind_kt' in appr: arows.append(("Headwind component", f"{appr['headwind_kt']:+d} kt"))
        if 'ap_engaged' in appr: arows.append(("AP engaged", "Yes" if appr['ap_engaged'] else "No"))
        decoded_html = '<div class="sec-hdr">Decoded approach parameters</div>' + _kv_table(arows)

    gear = d.get('gear_extension', {})
    if gear:
        grows = []
        if 'mlg_l_press_psi' in gear:
            grows.append(("MLG-L pressure", f"{gear['mlg_l_press_psi']} psi (ref {gear['mlg_l_ref_press_psi']} psi)"))
        if 'mlg_r_press_psi' in gear:
            grows.append(("MLG-R pressure", f"{gear['mlg_r_press_psi']} psi (ref {gear['mlg_r_ref_press_psi']} psi)"))
        if 'speed_delta_kt' in gear:
            grows.append(("Speed delta", f"{gear['speed_delta_kt']} kt"))
        decoded_html = '<div class="sec-hdr">Decoded gear extension parameters</div>' + _kv_table(grows)

    td = d.get('touchdown', {})
    if td:
        trows = [("Landing type", "Normal" if td.get('normal_landing') else "Abnormal"),
                 ("Autoland", "Yes" if td.get('autoland') else "No")]
        if td.get('soft_landing') is not None:
            trows.append(("Soft landing", "Yes" if td['soft_landing'] else "No"))
        if 'mlg_vload_l' in td:
            trows.append(("MLG vertical load L", str(td['mlg_vload_l'])))
            trows.append(("MLG vertical load R", str(td['mlg_vload_r'])))
        if 'mlg_long_l' in td:
            trows.append(("MLG longitudinal L", str(td['mlg_long_l'])))
            trows.append(("MLG longitudinal R", str(td['mlg_long_r'])))
        if 'nlg_vload' in td:
            trows.append(("NLG vertical load", str(td['nlg_vload'])))
        decoded_html = '<div class="sec-hdr">Decoded touchdown parameters</div>' + _kv_table(trows)

    tkoff = d.get('takeoff', {})
    if tkoff:
        trows = []
        orig2, dest2 = tkoff.get('origin_icao', ''), tkoff.get('dest_icao', '')
        if orig2:
            trows.append(("Route", f"{orig2} -> {dest2}"))
        for eng in tkoff.get('engines', []):
            trows.append((f"Engine {eng['engine']}",
                          f"Lat:{eng['lat']:.4f} Lon:{eng['lon']:.4f} "
                          f"Alt:{eng['alt_ft']}ft Hdg:{eng['hdg_deg']} VS:{eng['vs_fpm']}fpm TAS:{eng['tas_kt']}kt"))
        decoded_html = '<div class="sec-hdr">Decoded takeoff parameters</div>' + _kv_table(trows)

    flap = d.get('flap_config', {})
    if flap:
        frows = [(c['section'], f"Req: {c['requested']}  Act: {c['achieved']}  "
                  f"{'OK' if c['match'] else 'MISMATCH'}")
                 for c in flap.get('configurations', [])]
        decoded_html = '<div class="sec-hdr">Flap / slat configuration</div>' + _kv_table(frows)

    raw_secs = "".join(_section_table(code, vals) for code, vals in sorted(d.get('sections', {}).items()))

    return f"""
    <div class="rep-card" id="rep-{idx}">
      <div class="rep-hdr" style="border-left:4px solid {col}">
        <div class="rep-title">
          {_rep_badge(rep)}
          <span class="rep-desc">{escape(REP_DESCRIPTIONS.get(rep,('?',''))[0])}</span>
          <span class="rep-ts">{ts}</span>
          <span class="rep-reg">{escape(r['registration'])}</span>
          <span class="rep-atype muted">{escape(r['ac_type'])}</span>
        </div>
        <div class="rep-summ muted">{escape(summ)}</div>
      </div>
      <div class="rep-body">
        <div class="rep-cols">
          <div class="rep-meta">{meta_html}</div>
          <div class="rep-decoded">{decoded_html}</div>
        </div>
        <details>
          <summary class="muted">Raw section data ({len(d.get('sections',{}))} sections)</summary>
          <div class="raw-secs">{raw_secs}</div>
        </details>
      </div>
    </div>"""


def build_html(records, source_files, date_str):
    by_ac = defaultdict(list)
    for r in records:
        reg = r['registration'] or r['decoded'].get('h01', {}).get('ac_reg', '?')
        by_ac[reg].append(r)

    rep_counts = defaultdict(int)
    for r in records:
        rep_counts[r['decoded'].get('rep_type', '?')] += 1
    rep_pills = "".join(
        f'<div class="pill">{_rep_badge(rep)} <strong>{cnt}</strong></div>'
        for rep, cnt in sorted(rep_counts.items()))

    nav = "".join(
        f'<a href="#ac-{escape(reg)}" class="nav-ac">{escape(reg)} <span class="muted">{len(rs)}</span></a>'
        for reg, rs in sorted(by_ac.items()))

    sections_html = ""
    global_idx = 0
    for reg in sorted(by_ac.keys()):
        recs = sorted(by_ac[reg], key=lambda r: r['timestamp'])
        r0 = recs[0]; d0 = r0['decoded']; h02 = d0.get('h02', {})
        orig, dest = h02.get('origin_icao', ''), h02.get('dest_icao', '')
        flt = h02.get('flight_id', r0.get('flight', ''))
        route = f"{orig} -> {dest}" if orig and dest else ""

        rtypes = defaultdict(int)
        for r in recs:
            rtypes[r['decoded'].get('rep_type', '?')] += 1
        rtype_html = "  ".join(f'{_rep_badge(rep)} x{cnt}' for rep, cnt in sorted(rtypes.items()))

        cards = "".join(_render_record(r, global_idx + i) for i, r in enumerate(recs))
        global_idx += len(recs)

        sections_html += f"""
        <section class="ac-section" id="ac-{escape(reg)}">
          <div class="ac-hdr">
            <span class="ib">{escape(reg)}</span>
            <span class="atype muted">{escape(r0.get('ac_type',''))}</span>
            <span class="flt">{escape(flt)}</span>
            <span class="route muted">{escape(route)}</span>
            <span class="rtype-pills">{rtype_html}</span>
          </div>
          {cards}
        </section>"""

    CSS = """
:root{--bg:#0d0f18;--sf:#161929;--sf2:#1e2235;--bd:#272b40;
      --tx:#dde1f5;--mt:#7880a0;--ac:#5b8ef5;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font:14px/1.6 'Segoe UI',system-ui,sans-serif}
a{color:var(--ac);text-decoration:none}a:hover{text-decoration:underline}
header{background:var(--sf);border-bottom:1px solid var(--bd);padding:18px 28px}
header h1{font-size:1.35rem;font-weight:700;color:#fff}
.sub{color:var(--mt);font-size:.82rem;margin-top:3px}
.pills{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.pill{background:var(--sf2);border:1px solid var(--bd);border-radius:20px;
      padding:3px 13px;font-size:.8rem;display:flex;align-items:center;gap:5px}
.pill strong{color:#fff}
.disclaimer{margin-top:10px;font-size:.76rem;color:var(--mt);
            background:var(--sf2);border:1px solid var(--bd);border-radius:6px;
            padding:8px 12px;max-width:900px;line-height:1.5}
.rbadge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:.72rem;
        font-weight:700;color:#fff;font-family:monospace;white-space:nowrap}
.nav{background:var(--sf);border-bottom:1px solid var(--bd);
     padding:10px 28px;display:flex;flex-wrap:wrap;gap:8px}
.nav-ac{background:var(--sf2);border:1px solid var(--bd);border-radius:6px;
        padding:3px 10px;font-size:.82rem;font-family:monospace}
main{max-width:1400px;margin:0 auto;padding:20px 28px}
.ac-section{margin-bottom:32px}
.ac-hdr{display:flex;align-items:center;flex-wrap:wrap;gap:10px;
        padding:12px 0;border-bottom:2px solid var(--bd);margin-bottom:12px}
.ib{background:var(--ac);color:#fff;font-family:monospace;font-weight:800;
    font-size:.95rem;padding:3px 10px;border-radius:6px}
.atype{font-size:.82rem}.flt{font-weight:700}.route{font-size:.85rem}
.rtype-pills{display:flex;flex-wrap:wrap;gap:5px;margin-left:auto}
.rep-card{background:var(--sf);border:1px solid var(--bd);border-radius:10px;
          margin-bottom:10px;overflow:hidden}
.rep-hdr{padding:12px 16px;border-bottom:1px solid var(--bd)}
.rep-title{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.rep-desc{font-weight:600;font-size:.9rem}
.rep-ts{font-family:monospace;font-size:.82rem;color:var(--mt)}
.rep-reg{font-family:monospace;font-weight:700}
.rep-atype{font-size:.8rem}
.rep-summ{font-size:.78rem;margin-top:4px;font-family:monospace}
.rep-body{padding:14px 16px}
.rep-cols{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:12px}
@media(max-width:900px){.rep-cols{grid-template-columns:1fr}}
.sec-hdr{font-size:.75rem;font-weight:700;color:var(--mt);text-transform:uppercase;
         letter-spacing:.06em;margin:8px 0 4px}
.kvt{width:100%;border-collapse:collapse;font-size:.8rem}
.kvt tr:hover td{background:var(--sf2)}
.kk{color:var(--mt);padding:2px 8px 2px 0;width:45%;white-space:nowrap;vertical-align:top}
.kv{padding:2px 0;word-break:break-word}
.raw-secs{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));
          gap:10px;padding:12px 0}
.sec-block{background:var(--sf2);border:1px solid var(--bd);border-radius:6px;
           padding:8px 10px;font-size:.78rem}
.muted{color:var(--mt)}.mono{font-family:monospace}
details summary{cursor:pointer;font-size:.82rem;color:var(--mt);padding:6px 0;user-select:none}
details summary:hover{color:var(--tx)}
#sb{width:100%;max-width:380px;background:var(--sf);border:1px solid var(--bd);
    color:var(--tx);padding:7px 12px;border-radius:6px;font-size:.88rem;margin-bottom:14px}
#sb:focus{outline:none;border-color:var(--ac)}
footer{text-align:center;color:var(--mt);font-size:.75rem;
       padding:20px;border-top:1px solid var(--bd);margin-top:28px}
"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MIAM Decoder - {escape(date_str)}</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <h1>MIAM REP Decoder</h1>
  <div class="sub">
    {escape(date_str)} | {escape(', '.join(source_files))} |
    {len(records)} REP messages from {len(by_ac)} aircraft
  </div>
  <div class="pills">{rep_pills}</div>
  <div class="disclaimer">
    Route, flight ID, event timestamp and REP/phase classification are
    verified from the message structure. Raw data section values
    (A10, B10, C10 etc.) are airline-internal performance parameters
    whose exact field-to-unit mapping is not publicly documented and
    has not been independently verified, so they are shown as raw
    values rather than labelled engineering units.
  </div>
</header>
<nav class="nav">{nav}</nav>
<main>
  <input id="sb" type="search" placeholder="Filter by reg, flight, route, REP type..." oninput="ft(this.value)">
  {sections_html}
</main>
<footer>Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} by miam_decoder.py</footer>
<script>
function ft(q) {{
  q = q.toLowerCase();
  document.querySelectorAll('.rep-card').forEach(c => {{
    c.style.display = c.textContent.toLowerCase().includes(q) ? '' : 'none';
  }});
  document.querySelectorAll('.ac-section').forEach(s => {{
    const vis = [...s.querySelectorAll('.rep-card')].some(c => c.style.display !== 'none');
    s.style.display = vis ? '' : 'none';
  }});
}}
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Decode MIAM REP messages from dumpvdl2 logs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(__doc__))
    ap.add_argument("logs", nargs="+", help="Log file(s)")
    ap.add_argument("--output", default="miam_report.html")
    ap.add_argument("--text", action="store_true", help="Print full detail to console")
    ap.add_argument("--aircraft", default=None, metavar="REG")
    ap.add_argument("--rep", default=None, metavar="TYPE")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    all_records = []
    source_names = []
    for p in args.logs:
        path = Path(p)
        if not path.exists():
            print(f"WARNING: not found: {path}", file=sys.stderr)
            continue
        recs = extract_miam_blocks(path, verbose=args.verbose)
        all_records.extend(recs)
        source_names.append(path.name)

    if not all_records:
        print("No MIAM REP blocks found in the supplied log file(s).", file=sys.stderr)
        sys.exit(1)

    if args.aircraft:
        filt = args.aircraft.upper().lstrip('.')
        all_records = [r for r in all_records
                       if r['registration'].upper() == filt
                       or r['decoded'].get('h01', {}).get('ac_reg', '').upper() == filt]
    if args.rep:
        rtype = args.rep.upper()
        all_records = [r for r in all_records if r['decoded'].get('rep_type', '') == rtype]

    if not all_records:
        print("No records after filtering.", file=sys.stderr)
        sys.exit(1)

    all_records.sort(key=lambda r: r['timestamp'])
    dates = sorted({r['timestamp'].date() for r in all_records})
    date_str = ", ".join(str(d) for d in dates)

    print_console_summary(all_records, verbose=args.text)

    out = Path(args.output)
    out.write_text(build_html(all_records, source_names, date_str), encoding='utf-8')
    print(f"[HTML] Written: {out}")


if __name__ == "__main__":
    main()
