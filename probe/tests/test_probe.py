"""Tests for the probe script. They run without internet and never contact Avito.

Run from the probe folder:  python -m unittest discover -s tests -v
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import avito_probe as p  # noqa: E402

FIXTURE = (Path(__file__).parent / "synthetic_search.html").read_text(encoding="utf-8")
SECRETS = ("SECRET_CSRF_123", "SECRET_TOKEN_456", "SECRET_SESSION_789", "me@example.com",
           "Иван Петров", "123-45-67", "000 11 22", "H4sIAAAA")


class FakeResponse:
    def __init__(self, status=200, text=FIXTURE, url="https://www.avito.ru/moskva?q=iphone", history=()):
        self.status_code, self.text, self.url, self.history = status, text, url, list(history)
        self.encoding = "utf-8"


class TempOutDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        out = Path(self._tmp.name) / "probe_output"
        self._patches = [mock.patch.object(p, "OUT_DIR", out),
                         mock.patch.object(p, "LAST_RUN_FILE", out / ".last_live_request")]
        for patch in self._patches:
            patch.start()
        self.out = out

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        self._tmp.cleanup()


class ParseTests(unittest.TestCase):
    def test_parses_cards_and_removes_duplicates(self):
        items = p.parse_html(FIXTURE)
        self.assertEqual([i.item_id for i in items], ["81234567890", "112", "113", "114", "115"])
        self.assertEqual([i.price for i in items], [32000, 40000, 41000, 39000, 5000])

    def test_tracking_params_removed_from_links(self):
        url = p.parse_html(FIXTURE)[0].url
        self.assertEqual(url, "https://www.avito.ru/moskva/telefony/iphone_15_128_111")

    def test_phone_masked_in_description(self):
        descr = p.parse_html(FIXTURE)[0].description
        self.assertNotIn("123-45-67", descr)
        self.assertIn("[телефон скрыт]", descr)

    def test_outlier_detected(self):
        stats = p.price_stats([32000, 40000, 41000, 39000, 5000])
        self.assertEqual(stats["median"], 39000)
        self.assertEqual(stats["outliers"], 1)
        self.assertEqual(stats["clean_median"], 39500)


class SanitizeTests(unittest.TestCase):
    def test_no_secrets_or_personal_data_in_sample(self):
        sample = p.sanitize_html(FIXTURE)
        for secret in SECRETS:
            self.assertNotIn(secret, sample)

    def test_cards_survive_sanitizing(self):
        self.assertEqual(len(p.parse_html(p.sanitize_html(FIXTURE))), 5)


class BlockDetectionTests(unittest.TestCase):
    def test_http_errors(self):
        for status in (401, 403, 429, 500):
            self.assertIsNotNone(p.detect_block(status, "", "https://www.avito.ru/", 0))

    def test_block_page(self):
        html = "<html><body><h1>Доступ ограничен: проблема с IP</h1></body></html>"
        self.assertIsNotNone(p.detect_block(200, html, "https://www.avito.ru/", 0))

    def test_captcha_page(self):
        html = "<html><body>Подтвердите, что вы не робот</body></html>"
        self.assertIsNotNone(p.detect_block(200, html, "https://www.avito.ru/", 0))

    def test_login_redirect(self):
        self.assertIsNotNone(p.detect_block(200, "<html></html>", "https://www.avito.ru/profile/login", 0))

    def test_word_captcha_in_scripts_is_not_a_block(self):
        self.assertIsNone(p.detect_block(200, FIXTURE, "https://www.avito.ru/moskva", 5))
        self.assertIsNone(p.detect_block(200, FIXTURE, "https://www.avito.ru/moskva", 0))


class UrlTests(unittest.TestCase):
    def test_rejects_non_avito(self):
        for bad in ("https://evil.example/avito.ru", "https://avito.ru.evil.example/", "ftp://avito.ru/x"):
            with self.assertRaises(p.ProbeStop):
                p.validate_url(bad)

    def test_accepts_avito_with_quotes(self):
        self.assertEqual(p.validate_url('"https://www.avito.ru/moskva?q=a&b=c"'),
                         "https://www.avito.ru/moskva?q=a&b=c")


class LiveRequestTests(TempOutDir):
    def test_exactly_one_get_without_cookies(self):
        with mock.patch.object(p.requests.Session, "get", return_value=FakeResponse()) as get:
            code = p.run("https://www.avito.ru/moskva?q=iphone", None)
        self.assertEqual(code, 0)
        get.assert_called_once()
        headers = get.call_args.kwargs["headers"]
        self.assertNotIn("Cookie", headers)
        self.assertNotIn("Authorization", headers)

    def test_second_run_within_5_minutes_is_refused(self):
        with mock.patch.object(p.requests.Session, "get", return_value=FakeResponse()) as get:
            p.run("https://www.avito.ru/moskva?q=iphone", None)
            with self.assertRaises(p.ProbeStop) as ctx:
                p.run("https://www.avito.ru/moskva?q=iphone", None)
        self.assertEqual(ctx.exception.code, 5)
        get.assert_called_once()

    def test_block_stops_without_retry(self):
        blocked = FakeResponse(status=429, text="<h1>Доступ ограничен</h1>")
        with mock.patch.object(p.requests.Session, "get", return_value=blocked) as get:
            with self.assertRaises(p.ProbeStop) as ctx:
                p.run("https://www.avito.ru/moskva?q=iphone", None)
        self.assertEqual(ctx.exception.code, 3)
        get.assert_called_once()
        self.assertIn("ОСТАНОВЛЕНО", (self.out / "report.txt").read_text(encoding="utf-8"))

    def test_network_error_message(self):
        err = p.requests.exceptions.ConnectionError("boom")
        with mock.patch.object(p.requests.Session, "get", side_effect=err):
            with self.assertRaises(p.ProbeStop) as ctx:
                p.run("https://www.avito.ru/moskva?q=iphone", None)
        self.assertEqual(ctx.exception.code, 14)


class FileModeTests(TempOutDir):
    def test_file_mode_makes_no_requests_and_writes_clean_outputs(self):
        with mock.patch.object(p.requests.Session, "get") as get:
            code = p.run(None, str(Path(__file__).parent / "synthetic_search.html"))
        self.assertEqual(code, 0)
        get.assert_not_called()
        for name in ("report.txt", "items.csv", "page_sample.html"):
            content = (self.out / name).read_text(encoding="utf-8-sig")
            for secret in SECRETS:
                self.assertNotIn(secret, content, f"{secret!r} leaked into {name}")

    def test_missing_file(self):
        with self.assertRaises(p.ProbeStop):
            p.run(None, "C:/does/not/exist.html")

    def test_csv_opens_in_russian_excel(self):
        p.run(None, str(Path(__file__).parent / "synthetic_search.html"))
        raw = (self.out / "items.csv").read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"item_id;title;price", raw)


class InteractiveInputTests(unittest.TestCase):
    def test_link_and_dragged_file(self):
        with mock.patch("builtins.input", return_value="www.avito.ru/moskva?q=x"):
            self.assertEqual(p.ask_source(), ("https://www.avito.ru/moskva?q=x", None))
        with mock.patch("builtins.input", return_value='"C:\\Users\\Иван\\Downloads\\Авито.html"'):
            self.assertEqual(p.ask_source(), (None, "C:\\Users\\Иван\\Downloads\\Авито.html"))
        with mock.patch("builtins.input", return_value="& 'C:\\Мои файлы\\поиск.html'"):
            self.assertEqual(p.ask_source(), (None, "C:\\Мои файлы\\поиск.html"))


if __name__ == "__main__":
    unittest.main()
