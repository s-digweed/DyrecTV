#!/usr/bin/env python3
"""
Whiplash + Eel EPG + M3U generator.

- Whiplash: single consolidated ErsatzTV feed (schedule.xml) with all channels
  incl. zén!th. Channels are remapped to stable local tvg-ids and copied as-is
  (proper XMLTV, no title surgery needed).
- Eel: eel.xml is title-only with wildly inconsistent episode formatting. Titles
  are parsed into <title> / <sub-title> / <episode-num> (onscreen + xmltv_ns) and
  run through a show-name rename table so IPTVBoss can format them per settings.

Outputs epg.xml (merged) and playlist.m3u. No deps beyond `requests`.
"""

import re
import os
import sys
import json
import time
import html
import unicodedata
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ───────────────────────── Whiplash (single feed) ─────────────────────────
WL_URL = "https://whiplash.cc/scheds/schedule.xml"

# source ErsatzTV id -> (local tvg-id, display-name)
WL_CHANNEL_MAP = {
    "C1.1.146.ersatztv.org": ("whiplash",        "WHIPLASH"),
    "C2.1.147.ersatztv.org": ("whiplash2",       "WHIPLASH 2"),
    "C3.1.148.ersatztv.org": ("whiplashcinema",  "WHIPLASH CINEMA"),
    "C7.151.ersatztv.org":   ("whiplashatlas",   "WHIPLASH ATLAS"),
    "C3.147.ersatztv.org":   ("whiplashwindowtv","WHIPLASH WINDOW TV"),
    "C6.1.151.ersatztv.org": ("whiplashbiwi",    "BIWI"),
    "zenith":                ("whiplashzenith",  "ZÉN!TH"),
}
WL_LOGOS = {
    "whiplash":        "https://whiplash.cc/assets/img/channels/whiplash.png",
    "whiplash2":       "https://whiplash.cc/assets/img/channels/whiplash2.png",
    "whiplashcinema":  "https://whiplash.cc/assets/img/channels/whiplashcinema.png",
    "whiplashatlas":   "https://whiplash.cc/assets/img/channels/atlas.png",
    "whiplashwindowtv":"https://whiplash.cc/assets/img/channels/windowtv.png",
    "whiplashbiwi":    "https://whiplash.cc/assets/img/channels/biwi.png",
    "whiplashzenith":  "https://i.imgur.com/j63CKTM.png",
}
WL_STREAMS = {
    "whiplash":        "https://cdn.whiplash.cc/whiplash/index.m3u8",
    "whiplash2":       "https://cdn.whiplash.cc/whiplash-2/index.m3u8",
    "whiplashcinema":  "https://cdn.whiplash.cc/whiplash-cinema/index.m3u8",
    "whiplashatlas":   "https://cdn.whiplash.cc/whiplash-atlas/index.m3u8",
    "whiplashwindowtv":"https://cdn.whiplash.cc/whiplash-windowtv/index.m3u8",
    "whiplashbiwi":    "https://cdn.whiplash.cc/whiplash-biwi/index.m3u8",
    "whiplashzenith":  "https://cdn.whiplash.cc/whiplash-zenith/index.m3u8",
}
WL_M3U_ORDER = ["whiplash","whiplash2","whiplashatlas","whiplashcinema",
                "whiplashwindowtv","whiplashbiwi","whiplashzenith"]

# ───────────────────────────── Eel (normalized) ───────────────────────────
EEL_URL = "https://whiplash.cc/epg/eel.xml"
EEL_CHANNEL_MAP = {
    "C1.145.ersatztv.org": ("eel-channel",  "Eel Channel"),
    "C2.146.ersatztv.org": ("fizz",         "Fizz"),
    "C121.244.ersatztv.org": ("toon-diggity", "Toon Diggity"),  # feed moved it off C3.147 (now an orphan/stale id)
}
EEL_LOGOS = {
    "eel-channel":  "https://i.imgur.com/YBZUFXs.png",
    "fizz":         "https://i.imgur.com/0z5rG3A.png",
    "toon-diggity": "https://i.imgur.com/GhzJMF9.png",
}
# The eel feed occasionally RENUMBERS a source channel id (e.g. Toon Diggity moved
# C3.147 -> C121.244, leaving C3.147 as a stale orphan). build_eel self-heals: if a
# configured id is gone from the feed, it re-finds the channel by its display-name
# (keyed on the stable tvg-id) and logs a NOTE. Extra display-name spellings the feed
# might use go here; the display-names in EEL_CHANNEL_MAP are matched automatically.
EEL_NAME_ALIASES = {
    "toon disney shuffle": "toon-diggity",
    "toon disney":         "toon-diggity",
    "toon disney shorts":  "toon-diggity",
    "cartoon shuffle":     "toon-diggity",   # Nov 2026 rebrand; our guide keeps "Toon Diggity"
}
def _norm_chan_name(s):
    s = re.sub(r"^\s*\d+\s*", "", (s or "").strip())      # drop the feed's leading channel number
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

# ── MistLive: copy every channel as-is, enrich only these two ──
MIST_URL = "https://api.mistlive.tv/api/xmltv.xml"
MIST_OUTPUT = "MistLive.xml"
MIST_ENRICH = {"vortex.iptv.mistlive.tv", "nfktv.iptv.mistlive.tv"}
# Show-name fixes for the enriched channels (display + metadata lookup).
MIST_RENAMES = {
    "The 70s Show":            "That '70s Show",
    "Malcom In The Middle":    "Malcolm in the Middle",
    "Genarator Rex":           "Generator Rex",
    "Courage The Cowardly Dog":"Courage the Cowardly Dog",
}

# Shows numbered ABSOLUTELY (3+ digit code = absolute ep in S01, hundreds kept)
ABSOLUTE_SHOWS = {"dragon ball"}

# Show-name renames, keyed on the parsed base title (trailing "(YYYY)" kept)
RENAMES = {
    "Garfield": "Garfield and Friends",
    "DuckTales": "DuckTales (1987)",   # disambiguate from the 2017 reboot (feed = the original)
    "Batman The Animated Series": "Batman: The Animated Series",
    "Superman The Animated Series": "Superman: The Animated Series",
    "X-Men": "X-Men: The Animated Series",
    "Spider-Man": "Spider-Man: The Animated Series",
    "Eerie, Indiana - The Other Dimension": "Eerie, Indiana: The Other Dimension",
    "Sabrina TTW": "Sabrina the Teenage Witch",
    "Adventures Of Sonic The Hedgehog": "Adventures of Sonic the Hedgehog",
    "SOUTH PARK": "South Park",
    "DOUG": "Disney's Doug",
    "Sabrina The Animated Series": "Sabrina: The Animated Series",
    "Andy Milonakis Show": "The Andy Milonakis Show",
    "Secret World Of Alex Mack": "The Secret World of Alex Mack",
    "Bill Nye The Science Guy": "Bill Nye the Science Guy",
    "Homeless In Denton": "Homeless in Denton",
    "Shin-Chan": "Crayon Shin-chan",
    "Chip n' Dale Rescue Rangers": "Chip 'n Dale Rescue Rangers",
    "Oban Star-Racers": "Ōban Star-Racers",
    "Snoppy's Getting Married, Charlie Brown": "Snoopy's Getting Married, Charlie Brown",  # feed typo
    # parked (not in current feed, kept in case the source drifts):
    "It's Always Sunny at": "It's Always Sunny in Philadelphia",
    "Batman Animated": "Batman: The Animated Series",
    "Superman Animated": "Superman: The Animated Series",
    "The Grim Adv. of Billy & Mandy": "The Grim Adventures of Billy & Mandy",
    "Eerie, Indiana: Other": "Eerie, Indiana: The Other Dimension",
    "Mighty Mouse '87": "Mighty Mouse: The New Adventures (1987)",
    "SRMTHFG!": "Super Robot Monkey Team Hyperforce Go!",
    # Toon Diggity (Cartoon Shuffle) feed spellings, Nov 2026
    "Mighty Mouse TNA": "Mighty Mouse: The New Adventures (1987)",
    "Oblongs": "The Oblongs",
    "FUNKY COPS": "Funky Cops",
    "Charlie Brown and Snoopy Show": "The Charlie Brown and Snoopy Show",
    "Ping Pong Club": "The Ping Pong Club",
    "Super Milk Chan USA": "The Super Milk-Chan Show",   # TMDB/Netflix name
}

# Fuzzy renames: (pattern, canonical) applied when the exact table misses.
# Catches truncated / variant titles, e.g. a bare "It's Always Sunny".
REGEX_RENAMES = [
    (re.compile(r"^Bad Religion:?\s+Live at the Palladium$", re.I),
     "Bad Religion: Live at the Palladium (2006)"),
    (re.compile(r"^It[\u2019']?s\s+Always\s+Sunny\b.*$", re.I),
     "It's Always Sunny in Philadelphia"),
]

# Fixed descriptions for block/filler programmes with no episode data (keyed by title).
_FIZZ_MIX_DESC = ("Classic cartoons like Looney Tunes, Tom and Jerry, Woody Woodpecker, "
                  "Droopy, NFB Canada and Zagreb Film animated shorts plus some "
                  "Pop and Rock music videos.")
_LATE_NIGHT_MIX_DESC = ("Your hand-picked midnight refill of Looney Tunes, Tom and Jerry, "
                        "Woody Woodpecker, Droopy, and animated shorts from NFB Canada and "
                        "Zagreb Film, with pop and rock music videos fizzing away while the "
                        "rest of the world sleeps.")
