import json
import unittest

from gtfs_jp_monitor.gtfsdatajp import (
    ApiError,
    GtfsDataJpClient,
    NotFound,
    is_api_download_url,
)
from gtfs_jp_monitor.ids import InvalidIdentifier

BASE = "https://api.gtfs-data.jp/v2"
UID_CUR = "17ab34e1-dcb8-4b2e-9cda-ae2b68f4c444"
UID_PREV = "b1be1add-3553-4b31-86bc-348479c25526"
UID_NEXT = "4a4a81e7-166c-4671-bd5e-bf20c6dc52e0"


def gtfs_url(uid: str) -> str:
    return f"{BASE}/organizations/nagai-unyu/feeds/Nagaibus/files/feed.zip?uid={uid}"


def envelope(body) -> bytes:
    return json.dumps({"code": 200, "message": "ok", "body": body}).encode("utf-8")


FEEDS_BODY = [
    {
        "organization_id": "nagai-unyu",
        "organization_name": "永井運輸",
        "organization_email": "someone@example.jp",
        "feed_id": "Nagaibus",
        "feed_name": "永井運輸バス",
        "feed_pref_id": 10,
        "feed_license": "CC BY 4.0",
        "feed_license_url": "https://creativecommons.org/licenses/by/4.0/deed.ja",
        "feed_memo": "",
        "feed_is_discontinued": False,
        "feed_discontinued_date": "",
        "last_published_at": "2026-07-08",
        "latest_feed_start_date": "2026-04-01",
        "latest_feed_end_date": "2027-03-31",
    },
    {"organization_id": "../evil", "feed_id": "x", "organization_email": "a@b.c"},
    "not-an-object",
]

FILES_BODY = {
    "feed_license": "CC BY 4.0",
    "max_prev": 1,
    "max_next": 1,
    "gtfs_files": [
        {"gtfs_file_uid": UID_NEXT, "rid": "next_1", "gtfs_url": gtfs_url(UID_NEXT), "from_date": "2026-10-01",
         "to_date": "2027-03-31", "published_at": "2026-09-15T10:00:00.123456+09:00", "memo": "改正"},
        {"gtfs_file_uid": UID_CUR, "rid": "current", "gtfs_url": gtfs_url(UID_CUR), "from_date": "2026-04-01",
         "to_date": "2027-03-31", "published_at": "2026-07-08T18:09:26.517446+09:00", "memo": ""},
        {"gtfs_file_uid": UID_PREV, "rid": "prev_1", "gtfs_url": gtfs_url(UID_PREV), "from_date": "2025-10-01",
         "to_date": "2026-03-31", "published_at": "2025-09-26T06:23:03.882725+09:00", "memo": None},
    ],
}


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls: list[str] = []

    def __call__(self, url, timeout):
        self.urls.append(url)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def make_client(responses, **kwargs):
    fake_clock = FakeClock()
    transport = FakeTransport(responses)
    client = GtfsDataJpClient(transport=transport, sleep=fake_clock.sleep, clock=fake_clock.clock, **kwargs)
    return client, transport, fake_clock


class ListFeedsTest(unittest.TestCase):
    def test_parses_feeds_and_rejects_bad_ids(self):
        client, transport, _ = make_client([(200, {}, envelope(FEEDS_BODY))])
        feeds, rejected = client.list_feeds()
        self.assertEqual(transport.urls, [f"{BASE}/feeds"])
        self.assertEqual(len(feeds), 1)
        feed = feeds[0]
        self.assertEqual((feed.org_id, feed.feed_id, feed.feed_name), ("nagai-unyu", "Nagaibus", "永井運輸バス"))
        self.assertIsNone(feed.discontinued_date)  # "" becomes None
        self.assertEqual(feed.feed_pref_id, 10)
        self.assertEqual(sorted(r.code for r in rejected), ["FEED_NOT_OBJECT", "INVALID_FEED_ID"])

    def test_contact_fields_are_not_kept(self):
        client, _, _ = make_client([(200, {}, envelope(FEEDS_BODY))])
        feeds, _ = client.list_feeds()
        self.assertNotIn("someone@example.jp", repr(feeds))

    def test_bad_envelope(self):
        client, _, _ = make_client([(200, {}, json.dumps({"feeds": []}).encode())])
        with self.assertRaises(ApiError):
            client.list_feeds()


