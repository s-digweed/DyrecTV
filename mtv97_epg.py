#!/usr/bin/env python3
"""Looping XMLTV guide for Toonami Aftermath 'MTV97'.

MTV97 is a live/continuous channel: its /playlists catalog is FROZEN at a
single Mon-Sun week (2022-08-30 .. 2022-09-05). The iptv-org grabber can't
use it because it filters playlists to the grab date and finds nothing
current. This script instead treats that week as a repeating loop:

  1. fetch the frozen week's playlists (+ each playlist's media/times),
  2. bucket each playlist by its broadcast weekday,
  3. project the matching weekday onto a rolling window of DAYS days from
     today (UTC), shifting every programme by a whole number of days so the
     clock time and weekday are preserved,
  4. emit flat XMLTV (IPTVBoss-friendly).

Stdlib only. Safe to run in GitHub Actions or Termux.
"""

import sys, re, json, urllib.request, ssl
from datetime import datetime, timezone, timedelta
import xml.etree.ElementTree as ET

_VIDEXT = re.compile(r"\.(mp4|mkv|avi|mov|m4v|ts|webm|flv|wmv)$", re.I)

def _cap(s):
    return " ".join(w[:1].upper() + w[1:] if w else w for w in s.split(" "))

def parse_filepath(fp):
    """'t:\\mtv\\korn - 1999 - falling away from me.mp4' ->
    ('Korn - Falling Away From Me', '1999'). Falls back gracefully."""
    base = _VIDEXT.sub("", fp.replace("\\", "/").split("/")[-1]).strip()
    parts = [p.strip() for p in base.split(" - ")]
    year = None
    if len(parts) >= 3 and re.fullmatch(r"(19|20)\d\d", parts[1]):
        artist, year, song = parts[0], parts[1], " - ".join(parts[2:])
    elif len(parts) >= 2 and re.fullmatch(r"(19|20)\d\d", parts[-1]):
        artist, year, song = parts[0], parts[-1], " - ".join(parts[1:-1])
    elif len(parts) >= 2:
        artist, song = parts[0], " - ".join(parts[1:])
    else:
        artist, song = None, base
    title = f"{_cap(artist)} - {_cap(song)}" if artist else _cap(song)
    return title, year

API      = "https://api.toonamiaftermath.com"
SCHEDULE = "MTV97"
CH_ID    = "MTV97.ToonamiAftermath.us"   # tvg-id to match in your M3U
CH_NAME  = "MTV97"
LOGO     = ""                            # optional icon URL
DAYS     = 7                             # rolling window length
OUT      = "TOONAMIAM.xml"      # merge target (the grabber's output); or standalone if absent
# the frozen catalog's own anchor week (used only to pull the playlists)
SEED_DATE = "2022-08-30T00:00:00.000Z"


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
        return json.load(r)


def _dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def fetch_week():
    """Return {weekday: [media,...]} for the frozen MTV97 week."""
    lst = _get(f"{API}/playlists?scheduleName={SCHEDULE}"
               f"&startDate={SEED_DATE}&thisWeek=true&weekStartDay=monday")
    byday = {}
    for p in lst:
        content = _get(f"{API}/playlist?id={p['_id']}&addInfo=true")
        pl = content.get("playlist") or {}
        media = []
        for b in pl.get("blocks", []):
            bn = b.get("name")
            for m in b.get("mediaList", []):
                st, en = m.get("startDate"), m.get("endDate")
                if not st or not en:
                    continue
                name = m.get("name")
                year = None
                if not name and m.get("filepath"):
                    name, year = parse_filepath(m["filepath"])
                media.append({"name": name or bn,
                              "start": _dt(st), "stop": _dt(en),
                              "year": year, "info": m.get("info") or {}})
        if not media:
            continue
        media.sort(key=lambda x: x["start"])
        wd = media[0]["start"].weekday()          # broadcast-day weekday
        byday.setdefault(wd, media)
    return byday


def build(byday):
    tv = ET.Element("tv", {"generator-info-name": "mtv97-loop"})
    ch = ET.SubElement(tv, "channel", {"id": CH_ID})
    ET.SubElement(ch, "display-name").text = CH_NAME
    if LOGO:
        ET.SubElement(ch, "icon", {"src": LOGO})

    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    n = 0
    for d in range(DAYS):
        target = today + timedelta(days=d)
        src = byday.get(target.weekday())
        if not src:
            continue
        src_date = src[0]["start"].replace(hour=0, minute=0, second=0, microsecond=0)
        delta = target - src_date                 # whole number of days
        for m in src:
            st, sp = m["start"] + delta, m["stop"] + delta
            p = ET.SubElement(tv, "programme", {
                "start": st.strftime("%Y%m%d%H%M%S") + " +0000",
                "stop":  sp.strftime("%Y%m%d%H%M%S") + " +0000",
                "channel": CH_ID})
            ET.SubElement(p, "title", {"lang": "en"}).text = "Music Videos"
            desc = f"{m['name']} ({m['year']})" if m.get("year") else m["name"]
            ET.SubElement(p, "desc", {"lang": "en"}).text = desc
            info = m["info"]
            if info.get("image"):
                ET.SubElement(p, "icon", {"src": info["image"]})
            n += 1
    return tv, n


def _frag(tv):
    """Serialize MTV97's <channel> and <programme> elements to flat XML strings."""
    ch = ET.tostring(tv.find("channel"), encoding="unicode").strip()
    progs = "\n".join(ET.tostring(p, encoding="unicode").strip()
                      for p in tv.findall("programme"))
    return ch, progs


def merge_into(target, channel_str, prog_str):
    """Insert MTV97 into an existing XMLTV file: <channel> right after the
    opening <tv ...> tag, programmes right before </tv>. Leaves the rest
    of the grabber's output byte-for-byte intact."""
    xml = open(target, encoding="utf-8").read()
    # drop any stale MTV97 block from a previous run (idempotent re-runs)
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
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="mtv97-loop">\n')
        f.write(channel_str + "\n" + prog_str + "\n</tv>\n")


def main():
    # optional arg: path to an existing XMLTV file to MERGE into (e.g. TOONAMIAM.xml)
    target = sys.argv[1] if len(sys.argv) > 1 else OUT
    byday = fetch_week()
    print(f"frozen week: {len(byday)} daily playlists, weekdays {sorted(byday)}")
    if not byday:
        print("ERROR: no MTV97 playlists returned; leaving existing file untouched.")
        sys.exit(1)
    tv, n = build(byday)
    channel_str, prog_str = _frag(tv)
    import os
    if os.path.exists(target):
        try:
            merge_into(target, channel_str, prog_str)
            print(f"merged {n} MTV97 programmes into existing {target}")
            return
        except ValueError as e:
            print(f"WARNING: {e}; writing standalone instead")
    write_standalone(target, channel_str, prog_str)
    print(f"wrote standalone {target}: {n} MTV97 programmes over {DAYS} days")


if __name__ == "__main__":
    main()