_TOON_DISNEY_DESC = "Timeless theatrical animated shorts from Disney's golden age."
FIXED_DESC = {
    # our own write-ups (same text on CN Classic + Jpuffle5 in DyrecTV boom_enrich.py)
    "Tom and Jerry": "Tom the cat and Jerry the mouse are frenemies whose small squabbles spiral into "
                     "traps, crashes and all-out cartoon mayhem. Sometimes they team up against a common "
                     "enemy; other times one of them sides with the enemy against the other.",
    "Dexter's Laboratory": "Dexter, a boy-genius with a secret laboratory, constantly battles his sister "
                           "Dee Dee, who always gains access despite his best efforts to keep her out, as well "
                           "as his arch-rival and neighbor, Mandark.",
    "The Jetsons": "Meet George Jetson and his quirky family: wife Jane, son Elroy and daughter Judy. "
                   "Living in the automated, push-button world of the future hasn't made life any easier "
                   "for the harried husband and father, who gets into one comical misadventure after another!",
    "Classic Wrestling":  "Old-school grappling, larger-than-life legends and matches from wrestling's "
                          "golden era when every week felt like a pay-per-view.",   # Eel Channel block
    "Late Night Mix":     _LATE_NIGHT_MIX_DESC,
    "Evening Mix":        _FIZZ_MIX_DESC,
    "Toon Disney Shorts": _TOON_DISNEY_DESC,   # parked: old feed title for the block
    "Disney Cartoons":    _TOON_DISNEY_DESC,   # feed renamed "Toon Disney Cartoons/Shorts" -> "Disney Cartoons"
    "You're on Nickelodeon, Charlie Brown":
        "The animated adventures of Charlie Brown, Snoopy and the rest of the Peanuts gang.",
    "Disney Channel Original Movie":
        "A made-for-TV Disney Channel Original Movie, spanning teen comedies, musicals, fantasy and adventure.",
    "Spider-Man: The Animated Series":
        "When college student, Peter Parker, is bitten by a genetically altered spider, he "
        "gains Spider-like powers and must use his gifts to fight crime in New York City as "
        "the superhero, Spider-Man, whilst trying to have a normal personal life.",
    "AfterToon Movie":
        "An afternoon feature presentation, a family or animated film capping off the cartoon lineup.",
    "Saturday Night Movie":
        "A Saturday night feature presentation, a family film to round out the night.",
    "Sunday Night Movie":
        "A Sunday night feature presentation, a family film to close out the weekend.",
    "Cartoon Theater":
        "Feature-length animated movies, theatrical releases, and television specials "
        "distributed by Hanna-Barbera Studios, Warner Bros., Disney, 20th Century Fox, "
        "Universal, Columbia, Paramount, DreamWorks, and MGM.",
    "Adult Halloween Special":
        "The spooky stuff they aired after the kids went to bed."
        "\n\n"
        "Lights off, zero supervision.",
    "Retro Blast Halloween":
        "Enjoy classic Halloween specials and movies including Scooby-Doo, Courage the "
        "Cowardly Dog, The Simpsons, The Grim Adventures of Billy & Mandy, Hocus Pocus (1993), "
        "Halloweentown (1998), Nightmare Before Christmas (1993), and the original 1978 Halloween.",
    "Fridays Halloween":
        "Get ready for a spooky Friday night of classic chills and weird thrills. Retro Blast "
        "brings back the eeriest animation from the vault.",
    "Horror Movie":
        "Close the weekend with a scream. A classic horror movie, served late and uncut. "
        "\n\n"
        "Tune in if you dare.",
    "Saturday Late Night Movie":
        "Unwind your weekend the right way with the Saturday Late Night Movie, a nostalgic "
        "television sanctuary dedicated to the night owls, animation enthusiasts, and "
        "insomniacs. Broadcasting directly from the deep archives of the Cartoon Network and "
        "Adult Swim Universes, this retro block strips away the standard daytime schedule to "
        "showcase full-length animated features that are a little darker, a little weirder, "
        "and entirely captivating.",
    # ── Fizz blocks ──
    "Fizz Cartoons":
        "Vintage fizz from the golden age of animation: Tom and Jerry, Looney Tunes, MGM shorts, Walter Lantz "
        "Cartunes and Fleischer classics, mixed with international gems from the NFB/ONF, "
        "Zagreb Film and beyond.",
    "Fizz Vidz":
        "Three decades of rock and pop on tap, with music videos from the 80s to the 00s, "
        "fizzing you through the small hours.",
    # ── Toon Diggity (Cartoon Shuffle) shorts blocks ──
    "Breakfast Shorts":                       # feed: "Morning Shuffle"
        "Pour the cereal, the projector's already running."
        "\n\n"
        "Disney Cartoons, Tom and Jerry and MGM shorts, with Terrytoons and George Pal's "
        "Puppetoons rolling out of the vault."
        "\n\n"
        "Spoon in one hand, remote in the other.",
    "Afternoon Shuffle":
        "An after-school reel of theatrical shorts: Tom and Jerry, Looney Tunes, Tex Avery, Walter Lantz "
        "Cartunes and Fleischer classics, cut with international gems from NFB/ONF Canada and Zagreb Film."
        "\n\n"
        "Homework can wait.",
    # ── one-off specials ──
    "Bad Religion: Live at the Palladium (2006)":
        "Live at the Palladium is a live concert DVD by the punk band Bad Religion, released in "
        "March 2006. It features footage from two nights at the Palladium in Hollywood, "
        "California, on November 21 and 22, 2004.",
    # ── shows the metadata lookups can't describe ──
    "Nickelodeon GUTS":                       # IMDb show blurb
        "Three kids donning the different colors blue, red, and purple compete in relatively "
        "cool-looking olympic-style games to achieve as many points as they can.",
    "Homeless in Denton":
        "A local documentary-style comedy series that follows two \"homeless\" guys wandering "
        "around Denton, Texas.",
    "Dude, What Would Happen":                # fallback for episodes not in fizz_guide.json
        "Three teens, C.J., Jackson and Ali, wonder what would happen if some wild event, scheme "
        "or experiment were to occur, then set out to create it themselves, consulting experts "
        "when needed.",
    # ── Eel Channel blocks (blank lines between paragraphs render as line breaks) ──
    "The Late Shift":
        "The office's shuttered, the takeout's gone cold, and the tubes are still warm. "
        "The Late Shift is the Eel Channel after dark."
        "\n\n"
        "Insomniac cartoons, vintage grapplers, music videos warped off a third-gen tape, "
        "and static-soaked oddities out of the archive at 4 AM, all buzzing at a cozy 502p."
        "\n\n"
        "Don't adjust your set; that hum is on purpose.",
    "The Break Room":
        "Your midday jolt, no talk, no ads."
        "\n\n"
        "Back-to-back music videos: fresh, forgotten, and gloriously scuffed — enough to "
        "carry you clean through to clock-out."
        "\n\n"
        "Pull up a folding chair.",
    "The Desk Jockey":                      # feed: "Morning Videos" (~11 AM)
        "Badge on, inbox filling, the copier warming up somewhere down the hall."
        "\n\n"
        "A wake-up reel of music videos: chart hits, cult cuts and VHS-worn favorites you "
        "swore you'd forgotten."
        "\n\n"
        "Look busy.",
    "Nightwave":                              # retired Nov 2026 (no late-night video block); kept for reuse
        "Half past eleven and the signal goes molten."
        "\n\n"
        "A neon loop of music videos: new drops, deep cuts, and signal-bent classics bleeding "
        "one into the next, strobing through the dark like a jukebox nobody can unplug."
        "\n\n"
        "Lights low, voltage high.",
}

# Eel Channel segment renames that the title-only RENAMES table can't express.
#  - "Music Videos" airs twice under one feed title: a ~1 PM afternoon block and a
#    ~11:25-11:30 PM late-night block (2x/week). Split them by local start hour.
# ("The Late Shift" keeps its feed title; its description is handled via FIXED_DESC.)
# Feed start attr looks like "20260927131000 -0400"; digits [8:10] are the local hour.
def _eel_segment_rename(title, start, channel=None):
    # Fizz's nightly classic-shorts block is titled just "Cartoons" in the feed.
    if channel == "fizz" and title == "Cartoons":
        return "Fizz Cartoons"
    # Nov 2026 grid: Eel's video blocks got their own feed titles.
    if title == "Morning Videos":
        return "The Desk Jockey"
    if title == "Afternoon Videos":
        return "The Break Room"
    if channel == "toon-diggity" and title == "Morning Shuffle":
        return "Breakfast Shorts"
    if title == "Music Videos":
        try:
            hr = int((start or "")[8:10])
        except ValueError:
            hr = 13                      # unparsable -> treat as the afternoon block
        return "The Break Room" if 10 <= hr < 18 else "Nightwave"
    return title

# Renamed titles that should NOT carry the source's trailing (YYYY)
DROP_YEAR = {"Batman: The Animated Series"}

# Per-episode sub-titles the feed doesn't carry: (show, season, episode) -> sub-title
EPISODE_SUBS = {
    ("Homeless in Denton", 2, 5): "Fry St. Fair '06",
}

# Eel shows the feed sends with no episode number: fetch a SHOW-level synopsis
# (TMDB -> TVmaze -> TVDB, then OMDb). Value = the name to try on OMDb/IMDb if it differs.
# The feed sends films as "<Franchise> Movie" + sub-title "<Film> (YYYY)" + a fake S/E.
# Shown film-style instead: "<Franchise>: <Film> (YYYY)", no S/E, TMDB movie lookup.
# FILM_CANON fixes known films to TMDB's exact title (keyword in the sub-title -> canonical).
_FILM_FRANCHISE = re.compile(r"^(.*\S)\s+Movie$", re.I)
FILM_CANON = {
    "tree of might": "Dragon Ball Z: The Tree of Might (1990)",
    "bardock":       "Dragon Ball Z: Bardock \u2013 The Father of Goku (1990)",
}

# Episode names the feed spells differently from TVmaze/TVDB: (show, feed name) -> lookup name
EP_NAME_ALIAS = {
    ("Beavis and Butt-Head", "PTA"): "P.T.A.",
    ("Beavis and Butt-Head", "The Final Judgment of Beavis"): "The Final Judgement of Beavis",
}

EEL_SHOW_LOOKUP = {
    "The Super Milk-Chan Show": "The Super Milk Chan Show",      # IMDb spelling
    "Mighty Mouse: The New Adventures (1987)": None,
}

# ── Part-marker standardization (guardrail across all 3 Eel channels) ──────
# Normalize two-parter markers to "Title: Part N": "(1)/(2)", "(Part One)",
# "City of Stone: Part Four", bare " Part Two" -> ": Part 1/2/4...". Conservative:
# acts only on an explicit "Part" keyword or a trailing 1-2 digit "(N)"; never touches
# "Mock 5", "D2", or 4-digit years like "(1995)". Applied to title AND sub-title.
_WORDNUM = {"one":1,"two":2,"three":3,"four":4,"five":5,"six":6,"seven":7,"eight":8,"nine":9,"ten":10}
def _wordnum(w):
    w = w.strip().lower()
    return _WORDNUM.get(w, int(w) if w.isdigit() else None)
def norm_parts(title):
    if not title:
        return title
    t = title.rstrip()
    def mk(base, n):
        return base.rstrip().rstrip(":").rstrip() + f": Part {n}"
    m = re.search(r'\s*\(\s*Part\s+([A-Za-z]+|\d{1,2})\s*\)\s*$', t, re.I)   # "(Part One)"/"(Part 1)"
    if m and _wordnum(m.group(1)): return mk(t[:m.start()], _wordnum(m.group(1)))
    m = re.search(r'\s*\(\s*(\d{1,2})\s*\)\s*$', t)                          # "(1)"/"(2)"
    if m: return mk(t[:m.start()], int(m.group(1)))
    m = re.search(r'\s*:?\s*Part\s+([A-Za-z]+|\d{1,2})\s*$', t, re.I)        # ": Part X"/" Part X"
    if m and _wordnum(m.group(1)): return mk(t[:m.start()], _wordnum(m.group(1)))
    return t

# Normalize a title/segment name to a match key (mirrors build_guide.py _norm) so the
# feed's TMDB-sourced names resolve to the authoritative guide regardless of case,
# punctuation, accents, "&"/"and", or part-marker spelling ("(1)" == ": Part One").
def _guide_norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", "and")
    s = re.sub(r'\bpart\s+(one|two|three|four|five|six|seven|eight|nine|ten)\b',
               lambda m: "part" + str(_WORDNUM[m.group(1)]), s)
    s = re.sub(r'[:\-]?\s*part\s*(\d{1,2})\b', lambda m: " part" + m.group(1), s)
    s = re.sub(r'\(\s*(\d{1,2})\s*\)\s*$', lambda m: " part" + m.group(1), s.strip())
    return re.sub(r'[^a-z0-9]', '', s)

