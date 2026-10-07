#!/usr/bin/env python3
"""Build episode_guide.json — the authoritative, static episode guide for the
Snick / Toonami-Aftermath shows whose names + S/E TMDB resolves WRONG (it splits
segmented episodes). Parsed from the IMDb / TMDB / Wikipedia / Fandom pages Taqi
saved under 'Snick and Toonami Aftermath/', with his per-show rules applied.

Guide shape:
  { norm(display): { "display": <name>, "episodes": { "<absN>": {se,sub,desc} } } }
where absN is the feed's absolute episodeNumber (1-based broadcast order), se is
[season, ep] or null (null = don't set an S/E badge, keep whatever resolved),
sub is the episode subtitle ("" = omit), desc the synopsis ("" = fall back).

Run:  python3 build_guide.py "<zip root>/Snick and Toonami Aftermath" episode_guide.json
"""
import sys, os, re, json, difflib
import tam_parsers as P

# ───────────────────────── normalization ─────────────────────────
def norm_key(s):
    return re.sub(r'[^a-z0-9]+', ' ', (s or '').lower()).strip()

_WORDNUM = {'one':1,'two':2,'three':3,'four':4,'five':5,'six':6,'seven':7,
            'eight':8,'nine':9,'ten':10,'eleven':11,'twelve':12}
def _partnum(tok):
    tok = tok.strip()
    if tok.isdigit():
        return int(tok)
    return _WORDNUM.get(tok.lower())

def fix_parts(t):
    # Standardize every trailing part suffix to "<title>: Part N" (arabic). Handles
    # "Title (2)", "Title, Part 2", "Title: Part 2", "Title - Part 2",
    # "Title Part 2", "Title: Part Two". Only a trailing "Part <n>" is touched, so
    # a mid-title word "Part" (e.g. "The Best Part of...") is left alone.
    m = re.search(r'^(.*?)\s*\((\d+)\)\s*$', t)
    if m:
        return f"{m.group(1).strip()}: Part {int(m.group(2))}"
    m = re.search(r'^(.*?)[\s,:/–-]+Part\s+([0-9]+|[A-Za-z]+)\s*$', t, re.I)
    if m:
        n = _partnum(m.group(2))
        if n is not None:
            return f"{m.group(1).strip()}: Part {n}"
    return t

def fix_segspace(t):
    # segments / synopsis slashes -> " / "
    return re.sub(r'\s*/\s*', ' / ', t)

def tidy(t):
    return re.sub(r'\s+', ' ', t or '').strip()

PLACEHOLDER = re.compile(r'(?i)^(episode|ep|show)\s*#?\s*\d')
def is_placeholder(t):
    t = (t or '').strip()
    return (not t) or t.lower() in ('unknown', 'tba', 'tbd', 'n/a') or bool(PLACEHOLDER.match(t))

def clean_title(t, segments, parts, ft_fix=False, guest_fix=False):
    t = tidy(t)
    if parts:
        t = fix_parts(t)
    if segments:
        t = fix_segspace(t)
    if ft_fix:
        t = re.sub(r'\bft\b(?!\.)', 'ft.', t)
    if guest_fix:
        t = re.sub(r'(?<=\S)\(Guest', ' (Guest', t)
    return tidy(t)

def clean_syn(d, slashspace, synseg=False):
    d = tidy(d)
    if slashspace and '/' in d:
        d = fix_segspace(d)
    if synseg:
        # two segment synopses concatenated with no space ("end.Next" -> "end. / Next")
        d = re.sub(r'(?<=[a-z])\.(?=[A-Z])', '. / ', d)
    return d

# ───────────────────────── generic synopses ─────────────────────────
GEN = {
    "Figure It Out": "A group of four different panelists popular on Nickelodeon programs try to figure out the talents of different guests. They are given clues that they can feel, see, taste, and also given to them through charades.",
    "Finders Keepers (1987)": "Contestants try to find pictures in a hidden picture puzzle, then go on a scavenger hunt throughout an eight-room house.",
    "Mr. Wizard's World": "Mr. Wizard and his young friends conduct a variety of science experiments.",
    "You Can't Do That on Television (1979)": "Sketch-TV by young amateur actors in true classic Nick-style. Whatever you do, never ask for water or admit that you don't know.",
    "Nickelodeon GUTS": "Three kids donning the different colors blue, red, and purple compete in relatively cool-looking olympic-style games to achieve as many points as they can.",
    "Double Dare (1986)": "Two-member teams of children compete to answer questions and complete stunts.",
    "Get the Picture (1991)": "Two teams answer questions and play games for the opportunity to guess what the picture is for each amount of money.",
    "You're On!": "A team of kids go out to win prizes by convincing passersby to do one of three crazy things. If they get all three, they win a prize. In the studio, people randomly picked from the audience guess how many of the three challenges each team will achieve.",
}

