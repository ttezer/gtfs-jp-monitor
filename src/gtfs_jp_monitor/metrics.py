"""Metrics over time per feed and in total (data-model §13).

Computed at export time over the last WINDOW_DAYS days, by publication date, from the catalog,
the exported publications with their pair classes (§12) and the report index. Every metric
comes with its window, analysis key, engine version and coverage (the share of pairs whose class
is known); unknown pairs are never guessed.
"""

from __future__ import annotations

import datetime as _dt

WINDOW_DAYS = 365
PER_DAYS = 30  # frequencies are given per this many days
SEVERE = ("CRITICAL", "HIGH")


def _date(value: str | None) -> _dt.date | None:
    try:
        return _dt.datetime.fromisoformat(value).date()
    except (TypeError, ValueError):
        return None


def _regression(prev: dict, g: dict) -> bool:
    """Publish score fell, or a CRITICAL or HIGH rule appeared that the previous one did not have."""
    a, b = (prev.get("scores") or {}).get("publish"), (g.get("scores") or {}).get("publish")
    if a is not None and b is not None and b < a:
        return True
    return any(r[1] in SEVERE and r[0] > 0 and (prev["rules"].get(rid) or [0])[0] == 0 for rid, r in g["rules"].items())


def _ratio(n: int, d: int, digits: int = 4) -> float | None:
    return round(n / d, digits) if d else None


def _finish(c: dict) -> dict:
    known = c["M"] + c["T"] + c["E"]
    per = WINDOW_DAYS / PER_DAYS
    return {
        "publications": c["publications"],
        "pairs": c["pairs"],
        "counts": {"meaningful": c["M"], "technical": c["T"], "equivalent": c["E"], "unknown": c["U"],
                   "regressions": c["regressions"], "compared": c["compared"]},
        "coverage": _ratio(known, c["pairs"]),
        "publication_frequency": round(c["publications"] / per, 2),
        "meaningful_update_frequency": round(c["M"] / per, 2),
        "equivalent_republication_ratio": _ratio(c["E"], known),
        "technical_only_change_ratio": _ratio(c["T"], known),
        "unknown_classification_ratio": _ratio(c["U"], c["pairs"]),
        "validation_regression_count": c["regressions"],
        "unclassified_diff_ratio": _ratio(c["unclassified"], c["raw"], 6),  # tiny: keep precision
        "source_availability_rate": _ratio(c["available"], c["publications"]),
    }


def build_metrics(feeds: list[dict], catalog_gens: dict, report_index: dict, key: str, engine_version: str,
                  today: _dt.date) -> dict:
    """{"from", "to", "window_days", "analysis_key", "engine_version", "feeds": {"org/feed": m}, "total": m}.

    `feeds` are the exported feeds with `pair` codes and full rule tuples (before compaction)."""
    start = today - _dt.timedelta(days=WINDOW_DAYS)
    inside = lambda value: (d := _date(value)) is not None and start < d <= today
    fields = ("publications", "available", "pairs", "M", "T", "E", "U", "regressions", "compared", "unclassified", "raw")
    total = dict.fromkeys(fields, 0)
    out = {}
    for f in feeds:
        fk = (f["org_id"], f["feed_id"])
        c = dict.fromkeys(fields, 0)
        for e in (catalog_gens.get(fk) or {}).values():
            if inside(e.get("published_at")):
                c["publications"] += 1
                c["available"] += bool(e.get("present", True))
        gens = f["generations"]
        for prev, g in zip(gens, gens[1:]):
            if not inside(g.get("published_at")) or "pair" not in g:
                continue
            c["pairs"] += 1
            c[g["pair"][0]] += 1
            if "FATAL" not in (prev["status"], g["status"]) and not g.get("format_transition"):
                c["compared"] += 1
                c["regressions"] += _regression(prev, g)
            entry = report_index.get(f"{prev['uid']}__{g['uid']}")
            if entry:
                c["unclassified"] += entry["coverage"]["unclassified"]
                c["raw"] += entry["coverage"]["raw_total"]
        for k in fields:
            total[k] += c[k]
        out[f"{fk[0]}/{fk[1]}"] = _finish(c)
    return {"from": start.isoformat(), "to": today.isoformat(), "window_days": WINDOW_DAYS, "per_days": PER_DAYS,
            "analysis_key": key, "engine_version": engine_version, "feeds": out, "total": _finish(total)}
