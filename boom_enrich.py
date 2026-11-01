#!/usr/bin/env python3
"""Jpuffle5 Boomerang normalizer.

Runs after boom.sh pushes the day's "Jpuffle5 Boomerang.xml" (see
.github/workflows/boom.yml). For every programme it:
  1. fixes the title (house style, typos, en-dash, stray spaces)
  2. films (slot >= 60 min, or a known film) get "Title (Year)"
  3. adds a <desc>: OMDb (IMDb short plot) first, TMDB as fallback

Lookups are cached in boom_desc_cache.json (hits and misses, 60 days), so a
normal day costs only a handful of OMDb calls. Uses OMDB_API_KEY2 (its own
free key, separate from the Toonami enrichment's OMDB_API_KEY).
Pure stdlib apart from requests.
"""
import json, os, re, sys, time
import xml.etree.ElementTree as ET
from datetime import datetime

import requests

XML_FILE   = sys.argv[1] if len(sys.argv) > 1 else "Jpuffle5 Boomerang.xml"
CACHE_FILE = "boom_desc_cache.json"
TTL        = 60 * 86400
OMDB_KEY   = os.environ.get("OMDB_API_KEY2", "").strip()
TMDB_KEY   = os.environ.get("TMDB_API_KEY", "").strip()
BUDGET     = int(os.environ.get("BOOM_OMDB_BUDGET", "300"))

# ── 1. title fixes: Jpuffle5's name -> our name ─────────────────────────────
RENAME = {
    "Tom & Jerry": "Tom and Jerry",
    "Garfield & Friends": "Garfield and Friends",
    "Pixie & Dixie": "Pixie and Dixie",
    "Ed, Edd, n Eddy": "Ed, Edd n Eddy",
    "Bucky O' Hare and the Toad Wars!": "Bucky O'Hare and the Toad Wars!",
    "All Dogs Go To Heaven": "All Dogs Go to Heaven",
    "All Dogs Go To Heaven 2": "All Dogs Go to Heaven 2",
    "Rocky and Bullwinkle": "The Rocky and Bullwinkle Show",
    "Pink Panther": "The Pink Panther Show",
    "SIGN OFF": "Sign Off",
}

# ── 2. films: title -> release year (known ones; new films get OMDb's year) ──
FILM_YEAR = {
    "Ghostbusters": 1984, "Beetlejuice": 1988, "Casper": 1995, "Scooby-Doo": 2002,
    "Underdog": 2007, "Shrek": 2001, "Shrek 2": 2004, "Shrek Forever After": 2010,
    "Coraline": 2009, "Corpse Bride": 2005, "Monster House": 2006, "Nimona": 2023,
    "Hotel Transylvania": 2012, "Hotel Transylvania 2": 2015, "The LEGO Movie": 2014,
    "The Book of Life": 2014, "The Polar Express": 2004, "The Halloween Tree": 1993,
    "The Nightmare Before Christmas": 1993, "Cloudy with a Chance of Meatballs": 2009,
    "Diary of a Wimpy Kid": 2010, "All Dogs Go to Heaven": 1989, "All Dogs Go to Heaven 2": 1996,
    "Pirates of the Caribbean: Dead Man's Chest": 2006, "Charlie and the Chocolate Factory": 2005,
    "Willy Wonka and the Chocolate Factory": 1971,
    "Wallace & Gromit: The Curse of the Were-Rabbit": 2005,
    "Scooby-Doo Meets the Boo Brothers": 1987, "Scooby-Doo and the Ghoul School": 1988,
    "Scooby-Doo and the Reluctant Werewolf": 1988, "Scooby-Doo! in Arabian Nights": 1994,
    "Scooby-Doo and the Witch's Ghost": 1999, "Scooby-Doo and the Monster of Mexico": 2003,
    "The Flintstones: On the Rocks": 2001, "The Flintstones Meet Rockula and Frankenstone": 1979,
    "The Flintstones: Little Big League": 1978, "The Flintstones 25th Anniversary Celebration": 1986,
    "Alvin and the Chipmunks Meet Frankenstein": 1999, "Alvin and the Chipmunks Meet the Wolfman": 2000,
    "Scary Godmother: Halloween Spooktacular": 2003, "Scary Godmother: The Revenge of Jimmy": 2005,
    "Spookley the Square Pumpkin": 2004, "Dear Dracula": 2012, "A Monsterous Holiday": 2013,
    "Casper's Scare School": 2006, "Billy & Mandy: Wrath of the Spider Queen": 2007,
    "Underfist: Halloween Bash": 2008, "The Banana Splits in Hocus Pocus Park": 1972,
    "An American Tail: The Mystery of the Night Monster": 1999,
    "The Haunting Hour: Don't Think About It": 2007,
    "My Little Pony: Equestria Girls – Rainbow Rocks": 2014,
    "My Little Pony: Equestria Girls – Friendship Games": 2015,
    "It's the Great Pumpkin, Charlie Brown": 1966,
}
# long slots that are series, not films
NOT_FILM = {"The New Scooby-Doo Movies", "Sign Off", "MGM"}
# blocks / filler: no lookup
NO_LOOKUP = {"Sign Off", "MGM"}
# lookup alias for OMDb/TMDB when the on-air name differs from IMDb's
LOOKUP = {
    "Tom and Jerry": "Tom and Jerry", "Looney Tunes": "Looney Tunes",
    "The Rocky and Bullwinkle Show": "Rocky and His Friends",
    "Flintstone Frolics": "The Flintstones",
    "Pink Panther and Pals": "Pink Panther and Pals",
    "Pixie and Dixie": "Pixie and Dixie and Mr. Jinks",
}
YEAR_RE = re.compile(r"\s\((19|20)\d\d\)$")


