#!/usr/bin/env python3
"""Offline tests for scripts/scrape.py (standard library only, no network).
Run: python3 scripts/test_scrape.py"""
import csv, importlib.util, io, json, os, sys, tempfile, threading, unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

_spec = importlib.util.spec_from_file_location(
    "scrape", os.path.join(os.path.dirname(os.path.abspath(__file__)), "scrape.py"))
scrape = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scrape)

BBOX = ["30.0", "30.5", "-98.0", "-97.5"]  # Nominatim order: south, north, west, east


class GridCells(unittest.TestCase):
    def test_count_centers_radius(self):
        cells = scrape.grid_cells(BBOX, 3)
        self.assertEqual(len(cells), 9)
        for c in cells:
            self.assertTrue(30.0 < float(c["lat"]) < 30.5)
            self.assertTrue(-98.0 < float(c["lon"]) < -97.5)
            self.assertGreaterEqual(c["radius"], 1000)
            self.assertEqual(len(c["lat"].split(".")[1]), 6)
            self.assertEqual(c["zoom"], 13)  # ~16 km tiles: city-district view, not a 3 km block

    def test_zoom_follows_tile_size(self):
        self.assertEqual(scrape.grid_cells(BBOX, 1)[0]["zoom"], 11)                # ~48 km: clamped
        self.assertEqual(scrape.grid_cells(["0", "0.001", "0", "0.001"], 1)[0]["zoom"], 17)  # 110 m: clamped

    def test_one_cell_is_bbox_center(self):
        (c,) = scrape.grid_cells(BBOX, 1)
        self.assertEqual((c["lat"], c["lon"]), ("30.250000", "-97.750000"))
        self.assertGreater(c["radius"], 30000)  # ~0.5 x 0.5 degree box

    def test_tiny_box_floors_at_1000(self):
        self.assertEqual(scrape.grid_cells(["0", "0.001", "0", "0.001"], 2)[0]["radius"], 1000)


class Dedupe(unittest.TestCase):
    def test_key_order(self):
        rows = [
            {"place_id": "P1", "cid": "C1", "title": "a"},
            {"place_id": "P1", "cid": "C9", "title": "dup place_id"},
            {"cid": "C2", "link": "L1"},
            {"cid": "C2", "link": "L9", "title": "dup cid"},
            {"link": "L3"},
            {"link": "L3", "title": "dup link"},
            {"title": "Joe's", "address": "1 Main"},
            {"title": "Joe's", "address": "1 Main", "phone": "dup title+address"},
            {"title": "Joe's", "address": "2 Main"},
        ]
        out = scrape.dedupe(rows)
        self.assertEqual(len(out), 5)
        self.assertEqual(out[0]["title"], "a")  # first occurrence wins
        self.assertNotIn("dup", json.dumps(out))


class InBbox(unittest.TestCase):
    def test_margin_and_missing(self):
        self.assertTrue(scrape.in_bbox({"latitude": "30.2", "longitude": "-97.7"}, BBOX))
        self.assertTrue(scrape.in_bbox({"latitude": "30.54", "longitude": "-97.7"}, BBOX))   # inside 10%
        self.assertFalse(scrape.in_bbox({"latitude": "30.56", "longitude": "-97.7"}, BBOX))  # past 10%
        self.assertFalse(scrape.in_bbox({"latitude": "30.2", "longitude": "-98.1"}, BBOX))
        self.assertTrue(scrape.in_bbox({"latitude": "", "longitude": ""}, BBOX))
        self.assertTrue(scrape.in_bbox({"latitude": "n/a"}, BBOX))
        self.assertTrue(scrape.in_bbox({}, BBOX))


class ExtractEmails(unittest.TestCase):
    def test_mailto_regex_junk_preference_cap(self):
        html = """
          <a href="mailto:Owner@Gmail.com?subject=Hi">Mail</a>
          <a href="mailto:INFO@acmeplumbing.com">info</a>
          Write to sales@acmeplumbing.com or info@acmeplumbing.com.
          <img src="logo@2x.png"> hello@example.com x@sentry.io a@b.wixpress.com
          name@company.org bob@wordpress.com jane@outside.net
        """
        out = scrape.extract_emails(html, "www.acmeplumbing.com")
        self.assertEqual(out, ["info@acmeplumbing.com", "sales@acmeplumbing.com", "owner@gmail.com"])

    def test_empty(self):
        self.assertEqual(scrape.extract_emails("", "x.com"), [])
        self.assertEqual(scrape.extract_emails("no emails here", "x.com"), [])