# ── Per-show air-order override guide ──────────────────────────────────────
# Some Eel/Fizz shows are numbered by the feed in its own (broadcast) order, which
# the metadata DBs don't share (TMDB/IMDb use production/DVD order), so a DB lookup
# by the feed number lands on the wrong episode. fizz_guide.json maps the feed
# straight to the real episode, built OFFLINE from authoritative guides:
#   { "<Show>": {"mode": "absolute"|"se"|"name", "eps": <list|dict>} }
#   - mode "absolute": eps is a list indexed by the feed's absolute episode number
#     (feed_ep-1). Batman: The Animated Series — Wikipedia broadcast order + Wiki
#     S/E, synopsis from IMDb (matched by title), titles normalized "X: Part N".
#   - mode "se": eps is a dict keyed "<season>.<episode>". Garfield and Friends —
#     feed S/E already matches IMDb; segments joined " / ", per-segment IMDb
#     synopsis " / "-joined where IMDb labels them, else the full episode synopsis.
#   - mode "name": eps is a dict keyed by _guide_norm(<episode or segment name>). Used
#     when the feed numbers the show on TMDB's grid but TMDB/IMDb disagree on the
#     NUMBERING (so a by-number key would misalign). We resolve the item's name (the
#     feed's sub-title, else the TMDB episode name for the feed's S/E) and match THAT.
#     The displayed S/E is left as the feed's. Gargoyles (non-segmented: name -> that
#     episode's IMDb title+synopsis, Part-normalized). Dexter's Laboratory / Rocko's
#     Modern Life (SEGMENTED: the feed labels each half-hour slot with one TMDB segment;
#     every segment name maps to its IMDb HALF-HOUR -> all segment titles joined " / "
#     plus the half-hour's IMDb synopsis, Garfield-style). Non-matches fall through to
#     the normal TMDB/TVmaze/TVDB path, so the guide can only improve, never corrupt.
FIZZ_GUIDE_FILE = "fizz_guide.json"
def load_fizz_guide():
    try:
        with open(FIZZ_GUIDE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  fizz guide: {FIZZ_GUIDE_FILE} not loaded ({e})")
        return {}
FIZZ_GUIDE = load_fizz_guide()

# ── title parser ──
YEAR_ANY = re.compile(r'\((?:19|20)\d\d\)')
YEAR_END = re.compile(r'\s*(\((?:19|20)\d\d\))\s*$')
STRONG = re.compile(
    r'(?<![A-Za-z0-9])'
    r'(?:S(\d{1,2})\s*E[Pp]?\s*(\d{1,2})(?:\s*-\s*E?(\d{1,2}))?'
    r'|(\d{1,2})x(\d{1,2})(?:-(\d{1,2}))?)'
    r'(?![A-Za-z0-9])', re.I)
EP_MARK  = re.compile(r'(?<![A-Za-z0-9])E[Pp][Ss]?\s*(\d{2,4})(?![A-Za-z0-9])')
P_NdashN = re.compile(r'(?:^|\s)(\d{1,2})-(\d{2})$')
P_EPISODE= re.compile(r'(?:^|\s)Episode\s+(\d+)$', re.I)
P_DIGITS = re.compile(r'(?:^|\s)T?(\d{2,4})$')
P_TFRONT = re.compile(r'^T(\d{2,4})\s+(\S.*)$')

def _digits_to_se(d, show):
    if show.strip().lower() in ABSOLUTE_SHOWS: return 1, int(d)
    if len(d) == 2: return 1, int(d)
    if len(d) == 3: return (1, int(d[1:])) if d[0] == '0' else (int(d[0]), int(d[1:]))
    if len(d) == 4: return int(d[:2]), int(d[2:])
    return 1, int(d)

def _clean(s):
    s = re.sub(r'^[\s_\-]+|[\s_\-]+$', '', s.strip())
    return re.sub(r'\s{2,}', ' ', s).strip()

def _rename(title):
    m = YEAR_END.search(title)
    yr = " " + m.group(1) if m else ""
    base = title[:m.start()].strip() if m else title
    if base in RENAMES:
        base = RENAMES[base]
    else:
        for rx, canon in REGEX_RENAMES:
            if rx.match(base):
                base = canon
                yr = ""          # canonical already carries any year it needs
                break
    if base in DROP_YEAR:
        yr = ""                  # strip the source year for these titles
    return (base + yr).strip()

def parse_title(raw):
    """Return (title, subtitle_or_None, season_or_None, ep_or_None, ep_end_or_None)."""
    t = html.unescape(raw).strip().replace('_', ' - ')
    t = re.sub(r'\s{2,}', ' ', t)
    title, subtitle, season, ep, ep_end = t, None, None, None, None

    m = STRONG.search(t)
    if m:
        if m.group(1):
            season, ep = int(m.group(1)), int(m.group(2))
            ep_end = int(m.group(3)) if m.group(3) else None
        else:
            season, ep = int(m.group(4)), int(m.group(5))
            ep_end = int(m.group(6)) if m.group(6) else None
        title = _clean(t[:m.start()])
        subtitle = _clean(YEAR_ANY.sub('', t[m.end():])) or None
        return _rename(title), subtitle, season, ep, ep_end

    m = EP_MARK.search(t)
    if m:
        show = _clean(t[:m.start()])
        season, ep = _digits_to_se(m.group(1), show)
        subtitle = _clean(YEAR_ANY.sub('', t[m.end():])) or None
        return _rename(show), subtitle, season, ep, None

    segs = re.split(r'\s+-\s+', t)
    def peel(front):
        for rx, kind in ((P_NdashN,'nd'),(P_EPISODE,'ep'),(P_DIGITS,'dig')):
            mm = rx.search(front)
            if not mm: continue
            pref = front[:mm.start()].strip()
            if kind == 'nd': return pref, int(mm.group(1)), int(mm.group(2))
            if kind == 'ep': return pref, 1, int(mm.group(1))
            se, e = _digits_to_se(mm.group(1), pref); return pref, se, e
        return None
    if len(segs) >= 2:
        c = peel(segs[-1].strip())
        if c and c[0] == '':
            _, season, ep = c; title = _clean(" - ".join(segs[:-1]))
        else:
            subtitle = segs[-1].strip()
            c = peel(" - ".join(segs[:-1]))
            if c: title, season, ep = _clean(c[0]), c[1], c[2]
            else:
                # the episode name itself may contain " - " ("SOUTH PARK 316 - Are you there
                # God - It's me, Jesus"): find the earliest "<Show> <3-4 digits>" prefix.
                for i in range(1, len(segs) - 1):
                    mm = re.search(r'^(.*\S)\s+(\d{3,4})$', " - ".join(segs[:i]).strip())
                    if mm:
                        season, ep = _digits_to_se(mm.group(2), mm.group(1))
                        title = _clean(mm.group(1)); subtitle = " - ".join(segs[i:]).strip()
                        break
                else:
                    title = _clean(" - ".join(segs[:-1]))
    else:
        c = peel(t)
        if c: title, season, ep = _clean(c[0]), c[1], c[2]
        else:
            fm = P_TFRONT.match(t)
            if fm:
                season, ep = _digits_to_se(fm.group(1), fm.group(2))
                title = _clean(fm.group(2))
    return _rename(title), subtitle, season, ep, ep_end


# ───────────────────────── TMDB episode descriptions ──────────────────────
# Optional: set the TMDB_API_KEY env (GitHub Secret). If absent, descriptions
# are simply skipped and the EPG is generated exactly as before.
TMDB_KEY        = os.environ.get("TMDB_API_KEY", "").strip()
TMDB_BASE       = "https://api.themoviedb.org/3"
TMDB_LANG       = "en-US"
DESC_CACHE_FILE = "desc_cache.json"

# Force a TMDB show id when search picks the wrong one: {"Show Name": 1234}
SHOW_TMDB_OVERRIDES = {"DuckTales (1987)": 720}   # TMDB id 720 = the 1987 series, not the 2017 reboot

_QYEAR = re.compile(r'\s*\(((?:19|20)\d\d)\)\s*$')
_tmdb_session = requests.Session()

def load_desc_cache():
    try:
        with open(DESC_CACHE_FILE, encoding="utf-8") as f:
            c = json.load(f)
            for k in ("shows","episodes","tvmaze_shows","tvmaze_episodes","tmdb_seasons",
                      "tvmaze_eplist","tvdb_shows","tvdb_episodes",
                      "tvmaze_namemap","tvdb_namemap","tmdb_movies","show_syn"):
                c.setdefault(k, {})
            if not c.get("_tvdb_v2"):                    # refresh TVDB layer once (/eng + slug overrides)
                c["tvdb_shows"] = {}; c["tvdb_episodes"] = {}; c["_tvdb_v2"] = True
            if not c.get("_meta_v3"):                    # refresh episode caches once to capture episode NAMES
                for _k in ("episodes","tvmaze_episodes","tvdb_episodes","tvmaze_eplist"): c[_k] = {}
                c["_meta_v3"] = True
            return c
    except Exception:
        return {"shows": {}, "episodes": {}, "tvmaze_shows": {}, "tvmaze_episodes": {},
                "tmdb_seasons": {}, "tvmaze_eplist": {}, "tvdb_shows": {}, "tvdb_episodes": {},
                "tvmaze_namemap": {}, "tvdb_namemap": {}, "tmdb_movies": {}, "show_syn": {}}

def save_desc_cache(cache):
    with open(DESC_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=0, sort_keys=True)

def _tmdb_get(path, **params):
    params["api_key"] = TMDB_KEY
    for _ in range(3):
        try:
            r = _tmdb_session.get(TMDB_BASE + path, params=params, timeout=20)
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
        return cache["shows"][key]                       # may be None = "searched, not found"
    ym = _QYEAR.search(show)
    query = show[:ym.start()].strip() if ym else show
    params = {"query": query}
    if ym: params["first_air_date_year"] = ym.group(1)
    data = _tmdb_get("/search/tv", **params)
    sid = data["results"][0]["id"] if (data and data.get("results")) else None
    cache["shows"][key] = sid
    return sid

def _rec(v):
    """Normalize a cached episode value -> (overview, name). Legacy str = overview only."""
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

# ── TVmaze fallback (keyless) ──
ENABLE_TVMAZE = True
TVMAZE_BASE   = "https://api.tvmaze.com"
_TAGS = re.compile(r"<[^>]+>")

def _tvmaze_get(path, **params):
    for _ in range(3):
        try:
            r = _tmdb_session.get(TVMAZE_BASE + path, params=params, timeout=20)
            time.sleep(0.2)                               # ~5 req/s: well under TVmaze's limit
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "5")) + 1); continue
            if r.status_code == 200:
                return r.json()
            return None                                  # 404 = no such episode
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

def _tvmaze_eplist(tid, cache):
    """Flat list of regular-episode summaries in air order (index 0 = ep 1). Cached."""
    key = str(tid)
    if key in cache["tvmaze_eplist"]:
        return cache["tvmaze_eplist"][key]
    data = _tvmaze_get(f"/shows/{tid}/episodes")
    lst = []
    if isinstance(data, list):
        for e in data:
            if e.get("season") and e.get("number"):          # skip specials
                lst.append({"o": html.unescape(_TAGS.sub("", e.get("summary") or "")).strip(),
                            "n": (e.get("name") or "").strip()})
    cache["tvmaze_eplist"][key] = lst
    return lst

def _tmdb_seasons(sid, cache):
    """[[season_number, episode_count], ...] for regular seasons. Cached."""
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

def _absolute_meta(show, absN, cache):
    """Treat absN as an ABSOLUTE episode number and resolve via the show's episode list."""
    if not absN or absN < 1:
        return "", ""
    tid = cache["tvmaze_shows"].get(show.lower())               # set during the TVmaze direct try
    if tid:
        lst = _tvmaze_eplist(tid, cache)
        if 1 <= absN <= len(lst):
            o, n = _rec(lst[absN - 1])
            if o or n:
                return o, n
    sid = _resolve_show_id(show, cache) if TMDB_KEY else None
    if sid:
        rem = absN
        for snum, cnt in _tmdb_seasons(sid, cache):
            if rem <= cnt:
                ck = f"{sid}|{snum}|{rem}"
                if ck not in cache["episodes"]:
                    data = _tmdb_get(f"/tv/{sid}/season/{snum}/episode/{rem}", language=TMDB_LANG)
                    cache["episodes"][ck] = {"o": ((data or {}).get("overview") or "").strip(),
                                             "n": ((data or {}).get("name") or "").strip()}
                return _rec(cache["episodes"][ck])
            rem -= cnt
    return "", ""

# ── TheTVDB fallback (third source; strong on old-cartoon episode overviews) ──
TVDB_KEY   = os.environ.get("TVDB_API_KEY", "").strip()
TVDB_BASE  = "https://api4.thetvdb.com/v4"
ENABLE_TVDB = bool(TVDB_KEY)
_TVDB_TOKEN = None

def _tvdb_login():
    global _TVDB_TOKEN
    if not TVDB_KEY:
        return None
    try:
        r = _tmdb_session.post(TVDB_BASE + "/login", json={"apikey": TVDB_KEY}, timeout=20)
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
            r = _tmdb_session.get(TVDB_BASE + path, params=params,
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

# Force a TVDB series by its thetvdb.com slug (from the URL) when search mismatches.
SHOW_TVDB_OVERRIDES = {
    "Spider-Man: The Animated Series": "spider-man-1994",
}

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
    if tid is None and ":" in query:                     # retry on the base name
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
    for page in range(10):                               # safety cap; 500 eps/page
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
    """Match an episode NAME to its (season, number) via the show's episode list."""
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
    """(overview, name): first non-empty of each across sources. Cached; never raises."""
    if season is None:
        return None, None
    ov = nm = ""
    def take(res):
        nonlocal ov, nm
        o, n = res
        if o and not ov: ov = o
        if n and not nm: nm = n
        return bool(ov and nm)
    if take(_tmdb_meta(show, season, ep, cache)):                 return ov, nm
    if ENABLE_TVMAZE and take(_tvmaze_meta(show, season, ep, cache)): return ov, nm
    if ENABLE_TVDB and take(_tvdb_meta(show, season, ep, cache)):     return ov, nm
    if season == 1: take(_absolute_meta(show, ep, cache))
    return (ov or None), (nm or None)

def episode_overview(show, season, ep, cache):
    return episode_meta(show, season, ep, cache)[0]

# ── XML helpers ──
_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LOCAL_IP = re.compile(r'^(https?://)?(192\.168\.|10\.|172\.(1[6-9]|2[0-9]|3[01])\.)')
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; WhiplashEPGBot/1.0)"}
# Full browser headers for servers that WAF-block bot user-agents (e.g. MistLive 403).
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
    "Accept": "application/xml,text/xml,*/*;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
}

def _sanitize(text): return _XML_ILLEGAL.sub("", text)

def _strip_local_images(prog):
    for tag in ("icon", "image"):
        for elem in list(prog.findall(tag)):
            src = elem.get("src") or elem.text or ""
            if _LOCAL_IP.search(src.strip()): prog.remove(elem)
    return prog

def fetch_xml(url, headers=None):
    resp = requests.get(url, headers=headers or HEADERS, timeout=30); resp.raise_for_status()
    return ET.fromstring(_sanitize(resp.content.decode("utf-8-sig", errors="replace")))

def fetch_xml_safe(url, name, headers=None):
    try: return fetch_xml(url, headers=headers)
    except (ET.ParseError, requests.RequestException) as e:
        print(f"WARNING: {name} fetch/parse error ({e}) - skipping {name} this run")
        return ET.fromstring("<tv></tv>")

def _http_diag(name, status, hdrs):
    """Log why a fetch was refused (Cloudflare / IP block / auth) instead of guessing."""
    keys = ("server", "cf-ray", "cf-mitigated", "cf-cache-status", "retry-after",
            "www-authenticate", "x-sucuri-id")
    shown = {k: hdrs.get(k) for k in keys if hdrs.get(k)}
    print(f"  {name} HTTP {status} | {shown or 'no telltale headers'}")

