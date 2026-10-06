#!/usr/bin/env python3
"""
Fusion EPG : panel Xtream + sources XMLTV publiques → un seul epg.xml.gz prêt pour l'appli.

Même logique que XtreamRepository.syncXmlTv (côté télé), faite une fois pour toutes :
  - priorité = ordre des sources ; une chaîne servie par une source n'est plus complétée ;
  - bouche-trou (un seul titre sur >= 4 programmes, « Cette chaîne ne fait plus partie des
    offres ») : laissé aux sources suivantes ;
  - correspondance par tvg-id (casse ignorée), sinon par nom normalisé (EpgNames.kt) ;
  - chaînes sans tvg-id → identifiant « name:<nom> », identique à celui de l'appli ;
  - associations manuelles (manual.json) : id appli → id de chaîne d'une source.
Sortie : seulement les chaînes du panel, fenêtre -6 h → +72 h (WINDOW_BEFORE_HOURS /
WINDOW_AFTER_HOURS), ids = ceux de l'appli.

Identifiants du panel par variables d'environnement (jamais dans le dépôt) :
  IPTV_HOST (ex. http://hote:port), IPTV_USER, IPTV_PASS
Usage : python3 merge.py [--out epg.xml.gz] [--stats stats.json]
"""
import argparse
import gzip
import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

UA = "VLC/3.0.18 LibVLC/3.0.18"
SOURCES = [
    "https://xmltvfr.fr/xmltv/xmltv.xml.gz",
    "https://iptv-epg.org/files/epg-fr.xml.gz",
    "https://www.open-epg.com/files/france1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_FR1.xml.gz",
]
FILLER_MIN_PROGRAMS = 4
# Titres de remplissage : ignorés (sinon des lignes « No Data » à la place de « Pas d'information »).
PLACEHOLDER_TITLES = {"no data", "pas d'information", "no information", "no programme",
                      "cette chaîne ne fait plus partie des offres", "to be announced", "tba"}
SYNTHETIC_PREFIX = "name:"
HERE = os.path.dirname(os.path.abspath(__file__))

# --- Normalisation : copie conforme de data/epg/EpgNames.kt -----------------------------
COUNTRY_PREFIX = re.compile(r"^\s*\|?[a-z]{2}\|?\s*[-:|]\s+")
COUNTRY_SUFFIX = re.compile(r"\.(fr|be|ch|ca|lu|mc|af|ar)$")
QUALITY = re.compile(r"\b(fhd|uhd|hd|sd|hevc|h265|4k|8k|1080p|720p|backup|raw)\b")
NON_ALNUM = re.compile(r"[^a-z0-9]")


def normalize(raw: str) -> str:
    s = unicodedata.normalize("NFD", raw)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().strip()
    s = COUNTRY_SUFFIX.sub("", COUNTRY_PREFIX.sub("", s))
    s = s.replace("+", " plus ").replace("&", " et ")
    s = QUALITY.sub(" ", s)
    return NON_ALNUM.sub("", s)


def synthetic_id(name: str):
    n = normalize(name)
    return SYNTHETIC_PREFIX + n if n else None