class ContactLinks(unittest.TestCase):
    def test_discovers_same_site_only(self):
        html = """<a href="/contact-us#form">Get in touch</a>
                  <a href="https://www.acme.com/team">About us</a>
                  <a href="https://facebook.com/acme/about">FB</a>
                  <a href="mailto:a@acme.com">contact</a>
                  <a href="/services">Services</a>"""
        out = scrape.contact_links(html, "https://acme.com")
        self.assertEqual(out[:2], ["https://acme.com/contact-us", "https://www.acme.com/team"])
        self.assertEqual(len(out), 3)
        self.assertEqual(out[2], "https://acme.com/contact")  # first fixed fallback
        self.assertFalse(any("facebook" in u or "mailto" in u for u in out))

    def test_fallbacks_and_max(self):
        self.assertEqual(scrape.contact_links("", "acme.com"),
                         ["http://acme.com/contact", "http://acme.com/contact-us", "http://acme.com/about"])
        many = "".join(f'<a href="/contact{i}">x</a>' for i in range(6))
        self.assertEqual(len(scrape.contact_links(many, "http://acme.com")), 3)


# ── End to end against a mock engine ──
FIELDS = ["input_id", "link", "title", "category", "address", "website", "phone", "review_count",
          "review_rating", "latitude", "longitude", "cid", "place_id", "emails"]

def _row(pid, lat="30.2"):
    return {"title": f"Biz {pid}", "place_id": pid, "cid": "c" + pid, "latitude": lat,
            "longitude": "-97.7", "phone": "+1 555", "emails": f"{pid.lower()}@biz.net"}

CELL_ROWS = {"job-1": [_row("A"), _row("B"), _row("C")],
             "job-2": [_row("C"), _row("D")],
             "job-3": None,  # this cell fails
             "job-4": [_row("A"), _row("E"), _row("F", lat="40.0")]}  # F is outside the city box


