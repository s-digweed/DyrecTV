#!/usr/bin/env python3
"""Parsers for the authoritative episode guides saved as .mht (IMDb / TMDB /
Wikipedia / Fandom). Each parser returns an ordered list of episodes:
    [(season:int, ep:int, title:str, synopsis:str), ...]
sorted by (season, ep). The feed's absolute episodeNumber indexes into this
list (1-based), which is exactly what TMDB's segment-splitting gets wrong.
Pure stdlib."""
import email, re, html as _html, glob, os
from email import policy

def load_html(path):
    with open(path, 'rb') as f:
        msg = email.message_from_binary_file(f, policy=policy.default)
    parts = []
    for part in msg.walk():
        if part.get_content_type() == 'text/html':
            payload = part.get_payload(decode=True)
            cs = part.get_content_charset() or 'utf-8'
            try:
                parts.append(payload.decode(cs, 'replace'))
            except Exception:
                parts.append(payload.decode('utf-8', 'replace'))
    return "\n".join(parts)

def strip_tags(s):
    s = re.sub(r'<br\s*/?>', ' ', s, flags=re.I)
    s = re.sub(r'<[^>]+>', '', s)
    s = _html.unescape(s)
    s = s.replace('’', "'").replace('‘', "'")
    s = s.replace('“', '"').replace('”', '"')
    s = s.replace('–', '-').replace('—', '-')
    s = re.sub(r'\s+', ' ', s)
    return s.strip()

# ───────────────────────── IMDb ─────────────────────────
def parse_imdb(folder):
    eps = {}
    for p in sorted(glob.glob(os.path.join(folder, '*IMDb*.mht'))):
        h = load_html(p)
        marker = re.compile(r'<div class="ipc-title__text">S(\d+)\.E(\d+)')
        chunks = marker.split(h)
        # chunks: [pre, s, e, body, s, e, body, ...]
        for i in range(1, len(chunks), 3):
            s = int(chunks[i]); e = int(chunks[i+1]); body = chunks[i+2]
            mt = re.match(r'\s*[∙·•]?\s*(.*?)</div>', body, re.S)
            title = strip_tags(mt.group(1)) if mt else ""
            syn = ""
            ms = re.search(r'ipc-html-content-inner-div[^>]*>(.*?)</div>', body, re.S)
            if ms:
                syn = strip_tags(ms.group(1))
                if syn.lower().startswith('know what this is about'):
                    syn = ""
            if (s, e) not in eps or (not eps[(s, e)][1] and syn):
                eps[(s, e)] = (title, syn)
    return [(s, e, t, d) for (s, e), (t, d) in sorted(eps.items())]

# ───────────────────────── TMDB ─────────────────────────
def parse_tmdb(folder):
    eps = {}
    for p in sorted(glob.glob(os.path.join(folder, '*TMDB*.mht'))):
        h = load_html(p)
        for m in re.finditer(r'season/(\d+)/episode/(\d+)"\s+title="[^"]*?Episode \d+ - ([^"]*)">', h):
            s = int(m.group(1)); e = int(m.group(2)); nm = strip_tags(m.group(3))
            tail = h[m.end():m.end()+3000]
            ov = re.search(r'<div class="overview"[^>]*>\s*<p>(.*?)</p>', tail, re.S)
            ovt = strip_tags(ov.group(1)) if ov else ""
            if "don't have an overview" in ovt or "Help us expand" in ovt:
                ovt = ""
            if (s, e) not in eps or (not eps[(s, e)][1] and ovt):
                eps[(s, e)] = (nm, ovt)
    return [(s, e, t, d) for (s, e), (t, d) in sorted(eps.items())]

# ─────────────────────── Wikipedia ───────────────────────
def _season_from_name(fn):
    m = re.search(r'season\s+(\d+)', fn, re.I)
    return int(m.group(1)) if m else None