# ───────────────────────── per-show config ─────────────────────────
# source: imdb | tmdb | wiki | wiki_seg | fandom | synthetic
SHOWS = {
  "Aaahh!!! Real Monsters":      dict(folder="Aaahh!!! Real Monsters", source="imdb", seg=True, slash=True, synseg=True),
  "Alfred Hitchcock Presents":   dict(folder="Alfred Hitchcock Presents", source="imdb"),
  "Beetlejuice":                 dict(folder="Beetlejuice", source="imdb", seg=True, slash=True, synseg=True),
  "CatDog":                      dict(folder="CatDog", source="imdb", seg=True, slash=True, synseg=True),
  "Clarissa Explains It All":    dict(folder="Clarissa Explains It All", source="imdb"),
  "Doug":                        dict(folder="Doug", source="imdb", seg=True, slash=True, synseg=True),
  "Garfield and Friends":        dict(folder="Garfield and Friends", source="imdb", seg=True, slash=True, synseg=True),
  "Hey Arnold!":                 dict(folder="Hey Arnold!", source="imdb", seg=True, slash=True, synseg=True),
  "Little Bear":                 dict(folder="Little Bear", source="imdb", seg=True, slash=True, synseg=True),
  "Rocko's Modern Life":         dict(folder="Rocko's Modern Life", source="imdb", seg=True, slash=True, synseg=True),
  "Rugrats":                     dict(folder="Rugrats", source="imdb", seg=True, slash=True, synseg=True),
  "The Busy World of Richard Scarry": dict(folder="The Busy World of Richard Scarry", source="imdb", seg=True, slash=True, synseg=True),
  "The Ren & Stimpy Show":       dict(folder="The Ren & Stimpy Show", source="imdb", seg=True, slash=True, synseg=True),
  "The Adventures of Tintin":    dict(folder="The Adventures of Tintin", source="imdb", parts=True),
  "The Adventures of Rocky and Bullwinkle and Friends": dict(folder="The Adventures of Rocky and Bullwinkle and Friends", source="imdb", seg=True, slash=True),
  "Mr. Wizard's World":          dict(folder="Mr. Wizard's World", source="imdb",
                                      generic="Mr. Wizard's World",
                                      force_generic=[(2,4),(3,1),(3,15),(4,2),(5,3)]),
  "Weinerville":                 dict(folder="Weinerville", source="imdb", wiki_desc_fallback=True),
  "Figure It Out":               dict(folder="Figure It Out", source="imdb", seg=True,
                                      omit_placeholder=True, generic="Figure It Out"),
  "You Can't Do That on Television (1979)": dict(folder="You Can't Do That on Television", source="imdb",
                                      wiki_name_fallback=True, generic="You Can't Do That on Television (1979)",
                                      pins={6:{"se":[1,6],"sub":"The Toronto Producers","desc":"Tim is in the dungeon and is told not to pull the chains. He does not listen and gets slimed."},
                                            7:{"se":[1,7],"sub":"St. Patrick's Day","desc":"On St. Patrick's Day, amid disco-dancing finalists, call-in contests, and community announcements, Lisa sets out to get Bradfield wearin' green--slime, that is."}}),
  "Space Ghost Coast to Coast":  dict(folder="Space Ghost Coast to Coast", source="imdb",
                                      omit_placeholder=True, sg_wiki_match=True,
                                      pins={91:{"se":[8,5]}}),
  # Segmented cartoons now with IMDb (v2) — real S/E + synopses
  "Alvin and the Chipmunks (1983)": dict(folder="Alvin and the Chipmunks", source="imdb", seg=True, slash=True, synseg=True),
  "The Angry Beavers":           dict(folder="Angry Beavers", source="imdb", seg=True, slash=True, synseg=True),
  "KaBlam!":                     dict(folder="KaBlam", source="imdb"),
  "Celebrity Deathmatch":        dict(folder="Celebrity Deathmatch", source="imdb", parts=True),
  "Welcome Back, Kotter":        dict(folder="Welcome Back Kotter", source="imdb", parts=True),
  # Toonami anime / DC — named shows the feed couldn't S/E; guide by absolute #
  "Digimon Adventure (1999)":    dict(folder="Digimon Adventure 1999", source="tmdb"),
  "Initial D: First Stage":      dict(folder="Initial D First Stage", source="imdb"),
  # v3 batch
  "The Tick (1994)":             dict(folder="The Tick 1994", source="imdb"),
  "Are You Afraid of the Dark? (1990)": dict(folder="Are You Afraid of the Dark", source="imdb"),
  "Nickelodeon GUTS":            dict(folder="Nickelodeon GUTS", source="imdb",
                                      generic="Nickelodeon GUTS", omit_placeholder=True),
  "Get the Picture (1991)":      dict(folder="Get the Picture (1991)", source="imdb",
                                      generic="Get the Picture (1991)"),
  "Double Dare (1986)":          dict(folder="Double Dare (1986)", source="imdb",
                                      generic="Double Dare (1986)", all_generic=True),
  "You're On!":                  dict(source="synthetic", seasons=[26],
                                      omit_names=True, generic="You're On!", all_generic=True,
                                      pins={1:  {"sub": "Take a Bow Mr. Shumway"},
                                            25: {"desc": "Two sisters Amber and Ashley McKeen must get total strangers to help them get items out of a trash can and eat something from the trash!"}}),
  "Maya the Bee (1975)":         dict(folder="Maya the Bee", source="maya"),
  "Justice League":              dict(folder="Justice League", source="imdb"),   # IMDb already ": Part II" (roman) — leave as-is
  "Yu-Gi-Oh!":                   dict(folder="Yu-Gi-Oh", source="imdb", parts=True),
  # Wikipedia
  "All That":                    dict(folder="All That", source="wiki", seg=True, slash=True, ft_fix=True),
  # TMDB
  "Noozles":                     dict(folder="Nozzles", source="tmdb", txt_desc="Nozzles/Noozles_S01_EPG.txt",
                                      name_override={(1,20):"The Magical Vacations"}),
  "Grimm's Fairy Tale Classics": dict(folder="Grimm's Fairy Tale Classics", source="tmdb", parts=True),
  "The Mysterious Cities of Gold": dict(folder="The Mysterious Cities of Gold", source="tmdb", parts=True),
  "The Littl' Bits":             dict(folder="Littl' Bits", source="tmdb", parts=True),
  "Wild & Crazy Kids":           dict(folder="Wild & Crazy Kids", source="tmdb"),
  # Fandom
  "What Would You Do? (1991)":   dict(folder="What Would You Do", source="fandom",
                                      guest_fix=True, se_from_source=True,
                                      pins={48:{"se":[2,13],"sub":"Sumo Wrestling, Belly Dancing and Pizza Making","desc":"Two contestants wearing fat suits face off in a sumo wrestling match. Pizza-maker Tommy joins two contestants in a pizza-making contest. A belly dancer performs; she teaches a father and daughter, who perform later on. Another game has two contestants trying to shoot pie goop at Marc's photo on two other contestants' hats. The final game, Double Shot, has a contestant try to pour two drinks in his mouth at the same time."},
                                            49:{"se":[2,14],"sub":"Use Your Senses","desc":"The first game is guessing the animal word from watermelon rinds. A remote segment involves random people reviewing What Would You Do? fragrances. Then Marc quizzes two audience members on two actors that interrupted the show. The final game is \"Anything You Can Do\", where a father and son compete in throwing a Frisbee through a hoop."},
                                            50:{"sub":"Using Your Senses"}}),
  # Synthetic (no source files)
  "Finders Keepers (1987)":      dict(source="synthetic", seasons=[13,13,13,13,13,8],
                                      omit_names=True, generic="Finders Keepers (1987)"),
}

