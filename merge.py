#!/usr/bin/env python3
"""
Fusion EPG : panel Xtream + sources XMLTV publiques → un seul epg.xml.gz prêt pour l'appli.

Même logique que XtreamRepository.syncXmlTv (côté télé), faite une fois pour toutes :
  - priorité = ordre des sources ; une chaîne servie par une source n'est plus complétée ;
  - bouche-trou (un seul titre sur >= 4 programmes, « Cette chaîne ne fait plus partie des
    offres ») : laissé aux sources suivantes ;
  - correspondance par tvg-id (casse ignorée), sinon par nom normalisé (EpgNames.kt) ;
  - chaînes sans tvg-id → identifiant « name:<nom> », identique à celui de l'appli ;
  - associations manuelles (manual.json) : id appli → id de chaîne d'une source ;
  - chaînes restées vides : créneaux d'événement et boucles 24/7, programme lu dans le nom.
Sortie : seulement les chaînes du panel, fenêtre -6 h → +72 h (WINDOW_BEFORE_HOURS /
WINDOW_AFTER_HOURS), ids = ceux de l'appli. Trois fichiers, à côté de --out : epg.xml.gz (tout le
reste), epg-am.xml.gz (groupes « AM | … »), epg-vip.xml.gz (groupes « VIP… » et « For Adults »).

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
from zoneinfo import ZoneInfo

UA = "VLC/3.0.18 LibVLC/3.0.18"
# (url, portée) : portée = début du nom de groupe auquel la source est réservée, None = toutes
# les chaînes. Une source américaine ne doit pas remplir « FR - DISNEY CHANNEL » par son nom.
SOURCES = [
    ("https://xmltvfr.fr/xmltv/xmltv.xml.gz", None),
    ("https://iptv-epg.org/files/epg-fr.xml.gz", None),
    ("https://www.open-epg.com/files/france1.xml.gz", None),
    ("https://epgshare01.online/epgshare01/epg_ripper_FR1.xml.gz", None),
    ("https://epgshare01.online/epgshare01/epg_ripper_US2.xml.gz", "AM | USA"),
    ("https://epgshare01.online/epgshare01/epg_ripper_US_LOCALS1.xml.gz", "AM | USA"),
    ("https://epgshare01.online/epgshare01/epg_ripper_CA2.xml.gz", "AM | CA"),
]
# Un fichier par famille de groupes : la télé n'active que ceux qu'elle regarde.
OUTPUTS = {"main": "epg.xml.gz", "am": "epg-am.xml.gz", "vip": "epg-vip.xml.gz"}


def zone_of(group: str) -> str:
    if group.startswith("AM |"):
        return "am"
    if group.startswith("VIP") or group == "For Adults":
        return "vip"
    return "main"


# Sources à portée : ids « Pets.TV.HD.us2 », « WNCF-DT.us_locals1 ».
SOURCE_SUFFIX = re.compile(r"\.(us|ca)[a-z_]*\d*$", re.I)
# Indicatif d'une station locale dans le nom du panel : « US - ABC 32 MONTGOMERY AL (WNCF) HD ».
PANEL_CALLSIGN = re.compile(r"\(([KWC][A-Z]{2,3})(?:-[A-Z]{2})?\)")
# Canal principal d'une station côté source (pas les sous-canaux « WNCF-DT2 »).
SOURCE_CALLSIGN = re.compile(r"^([KWC][A-Z]{2,3})(?:-(?:DT|TV|HD|CD|LD|D))?$")
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


# --- Programmes tirés du nom des chaînes ---------------------------------------------------
# Les créneaux d'événement n'ont aucun guide : l'événement est écrit dans le nom
# (« US - NHL GAME 01 : PREDATORS @ MAPLE LEAFS OCT 6 – 7:00 PM ET / 12:00 AM UK »).
# « 01 : » est un numéro de créneau, « 23:00 » une heure.
SLOT = re.compile(r"^.*?\b\d{1,3}\s*:(?!\d{2}\b)\s*(.*)$")
LANG_PREFIX = re.compile(r"^\s*[A-Z]{2,4}\s*-\s*")
DATE = re.compile(r"\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|SEPT|OCT|NOV|DEC)\s+(\d{1,2})\b")
TIME_12 = re.compile(r"\b(\d{1,2}):(\d{2})\s*([AP]M)\s*(ET|UK|FR)\b")
TIME_24 = re.compile(r"\b(\d{1,2}):(\d{2})\s*(CET|CEST)\b")
MONTHS = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
MONTHS["SEPT"] = 9
ZONES = {"ET": "America/New_York", "UK": "Europe/London", "FR": "Europe/Paris",
         "CET": "Europe/Paris", "CEST": "Europe/Paris"}
QUALITY_TAIL = re.compile(r"\s*\b(FHD|UHD|HD|SD|HEVC|4K)\b\s*$")
NO_EVENT = re.compile(r"^(NO (MATCH|EVENT|GAME)|OFF ?AIR|TBA|TBD)\b")
EVENT_HOURS = 3
BLOCK_HOURS = 6


def event_from_name(name: str, now: datetime):
    """(titre, début) lu dans le nom ; début None si aucun horaire. None si rien d'exploitable."""
    up = name.upper()
    slot = SLOT.match(up)
    body = slot.group(1) if slot else LANG_PREFIX.sub("", up)
    times = list(TIME_12.finditer(body))
    t = next((m for m in times if m.group(4) == "ET"), times[0] if times else None) or TIME_24.search(body)
    if t is None:
        # Sans horaire, seul un créneau numéroté porte un événement (sinon c'est une chaîne normale).
        title = body.strip(" -–—|/•") if slot else ""
        return (title, None) if title and not NO_EVENT.match(title) else None
    d = DATE.search(body)
    first = min(m.start() for m in ([d] if d else []) + times + [t])
    title = body[:first].strip(" -–—|/•:")
    if "(" not in title:
        title = title.rstrip(" )")
    if not title or NO_EVENT.match(title):
        return None
    hour, minute = int(t.group(1)), int(t.group(2))
    if t.re is TIME_12:
        hour = hour % 12 + (12 if t.group(3) == "PM" else 0)
        zone = ZoneInfo(ZONES[t.group(4)])
    else:
        zone = ZoneInfo(ZONES[t.group(3)])
    if hour > 23 or minute > 59:
        return None
    local_now = now.astimezone(zone)
    if d:   # sans année : celle qui tombe le plus près d'aujourd'hui
        cands = []
        for y in (local_now.year - 1, local_now.year, local_now.year + 1):
            try:
                cands.append(datetime(y, MONTHS[d.group(1)], int(d.group(2)), hour, minute, tzinfo=zone))
            except ValueError:
                pass
    else:   # sans date : l'occurrence la plus proche de maintenant
        base = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        cands = [base + timedelta(days=k) for k in (-1, 0, 1)]
    if not cands:
        return None
    return title, min(cands, key=lambda c: abs(c - now))