def parse_wiki(folder, season_in_filename=True):
    """Standard Wikipedia episode table. Each <tr> for an episode has a
    th scope=row (overall#), a first <td> (within-season #), td.summary (title),
    and a following tr with td.description (synopsis). Returns (overall#, s, e,
    title, syn) collapsed into the (s,e,title,syn) list — but we also stash the
    overall# as the 'ep' when season isn't resolvable, so callers can choose."""
    rows_out = {}
    files = sorted(glob.glob(os.path.join(folder, '*Wikipedia*.mht')))
    for p in files:
        h = load_html(p)
        season = _season_from_name(os.path.basename(p)) if season_in_filename else None
        # Each episode = a tr with class="summary" somewhere; description is in the next tr.
        trs = re.findall(r'<tr[^>]*>.*?</tr>', h, re.S)
        pending = None
        for tr in trs:
            summ = re.search(r'class="summary"[^>]*>(.*?)</td>', tr, re.S)
            if summ:
                overall = re.search(r'<th[^>]*scope="row"[^>]*>(.*?)</th>', tr, re.S)
                tds = re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S)
                overall_n = strip_tags(overall.group(1)) if overall else (strip_tags(tds[0]) if tds else "")
                # within-season number: the td right before the summary, else overall
                within = ""
                # find index of summary td
                for j, td in enumerate(tds):
                    if 'class="summary"' in ('class="summary"' if summ.group(0) in td else ''):
                        pass
                # simpler: within-season = first numeric td that isn't the overall
                nums = [strip_tags(td) for td in tds if re.match(r'^\s*\d+\s*$', strip_tags(td))]
                if overall and nums:
                    within = nums[0]
                elif len(nums) >= 2:
                    within = nums[1]; overall_n = nums[0]
                elif nums:
                    within = nums[0]
                title = strip_tags(summ.group(1)).strip('"')
                pending = {"overall": overall_n, "within": within, "title": title, "syn": ""}
                rows_out[(season, overall_n, within, title)] = pending
            else:
                desc = re.search(r'class="description"[^>]*>(.*?)</td>', tr, re.S)
                if desc and pending is not None:
                    pending["syn"] = strip_tags(desc.group(1))
                    pending = None
    # emit ordered by overall# (int when possible)
    def _key(k):
        season, overall, within, title = k
        try: return (int(overall),)
        except: return (10**9,)
    out = []
    for k in sorted(rows_out.keys(), key=_key):
        season, overall, within, title = k
        r = rows_out[k]
        try: e = int(r["within"]) if r["within"] else int(overall)
        except: e = 0
        try: ov = int(overall)
        except: ov = 0
        out.append((season or 0, e, r["title"], r["syn"], ov))
    return out

# ─────────────── Wikipedia combined page (season headings) ───────────────
def _split_seasons(h):
    """Yield (season_int, html_section) using 'Season N' heading anchors."""
    idxs = []
    for m in re.finditer(r'id="Season_(\d+)(?:[^"]*)"', h):
        idxs.append((int(m.group(1)), m.start()))
    # keep first occurrence per season, in order
    seen = {}
    for s, pos in idxs:
        if s not in seen:
            seen[s] = pos
    ordered = sorted(seen.items(), key=lambda kv: kv[1])
    for i, (s, pos) in enumerate(ordered):
        end = ordered[i+1][1] if i+1 < len(ordered) else len(h)
        yield s, h[pos:end]

def parse_wiki_segmented(folder):
    """Combined Wikipedia page where rows carry a within-season id like '1a',
    '1b', '4' (segment letters). Groups segments per within-season episode,
    joins titles and descriptions. Returns [(season, ep, title, syn)] in order."""
    files = sorted(glob.glob(os.path.join(folder, '*Wikipedia*.mht')))
    if not files:
        return []
    h = load_html(files[0])
    out = []
    for season, sec in _split_seasons(h):
        rows = re.findall(r'<tr[^>]*class="[^"]*vevent[^"]*"[^>]*>.*?</tr>', sec, re.S)
        epmap = {}
        order = []
        seq = 0
        for tr in rows:
            thm = re.search(r'<th[^>]*>(.*?)</th>', tr, re.S)
            if not thm:
                continue
            idraw = strip_tags(thm.group(1))
            tds = [strip_tags(td) for td in re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S)]
            def _qt(cell):
                q = re.findall(r'"([^"]+)"', cell)
                return " / ".join(q) if q else cell.strip().strip('"')
            # title = first td that is quoted; else th's quoted segments; else first non-numeric td
            title = ""
            for td in tds:
                if '"' in td:
                    title = _qt(td); break
            if not title and '"' in idraw:
                title = _qt(idraw)
            if not title:
                for td in tds:
                    if td and not re.match(r'^\d+$', td) and not re.search(r'\d{4}', td):
                        title = td.strip('"'); break
            mi = re.match(r'(\d+)\s*([a-z])$', idraw)      # th like '1a' (within# + seg)
            if mi:
                within = int(mi.group(1))
            else:
                nums = [int(t) for t in tds if re.match(r'^\d+$', t)]
                if re.match(r'^\d+$', idraw) and nums:
                    within = nums[0]                        # th=overall#, td[0]=within#
                elif re.match(r'^\d+$', idraw):
                    within = int(idraw)
                elif nums:
                    within = nums[0]
                else:
                    seq += 1; within = seq                  # th=title, no number (Angry Beavers)
                    title = title or idraw.strip('"')
            syn = ""
            dm = re.search(r'class="description"[^>]*>(.*?)</td>', tr, re.S)
            if dm:
                syn = re.sub(r'^.*?shortSummaryText[^>]*>', '', dm.group(1), flags=re.S)
                syn = strip_tags(syn)
            if within not in epmap:
                epmap[within] = {"titles": [], "syns": []}
                order.append(within)
            if title and title not in epmap[within]["titles"]:
                epmap[within]["titles"].append(title)
            if syn:
                epmap[within]["syns"].append(syn)
        rank = 0
        for epn in order:
            r = epmap[epn]
            t = " / ".join(r["titles"])
            if epn > 200 or t.strip() in ("", "TBA", "TBD", "N/A"):
                continue                                   # drop garbage/unaired rows
            rank += 1                                       # within-season E by broadcast order
            out.append((season, rank, t, " / ".join(r["syns"])))
    return out

