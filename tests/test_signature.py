import tempfile
import unittest
import zipfile
from pathlib import Path

from gtfs_jp_monitor.signature import content_signature

STOPS = "stop_id,stop_name,stop_lat,stop_lon\nA,Station,35.1,135.1\nB,Hall,35.2,135.2\n"


class SignatureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.n = 0

    def tearDown(self):
        self.tmp.cleanup()

    def sig(self, files: dict[str, bytes | str], raw: bytes | None = None) -> dict:
        self.n += 1
        path = self.dir / f"{self.n}.zip"
        if raw is not None:
            path.write_bytes(raw)
        else:
            with zipfile.ZipFile(path, "w") as zf:
                for name, data in files.items():
                    zf.writestr(name, data)
        return content_signature(path, "0" * 64)

    def same(self, a, b):
        self.assertEqual(self.sig(a)["signature"], self.sig(b)["signature"])

    def differ(self, a, b):
        self.assertNotEqual(self.sig(a)["signature"], self.sig(b)["signature"])

    def test_representation_does_not_count(self):
        lines = STOPS.splitlines()
        self.same({"stops.txt": STOPS, "agency.txt": "agency_id\n1\n"}, {"agency.txt": "agency_id\n1\n", "stops.txt": STOPS})
        self.same({"stops.txt": STOPS}, {"stops.txt": "\n".join([lines[0], lines[2], lines[1]]) + "\n"})  # row order
        self.same({"stops.txt": STOPS}, {"stops.txt": "stop_name,stop_id,stop_lat,stop_lon\nStation,A,35.1,135.1\nHall,B,35.2,135.2\n"})
        self.same({"stops.txt": STOPS}, {"stops.txt": "﻿" + STOPS.replace("\n", "\r\n")})  # BOM, CRLF
        self.same({"stops.txt": STOPS}, {"stops.txt": STOPS.replace("Hall", '"Hall"')})  # quoting

    def test_values_count(self):
        self.differ({"stops.txt": STOPS}, {"stops.txt": STOPS.replace("35.2,", "35.20,")})  # no numeric rounding
        self.differ({"stops.txt": STOPS}, {"stops.txt": STOPS.replace("Hall", "Hall ")})  # no trimming
        self.differ({"stops.txt": STOPS}, {"stops.txt": STOPS.replace("Hall", "hall")})  # no case folding
        self.differ({"stops.txt": STOPS}, {"stops.txt": STOPS + "B,Hall,35.2,135.2\n"})  # duplicates count
        self.differ({"stops.txt": STOPS}, {"stops2.txt": STOPS})  # file names count
        self.differ({"stops.txt": STOPS}, {"stops.txt": STOPS, "extra.txt": "x\n"})
        # An all-empty column is not the same as a missing one: the engine decides, not the signature.
        self.differ({"stops.txt": "stop_id\nA\n"}, {"stops.txt": "stop_id,stop_desc\nA,\n"})

    def test_irregular_files_fall_back_to_raw_bytes(self):
        files = self.sig({"stops.txt": "stop_id,stop_name\nA,B,C\n", "shift.txt": "a\n\x82\xa0\n".encode("latin-1"),
                          "image.png": b"\x89PNG"})["files"]
        self.assertEqual({n: v["method"] for n, v in files.items()},
                         {"stops.txt": "raw_bytes", "shift.txt": "raw_bytes", "image.png": "raw_bytes"})
        self.differ({"shift.txt": b"a\n\x82\xa0\n"}, {"shift.txt": b"a\n\x82\xa1\n"})

    def test_field_counts(self):
        stops = "stop_id,stop_name,wheelchair_boarding,platform_code\nA,x,1,\nB,y,0,2\nC,z,,\n"
        doc = self.sig({"stops.txt": stops, "agency.txt": "agency_id\n1\n"})
        self.assertEqual(doc["fields"], {"stops.txt": {"rows": 3, "filled": {
            "wheelchair_boarding": 1, "platform_code": 1, "stop_code": 0, "stop_desc": 0, "parent_station": 0}}})
        self.assertEqual(self.sig({"stops.txt": stops})["signature"], self.sig({"stops.txt": stops})["signature"])

    def test_unreadable_zip_uses_the_zip_digest(self):
        doc = self.sig({}, raw=b"not a zip")
        self.assertEqual((doc["files"], doc["fields"], doc["signature"]), (None, None, "0" * 64))


if __name__ == "__main__":
    unittest.main()