def programmes_from_names(live, categories, taken, now, win_start, win_end):
    """Chaînes encore sans guide → programmes fabriqués depuis leur nom. id appli → [(début, fin, titre, desc)]."""
    out = {}
    block = timedelta(hours=BLOCK_HOURS)
    first_block = win_start.replace(minute=0, second=0, microsecond=0)
    first_block -= timedelta(hours=first_block.hour % BLOCK_HOURS)
    for s in live:
        name = (s.get("name") or "").strip()
        cid = (s.get("epg_channel_id") or "").strip() or synthetic_id(name)
        if not cid or cid in taken or cid in out or name.count("#") >= 4:
            continue
        ev = event_from_name(name, now)
        if ev is None and "24/7" in (categories.get(str(s.get("category_id")), "") + name):
            # Boucle 24/7 : le titre de la boucle sert de programme permanent.
            title = QUALITY_TAIL.sub("", LANG_PREFIX.sub("", name.upper())).split("|")[-1].strip(" -–—")
            ev = (title, None) if title else None
        if ev is None:
            continue
        title, start = ev
        if start is not None:
            stop = start + timedelta(hours=EVENT_HOURS)
            if stop > win_start and start < win_end:
                out[cid] = [(start, stop, title, name)]
        else:
            progs, t = [], first_block
            while t < win_end:
                progs.append((t, t + block, title, name))
                t += block
            out[cid] = progs
    return out


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
    cats = {str(c.get("category_id")): c.get("category_name") or ""
            for c in json.loads(fetch(f"{host}/player_api.php?username={user}&password={pwd}&action=get_live_categories"))}
    names_by_id = {}                # id appli → nom (pour <display-name>)
    channel_ids = {}                # id minuscule → id appli
    by_name = {}                    # nom normalisé → {ids appli}
    groups_by_id = {}               # id appli → {groupes}
    by_callsign = {}                # indicatif → {ids appli}
    for s in live:
        name = s.get("name") or ""
        cid = (s.get("epg_channel_id") or "").strip() or synthetic_id(name)
        if not cid:
            continue
        names_by_id.setdefault(cid, name)
        channel_ids[cid.lower()] = cid
        groups_by_id.setdefault(cid, set()).add(cats.get(str(s.get("category_id")), ""))
        n = normalize(name)
        if n:
            by_name.setdefault(n, set()).add(cid)
        call = PANEL_CALLSIGN.search(name.upper())
        if call:
            by_callsign.setdefault(call.group(1), set()).add(cid)

    def in_scope(cid, scope):
        return any(g.startswith(scope) for g in groups_by_id.get(cid, ()))

    manual_path = os.path.join(HERE, "manual.json")
    manual = json.load(open(manual_path)) if os.path.exists(manual_path) else {}
    manual_by_source_id = {}        # id source (minuscule) → {ids appli}
    for app_id, src_id in manual.items():
        manual_by_source_id.setdefault(src_id.lower(), set()).add(app_id)

    programmes = {}                 # id appli → [(start, stop, title, desc)]
    covered, filler = set(), set()
    stats = {"sources": []}

    panel_url = f"{host}/xmltv.php?username={user}&password={pwd}"
    callsign_served = set()         # ids appli déjà rattachés à une station par son indicatif
    for url, scope in [(panel_url, None)] + SOURCES:
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
                    labels = [d.text or "" for d in el.findall("display-name")] + [sid]
                    if scope:
                        base = SOURCE_SUFFIX.sub("", sid)
                        labels += [base, base.replace(".", " ")]
                        for dn in labels:
                            call = SOURCE_CALLSIGN.match(dn.strip().upper())
                            if call:    # un seul canal de la source par station
                                fresh = by_callsign.get(call.group(1), set()) - callsign_served
                                names |= fresh
                                callsign_served |= fresh
                    for dn in labels:
                        names |= by_name.get(normalize(dn), set())
                    # Le panel colle parfois un même tvg-id sur des chaînes sans rapport (« 01tv.fr »
                    # sur Cartoonito, Nickelodeon 4 Teen… alors que c'est Tech & Co) : si le nom
                    # désigne d'autres chaînes que l'id, le nom l'emporte.
                    if by_id and (not names or by_id in names):
                        targets.add(by_id)
                    elif names:
                        targets |= names
                        by_name_hits += 1
                if scope:
                    targets = {t for t in targets if in_scope(t, scope)}
                if targets:
                    src_to_app[sid] = targets
                el.clear()
            elif el.tag == "programme":
                sid = el.get("channel") or ""
                targets = src_to_app.get(sid)
                if targets is None and sid.lower() in channel_ids and not scope:
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

    # Chaînes restées vides : créneaux d'événement et boucles 24/7, lus dans le nom.
    named = programmes_from_names(live, cats, set(programmes), now, win_start, win_end)
    programmes.update(named)
    st = {"source": "noms des chaînes", "programmes": sum(len(v) for v in named.values()), "chaines": len(named)}
    stats["sources"].append(st)
    print(json.dumps(st, ensure_ascii=False), file=sys.stderr)

    # Écriture XMLTV (ids appli, triés).
    def fmt(dt):
        return dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S +0000")

    def write(path, ids):
        root = ET.Element("tv", {"generator-info-name": "iptv-epg-merge", "date": fmt(now)})
        for cid in ids:
            ch = ET.SubElement(root, "channel", {"id": cid})
            ET.SubElement(ch, "display-name").text = names_by_id.get(cid, cid)
        count = 0
        for cid in ids:
            for start, stop, title, desc in sorted(programmes[cid]):
                p = ET.SubElement(root, "programme", {"start": fmt(start), "stop": fmt(stop), "channel": cid})
                ET.SubElement(p, "title").text = title
                if desc:
                    ET.SubElement(p, "desc").text = desc
                count += 1
        with gzip.open(path, "wb", compresslevel=9) as f:
            f.write(ET.tostring(root, encoding="utf-8", xml_declaration=True))
        return count

    # Un id partagé par des chaînes de plusieurs familles est écrit dans chacun de leurs fichiers.
    out_dir = os.path.dirname(os.path.abspath(args.out))
    total, files = 0, {}
    for zone, filename in OUTPUTS.items():
        path = args.out if zone == "main" else os.path.join(out_dir, filename)
        ids = sorted(c for c in programmes if zone in {zone_of(g) for g in groups_by_id.get(c, ())})
        count = write(path, ids)
        total += count
        files[os.path.basename(path)] = {"chaines": len(ids), "programmes": count, "taille_octets": os.path.getsize(path)}
        print(f"→ {os.path.basename(path)} : {len(ids)} chaînes, {count} programmes, {os.path.getsize(path) // 1024} Ko",
              file=sys.stderr)
    stats.update({"genere": fmt(now), "chaines": len(programmes), "programmes": total, "fichiers": files,
                  "duree_s": round(time.time() - t0, 1)})
    json.dump(stats, open(args.stats, "w"), ensure_ascii=False, indent=1)
    print(f"total : {len(programmes)} chaînes, {total} programmes en {stats['duree_s']} s", file=sys.stderr)


if __name__ == "__main__":
    main()