def fetch_mist(url):
    """MistLive sits behind bot protection. Try a TLS-impersonating client (curl_cffi,
    real-Chrome handshake) first, then fall back to plain requests. Return root or <tv/>."""
    try:
        from curl_cffi import requests as creq
        r = creq.get(url, impersonate="chrome", timeout=30)
        if r.status_code == 200:
            print("  fetched via curl_cffi (chrome impersonation)")
            return ET.fromstring(_sanitize(r.content.decode("utf-8-sig", errors="replace")))
        _http_diag("MistLive(curl_cffi)", r.status_code, r.headers)
    except ImportError:
        print("  curl_cffi not installed - falling back to requests")
    except Exception as e:
        print(f"  curl_cffi attempt failed: {e}")
    try:
        r = requests.get(url, headers=BROWSER_HEADERS, timeout=30)
        if r.status_code == 200:
            return ET.fromstring(_sanitize(r.content.decode("utf-8-sig", errors="replace")))
        _http_diag("MistLive(requests)", r.status_code, r.headers)
    except (ET.ParseError, requests.RequestException) as e:
        print(f"  requests attempt failed: {e}")
    print("WARNING: MistLive unavailable - skipping MistLive this run")
    return ET.fromstring("<tv></tv>")

def add_channels(tv, chan_map, logos):
    for _, (local_id, display) in chan_map.items():
        if tv.find(f"./channel[@id='{local_id}']") is not None: continue
        ch = ET.SubElement(tv, "channel", {"id": local_id})
        ET.SubElement(ch, "display-name").text = display
        if local_id in logos: ET.SubElement(ch, "icon", {"src": logos[local_id]})

_ONSCR_SPACE = re.compile(r"\bS(\d+)E(\d+)")

def _space_onscreen(prog):
    """Reformat onscreen episode-num 'SxxEyy' -> 'Sxx Eyy' (xmltv_ns left untouched)."""
    for en in prog.findall("episode-num"):
        if en.get("system") == "onscreen" and en.text:
            en.text = _ONSCR_SPACE.sub(r"S\1 E\2", en.text)

def copy_whiplash(tv, root):
    for src_id, (local_id, _) in WL_CHANNEL_MAP.items():
        n = 0
        for prog in root.findall("programme"):
            if prog.get("channel") != src_id: continue
            np = ET.fromstring(ET.tostring(prog)); np.set("channel", local_id)
            _strip_local_images(np); _space_onscreen(np); tv.append(np); n += 1
        if n: print(f"  {local_id}: {n} programmes")

def build_eel(tv, root, cache):
    got_desc = got_sub = 0
    # Resolve each configured Eel channel to the source id present in THIS feed.
    # Prefer the configured id; if it has vanished (feed renumbered it), auto-switch
    # to whichever DECLARED channel now carries the matching display-name, keyed on
    # the stable tvg-id. Undeclared "orphan" programme ids are ignored.
    declared = {c.get("id"): (c.findtext("display-name") or "") for c in root.findall("channel")}
    name2local = {_norm_chan_name(disp): loc for loc, disp in EEL_CHANNEL_MAP.values()}
    for _alias, _loc in EEL_NAME_ALIASES.items():
        name2local[_norm_chan_name(_alias)] = _loc
    resolved = {}   # src_id_in_feed -> (local_id, display)
    for want_id, (local_id, display) in EEL_CHANNEL_MAP.items():
        if want_id in declared:
            resolved[want_id] = (local_id, display)
            continue
        alt = next((cid for cid, cname in declared.items()
                    if cid not in EEL_CHANNEL_MAP and name2local.get(_norm_chan_name(cname)) == local_id), None)
        if alt:
            resolved[alt] = (local_id, display)
            print(f"  NOTE: Eel '{display}' source id changed {want_id} -> {alt} "
                  f"(auto-matched by display-name; update EEL_CHANNEL_MAP to silence)")
        else:
            print(f"  WARNING: Eel '{display}' source id {want_id} not in feed and no "
                  f"display-name match found -> this channel will be EMPTY this run")
    for src_id, (local_id, _) in resolved.items():
        n = 0
        for prog in root.findall("programme"):
            if prog.get("channel") != src_id: continue
            raw_title = (prog.findtext("title") or "").strip()
            title, sub, season, ep, ep_end = parse_title(raw_title)
            title = _eel_segment_rename(title, prog.get("start"), local_id)   # time/channel-based renames
            fm = _FILM_FRANCHISE.match(title)
            if fm and sub:
                yr = re.search(r"\((?:19|20)\d\d\)", sub) or re.search(r"\((?:19|20)\d\d\)", raw_title)
                if yr:
                    film = re.sub(r"\s*\((?:19|20)\d\d\)\s*", " ", sub).strip()
                    canon = next((v for k, v in FILM_CANON.items() if k in film.lower()), None)
                    title = canon or f"{fm.group(1)}: {film} {yr.group(0)}"
                    sub, season, ep, ep_end = None, None, None, None
            desc = None
            # Per-show override guide (feed numbers the show in an order the DBs don't share).
            #   mode "absolute": index by the feed's absolute episode number (Batman).
            #   mode "se":       key by the feed's season.episode (Garfield; feed S/E == guide S/E).
            gentry = None
            gdef = FIZZ_GUIDE.get(title)
            if gdef and ep is not None:
                mode = gdef.get("mode")
                if mode == "absolute":
                    lst = gdef["eps"]
                    if 1 <= ep <= len(lst):
                        gentry = lst[ep - 1]
                        season, ep = gentry["se"]
                        sub = gentry.get("title") or sub
                elif mode == "se" and season is not None:
                    gentry = gdef["eps"].get(f"{season}.{ep}")
                    if gentry:
                        sub = gentry.get("title") or sub   # season/ep unchanged (feed matches guide)
                elif mode == "name":
                    # resolve the item's episode/segment name, then match by name (feed
                    # numbers on TMDB's grid but the NUMBERING differs from IMDb's).
                    look = sub
                    if not look and season is not None:
                        _d, look = episode_meta(title, season, ep, cache)
                    if look:
                        gentry = gdef["eps"].get(_guide_norm(look))
                        if gentry:
                            sub = gentry.get("title") or sub   # S/E left as the feed's
                            if gdef.get("renumber") and gentry.get("se"):
                                season, ep = gentry["se"]       # feed numbering unreliable -> guide's
            elif gdef and gdef.get("mode") == "name" and ep is None and sub:
                # name-mode show with no parsed number but a feed sub-title -> match on it
                gentry = gdef["eps"].get(_guide_norm(sub))
                if gentry:
                    sub = gentry.get("title") or sub
                    if gdef.get("renumber") and gentry.get("se"):
                        season, ep = gentry["se"]
            if gentry is not None:
                desc = gentry.get("desc") or None
            elif season is None and sub:                # have episode name, no number -> match it
                season, ep = _name_to_se(title, EP_NAME_ALIAS.get((title, sub), sub), cache)
            if gentry is None and season is not None:
                desc, epname = episode_meta(title, season, ep, cache)
                if not sub and epname:                 # fill missing episode name from metadata
                    sub = epname; got_sub += 1
            if season is not None and (title, season, ep) in EPISODE_SUBS:
                sub = EPISODE_SUBS[(title, season, ep)]
            if desc is None and title in FIXED_DESC:    # fixed desc for block/filler programmes
                desc = FIXED_DESC[title]
            if desc is None and title in EEL_SHOW_LOOKUP:   # show-level synopsis, IMDb spelling as backup
                desc = _show_overview_db(title, cache) or None
                if not desc and OMDB_KEY:
                    desc = _omdb_plot(EEL_SHOW_LOOKUP[title] or title, cache, "series") or None
            # Movie fallback: non-episodic entries carrying a (YYYY) in the title or
            # sub-title are films (e.g. "American History X (1998)", or "Teen Titans" +
            # "Trouble in Tokyo (2006)"). Look them up as MOVIES on TMDB. Display title
            # and sub-title are left exactly as-is; only the description is filled.
            if desc is None and season is None and title not in FIXED_DESC and TMDB_KEY:
                mq = f"{title}: {sub}" if sub else title
                if re.search(r"\((?:19|20)\d\d\)", mq):
                    _my, _mov = _movie_meta(mq, cache)
                    if _mov:
                        desc = _mov; got_desc += 1
            # Part-marker guardrail (runs AFTER movie detection so a "(YYYY)" is untouched):
            # standardize "(1)/(2)"/"(Part One)"/": Part Four" -> ": Part N" on every channel.
            title = norm_parts(title)
            if sub: sub = norm_parts(sub)
            np = ET.Element("programme", {k: prog.get(k) for k in prog.keys()})
            np.set("channel", local_id)
            ET.SubElement(np, "title", {"lang": "en"}).text = title
            if sub: ET.SubElement(np, "sub-title", {"lang": "en"}).text = sub
            if desc:
                ET.SubElement(np, "desc", {"lang": "en"}).text = desc
                got_desc += 1
            if season is not None:
                onscr = f"S{season:02d} E{ep:02d}" + (f"-E{ep_end:02d}" if ep_end else "")
                ET.SubElement(np, "episode-num", {"system": "onscreen"}).text = onscr
                ET.SubElement(np, "episode-num", {"system": "xmltv_ns"}).text = f"{season-1}.{ep-1}."
            tv.append(np); n += 1
        if n: print(f"  {local_id}: {n} programmes (normalized)")
    if TMDB_KEY: print(f"  descriptions attached: {got_desc} | episode names added: {got_sub}")

_MIST_DASH_SE = re.compile(r"S(\d{1,2})\s*[-–]\s*E(\d{1,2})", re.I)  # "S1 - E5" -> S1E5
_MIST_EPISODE = re.compile(r"episode[\s\-_]*(\d{1,3})", re.I)             # "...episode-17..."
_MIST_LEADNUM = re.compile(r"^(\d{1,2})\s+\D")                            # "10 Damien" -> ep 10

