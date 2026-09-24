import hashlib
import io
import tempfile
import unittest
import urllib.error
from pathlib import Path

from gtfs_jp_monitor.download import DownloadError, _HttpsOnlyRedirects, download_zip

URL = "https://api.gtfs-data.jp/v2/organizations/o/feeds/f/files/feed.zip?uid=17ab34e1-dcb8-4b2e-9cda-ae2b68f4c444"
SIGNED = "https://bucket.s3.amazonaws.com/feed.zip?AWSAccessKeyId=SECRETKEY&Signature=abc"


class BrokenStream(io.BytesIO):
    def read(self, n=-1):
        raise ConnectionError("reset")


def opener_for(status=200, body=b"PK\x03\x04data", headers=None, stream=None, exc=None):
    def opener(url, timeout):
        if exc:
            raise exc
        return status, headers or {}, stream or io.BytesIO(body)
    return opener


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name) / "work" / "feed.zip"

    def tearDown(self):
        self.tmp.cleanup()

    def test_success_hashes_while_streaming(self):
        body = b"PK" + b"x" * 200_000
        result = download_zip(URL, self.dest, opener=opener_for(body=body))
        self.assertEqual(result.sha256, hashlib.sha256(body).hexdigest())
        self.assertEqual(result.size, len(body))
        self.assertEqual(self.dest.read_bytes(), body)

    def test_untrusted_url_is_refused(self):
        with self.assertRaises(DownloadError) as ctx:
            download_zip("https://evil.example.com/feed.zip", self.dest, opener=opener_for())
        self.assertEqual(ctx.exception.code, "UNTRUSTED_URL")

    def test_error_codes_and_cleanup(self):
        cases = [
            (opener_for(status=404), "SOURCE_UNAVAILABLE"),
            (opener_for(status=410), "SOURCE_UNAVAILABLE"),
            (opener_for(status=500), "DOWNLOAD_FAILED"),
            (opener_for(headers={"Content-Length": "999"}), "TOO_LARGE"),
            (opener_for(body=b"x" * 1000), "TOO_LARGE"),
            (opener_for(body=b""), "DOWNLOAD_FAILED"),
            (opener_for(stream=BrokenStream(b"")), "DOWNLOAD_FAILED"),
            (opener_for(exc=urllib.error.URLError("refused non-https redirect")), "DOWNLOAD_FAILED"),
        ]
        for opener, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(DownloadError) as ctx:
                    download_zip(URL, self.dest, max_bytes=500, opener=opener)
                self.assertEqual(ctx.exception.code, code)
                self.assertFalse(self.dest.exists())

    def test_signed_url_never_appears_in_errors(self):
        err = urllib.error.URLError(f"cannot connect to {SIGNED}")
        with self.assertRaises(DownloadError) as ctx:
            download_zip(URL, self.dest, opener=opener_for(exc=err))
        self.assertNotIn("SECRETKEY", str(ctx.exception))
        self.assertNotIn("amazonaws", str(ctx.exception))

    def test_plain_http_redirect_is_refused(self):
        handler = _HttpsOnlyRedirects()
        with self.assertRaises(urllib.error.URLError) as ctx:
            handler.redirect_request(None, None, 302, "Found", {}, "http://bucket.s3.amazonaws.com/feed.zip?Signature=abc")
        self.assertNotIn("Signature", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
