"""Normalization of license values published by gtfs-data.jp (data-model §7).

Observed raw values (2026-09-24): "CC BY 4.0", "CC0 1.0", "CC BY 2.1 JP", "CC-BY".
"CC-BY" carries no version, so it is not mapped to a specific license.
"""

from __future__ import annotations

_KNOWN = {
    "CC BY 4.0": "CC-BY-4.0",
    "CC0 1.0": "CC0-1.0",
    "CC BY 2.1 JP": "CC-BY-2.1-JP",
}


def normalize_license(raw: str | None) -> str | None:
    """Map a raw value to a license id, or None when it is unknown or ambiguous."""
    if not isinstance(raw, str):
        return None
    return _KNOWN.get(" ".join(raw.split()))