def _mist_se(sub):
    """Extract (season, ep) from a MistLive sub-title. None,None if not episodic."""
    if not sub:
        return None, None
    s = sub.replace("+", " ").replace("_", " ").replace(".", " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = _MIST_DASH_SE.sub(lambda m: f"S{m.group(1)}E{m.group(2)}", s)
    _, _, season, ep, _ = parse_title(s)
    if season is not None:
        return season, ep
    m = _MIST_EPISODE.search(s)                 # bare "episode N" -> S01E N (absolute walk fixes season)
    if m:
        return 1, int(m.group(1))
    m = _MIST_LEADNUM.match(s)                  # leading "NN Title" -> S01E NN
    if m:
        return 1, int(m.group(1))
    return None, None

def _mist_lookup_name(title):
    """Show name for metadata lookup: drop parenthetical tags, apply renames."""
    name = MIST_RENAMES.get(title, title)
    return re.sub(r"\s*\([^)]*\)\s*", " ", name).strip() or name

_MV_MARKER = re.compile(r"(official|music|lyric)\s*(video|visualizer|audio)|\bvisualizer\b", re.I)

def _movie_meta(raw_title, cache):
    """Look up a non-episodic title as a MOVIE on TMDB. Returns (year, overview) or (None,None).
    Strict: exact normalized-title match + real vote count, so music videos/art shorts never match."""
    if not TMDB_KEY:
        return None, None
    if _MV_MARKER.search(raw_title) or " - " in raw_title:   # music video / "Artist - Title" -> not a movie
        return None, None
    ym = re.search(r"\((19|20)\d\d\)", raw_title)
    year = ym.group(0)[1:-1] if ym else None
    clean = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*", " ", raw_title)   # drop (…) and […]
    clean = re.sub(r"\s+", " ", clean).strip()
    if not clean:
        return None, None
    # year-tagged keys carry a "v2" so titles cached as misses under the old,
    # stricter vote rule get one fresh lookup
    key = clean.lower() + (f"|{year}|v2" if year else "")
    if key in cache["tmdb_movies"]:
        rec = cache["tmdb_movies"][key]
        return (rec["y"], rec["o"]) if rec else (None, None)
    params = {"query": clean}
    if year: params["year"] = year
    data = _tmdb_get("/search/movie", **params) or {}
    rec = None
    for res in (data.get("results") or [])[:5]:
        rd = res.get("release_date") or ""
        votes = res.get("vote_count") or 0
        if _norm(res.get("title")) != _norm(clean):
            continue
        if year:
            # a year in the feed is strong evidence: accept small titles (TV specials like
            # the Peanuts ones have only a handful of votes) if TMDB's year is within 1
            if not (rd[:4].isdigit() and abs(int(rd[:4]) - int(year)) <= 1 and votes >= 3):
                continue
        elif votes < 50:
            continue
        rec = {"y": rd[:4] if rd[:4].isdigit() else (year or ""),
               "o": (res.get("overview") or "").strip()}
        break
    cache["tmdb_movies"][key] = rec
    return (rec["y"], rec["o"]) if rec else (None, None)

def build_mist(root, cache):
    """Copy every channel/programme as-is; enrich only the MIST_ENRICH channels."""
    tv = ET.Element("tv", {"generator-info-name": "mistlive-epg-generator"})
    for ch in root.findall("channel"):
        tv.append(ET.fromstring(ET.tostring(ch)))
    kept = enriched = movies = got_desc = got_sub = 0
    for prog in root.findall("programme"):
        cid = prog.get("channel")
        if cid not in MIST_ENRICH:
            tv.append(ET.fromstring(ET.tostring(prog))); kept += 1
            continue
        raw_title = (prog.findtext("title") or "").strip()
        disp_title = MIST_RENAMES.get(raw_title, raw_title)   # keep content tags, fix names
        season, ep = _mist_se(prog.findtext("sub-title"))
        sub = desc = None
        if season is not None:
            lookup = _mist_lookup_name(raw_title)
            desc, epname = episode_meta(lookup, season, ep, cache)
            if epname: sub = epname; got_sub += 1
        if season is None:                                   # not a TV episode -> try MOVIE
            m_year, m_desc = _movie_meta(raw_title, cache)
            if m_year or m_desc:
                np = ET.Element("programme", {k: prog.get(k) for k in prog.keys()})
                mt = disp_title
                if m_year and not re.search(r"\((19|20)\d\d\)", mt):
                    mt = f"{mt} ({m_year})"
                ET.SubElement(np, "title", {"lang": "en"}).text = mt
                if m_desc: ET.SubElement(np, "desc", {"lang": "en"}).text = m_desc
                if m_year: ET.SubElement(np, "date").text = m_year
                ET.SubElement(np, "category", {"lang": "en"}).text = "Movie"
                tv.append(np); movies += 1
            else:                                            # music video / art short -> leave as-is
                tv.append(ET.fromstring(ET.tostring(prog))); kept += 1
            continue
        np = ET.Element("programme", {k: prog.get(k) for k in prog.keys()})
        ET.SubElement(np, "title", {"lang": "en"}).text = disp_title
        if sub: ET.SubElement(np, "sub-title", {"lang": "en"}).text = sub
        if desc:
            ET.SubElement(np, "desc", {"lang": "en"}).text = desc; got_desc += 1
        onscr = f"S{season:02d} E{ep:02d}"
        ET.SubElement(np, "episode-num", {"system": "onscreen"}).text = onscr
        ET.SubElement(np, "episode-num", {"system": "xmltv_ns"}).text = f"{season-1}.{ep-1}."
        tv.append(np); enriched += 1
    print(f"  copied as-is: {kept} | enriched: {enriched} | movies: {movies}")
    if TMDB_KEY: print(f"  descriptions attached: {got_desc} | episode names added: {got_sub}")
    return tv

# ── US-Central fixed-weekly-lineup channels (CCN, NickPicks): show-level synopses ──
# Each schedule file is {weekday: [[start_min, stop_min, "Title"], ...]} in Central local time.
# Schedules repeat weekly; each channel has its own exclusive end horizon.
WEEKLY_START = datetime(2026, 9, 30)    # first Central date to generate (covers "now")

CCN_OUTPUT     = "CCN.xml"
CCN_ID         = "CartoonCartoonsNetwork"
CCN_NAME       = "Cartoon Cartoons Network"
CCN_LOGO       = "https://i.imgur.com/CaeVZxd.png"
CCN_SCHED_FILE = "ccn_schedule.json"
CCN_DAYS       = 30                     # rolling: yesterday -> +30 days, rebuilt every run
# The CCN website keeps its weekly lineup inside its Owncast custom script, as a plain
# {Day: {"HH:MM": "Show"}} object (US Central, 30-min slots). We read it every run, so
# the guide follows the site; ccn_schedule.json is refreshed from it and is the fallback.
CCN_LIVE_URL   = "https://cartooncartoons.thatsoretro.com/customjavascript"
CCN_RENAMES    = {                       # site title -> our guide title
    "Cartoon Theatre": "Cartoon Theater",
    "DragonBall": "Dragon Ball",
    "DragonBall Z": "Dragon Ball Z",
    "Fullmetal Alchemist Brotherhood": "Fullmetal Alchemist: Brotherhood",
    "G Gundam": "Mobile Fighter G Gundam",
    "Gundam Series": "Mobile Fighter G Gundam",
    "G.I. Joe: American Hero ('89)": "G.I. Joe: American Hero (1989)",
    "G.I. Joe: Real American Hero ('83)": "G.I. Joe: Real American Hero (1983)",
    "Justice League/Unlimited": "Justice League Unlimited",
    "Mobile Suit Gundam series": "Mobile Suit Gundam",
    "New Scooby-Doo Movies": "The New Scooby-Doo Movies",
    "Scooby-Doo Movies": "The New Scooby-Doo Movies",
    "Real Advs of Jonny Quest": "Real Adventures of Jonny Quest",
    "Scooby and Scrappy": "Scooby and Scrappy-Doo",
    "Scooby-Doo! Mystery Inc.": "Scooby-Doo! Mystery Incorporated",
    "Superman: Animated Series": "Superman: The Animated Series",
    "TMNT 2K3": "Teenage Mutant Ninja Turtles (2003)",
    "Teenage Mutant Ninja Turtles 2K3": "Teenage Mutant Ninja Turtles (2003)",
    "The Batman": "The Batman (2004)",
    "Transformers '84": "Transformers (1984)",
    "Transformers ('01)": "Transformers (2001)",
    "Voltron Series": "Voltron (1984)",
}

NICK_OUTPUT     = "NickPicks.xml"
NICK_ID         = "NickPicks"
NICK_NAME       = "Nick Picks"
NICK_LOGO       = "https://i.imgur.com/6is9eGi.png"
NICK_SCHED_FILE = "nickpicks_schedule.json"
NICK_DAYS       = 60                     # rolling window (perpetual weekly loop)

TSD_OUTPUT     = "ThatsSoDisney.xml"
TSD_ID         = "ThatsSoDisney"
TSD_NAME       = "That's So Disney"
TSD_LOGO       = ""                        # no logo
TSD_SCHED_FILE = "thatssodisney_schedule.json"
TSD_DAYS       = 60                     # rolling window (perpetual weekly loop)

RB_OUTPUT     = "RetroBlast.xml"
RB_ID         = "RetroBlast"
RB_NAME       = "RetroBlast"
RB_LOGO       = "https://i.imgur.com/m9GHMe9.png"
RB_SCHED_FILE = "retroblast_schedule.json"
RB_DAYS       = 30                      # rolling window (Fall weekly grid, US Eastern)
# One-off airings on specific (Eastern) dates, laid over the weekly grid. Rows are
# [start_min, stop_min, title, desc, sub-title(optional)]; any grid show they overlap
# is trimmed or dropped. Times are minutes from local midnight of that date.
RB_SPECIALS = {
    # Mon Nov 2, from 7 PM: Looney Tunes Cartoons mini marathon, then X-Men (2000)
    "2026-11-02": [
        [1140, 1200, "Looney Tunes Cartoons",
         "A special mini marathon of Looney Tunes Cartoons, shown in honor of Eric Bauza."],
        [1200, 1305, "X-Men (2000)",
         "Wolverine and Rogue are drawn into the war between Professor X's X-Men and Magneto's "
         "Brotherhood of Mutants. Shown in honor of Patrick Stewart."],
    ],
    # Cartoon Theater (Sat/Sun 12:10-2:15 PM)
    "2026-11-01": [[730, 855, "The Nightmare Before Christmas (1993)",
        "Jack Skellington, the Pumpkin King of Halloween Town, stumbles into Christmas Town and "
        "decides to take over the holiday."]],
    "2026-11-07": [
        [730, 775, "Class of 3000",
         "In the series premiere, music star Sunny Bridges comes home to Atlanta and becomes a "
         "music teacher at the Westley School of Performing Arts.", "Home"],
        [775, 855, "Foster's Home for Imaginary Friends",
         "In this feature-length special, Wilt suddenly leaves Foster's, and Mac and the gang set "
         "out to find him and uncover the story of his past.", "Good Wilt Hunting"],
    ],
    "2026-11-08": [
        [0, 100, "The Wizard of Oz (1939)",
         "Swept away by a tornado to the land of Oz, Dorothy follows the yellow brick road to find "
         "the Wizard who can send her home."],
        [730, 855, "Beauty and the Beast (1991)",
         "A bright young woman named Belle takes her father's place as the prisoner of a cursed "
         "Beast, and finds the kind heart hidden beneath."],
    ],
    "2026-11-14": [[730, 855, "An American Tail (1986)",
        "Fievel Mousekewitz, a young Russian mouse, is separated from his family on the voyage to "
        "America and sets out to find them in New York City."]],
    "2026-11-15": [
        [0, 100, "Wicked: Slime Tutorial with the Original Broadway Cast",
         "The original Broadway cast recording of Wicked, presented in its famous slime tutorial "
         "form, as part of an Oz-themed month of late night movies."],
        [730, 855, "The Prince of Egypt (1998)",
         "The animated story of Moses, raised as a prince of Egypt, who discovers his true "
         "heritage and leads the Hebrew people to freedom."],
    ],
    "2026-11-21": [
        [730, 790, "Tom and Jerry & the Wizard of Oz (2011)",
         "Tom and Jerry join Dorothy and Toto on the yellow brick road to the Emerald City."],
        [790, 855, "Camp Lazlo",
         "Camp Lazlo's feature-length special, starring Lazlo, Raj, Clam and the rest of Camp "
         "Kidney.", "Where's Lazlo?"],
    ],
    "2026-11-22": [
        [0, 100, "Return to Oz (1985)",
         "Dorothy returns to a ruined Oz and, with a band of new friends, must save it from the "
         "Nome King and the wicked Princess Mombi."],
        [730, 810, "Tom & Jerry: Back to Oz (2016)",
         "Tom and Jerry return to Oz with Dorothy to stop the Gnome King from taking over the land."],
        [810, 855, "Samurai Jack",
         "How the evil Aku first came to Earth, and how Jack's father defeated him, setting the "
         "stage for Jack's quest.", "The Birth of Evil"],
    ],
    "2026-11-28": [[730, 855, "Mobile Suit Gundam Wing: Endless Waltz (1998)",
        "A year after the war, the Gundam pilots are drawn back into battle when a new rebellion "
        "threatens the fragile peace between Earth and the colonies."]],
    "2026-11-29": [
        [0, 100, "The Rocky Horror Picture Show (1975)",
         "Newly engaged Brad and Janet seek help at a strange castle on a stormy night and meet "
         "the outrageous Dr. Frank-N-Furter."],
        [730, 855, "The Polar Express (2004)",
         "On Christmas Eve, a young boy boards a mysterious train bound for the North Pole."],
    ],
}

def fetch_ccn_live():
    """Read CCN's weekly lineup from the site's custom script. Returns the same
    {weekday: [[start_m, stop_m, title], ...]} shape as ccn_schedule.json, or None."""
    WD = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
    try:
        r = requests.get(CCN_LIVE_URL, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        m = re.search(r"const\s+schedule\s*=\s*(\{.*?\n\s*\});", r.text, re.S)
        if not m:
            print("  CCN live: schedule object not found in the site script - using ccn_schedule.json"); return None
        lit = re.sub(r'(^|[{,]\s*)([A-Za-z_]\w*)\s*:', lambda x: f'{x.group(1)}"{x.group(2)}":', m.group(1), flags=re.M)
        lit = re.sub(r",(\s*[}\]])", r"\1", lit)
        raw = json.loads(lit)
    except Exception as e:
        print(f"  CCN live: unavailable ({e}) - using ccn_schedule.json"); return None
    out, new_titles = {}, set()
    for d in WD:
        slots = raw.get(d) or {}
        runs = []
        for hhmm in sorted(slots):
            try:
                h, mi = map(int, hhmm.split(":"))
            except ValueError:
                continue
            title = html.unescape(str(slots[hhmm])).strip()
            if not title:
                continue
            title = CCN_RENAMES.get(title, title)
            st = h * 60 + mi
            if runs and runs[-1][2] == title and runs[-1][1] == st:
                runs[-1][1] = st + 30                  # same show continues: one longer programme
            else:
                runs.append([st, st + 30, title])
        out[d] = runs
    covered = sum(b - a for runs in out.values() for a, b, _ in runs)
    if covered < 7 * 1440 * 0.9:                       # sanity: a broken/partial read keeps the fallback
        print(f"  CCN live: only {covered // 60}h of 168h parsed - using ccn_schedule.json"); return None
    return out

def _local_to_utc(dt, zone):
    """Naive local wall time -> naive UTC, with real DST rules for any year."""
    return dt.replace(tzinfo=ZoneInfo(zone)).astimezone(timezone.utc).replace(tzinfo=None)

def _central_to_utc(dt):
    """US Central -> UTC (CDT -5 / CST -6, switches automatically every year)."""
    return _local_to_utc(dt, "America/Chicago")

def _eastern_to_utc(dt):
    """US Eastern -> UTC (EDT -4 / EST -5, switches automatically every year)."""
    return _local_to_utc(dt, "America/New_York")

def _rolling(days, zone="America/Chicago"):
    """(start, end) local dates: yesterday through +days, so the guide never runs out."""
    today = datetime.now(ZoneInfo(zone)).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=1), today + timedelta(days=days)

# Display title -> name to search when the schedule's title is abbreviated/non-canonical
SYN_ALIAS = {
    "Dennis the Menace ('86)": "Dennis the Menace (1986)",
    "Hellsing": "Hellsing (2001)",   # disambiguate to the 2001 series for lookup; title stays "Hellsing"
}

# ── OMDb (IMDb plots) — first choice for CCN, RetroBlast, NickPicks, TSD ─────────────────
# Free OMDb keys allow ~1,000 calls/day and the same key may be shared with other
# repos, so calls are capped per run; results (hits AND misses) live in
# desc_cache.json["omdb_syn"] and are re-checked after OMDB_TTL_DAYS.
OMDB_KEY        = os.environ.get("OMDB_API_KEY", "").strip()
OMDB_RUN_BUDGET = int(os.environ.get("OMDB_BUDGET", "150"))
OMDB_TTL_DAYS   = 60
OMDB_CHANNELS   = {"CartoonCartoonsNetwork", "RetroBlast", "NickPicks", "ThatsSoDisney"}
_omdb_left      = OMDB_RUN_BUDGET
_omdb_stats     = {"calls": 0, "hits": 0}

def _omdb_get(**params):
    global _omdb_left
    if not OMDB_KEY or _omdb_left <= 0:
        return None
    _omdb_left -= 1; _omdb_stats["calls"] += 1
    try:
        r = requests.get("https://www.omdbapi.com/", params={"apikey": OMDB_KEY, **params}, timeout=20)
        j = r.json()
        if str(j.get("Error", "")).lower().startswith("request limit"):
            _omdb_left = 0; return None
        return j
    except Exception:
        return None

def _omdb_plot(title, cache, kind="series"):
    """IMDb short plot for a show (kind='series') or film (kind='movie') via OMDb.
    Returns the plot, "" if OMDb has none, or None if not looked up this run."""
    store = cache.setdefault("omdb_syn", {})
    key = f"{kind}|{title}"
    ent = store.get(key)
    if ent and time.time() - ent.get("t", 0) < OMDB_TTL_DAYS * 86400:
        return ent.get("p", "")
    lookup = SYN_ALIAS.get(title, title)
    ym = re.search(r"\((19|20)\d\d\)", lookup)
    year = ym.group(0)[1:-1] if ym else None
    clean = re.sub(r"\s*\([^)]*\)\s*", " ", lookup).strip() or lookup   # drop (YYYY) / (Detective Conan) etc.
    q = {"t": clean, "type": kind, "plot": "short"}
    if year: q["y"] = year
    j = _omdb_get(**q)
    if j is None:
        return None                                   # out of budget / no key: try again next run
    if j.get("Response") != "True":                   # exact title missed -> search, take the best match
        s = _omdb_get(s=clean, type=kind, **({"y": year} if year else {}))
        if s is None:
            return None
        hits = s.get("Search") or []
        if hits:
            j = _omdb_get(i=hits[0]["imdbID"], plot="short") or {}
    plot = (j.get("Plot") or "").strip()
    if plot in ("N/A", ""):
        plot = ""
    store[key] = {"t": time.time(), "p": plot, "id": j.get("imdbID", "")}
    if plot: _omdb_stats["hits"] += 1
    return plot

def _show_overview(title, cache, prefer_omdb=False):
    if prefer_omdb and title not in FIXED_DESC:
        p = _omdb_plot(title, cache, "series")
        if p:
            return p
    return _show_overview_db(title, cache)

def _show_overview_db(title, cache):
    """SHOW-level synopsis: TMDB -> TVmaze -> TVDB, cached. (YYYY) in title disambiguates."""
    if title in FIXED_DESC:                    # fixed override (no metadata lookup)
        return FIXED_DESC[title]
    if title in cache["show_syn"] and cache["show_syn"][title]:
        return cache["show_syn"][title]
    lookup = SYN_ALIAS.get(title, title)
    ym = re.search(r"\((19|20)\d\d\)", lookup)
    year = ym.group(0)[1:-1] if ym else None
    clean = re.sub(r"\s*\((19|20)\d\d\)\s*", " ", lookup).strip() or lookup
    ov = ""
    if TMDB_KEY:
        params = {"query": clean}
        if year: params["first_air_date_year"] = year
        data = _tmdb_get("/search/tv", **params) or {}
        for res in (data.get("results") or [])[:3]:
            if res.get("overview"): ov = res["overview"].strip(); break
    if not ov and ENABLE_TVMAZE:
        d = _tvmaze_get("/singlesearch/shows", q=clean)
        if isinstance(d, dict) and d.get("summary"):
            ov = _TAGS.sub("", d["summary"]).strip()
    if not ov and ENABLE_TVDB:
        tid = _resolve_tvdb_id(clean, cache)
        if tid:
            d = _tvdb_get(f"/series/{tid}") or {}
            ov = (d.get("overview") or "").strip()
    cache["show_syn"][title] = ov
    return ov

def load_weekly_schedule(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"WARNING: cannot read {path} ({e}) - skipping"); return None

def _overlay(rows, specials):
    """Lay date-specific airings over a day's grid rows: trim/drop what they cover."""
    if not specials:
        return rows
    out = []
    for r in rows:
        segs = [(r[0], r[1])]
        for sp in specials:
            nxt = []
            for a, b in segs:
                if sp[1] <= a or sp[0] >= b:
                    nxt.append((a, b)); continue
                if a < sp[0]: nxt.append((a, sp[0]))
                if sp[1] < b: nxt.append((sp[1], b))
            segs = nxt
        for a, b in segs:
            if b - a >= 5:                       # drop slivers under 5 minutes
                out.append([a, b] + list(r[2:]))
    return sorted(out + [list(s) for s in specials], key=lambda r: r[0])

def build_weekly(ch_id, name, logo, sched, cache, end, start=None, tz=_central_to_utc, specials=None):
    """Expand a source-local weekly grid forward over [start, end), convert to
    UTC via tz() (DST-aware), attach show-level synopses.

    Schedule rows are [start_m, stop_m, title] or [start_m, stop_m, title, desc];
    a 4th element is an inline description override (used e.g. for the Boomerang
    hour-segments, which all share the title "Boomerang")."""
    tv = ET.Element("tv", {"generator-info-name": f"{ch_id}-epg-generator"})
    ch = ET.SubElement(tv, "channel", {"id": ch_id})
    ET.SubElement(ch, "display-name").text = name
    if logo: ET.SubElement(ch, "icon", {"src": logo})
    WD = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
    n = got = 0
    day = start or WEEKLY_START
    while day < end:
        rows = _overlay(sched.get(WD[day.weekday()], []), (specials or {}).get(day.strftime("%Y-%m-%d")))
        for row in rows:
            start_m, stop_m, title = row[0], row[1], row[2]
            inline = row[3] if len(row) > 3 else None
            subt = row[4] if len(row) > 4 else None
            st = tz(day + timedelta(minutes=start_m))
            sp = tz(day + timedelta(minutes=stop_m))
            if inline is not None:
                desc = inline
                # one-off films on CCN/RetroBlast: prefer the IMDb plot when OMDb has it
                if ch_id in OMDB_CHANNELS and re.search(r"\((19|20)\d\d\)$", title) and not subt:
                    desc = _omdb_plot(title, cache, "movie") or inline
            else:
                desc = _show_overview(title, cache, prefer_omdb=ch_id in OMDB_CHANNELS)
            p = ET.SubElement(tv, "programme", {
                "start": st.strftime("%Y%m%d%H%M%S") + " +0000",
                "stop":  sp.strftime("%Y%m%d%H%M%S") + " +0000",
                "channel": ch_id})
            ET.SubElement(p, "title", {"lang": "en"}).text = title
            if subt:
                ET.SubElement(p, "sub-title", {"lang": "en"}).text = subt
            if desc:
                ET.SubElement(p, "desc", {"lang": "en"}).text = desc; got += 1
            n += 1
        day += timedelta(days=1)
    print(f"  programmes: {n}")
    if ch_id in OMDB_CHANNELS:
        print(f"  OMDb: {'key set' if OMDB_KEY else 'no OMDB_API_KEY - using TMDB/TVmaze/TVDB'}; "
              f"calls so far this run {_omdb_stats['calls']}, new IMDb plots {_omdb_stats['hits']}")
    if TMDB_KEY: print(f"  synopses attached: {got}")
    return tv

# ── Placid (webch.nl live weekly anime block) ──────────────────────────────
# Source serves only a short window, live ~once a week (Sat). We ROLLING-MERGE
# each run into placid_state.json (committed) so captured blocks persist, prune
# old history, then fill all off-air time with 12h filler. Output Placid.xml.
# Every schedule upload gets a NEW id. The bare endpoint lists them (newest first):
#   [{"id":44,"filename":"Oct 31.xml","uploaded_at":"2026-10-30 14:45:40"}, ...]
# so we read the list each run and pull the newest few uploads.
PLACID_LIST_URL     = "https://placid.webch.nl/assets/api/schedules.php"
PLACID_URL          = PLACID_LIST_URL + "?id={id}"
PLACID_FALLBACK_ID  = 44                      # used only if the list itself can't be read
PLACID_FETCH_LATEST = 4                       # how many of the newest uploads to pull each run
PLACID_OUTPUT       = "Placid.xml"
PLACID_ID           = "Placid"
PLACID_NAME         = "Placid"
PLACID_LOGO         = "https://i.imgur.com/0PCSYdW.png"
PLACID_STATE        = "placid_state.json"   # rolling store of real programmes seen (commit it)
PLACID_KEEP_DAYS    = 21                      # retain this much past history
PLACID_HORIZON_DAYS = 14                      # extend off-air filler this far ahead of now
PLACID_FILLER_TITLE = "PLACID IS OFFLINE FOR THE WEEK"
PLACID_FILLER_DESC  = ("Only the best in anime(tion)."
                       "\n\n"
                       "Placid is live every Saturday at 5:00 PM EST.")

def _xmltv_to_utc(ts):
    """'YYYYmmddHHMMSS ±HHMM' -> naive UTC datetime."""
    parts = (ts or "").split()
    d = parts[0]; off = parts[1] if len(parts) > 1 else "+0000"
    base = datetime.strptime(d, "%Y%m%d%H%M%S")
    sign = 1 if off[0] == "+" else -1
    return base - sign * timedelta(hours=int(off[1:3]), minutes=int(off[3:5]))

def _next_grid12(dt):
    """Next 00:00 or 12:00 UTC strictly after dt (12h filler grid)."""
    b = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    for h in (0, 12, 24):
        c = b + timedelta(hours=h)
        if c > dt:
            return c
    return dt + timedelta(hours=12)

def fetch_placid_uploads():
    """Newest Placid schedule uploads as a list of XML roots, OLDEST first (so a newer
    upload overrides an older one where they overlap)."""
    ids = []
    try:
        r = requests.get(PLACID_LIST_URL, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        rows = [x for x in r.json() if isinstance(x, dict) and str(x.get("id", "")).isdigit()]
        rows.sort(key=lambda x: (x.get("uploaded_at") or "", int(x["id"])))
        ids = [int(x["id"]) for x in rows][-PLACID_FETCH_LATEST:]
        print(f"  Placid uploads listed: {len(rows)}; using ids {ids} "
              f"(newest: {rows[-1].get('filename')} uploaded {rows[-1].get('uploaded_at')})" if rows else "  Placid: empty upload list")
    except Exception as e:
        print(f"WARNING: Placid upload list unreadable ({e}) - falling back to id {PLACID_FALLBACK_ID}")
    if not ids:
        ids = [PLACID_FALLBACK_ID]
    roots = []
    for i in ids:
        root = fetch_xml_safe(PLACID_URL.format(id=i), f"Placid id {i}")
        if root.findall("programme"):
            roots.append(root)
    return roots

def build_placid(src_roots, state_path=PLACID_STATE, now=None):
    now = now or datetime.utcnow()
    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}
    added = 0
    if not isinstance(src_roots, (list, tuple)):
        src_roots = [src_roots]
    for src_root in src_roots:                    # oldest upload first
        upl = [p for p in src_root.findall("programme") if p.get("start") and p.get("stop")]
        if not upl:
            continue
        # a newer upload REPLACES whatever we had inside its time span (handles reshuffles)
        a = min(_xmltv_to_utc(p.get("start")) for p in upl)
        b = max(_xmltv_to_utc(p.get("stop")) for p in upl)
        state = {k: v for k, v in state.items() if not (a <= _xmltv_to_utc(k) < b)}
        for prog in upl:
            np = ET.fromstring(ET.tostring(prog)); np.set("channel", PLACID_ID)
            _strip_local_images(np)
            key = np.get("start")
            if key not in state:
                added += 1
            state[key] = ET.tostring(np, encoding="unicode")        # add or refresh
    cutoff = now - timedelta(days=PLACID_KEEP_DAYS)
    state = {k: v for k, v in state.items() if _xmltv_to_utc(k) >= cutoff}
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=0, sort_keys=True)

    tv = ET.Element("tv", {"generator-info-name": "placid-epg-generator"})
    ch = ET.SubElement(tv, "channel", {"id": PLACID_ID})
    ET.SubElement(ch, "display-name").text = PLACID_NAME
    ET.SubElement(ch, "icon", {"src": PLACID_LOGO})

    reals = []
    for xmlstr in state.values():
        p = ET.fromstring(xmlstr)
        reals.append((_xmltv_to_utc(p.get("start")), _xmltv_to_utc(p.get("stop")), p))
    reals.sort(key=lambda r: r[0])

    span_start = min([r[0] for r in reals] + [now]) if reals else now
    span_end   = max([r[1] for r in reals] + [now + timedelta(days=PLACID_HORIZON_DAYS)])

    def add_filler(a, b):
        t = a
        while t < b:
            end = min(_next_grid12(t), b)
            fp = ET.SubElement(tv, "programme", {
                "start": t.strftime("%Y%m%d%H%M%S") + " +0000",
                "stop":  end.strftime("%Y%m%d%H%M%S") + " +0000",
                "channel": PLACID_ID})
            ET.SubElement(fp, "title", {"lang": "en"}).text = PLACID_FILLER_TITLE
            ET.SubElement(fp, "desc",  {"lang": "en"}).text = PLACID_FILLER_DESC
            t = end

    cursor = span_start
    for a, b, p in reals:
        if a > cursor:
            add_filler(cursor, a)
        tv.append(p)
        if b > cursor:
            cursor = b
    if cursor < span_end:
        add_filler(cursor, span_end)
    print(f"  Placid: {len(reals)} real programmes (+{added} new this run), filler to now+{PLACID_HORIZON_DAYS}d")
    return tv

