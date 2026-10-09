#!/usr/bin/env python3
"""Rebuild Rugrats + KaBlam guide entries using TMDB numbering (the order the
Toonami Aftermath feed follows), with IMDb names/synopses. See notes inline."""
import importlib.util, glob, re, os, json
spec=importlib.util.spec_from_file_location("tp","tam_parsers.py")
P=importlib.util.module_from_spec(spec); spec.loader.exec_module(P)
MON="January|February|March|April|May|June|July|August|September|October|November|December"

def parse_tmdb_segments(path):
    """Ordered list of (num, title, minutes, date) for one TMDB season page."""
    txt=P.strip_tags(P.load_html(path))
    out=[]
    for m in re.finditer(r'(\d{1,3})\s+([A-Z][^\n]{1,55}?)\s+\d+%\s+Rate it!', txt):
        n,t=m.group(1),re.sub(r'\s+',' ',m.group(2)).strip()
        if len(t)>55 or 'Season' in t or not t: continue
        tail=txt[m.end():m.end()+500]
        mm=re.search(r'•\s*(\d+)m', tail)
        mins=int(mm.group(1)) if mm else 11
        dsc=re.search(r'•\s*\d+m\s+(.+?)\s+Read More', tail)
        desc=re.sub(r'\s+',' ',dsc.group(1)).strip() if dsc else ""
        out.append((int(n),t,mins,desc))
    seen={}
    for n,t,mn,d in out:
        if n not in seen: seen[n]=(t,mn,d)
    return [(n,)+seen[n] for n in sorted(seen)]

def join_halfhours(segs):
    """Group segments into half-hours: a >=20m seg stands alone; consecutive
    short (<=15m) segs pair up (2 per half-hour). Returns (titles, descs)."""
    hh=[]; i=0
    while i<len(segs):
        n,t,mn,d=segs[i]
        if mn>=20:
            hh.append(([t],[d])); i+=1
        else:
            if i+1<len(segs) and segs[i+1][2]<=15:
                hh.append(([t,segs[i+1][1]],[d,segs[i+1][3]])); i+=2
            else:
                hh.append(([t],[d])); i+=1
    return hh

def tmdb_halfhour_sequence(tmdb_dir, seasons):
    """Return [(season, hh_index_in_season, overall, [seg_titles], [seg_descs])]."""
    seq=[]; overall=0
    for s in seasons:
        fs=glob.glob(os.path.join(tmdb_dir, f"*Season {s} *"))
        if not fs: continue
        segs=parse_tmdb_segments(fs[0])
        for idx,(titles,descs) in enumerate(join_halfhours(segs),1):
            overall+=1
            seq.append((s,idx,overall,titles,descs))
    return seq

if __name__=="__main__":
    # Rugrats self-test: where does Chicken Pops land?
    d=[x for x in glob.glob("rug_zip/*") if x.endswith("TMDB")][0]
    seq=tmdb_halfhour_sequence(d, range(1,10))
    print("total TMDB half-hours:", len(seq))
    for s,idx,ov,titles,descs in seq:
        if 63<=ov<=68: print(f"  #{ov}  S{s:02d}E{idx:02d}  {' / '.join(titles)}")

def norm(x): return re.sub(r'[^a-z0-9]','', (x or '').lower())

def imdb_halfhours(folder):
    """IMDb half-hours: list of (segset, display_name, synopsis)."""
    rows=P.parse_imdb(folder)
    out=[]
    for s,e,title,syn in rows:
        if e==0: continue
        segs=[p.strip() for p in re.split(r'\s*[/;]\s*', title) if p.strip()]
        out.append((segs, title, syn))
    return out

def build_show(tmdb_dir, imdb_folder, seasons, name_source):
    """name_source: 'imdb' (segmented) or 'tmdb'. Returns (episodes_dict, misses)."""
    seq=tmdb_halfhour_sequence(tmdb_dir, seasons)
    ihh=imdb_halfhours(imdb_folder)
    # index imdb by each normalized segment
    by_seg={}
    for segs,disp,syn in ihh:
        for sg in segs: by_seg.setdefault(norm(sg),[]).append((segs,disp,syn))
    eps={}; misses=[]
    for s,idx,ov,titles,descs in seq:
        # find imdb hh matching any tmdb segment
        match=None
        for t in titles:
            cand=by_seg.get(norm(t))
            if cand: match=cand[0]; break
        if not match:
            import difflib
            best=None;br=0
            for segs,disp,syn in ihh:
                r=max(difflib.SequenceMatcher(None,norm(t),norm(x)).ratio() for t in titles for x in segs)
                if r>br: br=r; best=(segs,disp,syn)
            if best and br>=0.8: match=best
        tmdb_name=" / ".join(titles)
        tmdb_desc=" / ".join(d for d in descs if d)
        if match:
            segs,disp,syn=match
            sub = disp if name_source=='imdb' else tmdb_name
            desc = syn or tmdb_desc
            eps[str(ov)]={"se":[s,idx],"sub":sub,"desc":desc}
        else:
            # IMDb has no entry for this half-hour -> TMDB name + TMDB synopsis
            eps[str(ov)]={"se":[s,idx],"sub":tmdb_name,"desc":tmdb_desc}
            misses.append((ov,s,idx,tmdb_name))
    return eps, misses

def build_rugrats():
    d=[x for x in glob.glob("rug_zip/*") if x.endswith("TMDB")][0]
    return build_show(d, "tam_zip/Snick and Toonami Aftermath/Rugrats", range(1,10), "imdb")
