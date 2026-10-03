#!/usr/bin/env python3
"""Enrich the Toonami Aftermath + Snickelodeon channels in TOONAMIAM.xml with
real descriptions and season/episode numbers.

Strategy (option B):
  - normalize the on-screen title to a canonical series name (ALIAS),
  - if the programme has an episode title (<sub-title>), match it to a
    season/episode via the series' episode list (TVmaze/TVDB name maps),
    then attach that EPISODE's synopsis + <episode-num>,
  - otherwise fall back to the SHOW-level synopsis.

Sources: TMDB (key) -> TVmaze (keyless) -> TheTVDB (key). Everything is cached
in toonami_desc_cache.json so repeat runs don't re-hit the APIs. The engine is
lifted from the Whiplash generator. Only `requests` is required.

Usage:  python3 enrich_toonami.py <path-to-TOONAMIAM.xml>
Leaves MTV97 and every other channel untouched.
"""

import os, re, sys, json, time, html, bisect
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
import requests
import xml.etree.ElementTree as ET

# ── channels we enrich: xmltv id -> (API scheduleName, stream delay minutes) ──
# East/West share one scheduleName; West is just the East feed delayed 180 min.
CH_MAP = {
    "ToonamiAftermath.us@East": ("Toonami Aftermath EST", 0),
    "ToonamiAftermath.us@West": ("Toonami Aftermath EST", 180),
    "Snickelodeon EST":         ("Snickelodeon EST", 0),
    "Snickelodeon EST+180":     ("Snickelodeon EST", 180),
}
TARGET_CHANNELS = set(CH_MAP)
API_ENDPOINT = "https://api.toonamiaftermath.com"
MATCH_TOL_S  = 120          # air-time match tolerance when recovering episodeNumber

# ── on-screen title -> canonical series name for lookups ──
ALIAS = {
    # Toonami
    "DBZ": "Dragon Ball Z",
    "Dragonball": "Dragon Ball",
    "Dexters Laboratory": "Dexter's Laboratory",
    "Ed Edd Eddy": "Ed, Edd n Eddy",
    "Space Ghost C2C": "Space Ghost Coast to Coast",
    "Full Metal Alchemist": "Fullmetal Alchemist",
    "Powerpuff Girls": "The Powerpuff Girls",
    "Jonny Quest Real Adventures": "The Real Adventures of Jonny Quest",
    "What A Cartoon! Show": "What a Cartoon!",
    "Dr Katz": "Dr. Katz, Professional Therapist",
    "Ranma": "Ranma ½",
    "Thundercats": "ThunderCats",
    "Reboot": "ReBoot",
    "Tick": "The Tick",
    "Batman": "Batman: The Animated Series",
    "Superman": "Superman: The Animated Series",
    "Spiderman": "Spider-Man (1967)",
    "Spider-Man": "Spider-Man: The Animated Series",
    "X-Men": "X-Men: The Animated Series",
    "Hulk": "The Incredible Hulk (1996)",
    "Men in Black": "Men in Black: The Series",
    "Pokemon": "Pokémon",
    "Birdman": "Birdman and the Galaxy Trio",
    "Nadia - Secret of Blue Water": "Nadia: The Secret of Blue Water",
    "Record of Lodoss War TV": "Record of Lodoss War",
    "Yu Yu Hakusho": "Yu Yu Hakusho",
    "Lupin III": "Lupin the Third",
    # Snickelodeon
    "Rockos Modern Life": "Rocko's Modern Life",
    "Ren and Stimpy": "The Ren & Stimpy Show",
    "Pete and Pete": "The Adventures of Pete & Pete",
    "Grimms Fairy Tale Classics": "Grimm's Fairy Tale Classics",
    "The Littl Bits": "The Littl' Bits",
    "Allegras Window": "Allegra's Window",
    "Secret World Of Alex Mack": "The Secret World of Alex Mack",
    "Car 54, Where Are You": "Car 54, Where Are You?",
    "Mr Wizard": "Mr. Wizard's World",
    "Rugrats": "Rugrats",
    "Spider-Man (1967)": "Spider-Man (1967)",   # identity (used as a lookup key)
}

