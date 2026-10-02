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

import os, re, sys, json, time, html
import requests
import xml.etree.ElementTree as ET

# ── channels we enrich (by xmltv channel id) ──
TARGET_CHANNELS = {
    "ToonamiAftermath.us@East", "ToonamiAftermath.us@West",
    "Snickelodeon EST", "Snickelodeon EST+180",
}

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
    "Spiderman": "Spider-Man: The Animated Series",
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
SHOW_TVDB_OVERRIDES = {"Spider-Man: The Animated Series": "spider-man-1994"}


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
    cache["show_syn"][show] = ov
    return ov


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
    """Keep XMLTV child order valid: title, sub-title, desc, episode-num, icon."""
    order = {"title": 0, "sub-title": 1, "desc": 2, "episode-num": 3, "icon": 4}
    kids = list(prog)
    for k in kids:
        prog.remove(k)
    for k in sorted(kids, key=lambda e: order.get(e.tag, 9)):
        prog.append(k)


def enrich(path):
    tree = ET.parse(path)
    root = tree.getroot()
    cache = load_cache()

    stats = {"progs": 0, "ep_matched": 0, "show_level": 0, "no_desc": 0,
             "ep_name_added": 0, "icons": 0}
    for prog in root.findall("programme"):
        if prog.get("channel") not in TARGET_CHANNELS:
            continue
        stats["progs"] += 1
        raw = (prog.findtext("title") or "").strip()
        if not raw:
            continue
        show = ALIAS.get(raw, raw)
        epname = (prog.findtext("sub-title") or "").strip()

        desc = ""
        season = ep = None
        try:
            if epname:
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
                    desc = ov or show_overview(show, cache)
                    if nm and not epname:
                        _set_child(prog, "sub-title", nm, {"lang": "en"})
                        stats["ep_name_added"] += 1
                    stats["ep_matched"] += 1
                else:
                    desc = show_overview(show, cache)
                    stats["show_level"] += 1
            else:
                desc = show_overview(show, cache)
                stats["show_level"] += 1
        except Exception as e:
            print(f"  warn: {raw!r}: {e}")

        if desc:
            _set_child(prog, "desc", desc, {"lang": "en"})
        else:
            stats["no_desc"] += 1
        if season:
            _set_child(prog, "episode-num",
                       f"{season - 1}.{ep - 1}.", {"system": "xmltv_ns"})
            # a second, human-readable episode-num for players that show it
            e2 = ET.SubElement(prog, "episode-num", {"system": "onscreen"})
            e2.text = f"S{season:02d}E{ep:02d}"
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
    print(f"  episode-level match: {stats['ep_matched']}  "
          f"(episode names backfilled: {stats['ep_name_added']})")
    print(f"  show-level fallback: {stats['show_level']}")
    print(f"  still no description: {stats['no_desc']}")
    print(f"  posters rewritten <image> -> <icon>: {stats['icons']}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: enrich_toonami.py <TOONAMIAM.xml>"); sys.exit(2)
    if not TMDB_KEY and not TVDB_KEY:
        print("NOTE: no TMDB/TVDB keys set - nothing to enrich; leaving file as-is.")
        sys.exit(0)
    enrich(sys.argv[1])
