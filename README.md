# dumpvdl2LogAnalyser

A set of dumpvdl2 plain-text log file analysis tools generated with the help of Claude.

These tools are

1. dumpvdl2 analyser

  dumpvdl2_analyser.py parses daily dumpvdl2 plain-text log files (ATN/AVLC format), builds a per-aircraft conversation summary, decodes message content where the structure is verifiable, flags sessions that deserve closer attention, and produces a searchable HTML report with optional CSV and SQLite outputs.

2. miam decoder

miam_decoder.py extracts and decodes MIAM CORE REP messages from dumpvdl2 plain-text log files. These are Airbus A350 Flight Operations Quality Assurance (FOQA) style performance reports, transmitted over VDL2 using the MIAM (Media Independent Aircraft Messaging) encoding.

dumpvdl2 itself handles the deflate decompression and reassembly of the MIAM envelope. This script decodes the inner ACARS payload that dumpvdl2 leaves as plain text in the log.

3. extract vdl2

  extract_vdl2.py reads a dumpvdl2 plain-text log file,  locates  every  message  block  that belongs to a specified aircraft, and writes those blocks verbatim to a plain-text output file. The aircraft to search for is identified either by its ICAO 24-bit hex address (e.g. 407739) or by its registration mark (e.g. G-TUMH). The tool auto-detects which type of identifier has been supplied: a string of exactly six hexadecimal characters is treated as an ICAO address; anything else is treated as a registration.