# ── per-title overrides: display name, lookup name, fixed desc, year field ──
# "display": on-screen <title>  |  "lookup": name used for metadata search
# "desc": pinned description (skips lookup)  |  "date": year -> <date> (movies/specials)
# "no_se": never attach season/episode (movies, or series we have no S/E for)
_D = "Dante journeys through the nine circles of Hell -- limbo, lust, gluttony, greed, anger, heresy, violence, fraud and treachery -- in search of his true love, Beatrice."
TITLE_OVERRIDES = {
    # ── movies / long specials (get a <date> year) ──
    "Little Giants": {
        "display": "Little Giants (1993)", "lookup": "Little Giants (1993)", "date": "1993",
        "no_se": True,
        "desc": "Misfits form their own opposing team to an elite peewee football team, "
                "coached by the elite team coach's brother."},
    "Dantes Inferno": {
        "display": "Dante's Inferno: An Animated Epic (2010)",
        "lookup": "Dante's Inferno: An Animated Epic", "date": "2010", "no_se": True, "desc": _D},
    "Scooby-Doo and The Legend Of The Vampire": {
        "display": "Scooby-Doo! and the Legend of the Vampire (2003)",
        "lookup": "Scooby-Doo! and the Legend of the Vampire", "date": "2003", "no_se": True,
        "desc": "The Mystery Gang travels to Australia for a vacation and attends a massive rock "
                "music festival near Vampire Rock. A legendary vampire creature known as the Yowie "
                "Yahoo begins kidnapping musical performers, prompting Scooby and the crew to solve "
                "the mystery."},
    "Bugs Bunnys Halloween Hijinks": {
        "display": "Bugs Bunny's Halloween Hijinks (2000)",
        "lookup": "Bugs Bunny's Halloween Hijinks", "date": "2000", "no_se": True},
    "The Dark Crystal": {
        "display": "The Dark Crystal (1984)", "lookup": "The Dark Crystal", "date": "1984",
        "no_se": True,
        "desc": "On another planet in the distant past, the last of the Gelfling race embarks on a "
                "quest to find the missing shard of a magical crystal and to restore order to his world."},
    "Interstella 5555": {
        "display": "Interstella 5555: The 5tory of the 5ecret 5tar 5ystem (2003)",
        "lookup": "Interstella 5555: The 5tory of the 5ecret 5tar 5ystem", "date": "2003",
        "no_se": True},
    # ── series (year in the title for distinction; no <date>) ──
    "Land of the Lost 1991": {
        "display": "Land of the Lost (1991)", "lookup": "Land of the Lost (1991)", "no_se": True,
        "desc": "Tom and his two teen children, Kevin and younger sister Annie, find themselves "
                "trapped in a parallel universe when their jeep falls into the time portal while "
                "exploring the countryside; together, they must learn to survive."},
    "The Tomorrow People": {
        "display": "The Tomorrow People (1992)", "lookup": "The Tomorrow People (1992)", "no_se": True,
        "desc": "The Tomorrow People are the next evolutionary stage of humans with abilities like "
                "teleportation, telepathy, and healing. Aided by an ancient spacecraft, they use "
                "their powers to protect the world while keeping their existence secret."},
    "Fist Of The North Star": {
        "display": "Fist of the North Star", "lookup": "Fist of the North Star (1984)", "no_se": True,
        "desc": "After a nuclear war turns Earth into a lawless wasteland, Kenshiro, a practitioner "
                "of the deadly master art \"Hokuto Shinken\", fights a succession of tyrannical "
                "warriors to restore order."},
    "Super Sloppy Double Dare": {
        "display": "Super Sloppy Double Dare", "lookup": "Super Sloppy Double Dare", "no_se": True,
        "desc": "On your mark, get set, go! Join four contestants as they answer questions and take "
                "on messy physical challenges (like running in a giant hamster wheel, popping "
                "balloons filled with shaving cream, and more) for the chance to win Super Sloppy "
                "Double Dare!"},
    # ── series with S/E kept, just display/lookup fixes ──
    "Spiderman":  {"display": "Spider-Man (1967)", "lookup": "Spider-Man (1967)"},
    "Spider-Man": {"display": "Spider-Man: The Animated Series",
                   "lookup": "Spider-Man: The Animated Series"},
}

# pinned season/episode for specific (lookup-show, normalized episode name) pairs
EPISODE_SE_OVERRIDE = {
    ("Spider-Man: The Animated Series", "six forgotten warriors chapter 2 unclaimed legacy"): (5, 3),
    ("Spider-Man: The Animated Series", "six forgotten warriors chapter 4 the six fight again"): (5, 5),
}

# pinned show-level descriptions (consulted first in show_overview)
SHOW_DESC_OVERRIDE = {
    "RiffTrax": "Feature films and short subjects presented with comedic running commentary -- "
                "packed with jokes, asides, and relentless riffing from start to finish.",
}

# Titles that embed their own metadata or are riff one-offs.
_MST3K = re.compile(r'^\s*MST3K\s*-\s*S(\d+)E(\d+)\s*-\s*(.+)$', re.I)
_RIFF  = re.compile(r'^\s*Rifftrax\s*-\s*(.+)$', re.I)

def special_title(raw):
    """Return {show, [season, ep], [sub]} for MST3K / RiffTrax titles, else None."""
    m = _MST3K.match(raw)
    if m:
        return {"show": "Mystery Science Theater 3000",
                "season": int(m.group(1)), "ep": int(m.group(2)), "sub": m.group(3).strip()}
    m = _RIFF.match(raw)
    if m:
        return {"show": "RiffTrax", "sub": m.group(1).strip()}
    if raw.strip().lower() == "rifftrax shorts":
        return {"show": "RiffTrax", "shorts": True}
    return None