# ── 40 Media Group (ENT + Totalmusic FAST channels) ─────────────────────────
# Each channel exposes a JSON guide at api.40mediagroup.com. Times are already
# ISO-8601 in UTC (…+00:00) so no tz conversion is needed (verified against the
# live on-air anchor). Titles/sub-titles/descriptions/categories are the feed's
# own Spanish-localized text, copied as-is. tvg-id == the JSON url slug.
# Rebuilt once per UTC day: the <tv date="YYYYMMDD"> stamp gates same-day reruns
# (the Whiplash workflow fires several times a day; this output only changes once).
MEDIA40_OUTPUT   = "40media.xml"
MEDIA40_URL      = "https://api.40mediagroup.com/xmltv/programacion/{slug}/guia.json"
MEDIA40_HEADERS  = {**BROWSER_HEADERS, "Accept": "application/json, text/plain, */*"}
# (tvg-id / url-slug, display-name, logo)
MEDIA40_CHANNELS = [
    ("entchannel",     "ENT Channel",      "https://i.imgur.com/5w7jlcr.png"),
    ("entclassic",     "ENT Classic",      "https://i.imgur.com/42wUAun.png"),
    ("entcomedy",      "ENT Comedy",       "https://i.imgur.com/22yAycO.png"),
    ("entdrama",       "ENT Drama",        "https://i.imgur.com/ARLD4hQ.png"),
    ("entextreme",     "ENT Extreme",      "https://i.imgur.com/AZwQU5n.png"),
    ("entfamily",      "ENT Family",       "https://i.imgur.com/ez5UE1a.png"),
    ("entlife",        "ENT Life",         "https://i.imgur.com/V6hdLT0.png"),
    ("entmovies",      "ENT Movies",       "https://i.imgur.com/wVcolSX.png"),
    ("totalmusic",     "Totalmusic",       "https://i.imgur.com/S9i0Vi4.png"),
    ("totalmusic-80s", "Totalmusic 80s",   "https://i.imgur.com/uG229hI.png"),
    ("totalmusic-00s", "Totalmusic 2000s", "https://i.imgur.com/JResjBY.png"),
]