def build_list(cfg, base):
    src = cfg["source"]
    folder = os.path.join(base, cfg["folder"]) if cfg.get("folder") else None
    if src == "imdb":
        return [(s,e,t,d) for (s,e,t,d) in P.parse_imdb(folder)]
    if src == "tmdb":
        return [(s,e,t,d) for (s,e,t,d) in P.parse_tmdb(folder)]
    if src == "wiki":
        return [(s,e,t,d) for (s,e,t,d,ov) in P.parse_wiki(folder)]
    if src == "wiki_seg":
        return P.parse_wiki_segmented(folder)
    if src == "fandom":
        return parse_fandom(folder)
    if src == "synthetic":
        out=[]
        for si,n in enumerate(cfg["seasons"],1):
            for e in range(1,n+1):
                out.append((si,e,"",""))
        return out
    return []

def parse_fandom(folder):
    """Nickstory Fandom 'What Would You Do?' page: table 0 = Season 1 (has an
    epnum column 1..35), table 1 = Season 2 (prodcode only -> sequential). The
    feed's absolute number runs S1 then S2, so #48 -> S2 E13 (S1 has 35)."""
    import glob
    f = sorted(glob.glob(os.path.join(folder,'*Fandom*.mht')))
    if not f: return []
    h = P.load_html(f[0])
    tables = re.findall(r'<table.*?</table>', h, re.S)
    out=[]
    season=0
    for t in tables:
        body=[tr for tr in re.findall(r'<tr[^>]*>.*?</tr>', t, re.S) if re.search(r'<td', tr)]
        if len(body) < 5:
            continue
        season += 1
        pos=0
        for tr in body:
            cells=[P.strip_tags(c) for _,c in re.findall(r'<(td|th)[^>]*>(.*?)</\1>', tr, re.S)]
            title=""
            for c in cells:
                if c and not re.match(r'^\d+$', c):
                    title=c; break
            if not title:
                continue
            pos += 1
            nums=[int(c) for c in cells if re.match(r'^\d+$', c)]
            within = nums[-1] if (season==1 and nums) else pos
            out.append((season, within, title, ""))
    return out