# RiffTrax feature riffs: normalized feed sub -> (display title, year, description)
RIFFTRAX_SHORTS_DESC = ("The stars of Mystery Science Theater 3000 (1988) riff on weird and "
                        "oddball educational shorts.")
RIFFTRAX_MAP = {
    "house on haunted hill": (
        "RiffTrax Live: House on Haunted Hill (2010)", "2010",
        "Hosted from Nashville on October 28th, 2010, the RiffTrax guys riff on \"House on "
        "Haunted Hill (1959)\" while also watching the short subjects \"Paper and I (1960)\" and "
        "\"Magical Disappearing Money (1972)\". Comedian, actor, and writer Paul F. Tompkins guest stars."),
    "drag me to hell": (
        "RiffTrax: Drag Me to Hell (2009)", "2009",
        "Join Mike and Bill on this sentimental excursion down Hell Lane. Just watch out for "
        "falling anvils and, really, just copious amounts of eyeball splatter."),
    "island of dr moreau": (
        "RiffTrax: The Island of Dr. Moreau (2006)", "2006",
        "And the people cried out with one voice, \"Maketh us a movie in which Marlon Brando can "
        "don a muumuu, false teeth, clown white make-up and a really gay bonnet. See that it also "
        "stareth Val Kilmer at his scenery-chewing best. And, yea, putteth the extras in hot, "
        "smelly animal suits and maketh you the plot absurd.\" And, lo, did John Frankenheimer "
        "deliver unto us The Island of Dr. Moreau. And it was good. Truly, you must see it to "
        "believe it. But you must only see it accompanied by this RiffTrax, for which Mike "
        "enlisted the talents of Kevin Murphy, or else you WILL die."),
    "twilight 4 breaking dawn": (
        "RiffTrax: The Twilight Saga: Breaking Dawn, Part 1 (2012)", "2012",
        "When word leaked that the final Twilight movie would be split into two parts, most "
        "people assumed that this was done by the studio as a cynical cash grab. Not so. The last "
        "chapter in the Twilight saga is so vast, so detailed, that it demanded the lush, "
        "panoramic two movie treatment.\n\n"
        "Okay, maybe they could have trimmed some of that twenty minute wedding because it was "
        "very straightforward and didn't impact the story in any way and essentially could have "
        "been a wedding from a Reese Witherspoon movie. And we probably didn't need every single "
        "one of the scenes where Jacob visits the Cullen's house and shouts at someone. And dear "
        "god, they are showing them playing chess on their honeymoon AGAIN!\n\n"
        "Fortunately, the remaining twelve minutes of the movie that advances the \"plot\" in "
        "some fashion makes up for the slow pace of the rest of the movie by being disgusting and "
        "incoherent. The birth of Bella and Edward's horrible mutant spawn is repellent, nasty "
        "and vile, and yes, we are just referring to the decision to name it Renesmee.*\n\n"
        "Also, this time the wolves go to a logging plant and communicate via telepathy.\n\n"
        "Mike, Kevin and Bill love to hang out at the logging plant too, or at least they did "
        "until that lame foreman called their parents and ruined all their fun.\n\n"
        "*DO NOT NAME YOUR CHILD THIS OR ALLOW ANYONE YOU KNOW TO NAME THEIR CHILD THIS"),
}

# ══════════════════════════════════════════════════════════════════════════
# Metadata engine (TMDB -> TVmaze -> TVDB), lifted from the Whiplash generator
# ══════════════════════════════════════════════════════════════════════════
TMDB_KEY  = os.environ.get("TMDB_API_KEY", "").strip()
TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_LANG = "en-US"
DESC_CACHE_FILE = "toonami_desc_cache.json"
SHOW_TMDB_OVERRIDES = {}
_QYEAR = re.compile(r'\s*\(((?:19|20)\d\d)\)\s*$')
_TAGS  = re.compile(r"<[^>]+>")
_sess  = requests.Session()


def load_cache():
    try:
        with open(DESC_CACHE_FILE, encoding="utf-8") as f:
            c = json.load(f)
    except Exception:
        c = {}
    for k in ("shows", "episodes", "tvmaze_shows", "tvmaze_episodes", "tmdb_seasons",
              "tvmaze_eplist", "tvdb_shows", "tvdb_episodes", "tvmaze_namemap",
              "tvdb_namemap", "show_syn"):
        c.setdefault(k, {})
    return c


def save_cache(c):
    with open(DESC_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False, indent=0, sort_keys=True)


def _tmdb_get(path, **params):
    params["api_key"] = TMDB_KEY
    for _ in range(3):
        try:
            r = _sess.get(TMDB_BASE + path, params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "2")) + 1); continue
            return r.json() if r.status_code == 200 else None
        except requests.RequestException:
            time.sleep(1)
    return None


