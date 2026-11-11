from gtfs_jp_monitor.lineage import build_route_lineage


def report(old_uid, new_uid, old_route, new_route, relation="same", confidence=1.0):
    return {
        "header": {"feed": {"org_id": "org", "feed_id": "feed"},
                   "old": {"uid": old_uid}, "new": {"uid": new_uid}},
        "lines": [{"old": {"route_ids": [old_route]}, "new": {"route_ids": [new_route]}}],
        "mappings": {"routes": [{"old": [old_route], "new": [new_route],
                                   "relation": relation, "confidence": confidence}]},
    }


def test_high_confidence_renumbering_keeps_canonical_identity():
    result = build_route_lineage([report("a", "b", "R1", "R9")])
    routes = {r["source_route_id"]: r["canonical_route_id"] for r in result["routes"]}
    assert routes["R1"] == routes["R9"]


def test_low_confidence_mapping_is_preserved_but_not_merged():
    result = build_route_lineage([report("a", "b", "R1", "R9", confidence=0.5)])
    routes = {r["source_route_id"]: r["canonical_route_id"] for r in result["routes"]}
    assert routes["R1"] != routes["R9"]
    assert result["relations"][0]["confidence"] == 0.5
