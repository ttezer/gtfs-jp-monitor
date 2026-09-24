import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from gtfs_jp_semantic.reader import ArchiveError, Config, read_feed

CONFIG = Config(bulk_threshold=100, max_rows_per_file=1000, max_uncompressed_bytes=10_000_000)


def write_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


class ReaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, members, config=CONFIG):
        return read_feed(write_zip(self.dir / "f.zip", members), config)

    def test_default_config_loads(self):
        cfg = Config.load()
        self.assertGreater(cfg.bulk_threshold, 0)
        self.assertEqual(set(cfg.as_dict()), {"bulk_threshold", "max_rows_per_file", "max_uncompressed_bytes", "special_max_days", "min_overlap_days", "matching", "report"})

    def test_values_are_exact_and_rows_padded(self):
        feed = self.read({"stops.txt": " stop_id , stop_name,x\nS1, A ,\n\nS2,B\n,,\n"})
        t = feed.tables["stops.txt"]
        self.assertEqual(t.header, ("stop_id", "stop_name", "x"))
        self.assertEqual(t.rows, [("S1", " A ", ""), ("S2", "B", "")])  # values untrimmed, empty rows skipped
        self.assertEqual((t.encoding, t.status, t.row_count), ("utf-8", "ok", 2))

    def test_bom_cp932_and_undecodable(self):
        feed = self.read({
            "a.txt": "﻿id\n1\n".encode("utf-8"),
            "b.txt": "id,name\n1,永井\n".encode("cp932"),
            "c.txt": b"id\n\x81\xff\xfe\n",
        })
        self.assertEqual(feed.tables["a.txt"].header, ("id",))
        self.assertEqual((feed.tables["b.txt"].encoding, feed.tables["b.txt"].rows), ("cp932", [("1", "永井")]))
        self.assertEqual(feed.tables["c.txt"].status, "undecodable")

    def test_quoted_fields_and_newlines(self):
        feed = self.read({"stops.txt": 'stop_id,stop_name\nS1,"A, ""B""\nC"\n'})
        self.assertEqual(feed.tables["stops.txt"].rows, [("S1", 'A, "B"\nC')])

    def test_ragged_and_duplicate_header(self):
        feed = self.read({"a.txt": "id,v\n1,x,extra\n", "b.txt": "id,id\n1,2\n"})
        self.assertEqual((feed.tables["a.txt"].rows, feed.tables["a.txt"].ragged_rows), ([("1", "x")], 1))
        self.assertEqual(feed.tables["b.txt"].status, "duplicate_header")

    def test_too_large_file_keeps_count_only(self):
        rows = "".join(f"{i}\n" for i in range(12))
        feed = self.read({"a.txt": "id\n" + rows}, Config(100, 10, 10_000_000))
        t = feed.tables["a.txt"]
        self.assertEqual((t.status, t.rows, t.row_count), ("too_large", [], 12))

    def test_single_subdirectory_is_accepted(self):
        feed = self.read({"feed/stops.txt": "stop_id\nS1\n", "feed/routes.txt": "route_id\nR1\n"})
        self.assertEqual(sorted(feed.tables), ["routes.txt", "stops.txt"])
        self.assertEqual(feed.warnings, [])

    def test_mixed_layout_is_reported(self):
        feed = self.read({"stops.txt": "stop_id\nS1\n", "__MACOSX/._stops.txt": b"\x00", "extra/trips.txt": "trip_id\n"})
        self.assertEqual(sorted(feed.tables), ["stops.txt"])
        self.assertEqual([w["member"] for w in feed.warnings], ["__MACOSX/._stops.txt", "extra/trips.txt"])

    def test_two_subdirectories_are_not_guessed(self):
        feed = self.read({"a/stops.txt": "stop_id\n", "b/stops.txt": "stop_id\n"})
        self.assertEqual(feed.tables, {})
        self.assertEqual([w["code"] for w in feed.warnings], ["archive_layout", "archive_layout"])

    def test_unsafe_members_are_ignored(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("stops.txt", "stop_id\nS1\n")
            zf.writestr("../evil.txt", "x\n")
            zf.writestr("/abs.txt", "x\n")
        path = self.dir / "u.zip"
        path.write_bytes(buf.getvalue())
        feed = read_feed(path, CONFIG)
        self.assertEqual(sorted(feed.tables), ["stops.txt"])
        self.assertEqual(sorted(w["code"] for w in feed.warnings), ["unsafe_member_ignored", "unsafe_member_ignored"])

    def test_archive_errors(self):
        bad = self.dir / "bad.zip"
        bad.write_bytes(b"not a zip")
        with self.assertRaises(ArchiveError):
            read_feed(bad, CONFIG)
        with self.assertRaises(ArchiveError):
            self.read({"a.txt": "x" * 2000}, Config(100, 10, 1000))

    def test_non_txt_members_are_ignored(self):
        feed = self.read({"stops.txt": "stop_id\n", "readme.pdf": b"%PDF"})
        self.assertEqual(sorted(feed.tables), ["stops.txt"])
        self.assertEqual(len(feed.sha256), 64)


if __name__ == "__main__":
    unittest.main()