def _resolve_show_id(show, cache):
    if show in SHOW_TMDB_OVERRIDES:
        return SHOW_TMDB_OVERRIDES[show]
    key = show.lower()
    if key in cache["shows"]:
        return cache["shows"][key]
    ym = _QYEAR.search(show)
    query = show[:ym.start()].strip() if ym else show
    params = {"query": query}
    if ym:
        params["first_air_date_year"] = ym.group(1)
    data = _tmdb_get("/search/tv", **params)
    sid = data["results"][0]["id"] if (data and data.get("results")) else None
    cache["shows"][key] = sid
    return sid


def _rec(v):
    if isinstance(v, dict):
        return (v.get("o") or ""), (v.get("n") or "")
    return (v or ""), ""


def _tmdb_meta(show, season, ep, cache):
    if not TMDB_KEY:
        return "", ""
    sid = _resolve_show_id(show, cache)
    if not sid:
        return "", ""
    ck = f"{sid}|{season}|{ep}"
    if ck in cache["episodes"]:
        return _rec(cache["episodes"][ck])
    data = _tmdb_get(f"/tv/{sid}/season/{season}/episode/{ep}", language=TMDB_LANG)
    ov = ((data or {}).get("overview") or "").strip()
    nm = ((data or {}).get("name") or "").strip()
    cache["episodes"][ck] = {"o": ov, "n": nm}
    return ov, nm


# ── TVmaze (keyless) ──
ENABLE_TVMAZE = True
TVMAZE_BASE = "https://api.tvmaze.com"


def _tvmaze_get(path, **params):
    for _ in range(3):
        try:
            r = _sess.get(TVMAZE_BASE + path, params=params, timeout=20)
            time.sleep(0.2)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "5")) + 1); continue
            return r.json() if r.status_code == 200 else None
        except requests.RequestException:
            time.sleep(1)
    return None


def _resolve_tvmaze_id(show, cache):
    key = show.lower()
    if key in cache["tvmaze_shows"]:
        return cache["tvmaze_shows"][key]
    ym = _QYEAR.search(show)
    query = show[:ym.start()].strip() if ym else show
    data = _tvmaze_get("/singlesearch/shows", q=query)
    tid = data.get("id") if isinstance(data, dict) else None
    cache["tvmaze_shows"][key] = tid
    return tid


def _tvmaze_meta(show, season, ep, cache):
    tid = _resolve_tvmaze_id(show, cache)
    if not tid:
        return "", ""
    ck = f"{tid}|{season}|{ep}"
    if ck in cache["tvmaze_episodes"]:
        return _rec(cache["tvmaze_episodes"][ck])
    data = _tvmaze_get(f"/shows/{tid}/episodebynumber", season=season, number=ep)
    d = data if isinstance(data, dict) else {}
    ov = html.unescape(_TAGS.sub("", d.get("summary") or "")).strip()
    nm = (d.get("name") or "").strip()
    cache["tvmaze_episodes"][ck] = {"o": ov, "n": nm}
    return ov, nm


# ── TheTVDB (key) ──
TVDB_KEY = os.environ.get("TVDB_API_KEY", "").strip()
TVDB_BASE = "https://api4.thetvdb.com/v4"
ENABLE_TVDB = bool(TVDB_KEY)
_TVDB_TOKEN = None
SHOW_TVDB_OVERRIDES = {
    "Spider-Man: The Animated Series": "spider-man-1994",
    "Spider-Man (1967)": "spider-man-1967",
}


def _tvdb_login():
    global _TVDB_TOKEN
    if not TVDB_KEY:
        return None
    try:
        r = _sess.post(TVDB_BASE + "/login", json={"apikey": TVDB_KEY}, timeout=20)
        _TVDB_TOKEN = r.json().get("data", {}).get("token") if r.status_code == 200 else None
    except requests.RequestException:
        _TVDB_TOKEN = None
    return _TVDB_TOKEN


def _tvdb_get(path, **params):
    global _TVDB_TOKEN
    if not TVDB_KEY:
        return None
    if _TVDB_TOKEN is None and _tvdb_login() is None:
        return None
    for attempt in range(2):
        try:
            r = _sess.get(TVDB_BASE + path, params=params,
                          headers={"Authorization": f"Bearer {_TVDB_TOKEN}"}, timeout=20)
            if r.status_code == 401 and attempt == 0:
                _TVDB_TOKEN = None
                if _tvdb_login() is None:
                    return None
                continue
            return r.json().get("data") if r.status_code == 200 else None
        except requests.RequestException:
            time.sleep(1)
    return None


def _tvdb_search_id(query):
    data = _tvdb_get("/search", query=query, type="series")
    if isinstance(data, list) and data:
        return data[0].get("tvdb_id") or data[0].get("id")
    return None


