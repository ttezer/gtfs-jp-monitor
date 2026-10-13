"""Normalization of license values published by the feed sources (data-model §7).

gtfs-data.jp raw values (2026-09-24): "CC BY 4.0", "CC0 1.0", "CC BY 2.1 JP", "CC-BY".
"CC-BY" carries no version, so it is not mapped to a specific license.
transport.data.gouv.fr licence codes (2026-10-13): "lov2" (Licence Ouverte 2.0), "odc-odbl";
"fr-lo", "mobility-licence" and "notspecified" have no SPDX identifier and stay unmapped.
"""

from __future__ import annotations

_KNOWN = {
    "CC BY 4.0": "CC-BY-4.0",
    "CC0 1.0": "CC0-1.0",
    "CC BY 2.1 JP": "CC-BY-2.1-JP",
    "lov2": "etalab-2.0",
    "odc-odbl": "ODbL-1.0",
}


def normalize_license(raw: str | None) -> str | None:
    """Map a raw value to a license id, or None when it is unknown or ambiguous."""
    if not isinstance(raw, str):
        return None
    return _KNOWN.get(" ".join(raw.split()))