class GenerationsTest(unittest.TestCase):
    def test_requests_whole_history_including_next(self):
        client, transport, _ = make_client([(200, {}, envelope(FILES_BODY))])
        result = client.get_generations("nagai-unyu", "Nagaibus")
        self.assertIn("max_prev=10000", transport.urls[0])
        self.assertIn("max_next=1000", transport.urls[0])
        self.assertEqual([g.rid for g in result.generations], ["next_1", "current", "prev_1"])
        self.assertTrue(result.complete)
        self.assertEqual(result.generations[2].memo, "")

    def test_incomplete_history_is_detected(self):
        body = dict(FILES_BODY, max_prev=5)
        client, _, _ = make_client([(200, {}, envelope(body))])
        self.assertFalse(client.get_generations("nagai-unyu", "Nagaibus").complete)

    def test_untrusted_records_are_rejected(self):
        files = [dict(f) for f in FILES_BODY["gtfs_files"]]
        files[0]["gtfs_url"] = "https://evil.example.com/feed.zip"  # next_1
        files[2]["gtfs_file_uid"] = "../../etc"  # prev_1
        files.append(dict(FILES_BODY["gtfs_files"][1]))  # duplicate of current
        client, _, _ = make_client([(200, {}, envelope(dict(FILES_BODY, gtfs_files=files)))])
        result = client.get_generations("nagai-unyu", "Nagaibus")
        self.assertEqual([g.uid for g in result.generations], [UID_CUR])
        self.assertEqual(sorted(r.code for r in result.rejected), ["DUPLICATE_UID", "INVALID_UID", "UNTRUSTED_GTFS_URL"])
        # Rejected entries with a known rid still count: the API did return them.
        self.assertTrue(result.complete)

    def test_unclassifiable_rid_makes_history_incomplete(self):
        files = [dict(f) for f in FILES_BODY["gtfs_files"]]
        files[2]["rid"] = "latest"  # was prev_1; cannot prove the prev_ history is whole
        client, _, _ = make_client([(200, {}, envelope(dict(FILES_BODY, gtfs_files=files)))])
        result = client.get_generations("nagai-unyu", "Nagaibus")
        self.assertEqual([r.code for r in result.rejected], ["INVALID_RID"])
        self.assertFalse(result.complete)

    def test_invalid_path_is_refused_before_any_request(self):
        client, transport, _ = make_client([])
        with self.assertRaises(InvalidIdentifier):
            client.get_generations("..", "Nagaibus")
        self.assertEqual(transport.urls, [])

    def test_download_url_host_check(self):
        self.assertTrue(is_api_download_url(gtfs_url(UID_CUR)))
        for bad in ("http://api.gtfs-data.jp/x", "https://api.gtfs-data.jp.evil.com/x",
                    "https://user@api.gtfs-data.jp/x", "https://s3.amazonaws.com/x", None):
            self.assertFalse(is_api_download_url(bad), bad)


class RetryAndPolitenessTest(unittest.TestCase):
    def test_retries_server_errors_then_succeeds(self):
        client, transport, clock = make_client([(503, {}, b""), (502, {}, b""), (200, {}, envelope([]))])
        self.assertEqual(client.list_feeds(), ([], []))
        self.assertEqual(len(transport.urls), 3)
        self.assertIn(1.0, clock.sleeps)  # backoff_base ** 0
        self.assertIn(2.0, clock.sleeps)  # backoff_base ** 1

    def test_honours_retry_after(self):
        client, _, clock = make_client([(429, {"Retry-After": "7"}, b""), (200, {}, envelope([]))])
        client.list_feeds()
        self.assertIn(7.0, clock.sleeps)

    def test_retry_after_is_capped(self):
        client, _, clock = make_client([(429, {"retry-after": "3600"}, b""), (200, {}, envelope([]))], max_backoff=30)
        client.list_feeds()
        self.assertIn(30.0, clock.sleeps)

    def test_network_errors_are_retried(self):
        client, transport, _ = make_client([ConnectionError("reset"), (200, {}, envelope([]))])
        client.list_feeds()
        self.assertEqual(len(transport.urls), 2)

    def test_certificate_errors_are_not_retried(self):
        import ssl
        import urllib.error

        err = urllib.error.URLError(ssl.SSLCertVerificationError("unable to get local issuer certificate"))
        client, transport, _ = make_client([err, (200, {}, envelope([]))])
        with self.assertRaises(ApiError):
            client.list_feeds()
        self.assertEqual(len(transport.urls), 1)

    def test_gives_up_after_max_retries(self):
        client, transport, _ = make_client([(503, {}, b"")] * 3, max_retries=2)
        with self.assertRaises(ApiError):
            client.list_feeds()
        self.assertEqual(len(transport.urls), 3)

    def test_404_and_other_4xx_are_not_retried(self):
        client, transport, _ = make_client([(404, {}, b"")])
        with self.assertRaises(NotFound):
            client.get_generations("nagai-unyu", "Missing")
        self.assertEqual(len(transport.urls), 1)
        client, transport, _ = make_client([(403, {}, b"")])
        with self.assertRaises(ApiError):
            client.list_feeds()
        self.assertEqual(len(transport.urls), 1)

    def test_invalid_json_is_an_error(self):
        client, _, _ = make_client([(200, {}, b"<html>")])
        with self.assertRaises(ApiError):
            client.list_feeds()

    def test_min_interval_between_requests(self):
        client, _, clock = make_client([(200, {}, envelope([])), (200, {}, envelope([]))], min_interval=1.0)
        client.list_feeds()
        clock.now += 0.25
        client.list_feeds()
        self.assertEqual(clock.sleeps, [0.75])


if __name__ == "__main__":
    unittest.main()