def _resolve_tvdb_id(show, cache):
    if show in SHOW_TVDB_OVERRIDES:
        slug = SHOW_TVDB_OVERRIDES[show]
        ck = "slug:" + slug
        if ck in cache["tvdb_shows"]:
            return cache["tvdb_shows"][ck]
        d = _tvdb_get(f"/series/slug/{slug}")
        tid = d.get("id") if isinstance(d, dict) else None
        cache["tvdb_shows"][ck] = tid
        return tid
    key = show.lower()
    if key in cache["tvdb_shows"]:
        return cache["tvdb_shows"][key]
    ym = _QYEAR.search(show)
    query = show[:ym.start()].strip() if ym else show
    tid = _tvdb_search_id(query)
    if tid is None and ":" in query:
        tid = _tvdb_search_id(query.split(":")[0].strip())
    cache["tvdb_shows"][key] = tid
    return tid


def _tvdb_meta(show, season, ep, cache):
    if not ENABLE_TVDB:
        return "", ""
    tid = _resolve_tvdb_id(show, cache)
    if not tid:
        return "", ""
    ck = f"{tid}|{season}|{ep}"
    if ck in cache["tvdb_episodes"]:
        return _rec(cache["tvdb_episodes"][ck])
    ov = nm = ""
    for path in (f"/series/{tid}/episodes/default/eng", f"/series/{tid}/episodes/default"):
        data = _tvdb_get(path, season=season, episodeNumber=ep, page=0)
        eps = data.get("episodes") if isinstance(data, dict) else None
        if isinstance(eps, list) and eps:
            m = next((e for e in eps if e.get("seasonNumber") == season and e.get("number") == ep), eps[0])
            ov = (m.get("overview") or "").strip()
            if not nm:
                nm = (m.get("name") or "").strip()
            if ov:
                break
    cache["tvdb_episodes"][ck] = {"o": ov, "n": nm}
    return ov, nm


def _norm(name):
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()

def _segments(name):
    """Normalized halves of a paired episode title like 'A / B' (for segment matching)."""
    if not name or "/" not in name:
        return []
    return [s for s in (_norm(p) for p in name.split("/")) if s]

_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8,
          "ix": 9, "x": 10, "xi": 11, "xii": 12, "xiii": 13, "xiv": 14, "xv": 15}

def clean_chapter(s):
    """House rule: 'Foo, Chapter IV: Bar' -> 'Foo Chapter 4: Bar' (drop the comma
    before Chapter, roman numeral -> arabic)."""
    if not s:
        return s
    s = re.sub(r",\s*(Chapter)\b", r" \1", s, flags=re.I)
    def repl(m):
        return "Chapter " + str(_ROMAN.get(m.group(1).lower(), m.group(1)))
    s = re.sub(r"\bChapter\s+([IVXLCDM]+)\b", repl, s)
    return re.sub(r"\s{2,}", " ", s).strip()


def _tvmaze_namemap(tid, cache):
    key = str(tid)
    if key in cache["tvmaze_namemap"]:
        return cache["tvmaze_namemap"][key]
    data = _tvmaze_get(f"/shows/{tid}/episodes")
    m = {}
    if isinstance(data, list):
        for e in data:
            nm = _norm(e.get("name")); sn = e.get("season"); num = e.get("number")
            if nm and sn and num:
                m.setdefault(nm, [sn, num])
                for seg in _segments(e.get("name")):   # "A / B" pairs -> index each half
                    m.setdefault(seg, [sn, num])
    cache["tvmaze_namemap"][key] = m
    return m


def _tvdb_namemap(tid, cache):
    key = str(tid)
    if key in cache["tvdb_namemap"]:
        return cache["tvdb_namemap"][key]
    m = {}
    for page in range(10):
        data = _tvdb_get(f"/series/{tid}/episodes/default/eng", page=page)
        eps = data.get("episodes") if isinstance(data, dict) else None
        if not eps:
            break
        for e in eps:
            nm = _norm(e.get("name")); sn = e.get("seasonNumber"); num = e.get("number")
            if nm and sn and num:
                m.setdefault(nm, [sn, num])
                for seg in _segments(e.get("name")):
                    m.setdefault(seg, [sn, num])
        if len(eps) < 100:
            break
    cache["tvdb_namemap"][key] = m
    return m


def _name_to_se(show, name, cache):
    target = _norm(name)
    if not target:
        return None, None
    if ENABLE_TVMAZE:
        tid = _resolve_tvmaze_id(show, cache)
        if tid:
            se = _tvmaze_namemap(tid, cache).get(target)
            if se:
                return se[0], se[1]
    if ENABLE_TVDB:
        tid = _resolve_tvdb_id(show, cache)
        if tid:
            se = _tvdb_namemap(tid, cache).get(target)
            if se:
                return se[0], se[1]
    return None, None


def episode_meta(show, season, ep, cache):
    if season is None:
        return None, None
    ov = nm = ""
    def take(res):
        nonlocal ov, nm
        o, n = res
        if o and not ov: ov = o
        if n and not nm: nm = n
        return bool(ov and nm)
    if take(_tmdb_meta(show, season, ep, cache)):                     return ov, nm
    if ENABLE_TVMAZE and take(_tvmaze_meta(show, season, ep, cache)): return ov, nm
    if ENABLE_TVDB and take(_tvdb_meta(show, season, ep, cache)):     return ov, nm
    return (ov or None), (nm or None)


