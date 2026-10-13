"""Atom feeds of the important changes of each feed (data-model §10.3).

The site publishes, per page language, one Atom feed per transit feed and one for all feeds, with
the reports whose impact is major (§12.1), newest first. A reader subscribes in any feed reader;
nothing is sent and nothing needs an account. Entries are worded with the same labels as the page
(a test keeps them equal), and the output depends only on the reports, so an unchanged site
rewrites the same files.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

ATOM = "http://www.w3.org/2005/Atom"
SITE_URL = "https://ttezer.github.io/gtfs-jp-monitor/"
PER_FEED = 20
ALL_FEEDS = 50
MAX_LINES = 10

# Same as the page's `imp` and `evtShort` texts, events in order of importance (tests/test_atom.py
# checks it).
LABELS = {
    "tr": {
        "imp": {"major": "Büyük etki", "minor": "Küçük etki", "technical": "Teknik"},
        "evt": {
            "ROUTE_ADDED": "yeni hat", "ROUTE_DISCONTINUED": "kaldırıldı", "ROUTE_MERGED": "birleşti",
            "ROUTE_SPLIT": "bölündü", "ROUTE_RESTRUCTURED": "yeniden düzenlendi",
            "ROUTE_RENAMED": "adı değişti", "SERVICE_REDUCED": "sefer azaldı",
            "SERVICE_INCREASED": "sefer arttı", "PATTERN_EXTENDED": "güzergâh uzadı",
            "PATTERN_SHORTENED": "güzergâh kısaldı", "STOP_SKIPPED": "durak atlandı",
            "STOP_INSERTED": "güzergâha durak eklendi", "DETOUR_ADDED": "sapma eklendi",
            "DETOUR_REMOVED": "sapma kaldırıldı", "DETOUR_SHIFTED": "sapma başka seferlere geçti",
            "STOPS_REORDERED": "durak sırası değişti", "TRIPS_MOVED": "seferler başka hatta geçti",
            "FIRST_LAST_CHANGED": "ilk/son sefer değişti", "TRIPS_RETIMED": "saatler değişti",
            "STOP_ADDED": "durak eklendi", "STOP_REMOVED": "durak kaldırıldı",
            "STOP_RENAMED": "durak adı değişti", "STOP_MOVED": "durak taşındı",
        },
    },
    "en": {
        "imp": {"major": "Major impact", "minor": "Minor impact", "technical": "Technical"},
        "evt": {
            "ROUTE_ADDED": "new line", "ROUTE_DISCONTINUED": "discontinued", "ROUTE_MERGED": "merged",
            "ROUTE_SPLIT": "split", "ROUTE_RESTRUCTURED": "restructured", "ROUTE_RENAMED": "renamed",
            "SERVICE_REDUCED": "fewer trips", "SERVICE_INCREASED": "more trips",
            "PATTERN_EXTENDED": "route extended", "PATTERN_SHORTENED": "route shortened",
            "STOP_SKIPPED": "stop skipped", "STOP_INSERTED": "stop inserted", "DETOUR_ADDED": "detour added",
            "DETOUR_REMOVED": "detour removed", "DETOUR_SHIFTED": "detour moved to other trips",
            "STOPS_REORDERED": "stop order changed", "TRIPS_MOVED": "trips moved to another line",
            "FIRST_LAST_CHANGED": "first/last trip changed", "TRIPS_RETIMED": "retimed",
            "STOP_ADDED": "stop added", "STOP_REMOVED": "stop removed", "STOP_RENAMED": "stop renamed",
            "STOP_MOVED": "stop moved",
        },
    },
    "fr": {
        "imp": {"major": "Impact majeur", "minor": "Impact mineur", "technical": "Technique"},
        "evt": {
            "ROUTE_ADDED": "nouvelle ligne", "ROUTE_DISCONTINUED": "supprimée", "ROUTE_MERGED": "fusionnée",
            "ROUTE_SPLIT": "scindée", "ROUTE_RESTRUCTURED": "restructurée", "ROUTE_RENAMED": "renommée",
            "SERVICE_REDUCED": "moins de courses", "SERVICE_INCREASED": "plus de courses",
            "PATTERN_EXTENDED": "itinéraire prolongé", "PATTERN_SHORTENED": "itinéraire raccourci",
            "STOP_SKIPPED": "arrêt non desservi", "STOP_INSERTED": "arrêt ajouté à l'itinéraire",
            "DETOUR_ADDED": "détour ajouté", "DETOUR_REMOVED": "détour supprimé",
            "DETOUR_SHIFTED": "détour reporté sur d'autres courses", "STOPS_REORDERED": "ordre des arrêts modifié",
            "TRIPS_MOVED": "courses transférées à une autre ligne", "FIRST_LAST_CHANGED": "première/dernière course modifiée",
            "TRIPS_RETIMED": "horaires modifiés", "STOP_ADDED": "arrêt créé", "STOP_REMOVED": "arrêt supprimé",
            "STOP_RENAMED": "arrêt renommé", "STOP_MOVED": "arrêt déplacé",
        },
    },
    "ja": {
        "imp": {"major": "影響大", "minor": "影響小", "technical": "技術的"},
        "evt": {
            "ROUTE_ADDED": "新設路線", "ROUTE_DISCONTINUED": "廃止", "ROUTE_MERGED": "統合", "ROUTE_SPLIT": "分割",
            "ROUTE_RESTRUCTURED": "再編", "ROUTE_RENAMED": "名称変更", "SERVICE_REDUCED": "減便",
            "SERVICE_INCREASED": "増便", "PATTERN_EXTENDED": "経路延長", "PATTERN_SHORTENED": "経路短縮",
            "STOP_SKIPPED": "停留所通過", "STOP_INSERTED": "停留所追加", "DETOUR_ADDED": "経由追加",
            "DETOUR_REMOVED": "経由廃止", "DETOUR_SHIFTED": "経由便の入替", "STOPS_REORDERED": "停留所順序変更",
            "TRIPS_MOVED": "他路線へ移管", "FIRST_LAST_CHANGED": "始発・終発変更", "TRIPS_RETIMED": "時刻変更",
            "STOP_ADDED": "停留所新設", "STOP_REMOVED": "停留所廃止", "STOP_RENAMED": "停留所名変更", "STOP_MOVED": "停留所移設",
        },
    },
}
TITLE = {"tr": "GTFS-JP Monitor · önemli değişiklikler", "en": "GTFS-JP Monitor · major changes",
         "ja": "GTFS-JP Monitor・主な変更"}
STOPS = {"tr": "Duraklar", "en": "Stops", "fr": "Arrêts", "ja": "停留所"}
LANGS = ("tr", "en", "ja")  # this site's feeds; the other labels serve other sites (langs=)


def _updated(pub: dict) -> str:
    """When the new publication appeared: its published_at, else its start date (JST)."""
    return pub.get("published_at") or f"{pub['from_date']}T00:00:00+09:00"


def _content(report: dict, lang: str) -> str:
    imp, labels = report["impact"], LABELS[lang]["evt"]
    lines = sorted(((k, v) for k, v in imp["lines"].items() if v["class"] != "technical"),
                   key=lambda kv: (kv[1]["class"] != "major", kv[0]))
    out = []
    for key, v in lines[:MAX_LINES]:
        out.append(f"{key}: " + " · ".join(dict.fromkeys(labels[t] for t in labels if t in v["types"])))
    if len(lines) > MAX_LINES:
        out.append(f"+{len(lines) - MAX_LINES}")
    stops = [f"{labels[t]} ×{c.get('major', 0) + c.get('minor', 0)}" for t, c in imp["types"].items()
             if t in ("STOP_ADDED", "STOP_REMOVED", "STOP_RENAMED", "STOP_MOVED") and c.get("major", 0) + c.get("minor", 0)]
    if stops:
        out.append(f"{STOPS[lang]}: " + " · ".join(stops))
    return "\n".join(out)


def _entry(parent: ET.Element, feed: dict, report: dict, lang: str, site_url: str, tag: str) -> None:
    h = report["header"]
    org, fid, old, new = h["feed"]["org_id"], h["feed"]["feed_id"], h["old"]["uid"], h["new"]["uid"]
    e = ET.SubElement(parent, "entry")
    ET.SubElement(e, "id").text = f"tag:ttezer.github.io,2026:{tag}/{org}/{fid}/{old}__{new}"
    ET.SubElement(e, "title").text = (f"{feed.get('name') or fid} · {h['new']['from_date']} · "
                                      f"{LABELS[lang]['imp'][report['impact']['class']]}")
    ET.SubElement(e, "updated").text = _updated(h["new"])
    ET.SubElement(e, "link", href=f"{site_url}#feed={org}/{fid}&old={old}&new={new}&tab=reports")
    ET.SubElement(e, "content", type="text").text = _content(report, lang)


def _feed(entries: list[tuple[dict, dict]], lang: str, title: str, self_href: str, site_url: str, author_name: str, tag: str) -> bytes:
    root = ET.Element("feed", xmlns=ATOM, attrib={"xml:lang": lang})
    ET.SubElement(root, "id").text = self_href
    ET.SubElement(root, "title").text = title
    ET.SubElement(root, "updated").text = max(_updated(r["header"]["new"]) for _, r in entries)
    ET.SubElement(root, "link", rel="self", href=self_href)
    ET.SubElement(root, "link", href=site_url)
    author = ET.SubElement(root, "author")
    ET.SubElement(author, "name").text = author_name
    for feed, report in entries:
        _entry(root, feed, report, lang, site_url, tag)
    return b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="utf-8") + b"\n"


def build_atom(feeds: list[dict], files: dict[str, dict], site_url: str = SITE_URL, langs: list[str] | None = None,
               titles: dict[str, str] | None = None, author: str = "GTFS-JP Monitor", tag: str = "gtfs-jp-monitor") -> dict[str, bytes]:
    """{"atom/<lang>/<org_id>/<feed_id>.xml": bytes, "atom/<lang>/all.xml": bytes}: the reports with a
    major impact, newest first. A feed without such a report has no file. langs, titles, author and
    tag (part of the entry ids) come from another site's profile; the defaults are this site's."""
    titles = titles or TITLE
    names = {(f["org_id"], f["feed_id"]): f for f in feeds}
    major = []
    for doc in files.values():
        if (doc.get("impact") or {}).get("class") == "major":
            h = doc["header"]["feed"]
            major.append((names.get((h["org_id"], h["feed_id"]), {}), doc))
    order = lambda item: (_updated(item[1]["header"]["new"]), item[1]["header"]["new"]["uid"])
    major.sort(key=order, reverse=True)
    out: dict[str, bytes] = {}
    for lang in langs or LANGS:
        by_feed: dict[tuple[str, str], list] = {}
        for feed, doc in major:
            h = doc["header"]["feed"]
            by_feed.setdefault((h["org_id"], h["feed_id"]), []).append((feed, doc))
        for (org, fid), entries in sorted(by_feed.items()):
            path = f"atom/{lang}/{org}/{fid}.xml"
            name = (entries[0][0].get("name") or fid)
            out[path] = _feed(entries[:PER_FEED], lang, f"{titles[lang]} · {name}", site_url + path, site_url, author, tag)
        if major:
            path = f"atom/{lang}/all.xml"
            out[path] = _feed(major[:ALL_FEEDS], lang, titles[lang], site_url + path, site_url, author, tag)
    return out
