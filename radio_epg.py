#!/usr/bin/env python3
"""Continuous filler guide for 'Toonami Aftermath Radio'.

The radio stream has no per-item schedule, so we emit simple contiguous 12-hour
blocks titled "Radio" across a rolling window (no gaps). Stdlib only; merges into
TOONAMIAM.xml, or writes standalone if the target is missing. Mirrors mtv97_epg.py.
"""

import sys, os, re
from datetime import datetime, timezone, timedelta
import xml.etree.ElementTree as ET

CH_ID   = "Radio.ToonamiAftermath.us"      # tvg-id to match in your M3U
CH_NAME = "Toonami Aftermath Radio"
LOGO    = "https://i.imgur.com/sBUyVQu.png"
DAYS    = 8                                 # rolling window length (days)
BLOCK_H = 12                                # block length in hours
OUT     = "TOONAMIAM.xml"                   # merge target; standalone if absent
TITLE   = "Radio"
DESC    = ("Toonami for your ears, 24/7.\n\n"
           "Electronic, trip-hop, and lo-fi beats, with OSTs, sci-fi sound bites, "
           "and official Toonami mixes.\n\n"
           "Plug in, space out.")


def build():
    tv = ET.Element("tv", {"generator-info-name": "radio-filler"})
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
        if LOGO:
            ET.SubElement(p, "icon", {"src": LOGO})
        n += 1
    return tv, n


def _frag(tv):
    ch = ET.tostring(tv.find("channel"), encoding="unicode").strip()
    progs = "\n".join(ET.tostring(p, encoding="unicode").strip()
                      for p in tv.findall("programme"))
    return ch, progs


def merge_into(target, channel_str, prog_str):
    """Insert Radio into an existing XMLTV file: <channel> right after <tv ...>,
    programmes right before </tv>. Idempotent (drops any prior Radio block)."""
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
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="radio-filler">\n')
        f.write(channel_str + "\n" + prog_str + "\n</tv>\n")


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else OUT
    tv, n = build()
    channel_str, prog_str = _frag(tv)
    if os.path.exists(target):
        try:
            merge_into(target, channel_str, prog_str)
            print(f"merged {n} Radio filler blocks into {target}")
            return
        except ValueError as e:
            print(f"WARNING: {e}; writing standalone instead")
    write_standalone(target, channel_str, prog_str)
    print(f"wrote standalone {target}: {n} Radio filler blocks over {DAYS} days")


if __name__ == "__main__":
    main()