def show_overview(show, cache):
    if show in SHOW_DESC_OVERRIDE:
        return SHOW_DESC_OVERRIDE[show]
    if show in cache["show_syn"] and cache["show_syn"][show]:
        return cache["show_syn"][show]
    ym = _QYEAR.search(show)
    year = ym.group(1) if ym else None
    clean = show[:ym.start()].strip() if ym else show
    ov = ""
    if TMDB_KEY:
        params = {"query": clean}
        if year:
            params["first_air_date_year"] = year
        data = _tmdb_get("/search/tv", **params) or {}
        for res in (data.get("results") or [])[:3]:
            if res.get("overview"):
                ov = res["overview"].strip(); break
    if not ov and ENABLE_TVMAZE:
        d = _tvmaze_get("/singlesearch/shows", q=clean)
        if isinstance(d, dict) and d.get("summary"):
            ov = html.unescape(_TAGS.sub("", d["summary"])).strip()
    if not ov and ENABLE_TVDB:
        tid = _resolve_tvdb_id(clean, cache)
        if tid:
            d = _tvdb_get(f"/series/{tid}") or {}
            ov = (d.get("overview") or "").strip()
    if not ov and TMDB_KEY:                      # movie fallback (films / specials)
        params = {"query": clean}
        if year:
            params["year"] = year
        data = _tmdb_get("/search/movie", **params) or {}
        for res in (data.get("results") or [])[:3]:
            if res.get("overview"):
                ov = res["overview"].strip(); break
    cache["show_syn"][show] = ov
    return ov


# ── absolute-episode resolution (option B): number -> (season, ep, overview, name) ──
def _tvmaze_eplist_se(tid, cache):
    """Flat list of (season, number, overview, name) in air order (0 = ep 1)."""
    key = "se:" + str(tid)
    if key in cache["tvmaze_eplist"]:
        return cache["tvmaze_eplist"][key]
    data = _tvmaze_get(f"/shows/{tid}/episodes")
    lst = []
    if isinstance(data, list):
        for e in data:
            if e.get("season") and e.get("number"):
                lst.append([e["season"], e["number"],
                            html.unescape(_TAGS.sub("", e.get("summary") or "")).strip(),
                            (e.get("name") or "").strip()])
    cache["tvmaze_eplist"][key] = lst
    return lst


def _tmdb_seasons(sid, cache):
    key = str(sid)
    if key in cache["tmdb_seasons"]:
        return cache["tmdb_seasons"][key]
    data = _tmdb_get(f"/tv/{sid}")
    seasons = []
    if data:
        for s in data.get("seasons", []):
            if s.get("season_number", 0) >= 1 and s.get("episode_count"):
                seasons.append([s["season_number"], s["episode_count"]])
    seasons.sort()
    cache["tmdb_seasons"][key] = seasons
    return seasons


def absolute_se(show, absN, cache):
    """Map an ABSOLUTE episode number to (season, ep, overview, name)."""
    if not absN or absN < 1:
        return None, None, "", ""
    # Prefer TMDB's season structure (deterministic), then pull that episode.
    sid = _resolve_show_id(show, cache) if TMDB_KEY else None
    if sid:
        rem = absN
        for snum, cnt in _tmdb_seasons(sid, cache):
            if rem <= cnt:
                ov, nm = _tmdb_meta(show, snum, rem, cache)
                if ov or nm:
                    return snum, rem, ov, nm
                break
            rem -= cnt
    # Fallback: TVmaze flat air-order list already carries S/E + text.
    tid = _resolve_tvmaze_id(show, cache)
    if tid:
        lst = _tvmaze_eplist_se(tid, cache)
        if 1 <= absN <= len(lst):
            s, n, o, nm = lst[absN - 1]
            return s, n, o, nm
    return None, None, "", ""


# ── recover each programme's absolute episodeNumber from the live playlists ──
def _api_get(path):
    for _ in range(3):
        try:
            r = _sess.get(API_ENDPOINT + path, timeout=25)
            if r.status_code == 200:
                return r.json()
            return None
        except requests.RequestException:
            time.sleep(1)
    return None


def _dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def build_epindex(base_sched, dates):
    """{base_sched -> sorted [(epoch_seconds, episodeNumber)]} from the live feed."""
    seen_pl, rows = set(), []
    for d in sorted(dates):
        lst = _api_get(f"/playlists?scheduleName={quote(base_sched)}"
                       f"&startDate={d}T00:00:00.000Z&thisWeek=true&weekStartDay=monday")
        if not isinstance(lst, list):
            continue
        for p in lst:
            pid = p.get("_id")
            if not pid or pid in seen_pl:
                continue
            seen_pl.add(pid)
            content = _api_get(f"/playlist?id={pid}&addInfo=true")
            pl = (content or {}).get("playlist") or {}
            for b in pl.get("blocks", []):
                for m in b.get("mediaList", []):
                    st = m.get("startDate"); enum = m.get("episodeNumber")
                    if st and isinstance(enum, int):
                        rows.append((_dt(st).timestamp(), enum))
    rows.sort()
    return rows