def _iso_to_xmltv(s):
    """ISO-8601 (feed is already UTC, +00:00) -> 'YYYYmmddHHMMSS +0000'."""
    dt = datetime.fromisoformat(s)
    dt = dt.astimezone(timezone.utc) if dt.tzinfo else dt
    return dt.strftime("%Y%m%d%H%M%S") + " +0000"

def build_40media(now=None):
    """Scrape the 11 40 Media Group JSON guides into one XMLTV tree. Returns the
    tree, or None to leave the committed 40media.xml untouched (already built today,
    or every feed failed)."""
    now = now or datetime.utcnow()
    today = now.strftime("%Y%m%d")
    try:                                   # once-a-day gate
        if ET.parse(MEDIA40_OUTPUT).getroot().get("date") == today:
            print(f"  40media: already built today ({today}) - skipping")
            return None
    except Exception:
        pass

    tv = ET.Element("tv", {"generator-info-name": "40media-epg-generator", "date": today})
    for slug, name, logo in MEDIA40_CHANNELS:      # declare every channel up front
        ch = ET.SubElement(tv, "channel", {"id": slug})
        ET.SubElement(ch, "display-name").text = name
        if logo:
            ET.SubElement(ch, "icon", {"src": logo})

    ok = total = 0
    for slug, name, logo in MEDIA40_CHANNELS:
        try:
            r = requests.get(MEDIA40_URL.format(slug=slug), headers=MEDIA40_HEADERS, timeout=30)
            r.raise_for_status()
            progs = (r.json() or {}).get("programmes") or []
        except Exception as e:
            print(f"  40media {slug}: fetch failed ({e}) - channel left empty this run")
            continue
        n = 0
        for pr in progs:
            try:
                st, sp = _iso_to_xmltv(pr["start"]), _iso_to_xmltv(pr["stop"])
            except Exception:
                continue
            p = ET.SubElement(tv, "programme", {"start": st, "stop": sp, "channel": slug})
            ET.SubElement(p, "title", {"lang": "es"}).text = (pr.get("title") or "").strip() or name
            for src, tag in (("subtitle", "sub-title"), ("desc", "desc"), ("category", "category")):
                val = (pr.get(src) or "").strip()
                if val:
                    ET.SubElement(p, tag, {"lang": "es"}).text = val
            icon = (pr.get("icon") or "").strip()
            if icon:
                ET.SubElement(p, "icon", {"src": icon})
            n += 1
        ok += 1; total += n
        print(f"  40media {slug}: {n} programmes")
    if ok == 0:
        print("  40media: ALL feeds failed - keeping existing 40media.xml unchanged")
        return None
    print(f"  40media: {ok}/{len(MEDIA40_CHANNELS)} channels, {total} programmes")
    return tv

# ── Locomotion (fan-revived Latin American anime channel) ────────────────────
# Source: the channel's own XMLTV guide. It went stale in Jan 2026 (3 identical
# days), but the channel still plays that same 24h schedule every day - verified
# Oct 25 2026 on air (Duckman, then South Park on the half hour). Its wall-clock
# times are re-read at LOCO_UTC_OFFSET_H: the stream ran at UTC-5 on Oct 25, then
# moved to UTC-4 (= the file's own -0400) by Nov 2 2026 - verified on air (Los
# Cazadores de Duendes after Evangelion). If the feed ever covers "now" again, its
# real dates are used (same offset); otherwise its latest full day is looped forward.
LOCO_URL          = "http://epg.locomotiontv.com/guia.xml"
LOCO_OUTPUT       = "Locomotion.xml"
LOCO_ID           = "Locomotion"
LOCO_NAME         = "Locomotion"
LOCO_LOGO         = "https://i.imgur.com/g5psxlY.png"
LOCO_SRC_CHANNEL  = "Locomotion"
LOCO_UTC_OFFSET_H = -4        # channel's real clock (UTC-4 since Nov 2026); change if it drifts an hour
LOCO_DAYS         = 14        # loop this many days ahead

def _loco_wallclock(ts):
    """'YYYYmmddHHMMSS ±HHMM' -> naive wall-clock datetime (offset ignored on purpose)."""
    return datetime.strptime((ts or "").split()[0], "%Y%m%d%H%M%S")

def build_locomotion(src_root, now=None):
    now = now or datetime.utcnow()
    progs = [p for p in src_root.findall("programme") if p.get("channel") == LOCO_SRC_CHANNEL]
    if not progs:
        return None                                   # fetch failed: keep last file
    off = timedelta(hours=LOCO_UTC_OFFSET_H)
    def utc(wall): return wall - off                  # wall-clock at UTC-5 -> UTC
    items = []
    for p in progs:
        try:
            a, b = _loco_wallclock(p.get("start")), _loco_wallclock(p.get("stop"))
        except ValueError:
            continue
        if b > a:
            items.append((a, b, p))
    items.sort(key=lambda x: x[0])
    tv = ET.Element("tv", {"generator-info-name": "locomotion-epg-generator"})
    ch = ET.SubElement(tv, "channel", {"id": LOCO_ID})
    ET.SubElement(ch, "display-name").text = LOCO_NAME
    ET.SubElement(ch, "icon", {"src": LOCO_LOGO})

    def emit(start_utc, stop_utc, p):
        np_ = ET.SubElement(tv, "programme", {
            "start": start_utc.strftime("%Y%m%d%H%M%S") + " +0000",
            "stop":  stop_utc.strftime("%Y%m%d%H%M%S") + " +0000",
            "channel": LOCO_ID})
        for child in p:
            if child.tag in ("title", "sub-title", "desc", "date", "category", "icon", "episode-num"):
                np_.append(ET.fromstring(ET.tostring(child)))

    if items and utc(items[-1][1]) > now + timedelta(hours=24):
        for a, b, p in items:                          # feed is current: use its dates
            emit(utc(a), utc(b), p)
        print(f"  Locomotion: live guide is current - {len(items)} programmes")
        return tv

    # stale: take the latest complete day and repeat it daily
    last_day = items[-1][0].date()
    day = [(a, b, p) for a, b, p in items if a.date() == last_day]
    if sum((b - a).total_seconds() for a, b, _ in day) < 20 * 3600:   # partial last day
        prev = last_day - timedelta(days=1)
        day = [(a, b, p) for a, b, p in items if a.date() == prev] or day
    base = day[0][0].replace(hour=0, minute=0, second=0)
    template = [(a - base, b - base, p) for a, b, p in day]
    first = (now + off).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    n = 0
    for d in range(LOCO_DAYS + 1):
        day0 = first + timedelta(days=d)
        for sa, sb, p in template:
            emit(utc(day0 + sa), utc(day0 + sb), p); n += 1
    print(f"  Locomotion: guide is stale (last day {last_day}) - looped its "
          f"{len(template)}-slot day for {LOCO_DAYS + 1} days at UTC{LOCO_UTC_OFFSET_H:+d} ({n} programmes)")
    return tv

def build_m3u():
    lines = [f'#EXTM3U url-tvg="{EPG_RAW_URL}"', ""]
    disp = {v[0]: v[1] for v in WL_CHANNEL_MAP.values()}
    for local_id in WL_M3U_ORDER:
        lines.append(
            f'#EXTINF:-1 group-title="whiplash" tvg-id="{local_id}" '
            f'tvg-logo="{WL_LOGOS[local_id]}",{disp[local_id]}')
        lines.append(WL_STREAMS[local_id])
    return "\n".join(lines) + "\n"

def indent(elem, level=0):
    i = "\n" + level * "  "
    if len(elem):
        if not elem.text or not elem.text.strip(): elem.text = i + "  "
        for child in elem:
            indent(child, level + 1)
            if not child.tail or not child.tail.strip(): child.tail = i + "  "
        if not elem[-1].tail or not elem[-1].tail.strip(): elem[-1].tail = i
    else:
        if level and (not elem.tail or not elem.tail.strip()): elem.tail = i

EPG_OUTPUT = "Whiplash.xml"          # renamed from epg.xml (same tvg-ids)
M3U_OUTPUT = "playlist.m3u"
EPG_RAW_URL = "https://raw.githubusercontent.com/s-digweed/Whiplash/main/Whiplash.xml"

