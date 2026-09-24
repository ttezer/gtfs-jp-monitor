"""Independent semantic diff engine for GTFS feeds (docs/semantic/).

Input: two GTFS ZIP files. Output: canonical JSON. No dependency on gtfs_jp_monitor internals
except the shared canonical serializer, so the package can move elsewhere later.
"""

ENGINE_VERSION = "0.4.0"