def lookup_epnum(rows, target_ts):
    """Nearest episodeNumber to target_ts within MATCH_TOL_S, else None."""
    if not rows:
        return None
    keys = [r[0] for r in rows]
    i = bisect.bisect_left(keys, target_ts)
    best = None
    for j in (i - 1, i, i + 1):
        if 0 <= j < len(rows):
            d = abs(rows[j][0] - target_ts)
            if d <= MATCH_TOL_S and (best is None or d < best[0]):
                best = (d, rows[j][1])
    return best[1] if best else None


# ══════════════════════════════════════════════════════════════════════════
# Enrichment
# ══════════════════════════════════════════════════════════════════════════
def _set_child(prog, tag, text, attrib=None):
    """Replace (or create) a single child element with given text/attrib."""
    for e in prog.findall(tag):
        prog.remove(e)
    if text is None and not attrib:
        return
    e = ET.SubElement(prog, tag, attrib or {})
    if text is not None:
        e.text = text


def _reorder(prog):
    """Keep XMLTV child order valid: title, sub-title, desc, date, episode-num, icon."""
    order = {"title": 0, "sub-title": 1, "desc": 2, "date": 3, "episode-num": 4, "icon": 5}
    kids = list(prog)
    for k in kids:
        prog.remove(k)
    for k in sorted(kids, key=lambda e: order.get(e.tag, 9)):
        prog.append(k)


