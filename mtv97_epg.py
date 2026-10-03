#!/usr/bin/env python3
"""Generic filler guide for Toonami Aftermath 'MTV97'.

MTV97 is a continuous music-video channel with no usable per-item schedule
(its /playlists catalog is frozen/unreliable), so instead of faking a track
list we emit simple, honest 12-hour blocks titled "Music Videos" across a
rolling window. Contiguous (no gaps), stdlib only, merges into TOONAMIAM.xml.
"""

import sys, os, re
from datetime import datetime, timezone, timedelta
import xml.etree.ElementTree as ET

CH_ID   = "MTV97.ToonamiAftermath.us"     # tvg-id to match in your M3U
CH_NAME = "MTV97"
LOGO    = ""                               # optional icon URL
DAYS    = 8                                # rolling window length (days)
BLOCK_H = 12                               # block length in hours
OUT     = "TOONAMIAM.xml"                  # merge target; standalone if absent
TITLE   = "Music Videos"
DESC    = ("Stuck in the TRL era. Nonstop 90s and early-2000s music videos, "
           "vintage MTV idents, and retro commercials you forgot existed.")


def build():
    tv = ET.Element("tv", {"generator-info-name": "mtv97-filler"})
    ch = ET.SubElement(tv, "channel", {"id": CH_ID})
    ET.SubElement(ch, "display-name").text = CH_NAME
    if LOGO:
        ET.SubElement(ch, "icon", {"src": LOGO})

    # start one day back at 00:00 UTC so "now" is always covered, then tile
    # contiguous 12h blocks forward across the window.
    anchor = (datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
              - timedelta(days=1))
    blocks = (DAYS + 1) * (24 // BLOCK_H)
    n = 0
    for i in range(blocks):
        st = anchor + timedelta(hours=i * BLOCK_H)
        sp = st + timedelta(hours=BLOCK_H)
        p = ET.SubElement(tv, "programme", {
            "start": st.strftime("%Y%m%d%H%M%S") + " +0000",
            "stop":  sp.strftime("%Y%m%d%H%M%S") + " +0000",
            "channel": CH_ID})
        ET.SubElement(p, "title", {"lang": "en"}).text = TITLE
        ET.SubElement(p, "desc", {"lang": "en"}).text = DESC
        n += 1
    return tv, n


def _frag(tv):
    ch = ET.tostring(tv.find("channel"), encoding="unicode").strip()
    progs = "\n".join(ET.tostring(p, encoding="unicode").strip()
                      for p in tv.findall("programme"))
    return ch, progs


def merge_into(target, channel_str, prog_str):
    """Insert MTV97 into an existing XMLTV file: <channel> right after <tv ...>,
    programmes right before </tv>. Idempotent (drops any prior MTV97 block)."""
    xml = open(target, encoding="utf-8").read()
    xml = re.sub(r'\s*<channel id="%s">.*?</channel>' % re.escape(CH_ID), "", xml, flags=re.S)
    xml = re.sub(r'\s*<programme[^>]*channel="%s".*?</programme>' % re.escape(CH_ID), "", xml, flags=re.S)
    m = re.search(r"<tv\b[^>]*>", xml)
    if not m or "</tv>" not in xml:
        raise ValueError("target is not an XMLTV <tv> document")
    pos = m.end()
    xml = xml[:pos] + "\n" + channel_str + xml[pos:]
    idx = xml.rfind("</tv>")
    xml = xml[:idx] + prog_str + "\n" + xml[idx:]
    open(target, "w", encoding="utf-8").write(xml)


def write_standalone(target, channel_str, prog_str):
    with open(target, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="mtv97-filler">\n')
        f.write(channel_str + "\n" + prog_str + "\n</tv>\n")


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else OUT
    tv, n = build()
    channel_str, prog_str = _frag(tv)
    if os.path.exists(target):
        try:
            merge_into(target, channel_str, prog_str)
            print(f"merged {n} MTV97 filler blocks into {target}")
            return
        except ValueError as e:
            print(f"WARNING: {e}; writing standalone instead")
    write_standalone(target, channel_str, prog_str)
    print(f"wrote standalone {target}: {n} MTV97 filler blocks over {DAYS} days")


if __name__ == "__main__":
    main()
