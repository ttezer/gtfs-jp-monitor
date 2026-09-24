import io
import json
import os
import stat
import sys
import tarfile
import tempfile
import textwrap
import unittest
from pathlib import Path

from gtfs_jp_monitor.analyzer import (
    AnalyzerBinary,
    AnalyzerError,
    Lock,
    install_from_archive,
    iso_to_today_arg,
    run_validate,
    sha256_file,
)

ROOT = Path(__file__).resolve().parent.parent

# A stand-in for gtfs-analyzer. Behaviour is chosen by the feed file name.
FAKE = textwrap.dedent(
    """\
    #!{python}
    import json, sys, time
    args = sys.argv[1:]
    if args == ["--version"]:
        print("gtfs-analyzer 0.14.0"); sys.exit(0)
    feed = args[1]
    out = args[args.index("-o") + 1]
    mode = feed.rsplit("/", 1)[-1].split(".")[0]
    with open(out + ".args", "w") as fh:
        json.dump(args, fh)
    def write(doc):
        with open(out, "w") as fh:
            json.dump(doc, fh)
    if mode == "ok":
        write({{"status": "ok", "validation_status": "COMPLETE"}}); sys.exit(1)
    if mode == "fatal":
        write({{"status": "fatal", "code": "ZipUnreadable", "message": "x"}}); sys.exit(2)
    if mode == "slow":
        time.sleep(5); sys.exit(0)
    if mode == "crash":
        print("boom", file=sys.stderr); sys.exit(101)
    if mode == "noreport":
        sys.exit(0)
    if mode == "garbage":
        open(out, "w").write("{{not json"); sys.exit(0)
    if mode == "inconsistent":
        write({{"status": "ok", "validation_status": "COMPLETE"}}); sys.exit(2)
    """
)