def _prog_start_ts(prog):
    s = (prog.get("start") or "").split()[0]
    try:
        return datetime.strptime(s, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def enrich(path):
    tree = ET.parse(path)
    root = tree.getroot()
    cache = load_cache()
    progs = [p for p in root.findall("programme") if p.get("channel") in TARGET_CHANNELS]

    # ── build the absolute-episodeNumber index from the live feed (per base) ──
    # Collect, per base scheduleName, the set of local air-dates we need to cover
    # (programme start minus the channel's stream delay), then pull those playlists.
    want = {}   # base_sched -> set("YYYY-MM-DD")
    for p in progs:
        base, delay = CH_MAP[p.get("channel")]
        ts = _prog_start_ts(p)
        if ts is None:
            continue
        d = datetime.fromtimestamp(ts - delay * 60, tz=timezone.utc).strftime("%Y-%m-%d")
        want.setdefault(base, set()).add(d)
    epindex = {}
    for base, dates in want.items():
        try:
            epindex[base] = build_epindex(base, dates)
            print(f"  episodeNumber index [{base}]: {len(epindex[base])} entries "
                  f"across {len(dates)} day(s)")
        except Exception as e:
            print(f"  warn: could not build episode index for {base}: {e}")
            epindex[base] = []

    stats = {"progs": 0, "by_name": 0, "by_absolute": 0, "show_level": 0,
             "no_desc": 0, "ep_name_added": 0, "icons": 0}
    for prog in progs:
        stats["progs"] += 1
        raw = (prog.findtext("title") or "").strip()
        if not raw:
            continue
        sp = special_title(raw)
        ovr = TITLE_OVERRIDES.get(raw)
        force_no_sub = False
        # resolve lookup show, on-screen title, pinned desc/date, and no-S/E flag
        if ovr:
            show = ovr.get("lookup", raw)
            disp_title = ovr.get("display")
            forced_desc = ovr.get("desc")
            year = ovr.get("date")
            no_se = ovr.get("no_se", False)
        elif sp and sp.get("season"):              # MST3K (keeps S/E)
            show = sp["show"]
            disp_title = "Mystery Science Theater 3000"
            forced_desc = None
            year = None
            no_se = False
        elif sp and sp.get("shorts"):              # RiffTrax Shorts
            show = "RiffTrax"
            disp_title = "RiffTrax Shorts (2007)"
            forced_desc = RIFFTRAX_SHORTS_DESC
            year = "2007"
            no_se = True
            force_no_sub = True
        elif sp:                                   # RiffTrax feature riff
            show = "RiffTrax"
            no_se = True
            rt = RIFFTRAX_MAP.get(_norm(sp.get("sub", "")))
            if rt:
                disp_title, year, forced_desc = rt
                force_no_sub = True                # the title already carries the film + year
            else:
                disp_title = "RiffTrax"
                year = None
                forced_desc = SHOW_DESC_OVERRIDE["RiffTrax"]
        else:
            show = ALIAS.get(raw, raw)
            disp_title = None
            forced_desc = None
            year = None
            no_se = False

        orig_sub = (prog.findtext("sub-title") or "").strip()
        clean_sub = clean_chapter(orig_sub)        # roman->arabic, drop comma before Chapter
        epname = clean_sub or (sp.get("sub") if sp else "")
        base, delay = CH_MAP[prog.get("channel")]
        ts = _prog_start_ts(prog)
        absN = lookup_epnum(epindex.get(base, []), ts - delay * 60) if ts is not None else None

        desc = ""
        season = ep = None
        ov = nm = ""
        source = None
        seg_fullname = ""
        try:
            if not no_se:
                # 0a) explicit episode pin: (lookup show, normalized episode name)
                pin = EPISODE_SE_OVERRIDE.get((show, _norm(epname))) if epname else None
                if pin:
                    season, ep = pin
                    ov, nm = episode_meta(show, season, ep, cache)
                    source = "name"
                # 0b) MST3K titles carry their own S##E##
                if season is None and sp and sp.get("season"):
                    season, ep = sp["season"], sp["ep"]
                    ov, nm = episode_meta(show, season, ep, cache)
                    source = "name"
                # 1) episode NAME match (exact / segment, best for Western cartoons)
                if season is None and epname:
                    cands = [epname]
                    for sep in (" / ", "/", " - "):
                        if sep in epname:
                            cands.append(epname.split(sep)[0].strip())
                    for cand in cands:
                        season, ep = _name_to_se(show, cand, cache)
                        if season:
                            break
                    if season:
                        ov, nm = episode_meta(show, season, ep, cache)
                        source = "name"
                        # if we matched a HALF of a paired DB title, show the full pair
                        if nm and "/" in nm and _norm(epname) in _segments(nm):
                            seg_fullname = nm
                # 2) ABSOLUTE episodeNumber from the feed (dubbed anime + Snick)
                if season is None and absN:
                    s2, e2n, ov2, nm2 = absolute_se(show, absN, cache)
                    if s2:
                        season, ep, ov, nm = s2, e2n, ov2, nm2
                        source = "absolute"
            # 3) description
            if forced_desc:
                desc = forced_desc
            elif season is not None:
                desc = (ov or show_overview(show, cache))
            else:
                desc = show_overview(show, cache)
            # tally how S/E was resolved
            if season is not None:
                stats["by_name" if source == "name" else "by_absolute"] += 1
            else:
                stats["show_level"] += 1
        except Exception as e:
            print(f"  warn: {raw!r}: {e}")
            desc = desc or forced_desc or show_overview(show, cache)

        # sub-title: RiffTrax feature/shorts carry everything in the title -> no sub;
        # else upgrade a half-title to the full pair, apply chapter cleanup, or backfill
        if force_no_sub:
            for e in prog.findall("sub-title"):
                prog.remove(e)
        elif seg_fullname and _norm(seg_fullname) != _norm(orig_sub):
            _set_child(prog, "sub-title", seg_fullname, {"lang": "en"})
        elif orig_sub and clean_sub != orig_sub:
            _set_child(prog, "sub-title", clean_sub, {"lang": "en"})
        elif not orig_sub:
            fill = (sp.get("sub") if sp else "") or (nm if season is not None else "")
            if fill:
                _set_child(prog, "sub-title", fill, {"lang": "en"})
                stats["ep_name_added"] += 1

        if disp_title:
            te = prog.find("title")
            if te is not None:
                te.text = disp_title

        if desc:
            _set_child(prog, "desc", desc, {"lang": "en"})
        else:
            stats["no_desc"] += 1
        if year:                                   # year field for movies / long specials
            _set_child(prog, "date", str(year))
        if season is not None:
            _set_child(prog, "episode-num",
                       f"{season - 1}.{ep - 1}.", {"system": "xmltv_ns"})
            e2 = ET.SubElement(prog, "episode-num", {"system": "onscreen"})
            e2.text = f"S{season:02d} E{ep:02d}"
        # Rewrite the grabber's non-standard <image>URL</image> (which players
        # ignore) into a proper <icon src="URL"/> so posters actually show.
        for im in prog.findall("image"):
            src = (im.get("src") or (im.text or "")).strip()
            prog.remove(im)
            if src and prog.find("icon") is None:
                ET.SubElement(prog, "icon", {"src": src})
                stats["icons"] += 1
        _reorder(prog)

    save_cache(cache)
    tree.write(path, encoding="UTF-8", xml_declaration=True)
    print(f"enriched {stats['progs']} programmes on {len(TARGET_CHANNELS)} channels")
    print(f"  S/E by episode name    : {stats['by_name']}")
    print(f"  S/E by absolute number : {stats['by_absolute']}  "
          f"(episode names backfilled: {stats['ep_name_added']})")
    print(f"  show-level fallback    : {stats['show_level']}")
    print(f"  still no description   : {stats['no_desc']}")
    print(f"  posters <image>-><icon>: {stats['icons']}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: enrich_toonami.py <TOONAMIAM.xml>"); sys.exit(2)
    if not TMDB_KEY and not TVDB_KEY:
        print("NOTE: no TMDB/TVDB keys set - nothing to enrich; leaving file as-is.")
        sys.exit(0)
    enrich(sys.argv[1])