def wiki_name_map(folder):
    """(s,e)->title and a list of (title,syn) for fuzzy matching."""
    rows = P.parse_wiki(folder) if os.path.isdir(folder) else []
    se={}; pairs=[]
    for (s,e,t,d,ov) in rows:
        if s and e: se[(s,e)]=t
        if t: pairs.append((t,d))
    return se, pairs

def wiki_seg_syn(folder):
    return P.parse_wiki_segmented(folder)

def build_maya(cfg, base, display):
    """Maya the Bee: titles + S/E from TMDB (S1 1-52, S2 1-52 -> absolute 1-104),
    'Maja'->'Maya' fixed. Synopsis: TMDB for abs 1-19, the Fandom PDF
    (maya_fandom_syn.json, keyed by overall #) for abs 20-104."""
    folder = os.path.join(base, cfg["folder"])
    tmdb = sorted(P.parse_tmdb(folder), key=lambda r: (r[0], r[1]))
    fsyn = {}
    try:
        fsyn = json.load(open(os.path.join(folder, "maya_fandom_syn.json"), encoding="utf-8"))
    except Exception:
        pass
    eps = {}
    for absn, (s, e, title, tsyn) in enumerate(tmdb, 1):
        t = re.sub(r'\bMaja\b', 'Maya', tidy(title))
        desc = tsyn if absn <= 19 else fsyn.get(str(absn), "")
        desc = re.sub(r'\bMaja\b', 'Maya', tidy(desc))
        eps[str(absn)] = {"se": [s, e], "sub": t, "desc": desc}
    return {"display": display, "episodes": eps}