# ── Webchnl dummies (webchnldummies.xml) ──────────────────────────────────────
# Guide for the Webchnl playlist's dummy channels: the "##### GROUP #####" separators
# that head each group in TiviMate (guide row just repeats the name) plus simple
# filler channels like Aurora. tvg-id here = tvg-id in webchnl.m3u.
DUMMIES_OUTPUT = "webchnldummies.xml"
SEPARATORS = [
    ("SEP.Webchnl",   "##### WEBCHNL #####",       "https://i.imgur.com/SGcDJuQ.png"),
    ("SEP.Vueue",     "##### VUEUE #####",         "https://i.imgur.com/EPyPPzh.png"),
    ("SEP.RetroTV",   "##### RETRO TV #####",      "https://i.imgur.com/LT3nBqW.png"),
    ("SEP.MistLive",  "##### MIST LIVE #####",     "https://i.imgur.com/vZhDOlD.png"),
    ("SEP.40Media",   "##### 40MEDIA GROUP #####", "https://i.imgur.com/Q9lOrSk.png"),
    ("SEP.Others",    "##### OTHERS #######",      "https://i.imgur.com/6UZEDRL.png"),
    ("SEP.Kift",      "##### KIFT #####",          "https://i.imgur.com/TZQU11E.png"),
]
# (tvg-id, display name, logo, title, description, block hours)
DUMMY_CHANNELS = [
    ("Aurora", "Aurora", "https://i.imgur.com/KHHOX8e.png", "Aurora",
     "Aurora is a family channel broadcasting cartoons, anime and live-action films every weekend.", 12),
    ("flashBAK", "flashBAK", "https://i.imgur.com/AWzw5cX.png", "flashBAK",
     "Take a break from the modern world. Come say hello again to the shows, movies, and songs that defined your childhood.", 12),
    ("TLPlus", "TL+", "https://i.imgur.com/TbL9E10.png", "TL+",
     "Somos un canal de Televisión online de origen salvadoreño con programación de contenido anime para toda América Latina.", 12),
    ("Magical", "Magical", "https://i.imgur.com/NhU22Zs.png", "Magical",
     "Magical celebrates several anime genres from magical girl to idol culture.", 12),
    ("OptiradioTV", "Optiradio TV", "https://i.imgur.com/7BSz9wO.png", "Optiradio TV",
     "Optiradio TV", 12),
    ("DIJIT", "DIJIT", "https://i.imgur.com/7h0QRo9.png", "DIJIT",
     "Accept no imitation.", 12),
    ("Dotwave", "Dotwave", "https://i.imgur.com/GpmmAxf.png", "Dotwave",
     "Canada's spot for comedy. From sitcoms like Fresh Prince to reality comedies like Kenny vs. Spenny.\n\nWe're comedy on the dot.", 12),
    ("NICAPlus", "NICA+", "https://i.imgur.com/05LqN3Q.png", "NICA+",
     "El primer canal online en Nicaragua trayendo la mejor programación Geek hacia los chavalos nicaragüenses con un gran orgullo pinolero.", 12),
    ("JNIME", "JNIME", "https://i.imgur.com/kVC9ASD.png", "JNIME",
     "it feels right at home.", 12),
    ("WYLP", "WYLP", "https://i.imgur.com/yzydOWR.png", "WYLP 5",
     "WYLP, also known as Channel 5, is an independent low-power analog television station in York, Pennsylvania.\n\nThe station primarily broadcasts sitcoms from the 1980s and 1990s, cartoons and movies, in the structure of a mid-to-late 90s independent TV station.", 12),
    ("OTVN", "OTVN", "https://i.imgur.com/Tx28h4X.png", "OTVN",
     "When the other toon channels weren't enough for some reason, you find yourself buried so deep in video tapes that you'd have to get someone to do some shoveling to get you out.", 12),
    ("CrossRift", "CrossRift", "https://i.imgur.com/bLuLNHs.png", "CROSSRIFT IS OFFLINE. PLEASE STAND BY.",
     "CrossRift is an animation channel all about serving obscure cartoons by day and obscure anime by night. "
     "Comes with morning video game music through V.G.R (Video Game Radio) and nightly live action programming "
     "through Terra.\n\nSurfing through the Obscure.", 12),
    ("TelePosadas", "Tele Posadas", "https://i.imgur.com/XJD65oi.png", "Tele Posadas",
     "Entretenimiento y películas en vivo desde Posadas, Misiones.", 12),
    ("ObjectCentral", "Object Central", "https://i.imgur.com/Sm3bF3R.png", "Object Central",
     "A TV Channel for the osc!", 12),
    ("EnerGeek", "EnerGeek", "https://i.imgur.com/clbbkA3.png", "EnerGeek",
     "El canal insignia de la familia EnerGeek te transporta a través de un viaje nostálgico con una cuidadosa selección de series y películas icónicas de las décadas de los 80, 90 y 2000. Además, ofrece un contenido original producido por su talentoso equipo, garantizando una experiencia única. Prepárate para sumergirte en una emocionante programación que te hará revivir momentos inolvidables y despertarán una nostalgia inigualable.", 12),
    ("EnerGeekRadio", "EnerGeek Radio", "https://i.imgur.com/2PQ1wFT.png", "EnerGeek Radio",
     "Canal parte de la familia EnerGeek, donde podrás explorar un poco de la cultura chilena mientras te deleitas con las mejores melodías de K-Pop, J-Pop y J-Rock. Además, no te pierdas la oportunidad de disfrutar de la producción de algunos de nuestros programas originales. ¡Una experiencia única que combina lo mejor de la música y la diversidad cultural!", 12),
    ("EnerGeekFan", "EnerGeek Fan", "https://i.imgur.com/spdqBo8.png", "EnerGeek Fan",
     "Este canal, parte de la familia EnerGeek, ofrece una programación diversa las 24 horas del día. Desde emocionantes caricaturas hasta cautivadoras series de animación japonesa, pasando por vibrantes series live-action y divertidas sitcoms, es el destino perfecto para todos los amantes del entretenimiento.", 12),
]
DUMMY_DAYS = 14

def build_dummies():
    tv = ET.Element("tv", {"generator-info-name": "webchnl-dummies"})
    day0 = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    rows = [(cid, name, logo, name, None, 24) for cid, name, logo in SEPARATORS] + DUMMY_CHANNELS
    for cid, name, logo, *_ in rows:
        ch = ET.SubElement(tv, "channel", {"id": cid})
        ET.SubElement(ch, "display-name").text = name
        if logo:
            ET.SubElement(ch, "icon", {"src": logo})
    for cid, _, _, title, desc, hours in rows:
        t, end = day0, day0 + timedelta(days=DUMMY_DAYS + 1)
        while t < end:
            pr = ET.SubElement(tv, "programme", {"start": t.strftime("%Y%m%d%H%M%S +0000"),
                                                 "stop": (t + timedelta(hours=hours)).strftime("%Y%m%d%H%M%S +0000"),
                                                 "channel": cid})
            ET.SubElement(pr, "title", {"lang": "en"}).text = title
            if desc:
                ET.SubElement(pr, "desc", {"lang": "en"}).text = desc
            t += timedelta(hours=hours)
    return tv

def build_all(wl_root, eel_root, cache):
    tv = ET.Element("tv", {"generator-info-name": "whiplash-eel-epg-generator"})
    add_channels(tv, WL_CHANNEL_MAP, WL_LOGOS)
    add_channels(tv, EEL_CHANNEL_MAP, EEL_LOGOS)
    print("Whiplash:"); copy_whiplash(tv, wl_root)
    print("Eel:");      build_eel(tv, eel_root, cache)
    return tv

def main():
    cache = load_desc_cache()
    if not TMDB_KEY:
        print("NOTE: TMDB_API_KEY not set - descriptions skipped this run.")
    print(f"Fetching {WL_URL} ...");  wl_root  = fetch_xml_safe(WL_URL, "Whiplash")
    print(f"Fetching {EEL_URL} ..."); eel_root = fetch_xml_safe(EEL_URL, "Eel")
    tv = build_all(wl_root, eel_root, cache)
    indent(tv)
    ET.ElementTree(tv).write(EPG_OUTPUT, encoding="UTF-8", xml_declaration=True)
    with open(M3U_OUTPUT, "w", encoding="utf-8") as f: f.write(build_m3u())
    print(f"Wrote {EPG_OUTPUT} and {M3U_OUTPUT}")
    print(f"Channels: {len(tv.findall('channel'))}, programmes: {len(tv.findall('programme'))}")

    print(f"Fetching {MIST_URL} ...")
    mist_root = fetch_mist(MIST_URL)
    if mist_root.findall("channel") or mist_root.findall("programme"):
        print("MistLive:"); mtv = build_mist(mist_root, cache)
        indent(mtv)
        ET.ElementTree(mtv).write(MIST_OUTPUT, encoding="UTF-8", xml_declaration=True)
        print(f"Wrote {MIST_OUTPUT}")
        print(f"Channels: {len(mtv.findall('channel'))}, programmes: {len(mtv.findall('programme'))}")
    else:
        print(f"MistLive feed empty/unavailable - keeping existing {MIST_OUTPUT} unchanged")

    ccn_s, ccn_e   = _rolling(CCN_DAYS)
    nick_s, nick_e = _rolling(NICK_DAYS)
    tsd_s, tsd_e   = _rolling(TSD_DAYS)
    rb_s, rb_e     = _rolling(RB_DAYS, "America/New_York")
    # CCN: prefer the live site lineup; keep ccn_schedule.json in step as the fallback
    ccn_live = fetch_ccn_live()
    if ccn_live:
        try:
            old = load_weekly_schedule(CCN_SCHED_FILE) or {}
        except Exception:
            old = {}
        changed = sum(1 for d in ccn_live for row in ccn_live[d] if row not in (old.get(d) or []))
        if changed:
            with open(CCN_SCHED_FILE, "w", encoding="utf-8") as f:
                json.dump(ccn_live, f, ensure_ascii=False, indent=1)
            print(f"  CCN live: lineup CHANGED on the site ({changed} programme slots) - ccn_schedule.json updated")
        else:
            print("  CCN live: lineup read from the site, no change")
        unmapped = sorted({t for d in ccn_live for _, _, t in ccn_live[d]} - {t for d in old for _, _, t in old[d]})
        if unmapped:
            print("  CCN live: new titles (add to CCN_RENAMES if they need a cleaner name): " + "; ".join(unmapped))
    for out, cid, nm, logo, sfile, end, start, tz in (
        (CCN_OUTPUT,  CCN_ID,  CCN_NAME,  CCN_LOGO,  CCN_SCHED_FILE,  ccn_e,  ccn_s,  _central_to_utc),
        (NICK_OUTPUT, NICK_ID, NICK_NAME, NICK_LOGO, NICK_SCHED_FILE, nick_e, nick_s, _central_to_utc),
        (TSD_OUTPUT,  TSD_ID,  TSD_NAME,  TSD_LOGO,  TSD_SCHED_FILE,  tsd_e,  tsd_s,  _central_to_utc),
        (RB_OUTPUT,   RB_ID,   RB_NAME,   RB_LOGO,   RB_SCHED_FILE,   rb_e,   rb_s,   _eastern_to_utc)):
        sched = load_weekly_schedule(sfile)
        if not sched:
            continue
        print(f"{nm}:"); wtv = build_weekly(cid, nm, logo, sched, cache, end, start, tz,
                                            specials=RB_SPECIALS if cid == RB_ID else None)
        indent(wtv)
        ET.ElementTree(wtv).write(out, encoding="UTF-8", xml_declaration=True)
        print(f"Wrote {out}")
        print(f"Channels: {len(wtv.findall('channel'))}, programmes: {len(wtv.findall('programme'))}")

    print(f"Fetching Placid uploads from {PLACID_LIST_URL} ...")
    placid_src = fetch_placid_uploads()
    print("Placid:"); ptv = build_placid(placid_src)
    indent(ptv)
    ET.ElementTree(ptv).write(PLACID_OUTPUT, encoding="UTF-8", xml_declaration=True)
    print(f"Wrote {PLACID_OUTPUT}")
    print(f"Channels: {len(ptv.findall('channel'))}, programmes: {len(ptv.findall('programme'))}")

    print("40 Media Group:")
    mtv40 = build_40media()
    if mtv40 is not None:
        indent(mtv40)
        ET.ElementTree(mtv40).write(MEDIA40_OUTPUT, encoding="UTF-8", xml_declaration=True)
        print(f"Wrote {MEDIA40_OUTPUT}")
        print(f"Channels: {len(mtv40.findall('channel'))}, programmes: {len(mtv40.findall('programme'))}")

    print(f"Fetching {LOCO_URL} ...")
    loco_src = fetch_xml_safe(LOCO_URL, "Locomotion", headers=BROWSER_HEADERS)
    print("Locomotion:"); ltv = build_locomotion(loco_src)
    if ltv is not None:
        indent(ltv)
        ET.ElementTree(ltv).write(LOCO_OUTPUT, encoding="UTF-8", xml_declaration=True)
        print(f"Wrote {LOCO_OUTPUT} ({len(ltv.findall('programme'))} programmes)")
    else:
        print(f"  Locomotion feed empty/unavailable - keeping existing {LOCO_OUTPUT}")

    print("Webchnl dummies:")
    dtv = build_dummies()
    indent(dtv)
    ET.ElementTree(dtv).write(DUMMIES_OUTPUT, encoding="UTF-8", xml_declaration=True)
    print(f"Wrote {DUMMIES_OUTPUT} ({len(dtv.findall('channel'))} channels, {len(dtv.findall('programme'))} programmes)")

    save_desc_cache(cache)

if __name__ == "__main__":
    main()