class Proxies(unittest.TestCase):
    def args(self, *proxies, proxies_file=None):
        return mock.Mock(proxies=list(proxies) or None, proxies_file=proxies_file)

    def test_flag_file_dedupe(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("# pool\nhttp://h2:8080\n\nsocks5h://u:p@h3:1080\nh4:3128\n")
        got = scrape.collect_proxies(self.args(" socks5://u:p@h1:1080 ,http://h2:8080", proxies_file=f.name))
        self.assertEqual(got, ["socks5://u:p@h1:1080", "http://h2:8080", "socks5h://u:p@h3:1080", "http://h4:3128"])

    def test_list_forms_and_repeat(self):
        got = scrape.collect_proxies(self.args('["socks5://h1:1", "h2:2"]', "http://h3:3 https://h4:4", "h1:9"))
        self.assertEqual(got, ["socks5://h1:1", "http://h2:2", "http://h3:3", "https://h4:4", "http://h1:9"])

    def test_env_fallback_and_override(self):
        with mock.patch.dict(os.environ, {"SCRAPER_PROXIES": "http://env:1"}):
            self.assertEqual(scrape.collect_proxies(self.args()), ["http://env:1"])
            self.assertEqual(scrape.collect_proxies(self.args("https://cli:2")), ["https://cli:2"])
        with mock.patch.dict(os.environ, {"SCRAPER_PROXIES": ""}):
            self.assertEqual(scrape.collect_proxies(self.args()), [])

    def test_bad_scheme_masks_credentials(self):
        with self.assertRaises(ValueError) as cm:
            scrape.collect_proxies(self.args("ftp://user:secret@h:21"))
        self.assertNotIn("secret", str(cm.exception))

    def test_mask(self):
        self.assertEqual(scrape.mask_proxy("socks5://u:p@h:1080"), "socks5://***@h:1080")
        self.assertEqual(scrape.mask_proxy("http://h:8080"), "http://h:8080")

    def test_verdicts(self):
        v = scrape.proxy_verdict
        self.assertIsNone(v("200 200 "))
        self.assertIsNone(v("302 000 https://consent.google.com/"))
        self.assertEqual(v("302 200 https://www.google.com/sorry/index"), "blocked by Google")
        self.assertEqual(v("429 200 "), "blocked by Google")
        self.assertEqual(v("000 000 "), "unreachable or timed out")
        self.assertEqual(v(""), "unreachable or timed out")
        self.assertEqual(v("000 407 "), "proxy refused (HTTP 407)")
        self.assertEqual(v("503 200 "), "HTTP 503")

    def test_filter_keeps_order_and_masks(self):
        verdicts = {"http://a:1": None, "http://u:secret@b:2": "proxy refused (HTTP 407)", "http://c:3": None}
        log = io.StringIO()
        with mock.patch.object(scrape.shutil, "which", return_value="/usr/bin/curl"), \
             mock.patch.object(scrape, "check_proxy", side_effect=verdicts.get), redirect_stdout(log):
            self.assertEqual(scrape.filter_proxies(list(verdicts)), ["http://a:1", "http://c:3"])
        self.assertIn("http://***@b:2: proxy refused (HTTP 407)", log.getvalue())
        self.assertNotIn("secret", log.getvalue())

    def test_no_curl_sends_unchecked(self):
        with mock.patch.object(scrape.shutil, "which", return_value=None), redirect_stdout(io.StringIO()):
            self.assertEqual(scrape.filter_proxies(["http://a:1"]), ["http://a:1"])


class MockEngine(BaseHTTPRequestHandler):
    created, polls, bodies = 0, {}, []

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        MockEngine.bodies.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)))))
        MockEngine.created += 1
        self._send(201, json.dumps({"id": f"job-{MockEngine.created}"}))

    def do_GET(self):
        parts = self.path.strip("/").split("/")  # api v1 jobs [id [download]]
        if len(parts) == 3:
            return self._send(200, "[]")
        jid = parts[3]
        if len(parts) == 5:
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=FIELDS, restval="")
            w.writeheader(); w.writerows(CELL_ROWS[jid])
            return self._send(200, buf.getvalue(), "text/csv")
        n = MockEngine.polls[jid] = MockEngine.polls.get(jid, 0) + 1
        status = "failed" if CELL_ROWS[jid] is None else ("ok" if n >= 2 else "working")
        self._send(200, json.dumps({"Status": status}))


class EndToEnd(unittest.TestCase):
    def test_grid_run(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), MockEngine)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        out = os.path.join(tempfile.mkdtemp(), "leads.csv")
        argv = ["scrape.py", "plumbers in Austin TX", "--city", "Austin, TX",
                "--grid", "2", "--no-deep-emails", "--out", out,
                "--proxies", "socks5://u:secret@p1:1080,http://p2:8080"]
        log = io.StringIO()
        try:
            with mock.patch.object(scrape, "BASE", f"http://127.0.0.1:{srv.server_port}"), \
                 mock.patch.object(scrape, "geocode", return_value=("30.25", "-97.75", BBOX)), \
                 mock.patch.object(scrape.time, "sleep"), \
                 mock.patch.object(scrape, "check_proxy", return_value=None), \
                 mock.patch.object(sys, "argv", argv), redirect_stdout(log):
                scrape.main()
        finally:
            srv.shutdown(); srv.server_close()
        text = log.getvalue()
        self.assertIn("cell 4/4: 3 rows, 6 unique so far", text)
        self.assertIn("cell 3/4 skipped", text)
        self.assertIn("dropped 1 rows outside the city box", text)
        self.assertIn("2 of 2 working", text)
        self.assertIn("Rotating 2 proxies: socks5://***@p1:1080, http://p2:8080", text)
        self.assertNotIn("secret", text)
        self.assertEqual(len(MockEngine.bodies), 4)
        for b in MockEngine.bodies:
            self.assertEqual(b["proxies"], ["socks5://u:secret@p1:1080", "http://p2:8080"])
        with open(out, newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0], scrape.LEAD)
        self.assertEqual([r[0] for r in rows[1:]], ["Biz A", "Biz B", "Biz C", "Biz D", "Biz E"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