def fix_title(t):
    t = re.sub(r"\s+", " ", (t or "")).strip()
    # "Equestria Girls - X" -> en dash like the official titles
    t = re.sub(r"(Equestria Girls) - ", r"\1 – ", t)
    return RENAME.get(t, t)


def minutes(p):
    f = lambda s: datetime.strptime(s[:14].ljust(14, "0"), "%Y%m%d%H%M%S")
    try:
        return (f(p.get("stop")) - f(p.get("start"))).total_seconds() / 60
    except Exception:
        return 0


# ── lookups ─────────────────────────────────────────────────────────────────
cache = {}
calls = {"omdb": 0, "tmdb": 0}
left = BUDGET


def omdb(**q):
    global left
    if not OMDB_KEY or left <= 0:
        return None
    left -= 1; calls["omdb"] += 1
    try:
        j = requests.get("https://www.omdbapi.com/", params={"apikey": OMDB_KEY, **q}, timeout=20).json()
    except Exception:
        return None
    if "limit" in str(j.get("Error", "")).lower():
        left = 0; return None
    return j


def omdb_find(name, kind, year=None):
    """-> dict(plot, year, title) or {} if OMDb has nothing, None if not asked."""
    q = {"t": name, "type": kind, "plot": "short", **({"y": year} if year else {})}
    j = omdb(**q)
    if j is None:
        return None
    if j.get("Response") != "True":
        s = omdb(s=name, type=kind, **({"y": year} if year else {}))
        if s is None:
            return None
        hits = s.get("Search") or []
        j = (omdb(i=hits[0]["imdbID"], plot="short") or {}) if hits else {}
    plot = (j.get("Plot") or "").strip()
    return {"plot": "" if plot == "N/A" else plot, "year": (j.get("Year") or "")[:4],
            "title": j.get("Title", ""), "id": j.get("imdbID", "")}


def tmdb_plot(name, kind, year=None):
    if not TMDB_KEY:
        return ""
    calls["tmdb"] += 1
    path = "movie" if kind == "movie" else "tv"
    params = {"api_key": TMDB_KEY, "query": name}
    if year:
        params["year" if kind == "movie" else "first_air_date_year"] = year
    try:
        r = requests.get(f"https://api.themoviedb.org/3/search/{path}", params=params, timeout=20).json()
        res = r.get("results") or []
        return (res[0].get("overview") or "").strip() if res else ""
    except Exception:
        return ""


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def resolve(title, mins):
    """-> (display title, desc). Films are slots of 60+ min (Beetlejuice/Underdog/
    Scooby-Doo air both as a 30-min series and as a film)."""
    base = YEAR_RE.sub("", title)                       # already has (Year) from an earlier run
    if base in NO_LOOKUP:
        return base, ""
    film = mins >= 60 and base not in NOT_FILM
    key = f"{'movie' if film else 'series'}|{base}"
    ent = cache.get(key)
    if ent and time.time() - ent.get("t", 0) < TTL:
        return ent["show"], ent["desc"]
    name, known = LOOKUP.get(base, base), FILM_YEAR.get(base)
    show, desc = base, ""
    if film:
        if known:                                       # known film: exact year, search allowed
            r = omdb_find(name, "movie", known)
            if r is None: return (f"{base} ({known})", "")      # out of budget: retry next run
            show, desc = f"{base} ({known})", r.get("plot", "")
        else:                                           # unknown long slot: exact title match only
            j = omdb(t=name, type="movie", plot="short")
            if j is None: return base, ""
            if j.get("Response") == "True" and norm(j.get("Title", "")) == norm(name):
                yr = (j.get("Year") or "")[:4]
                show = f"{base} ({yr})" if yr else base
                desc = "" if j.get("Plot") in (None, "N/A") else j["Plot"].strip()
                known = yr or None
            else:
                film = False                            # not a film after all -> series lookup
        if film and not desc:
            desc = tmdb_plot(name, "movie", known)
    if not film:
        r = omdb_find(name, "series")
        if r is None: return base, ""
        desc = r.get("plot", "") or tmdb_plot(name, "tv")
    cache[key] = {"t": time.time(), "show": show, "desc": desc}
    return show, desc


def main():
    global cache
    try:
        cache = json.load(open(CACHE_FILE, encoding="utf-8"))
    except Exception:
        cache = {}
    tree = ET.parse(XML_FILE)
    root = tree.getroot()
    if root.find("channel[@id='Jpuffle5']") is None:
        sys.exit("not the Jpuffle5 guide - skipped")
    n = renamed = films = descs = 0
    for p in root.iter("programme"):
        te = p.find("title")
        if te is None or not (te.text or "").strip():
            continue
        n += 1
        old = te.text
        t = fix_title(old)
        show, desc = resolve(t, minutes(p))
        if show != old:
            te.text = show; renamed += 1
        if YEAR_RE.search(show):
            films += 1
        de = p.find("desc")
        if desc and de is None:
            de = ET.Element("desc", {"lang": "en"}); de.text = desc
            p.insert(list(p).index(te) + 1, de)        # title, desc, icon - XMLTV order
        if p.find("desc") is not None:
            descs += 1
    ET.indent(tree, space="  ")
    tree.write(XML_FILE, encoding="UTF-8", xml_declaration=True)
    json.dump(cache, open(CACHE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1, sort_keys=True)
    print(f"Jpuffle5: {n} programmes · {renamed} titles fixed · {films} films with (Year) · "
          f"{descs} with descriptions · OMDb calls {calls['omdb']}"
          f"{'' if OMDB_KEY else ' (no OMDB_API_KEY2 secret)'} · TMDB calls {calls['tmdb']}")


if __name__ == "__main__":
    main()