def make_fake_binary(directory: Path) -> Path:
    path = directory / "gtfs-analyzer"
    path.write_text(FAKE.format(python=sys.executable), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def make_archive(directory: Path, members: list[tuple[str, bytes | None, str | None]]) -> Path:
    """members: (name, data, symlink_target)."""
    archive = directory / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name, data, link in members:
            info = tarfile.TarInfo(name)
            if link is not None:
                info.type = tarfile.SYMTYPE
                info.linkname = link
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return archive


class LockTest(unittest.TestCase):
    def test_repository_lock_is_consistent(self):
        lock = Lock.load(ROOT / "analyzer.lock.json")
        self.assertEqual(lock.release_tag, f"v{lock.version}")
        self.assertEqual(lock.production_platform, "x86_64-linux")
        self.assertIn(lock.production_platform, lock.assets)
        self.assertEqual(
            lock.download_url("linux", "x86_64"),
            f"https://github.com/ttezer/gtfs-analyzer/releases/download/{lock.release_tag}/gtfs-analyzer-x86_64-linux.tar.gz",
        )
        with self.assertRaises(AnalyzerError):
            lock.asset_for("windows", "aarch64")


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_extracts_only_the_binary(self):
        archive = make_archive(self.dir, [("./gtfs-analyzer", b"BIN", None), ("./LICENSE", b"MIT", None)])
        binary = install_from_archive(archive, sha256_file(archive), self.dir / "bin")
        self.assertEqual(binary.read_bytes(), b"BIN")
        self.assertEqual(sorted(p.name for p in (self.dir / "bin").iterdir()), ["gtfs-analyzer"])
        self.assertTrue(os.access(binary, os.X_OK))

    def test_checksum_mismatch(self):
        archive = make_archive(self.dir, [("./gtfs-analyzer", b"BIN", None)])
        with self.assertRaises(AnalyzerError):
            install_from_archive(archive, "0" * 64, self.dir / "bin")
        self.assertFalse((self.dir / "bin").exists())

    def test_traversal_and_symlinks_are_refused(self):
        for members in (
            [("../gtfs-analyzer", b"BIN", None)],
            [("./gtfs-analyzer", None, "/bin/sh")],
            [("./gtfs-analyzer", b"A", None), ("gtfs-analyzer", b"B", None)],
            [("./other", b"X", None)],
        ):
            with self.subTest(members=[m[0] for m in members]):
                archive = make_archive(self.dir, members)
                with self.assertRaises(AnalyzerError):
                    install_from_archive(archive, sha256_file(archive), self.dir / "bin")
                self.assertFalse((self.dir.parent / "gtfs-analyzer").exists())


class PinningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.lock = Lock.load(ROOT / "analyzer.lock.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_marker_pins_only_the_installed_binary(self):
        from gtfs_jp_monitor.analyzer import pinned_release_for, write_install_marker

        binary = self.dir / "gtfs-analyzer"
        binary.write_bytes(b"release build")
        self.assertIsNone(pinned_release_for(binary, self.lock))
        write_install_marker(binary, self.lock)
        self.assertEqual(pinned_release_for(binary, self.lock), self.lock.release_tag)
        binary.write_bytes(b"swapped afterwards")
        self.assertIsNone(pinned_release_for(binary, self.lock))

    def test_marker_for_another_release_does_not_pin(self):
        from gtfs_jp_monitor.analyzer import pinned_release_for

        binary = self.dir / "gtfs-analyzer"
        binary.write_bytes(b"x")
        (self.dir / "INSTALLED.json").write_text(json.dumps({"release_tag": "v0.13.0", "binary_sha256": sha256_file(binary)}))
        self.assertIsNone(pinned_release_for(binary, self.lock))
        (self.dir / "INSTALLED.json").write_text("{broken")
        self.assertIsNone(pinned_release_for(binary, self.lock))

    def test_install_release_verifies_and_marks(self):
        from dataclasses import replace

        from gtfs_jp_monitor.analyzer import current_platform, install_release, pinned_release_for

        os_name, arch = current_platform()
        archive = make_archive(self.dir, [("./gtfs-analyzer", b"BIN", None)])
        lock = replace(self.lock, assets={f"{arch}-{os_name}": {"name": "a.tar.gz", "sha256": sha256_file(archive)}})
        fetched = []

        def fetch(url, dest):
            fetched.append(url)
            dest.write_bytes(archive.read_bytes())

        binary = install_release(lock, self.dir / "bin", fetch)
        self.assertEqual(pinned_release_for(binary, lock), lock.release_tag)
        self.assertTrue(fetched[0].startswith("https://github.com/ttezer/gtfs-analyzer/releases/download/"))
        self.assertFalse((self.dir / "bin" / "a.tar.gz").exists())

        bad = replace(lock, assets={f"{arch}-{os_name}": {"name": "a.tar.gz", "sha256": "0" * 64}})
        with self.assertRaises(AnalyzerError):
            install_release(bad, self.dir / "bin2", fetch)
        self.assertFalse((self.dir / "bin2" / "INSTALLED.json").exists())


class RunValidateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.binary = AnalyzerBinary.inspect(make_fake_binary(self.dir))

    def tearDown(self):
        self.tmp.cleanup()

    def run_mode(self, mode: str, timeout: float = 30):
        feed = self.dir / f"{mode}.zip"
        feed.write_bytes(b"PK")
        return run_validate(self.binary, feed, "2026-04-01", "v3", self.dir, timeout=timeout)

    def test_inspect_reads_version_and_hash(self):
        self.assertEqual(self.binary.version, "0.14.0")
        self.assertEqual(len(self.binary.sha256), 64)
        self.assertFalse(self.binary.is_pinned)
        with self.assertRaises(AnalyzerError):
            AnalyzerBinary.inspect(self.binary.path, pinned_release="v9.9.9")
        self.assertTrue(AnalyzerBinary.inspect(self.binary.path, pinned_release="v0.14.0").is_pinned)

    def test_notices_exit_is_success(self):
        run = self.run_mode("ok")
        self.assertEqual((run.exit_code, run.error_code), (1, None))
        self.assertEqual(run.report["status"], "ok")
        self.assertFalse((self.dir / "report.json").exists())

    def test_date_and_profile_are_always_passed(self):
        self.run_mode("ok")
        args = json.loads((self.dir / "report.json.args").read_text())
        self.assertEqual(args[args.index("--today") + 1], "20260401")
        self.assertEqual(args[args.index("--gtfs-jp-profile") + 1], "v3")

    def test_fatal_report(self):
        run = self.run_mode("fatal")
        self.assertEqual((run.exit_code, run.error_code, run.report["code"]), (2, None, "ZipUnreadable"))

    def test_failures(self):
        for mode, code in (("crash", "ANALYZER_CRASH"), ("noreport", "NO_REPORT"),
                           ("garbage", "INVALID_REPORT"), ("inconsistent", "INCONSISTENT_EXIT")):
            with self.subTest(mode=mode):
                run = self.run_mode(mode)
                self.assertIsNone(run.report)
                self.assertEqual(run.error_code, code)
        self.assertIn("boom", self.run_mode("crash").stderr_tail)

    def test_timeout(self):
        run = self.run_mode("slow", timeout=0.5)
        self.assertEqual(run.error_code, "TIMEOUT")
        self.assertIsNone(run.report)

    def test_bad_arguments(self):
        with self.assertRaises(ValueError):
            run_validate(self.binary, self.dir / "x.zip", "2026-04-01", "v5", self.dir)
        with self.assertRaises(ValueError):
            iso_to_today_arg("20260401")


@unittest.skipUnless(os.environ.get("GTFS_ANALYZER_BIN") and os.environ.get("GTFS_SAMPLE_ZIP"),
                     "set GTFS_ANALYZER_BIN and GTFS_SAMPLE_ZIP to run against a real analyzer")
class RealAnalyzerTest(unittest.TestCase):
    def test_real_binary_produces_a_valid_generation_record(self):
        from gtfs_jp_monitor.generation import AnalyzerIdentity, FeedMeta, GenerationMeta, build_generation

        from .schema_support import HAVE_JSONSCHEMA, errors, validator

        binary = AnalyzerBinary.inspect(Path(os.environ["GTFS_ANALYZER_BIN"]))
        zip_path = Path(os.environ["GTFS_SAMPLE_ZIP"])
        with tempfile.TemporaryDirectory() as tmp:
            first = run_validate(binary, zip_path, "2026-04-01", "auto", Path(tmp))
            second = run_validate(binary, zip_path, "2026-04-01", "auto", Path(tmp))
        self.assertIsNone(first.error_code, first.stderr_tail)
        ident = AnalyzerIdentity(binary.version, f"v{binary.version}", binary.sha256, binary.os, binary.arch, "auto")
        meta = GenerationMeta("17ab34e1-dcb8-4b2e-9cda-ae2b68f4c444", sha256_file(zip_path), "", "2026-04-01", "2027-03-31", "")
        feed = FeedMeta("sample-org", "SampleFeed", "")
        doc_a = build_generation(first.report, feed, meta, ident)
        doc_b = build_generation(second.report, feed, meta, ident)
        self.assertEqual(doc_a, doc_b)  # same binary, same date -> same record
        if HAVE_JSONSCHEMA:
            self.assertEqual(errors(validator("generation.schema.json"), doc_a), [])


if __name__ == "__main__":
    unittest.main()