def main():
    base = sys.argv[1]
    outpath = sys.argv[2]
    guide={}
    report=[]
    for display, cfg in SHOWS.items():
        if cfg["source"] == "maya":
            g = build_maya(cfg, base, display)
            guide[norm_key(display)] = g
            report.append(f"   {display}: {len(g['episodes'])} episodes (source=maya: TMDB titles + Fandom synopses)")
            continue
        lst = build_list(cfg, base)
        if not lst and cfg["source"]!="synthetic":
            report.append(f"!! {display}: NO EPISODES PARSED ({cfg.get('folder')})")
            continue
        # Drop E0 entries: IMDb lists unaired pilots / specials as episode 0, but a
        # 24/7 linear channel never airs those, so its absolute episodeNumber starts
        # at the first AIRED episode. Keeping them would shift every number by one.
        if cfg["source"] in ("imdb", "wiki", "tmdb"):
            lst = [(s,e,t,d) for (s,e,t,d) in lst if e != 0]
        seg=cfg.get("seg",False); slash=cfg.get("slash",False)
        parts=cfg.get("parts",False); ftf=cfg.get("ft_fix",False); gf=cfg.get("guest_fix",False)
        omit_ph=cfg.get("omit_placeholder",False); omit_names=cfg.get("omit_names",False)
        generic=GEN.get(cfg.get("generic","")) if cfg.get("generic") else ""
        se_from_source=cfg.get("se_from_source",True)
        force_generic=set(tuple(x) for x in cfg.get("force_generic",[]))
        name_override=cfg.get("name_override",{})
        pins=cfg.get("pins",{})
        synseg=cfg.get("synseg",False)

        # external desc/name sources
        txt_desc={}
        if cfg.get("txt_desc"):
            txt_desc = parse_txt_desc(os.path.join(base,cfg["txt_desc"]))
        wiki_se={}; wiki_pairs=[]; wiki_td={}
        if cfg.get("wiki_desc_fallback") or cfg.get("wiki_name_fallback") or cfg.get("sg_wiki_match"):
            wfolder=os.path.join(base,cfg["folder"])
            wiki_se, wiki_pairs = wiki_name_map(wfolder)
            wiki_td = {norm_key(t): d for (t, d) in wiki_pairs if t and d}

        eps={}
        for absn,(s,e,t,d) in enumerate(lst,1):
            title = t
            desc  = d
            # name override by (s,e)
            if (s,e) in name_override:
                title = name_override[(s,e)]
            # Space Ghost S9/S10: placeholder name -> fuzzy match wiki synopsis
            if cfg.get("sg_wiki_match") and is_placeholder(title) and wiki_pairs and d:
                best=None; bestr=0.0
                for wt,wd in wiki_pairs:
                    r=difflib.SequenceMatcher(None, d.lower(), (wd or "").lower()).ratio()
                    if r>bestr: bestr=r; best=wt
                if best and bestr>=0.55:
                    title=best
            sub = clean_title(title, seg, parts, ftf, gf)
            if omit_names or is_placeholder(sub) and (omit_ph or cfg["source"]=="fandom"):
                if omit_names or omit_ph: sub=""
            # desc
            desc = clean_syn(desc, slash, synseg)
            if cfg.get("wiki_desc_fallback") and not desc:
                wd = wiki_td.get(norm_key(title)) or (wiki_se.get((s,e)) and "")
                if wd:
                    desc = clean_syn(wd, slash, synseg)
            if (s,e) in force_generic:
                desc = generic
            if cfg.get("all_generic") and generic:    # game shows: show blurb on every episode
                desc = generic
            if not desc and generic:
                desc = generic
            # S/E
            se = [s,e] if (se_from_source and s) else None
            if cfg.get("renumber"):          # IMDb numbering is broken -> S01 Exx by airing order
                se = [1, absn]
            eps[str(absn)] = {"se":se, "sub":sub, "desc":desc}
            # txt desc override by (s,e)
            if txt_desc.get((s,e)):
                eps[str(absn)]["desc"]=txt_desc[(s,e)]
        # pins override (by absolute number)
        for absn,ov in pins.items():
            cur = eps.get(str(absn), {"se":None,"sub":"","desc":""})
            if "se" in ov: cur["se"]=ov["se"]
            if "sub" in ov: cur["sub"]=ov["sub"]
            if "desc" in ov: cur["desc"]=ov["desc"]
            eps[str(absn)]=cur
        guide[norm_key(display)] = {"display":display, "episodes":eps}
        report.append(f"   {display}: {len(eps)} episodes (source={cfg['source']})")

    with open(outpath,"w",encoding="utf-8") as f:
        json.dump(guide, f, ensure_ascii=False, indent=0)
    # human review file
    rv = os.path.join(os.path.dirname(os.path.abspath(outpath)), "guide_review.txt")
    with open(rv,"w",encoding="utf-8") as f:
        f.write("# AUTHORITATIVE EPISODE GUIDE — built from your IMDb/TMDB/Wikipedia/Fandom pages.\n")
        f.write("# Feed absolute episodeNumber -> entry below. Sample rows per show:\n\n")
        for k,v in guide.items():
            eps=v["episodes"]; keys=sorted(eps,key=lambda x:int(x))
            f.write(f"== {v['display']}  ({len(eps)} eps) ==\n")
            for kk in [keys[0], keys[len(keys)//2], keys[-1]] if keys else []:
                e=eps[kk]
                f.write(f"   #{kk}: S{(e['se'] or ['-','-'])[0]}E{(e['se'] or ['-','-'])[1]} | {e['sub']!r} | {e['desc'][:60]!r}\n")
            f.write("\n")
    print("\n".join(report))
    print(f"\nWROTE {outpath}: {len(guide)} shows, {sum(len(v['episodes']) for v in guide.values())} episodes")
    print(f"WROTE {rv}")

def parse_txt_desc(path):
    """Parse 'Sxx Eyy\\n\"desc\"' blocks (Noozles txt)."""
    out={}
    try:
        txt=open(path,encoding="utf-8").read()
    except Exception:
        return out
    cur=None
    for line in txt.splitlines():
        m=re.match(r'\s*S(\d+)\s*E(\d+)\s*$', line)
        if m:
            cur=(int(m.group(1)),int(m.group(2))); continue
        s=line.strip().strip('"').strip()
        if cur and s:
            if cur not in out:          # keep the FIRST description (ignore the trailing name line)
                out[cur]=s
            cur=None
    return out

if __name__=="__main__":
    main()