# --- Téléchargement ------------------------------------------------------------------------
def fetch(url: str, timeout=120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def open_xml(data: bytes):
    return io.BytesIO(gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data)


def parse_time(raw: str):
    raw = (raw or "").strip()
    for fmt in ("%Y%m%d%H%M%S %z", "%Y%m%d%H%M%S%z", "%Y%m%d%H%M%S"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


# --- Fusion --------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="epg.xml.gz")
    ap.add_argument("--stats", default="stats.json")
    args = ap.parse_args()
    host, user, pwd = (os.environ.get(k) for k in ("IPTV_HOST", "IPTV_USER", "IPTV_PASS"))
    if not (host and user and pwd):
        sys.exit("IPTV_HOST, IPTV_USER et IPTV_PASS requis")
    t0 = time.time()
    now = datetime.now(timezone.utc)
    # Fenêtre : le guide de l'appli remonte de 3 h et va jusqu'à 3 jours (réglage « Profondeur »).
    win_start = now - timedelta(hours=int(os.environ.get("WINDOW_BEFORE_HOURS", "6")))
    win_end = now + timedelta(hours=int(os.environ.get("WINDOW_AFTER_HOURS", "72")))

    # Chaînes du panel → ids appli + index par nom.
    live = json.loads(fetch(f"{host}/player_api.php?username={user}&password={pwd}&action=get_live_streams"))
    names_by_id = {}                # id appli → nom (pour <display-name>)
    channel_ids = {}                # id minuscule → id appli
    by_name = {}                    # nom normalisé → {ids appli}
    for s in live:
        name = s.get("name") or ""
        cid = (s.get("epg_channel_id") or "").strip() or synthetic_id(name)
        if not cid:
            continue
        names_by_id.setdefault(cid, name)
        channel_ids[cid.lower()] = cid
        n = normalize(name)
        if n:
            by_name.setdefault(n, set()).add(cid)

    manual_path = os.path.join(HERE, "manual.json")
    manual = json.load(open(manual_path)) if os.path.exists(manual_path) else {}
    manual_by_source_id = {}        # id source (minuscule) → {ids appli}
    for app_id, src_id in manual.items():
        manual_by_source_id.setdefault(src_id.lower(), set()).add(app_id)

    programmes = {}                 # id appli → [(start, stop, title, desc)]
    covered, filler = set(), set()
    stats = {"sources": []}

    panel_url = f"{host}/xmltv.php?username={user}&password={pwd}"
    for url in [panel_url] + SOURCES:
        is_panel = url == panel_url
        label = "panel" if is_panel else url
        ts = time.time()
        try:
            data = fetch(url, timeout=300)
        except Exception as e:  # une source en panne n'empêche pas les suivantes
            stats["sources"].append({"source": label, "erreur": str(e)})
            print(f"{label} : ERREUR {e}", file=sys.stderr)
            continue
        src_to_app = {}             # id source → {ids appli}
        served, titles, count, by_name_hits = {}, {}, 0, 0
        for _, el in ET.iterparse(open_xml(data), events=("end",)):
            if el.tag == "channel":
                sid = el.get("id") or ""
                if is_panel:
                    targets = {channel_ids[sid.lower()]} if sid.lower() in channel_ids else set()
                else:
                    targets = set(manual_by_source_id.get(sid.lower(), ()))
                    by_id = channel_ids.get(sid.lower())
                    names = set()
                    for dn in [d.text or "" for d in el.findall("display-name")] + [sid]:
                        names |= by_name.get(normalize(dn), set())
                    # Le panel colle parfois un même tvg-id sur des chaînes sans rapport (« 01tv.fr »
                    # sur Cartoonito, Nickelodeon 4 Teen… alors que c'est Tech & Co) : si le nom
                    # désigne d'autres chaînes que l'id, le nom l'emporte.
                    if by_id and (not names or by_id in names):
                        targets.add(by_id)
                    elif names:
                        targets |= names
                        by_name_hits += 1
                if targets:
                    src_to_app[sid] = targets
                el.clear()
            elif el.tag == "programme":
                sid = el.get("channel") or ""
                targets = src_to_app.get(sid)
                if targets is None and sid.lower() in channel_ids:
                    targets = {channel_ids[sid.lower()]}
                start, stop = parse_time(el.get("start")), parse_time(el.get("stop"))
                title = (el.findtext("title") or "").strip()
                if title.lower() in PLACEHOLDER_TITLES:
                    title = ""
                if targets and start and stop and title and stop > win_start and start < win_end:
                    desc = (el.findtext("desc") or "").strip()
                    for t in targets - covered:
                        if t in filler:     # bouche-trou d'une source précédente : remplacé
                            programmes.pop(t, None)
                            filler.discard(t)
                        programmes.setdefault(t, []).append((start, stop, title, desc))
                        served[t] = served.get(t, 0) + 1
                        ts_ = titles.setdefault(t, set())
                        if len(ts_) < 2:
                            ts_.add(title.lower())
                        count += 1
                el.clear()
        new_filler = {t for t, n in served.items() if n >= FILLER_MIN_PROGRAMS and len(titles[t]) == 1}
        filler |= new_filler
        covered |= set(served) - new_filler
        st = {"source": label, "programmes": count, "chaines": len(served),
              "par_nom": by_name_hits, "bouche_trou": len(new_filler), "secondes": round(time.time() - ts, 1)}
        stats["sources"].append(st)
        print(json.dumps(st, ensure_ascii=False), file=sys.stderr)

    # Écriture XMLTV (ids appli, triés).
    def fmt(dt):
        return dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S +0000")

    root = ET.Element("tv", {"generator-info-name": "iptv-epg-merge", "date": fmt(now)})
    for cid in sorted(programmes):
        ch = ET.SubElement(root, "channel", {"id": cid})
        ET.SubElement(ch, "display-name").text = names_by_id.get(cid, cid)
    total = 0
    for cid in sorted(programmes):
        for start, stop, title, desc in sorted(programmes[cid]):
            p = ET.SubElement(root, "programme", {"start": fmt(start), "stop": fmt(stop), "channel": cid})
            ET.SubElement(p, "title").text = title
            if desc:
                ET.SubElement(p, "desc").text = desc
            total += 1
    raw = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    with gzip.open(args.out, "wb", compresslevel=9) as f:
        f.write(raw)
    stats.update({"genere": fmt(now), "chaines": len(programmes), "programmes": total,
                  "taille_octets": os.path.getsize(args.out), "duree_s": round(time.time() - t0, 1)})
    json.dump(stats, open(args.stats, "w"), ensure_ascii=False, indent=1)
    print(f"→ {args.out} : {len(programmes)} chaînes, {total} programmes, "
          f"{os.path.getsize(args.out) // 1024} Ko en {stats['duree_s']} s", file=sys.stderr)


if __name__ == "__main__":
    main()
