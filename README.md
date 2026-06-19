# dumpvdl2LogAnalyzer
dumpvdl2 log file analysis tools
A set of dumpvdl2 log file analysis tools generated with the help of Claude

These tools are

1. dumpvdl2 analyser
Parses daily dumpvdl2 plain-text log files, decodes message content for all major ACARS/ATN message types, builds per-aircraft conversation summaries, flags sessions deserving further analysis, and writes a self-contained HTML report 

2. miam decoder
Extracts and decodes MIAM CORE REP (approach & landing performance) messages from dumpvdl2 plain-text log files and produces a self-contained HTML report plus a console summary.

3. extract vdl2
extract_vdl2 reads a dumpvdl2 VHF Data Link Mode2  (VDL2) log  file,  locates  every  message  block  that belongs to a specified aircraft, and writes those blocks verbatim to a plain-text output file.

The aircraft to search for is identified either by its ICAO 24-bit hex address (e.g. 407739) or by its registration mark (e.g. G-TUMH). The tool auto-detects which type of identifier has been supplied: a string of exactly six hexadecimal characters is treated as an ICAO address; anything else is treated as a registration.




