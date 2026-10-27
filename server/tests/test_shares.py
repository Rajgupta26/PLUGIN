"""Tests for shared catalog links. Run: python3 -m unittest discover -s server/tests -v"""
import base64
import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import shares  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"jewellery photo" * 20
PNG = b"\x89PNG\r\n\x1a\n" + b"another photo" * 20


def data_url(data, kind="jpeg"):
    return f"data:image/{kind};base64," + base64.b64encode(data).decode()


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = shares.LocalStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def link(self, **extra):
        img = self.store.save_image(data_url(JPEG))["url"]
        body = {"title": "Bridal options", "business": "Mehta Gold Works", "phone": "98200 12345",
                "items": [{"title": "Pear Halo Ring", "kind": "Ring", "sku": "RG-0942", "img": img, "w": 960, "h": 1200,
                           "details": [["Purity", "18K"], ["Carat", ""], ["Weight", "4.10 g"]]}]}
        return self.store.create({**body, **extra})

    def test_photos_are_stored_once_under_their_hash(self):
        a = self.store.save_image(data_url(JPEG))
        b = self.store.save_image(data_url(JPEG))
        self.assertEqual(a, b)
        self.assertEqual(a["name"], hashlib.sha256(JPEG).hexdigest() + ".jpg")
        self.assertEqual(len(list((Path(self.tmp.name) / "img").iterdir())), 1)
        self.assertEqual(self.store.image(a["name"]), (JPEG, "image/jpeg"))
        self.assertTrue(self.store.save_image(data_url(PNG, "png"))["name"].endswith(".png"))

    def test_rejects_bad_photos(self):
        for bad in (None, "", "hello", data_url(b"GIF89a not allowed", "gif"), "data:image/jpeg;base64,***",
                    data_url(b"\xff\xd8\xff" + b"0" * shares.MAX_IMAGE_BYTES)):
            with self.assertRaises(shares.ShareError):
                self.store.save_image(bad)
        self.assertIsNone(self.store.image("../../app.py"))

    def test_create_get_and_public_view(self):
        r = self.link()
        self.assertRegex(r["id"], r"^[A-Za-z0-9]{10}$")
        self.assertEqual(r["url"], f"/s/{r['id']}")
        stored = self.store.get(r["id"])
        self.assertEqual(stored["keyHash"], hashlib.sha256(r["key"].encode()).hexdigest())
        view = shares.public(stored, r["id"])
        self.assertNotIn("keyHash", view)
        self.assertEqual(view["id"], r["id"])
        self.assertEqual(view["items"][0]["details"], [["Purity", "18K"], ["Weight", "4.10 g"]])   # empty values dropped
        self.assertEqual((view["title"], view["business"]), ("Bridal options", "Mehta Gold Works"))

    def test_validation(self):
        img = self.store.save_image(data_url(JPEG))["url"]
        item = {"title": "  A   ring ", "img": img}
        for body in ({}, {"items": []}, {"items": [item] * (shares.MAX_ITEMS + 1)},
                     {"items": [{"title": "x", "img": "https://evil.example/x.jpg"}]},
                     {"items": [{"title": "x", "img": "/api/share/img/" + "0" * 64 + ".jpg"}]}):   # never uploaded
            with self.assertRaises(shares.ShareError):
                self.store.create(body)
        view = self.store.get(self.store.create({"title": "", "items": [item]})["id"])
        self.assertEqual(view["title"], "Selected designs")
        self.assertEqual(view["items"][0]["title"], "A ring")
        self.assertEqual((view["items"][0]["w"], view["items"][0]["h"]), (4, 5))

    def test_only_the_creator_can_stop_sharing(self):
        r = self.link()
        with self.assertRaises(shares.Forbidden):
            self.store.delete(r["id"], "wrong-key")
        with self.assertRaises(shares.Forbidden):
            self.store.delete(r["id"], None)
        self.store.delete(r["id"], r["key"])
        self.assertIsNone(self.store.get(r["id"]))
        with self.assertRaises(shares.NotFound):
            self.store.delete(r["id"], r["key"])
        self.assertIsNone(self.store.get("../shares"))

    def test_page_head_is_escaped(self):
        r = self.link(title='Rings </script><script>alert(1)</script> "&"')
        head = shares.page_head(shares.public(self.store.get(r["id"]), r["id"]), "https://shop.example")
        self.assertEqual(head.count("</script>"), 1)   # only the closing tag of the data script
        self.assertIn("&lt;/script&gt;", head)
        self.assertIn('og:image" content="https://shop.example/api/share/img/', head)
        data = json.loads(head.split("window.SHARE=", 1)[1].rsplit("</script>", 1)[0])
        self.assertTrue(data["title"].startswith("Rings </script>"))
        self.assertIn("window.SHARE=null", shares.page_head(None, "https://shop.example"))


class ShareApiTest(unittest.TestCase):
    """The share API and client page through the local HTTP server."""

    @classmethod
    def setUpClass(cls):
        import importlib
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["JEWELREX_DB"] = os.path.join(cls.tmp.name, "api.db")
        import app
        cls.app = importlib.reload(app)
        from http.server import ThreadingHTTPServer
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), cls.app.Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.tmp.cleanup()

    def call(self, method, path, body=None, headers=None):
        req = urllib.request.Request(self.base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_share_lifecycle(self):
        s, _, raw = self.call("POST", "/api/share/image", {"data": data_url(JPEG)})
        self.assertEqual(s, 201)
        img = json.loads(raw)["url"]
        s, h, raw = self.call("GET", img)
        self.assertEqual((s, raw, h["Content-Type"]), (200, JPEG, "image/jpeg"))
        self.assertIn("immutable", h["Cache-Control"])

        s, _, raw = self.call("POST", "/api/share", {"title": "For Mrs Shah", "business": "Mehta Gold Works",
                                                     "items": [{"title": "Lotus Ring", "img": img, "w": 736, "h": 736}]})
        self.assertEqual(s, 201)
        link = json.loads(raw)
        s, _, raw = self.call("GET", f"/api/share/{link['id']}")
        self.assertEqual(s, 200)
        self.assertNotIn("keyHash", json.loads(raw))

        s, h, raw = self.call("GET", link["url"])
        page = raw.decode()
        self.assertEqual(s, 200)
        self.assertEqual(h["Cache-Control"], "no-store")
        self.assertIn('<meta property="og:title" content="For Mrs Shah · Mehta Gold Works">', page)
        self.assertIn('"title": "Lotus Ring"', page)
        self.assertNotIn("<!--SHARE-->", page)

        s, _, _ = self.call("DELETE", f"/api/share/{link['id']}", headers={"X-Share-Key": "nope"})
        self.assertEqual(s, 403)
        s, _, _ = self.call("DELETE", f"/api/share/{link['id']}", headers={"X-Share-Key": link["key"]})
        self.assertEqual(s, 200)
        s, _, raw = self.call("GET", link["url"])
        self.assertEqual(s, 404)
        self.assertIn("window.SHARE=null", raw.decode())
        s, _, _ = self.call("GET", f"/api/share/{link['id']}")
        self.assertEqual(s, 404)

    def test_private_files_are_not_served(self):
        for path in ("/server/app.py", "/%73erver/app.py", "/./server/billing.py", "/models/../server/app.py",
                     "/server%2Fapp.py", "/.git/config", "/%2egit/config", "/.claude/launch.json", "/catalog/",
                     "/README.md", "/package.json", "/api/share.mjs"):
            req = urllib.request.Request(self.base + path)
            req.full_url = self.base + path   # keep "." and ".." exactly as written
            for method in ("GET", "HEAD"):
                req.method = method
                try:
                    with urllib.request.urlopen(req) as r:
                        self.fail(f"{method} {path} was served ({r.status})")
                except urllib.error.HTTPError as e:
                    self.assertEqual(e.code, 404, f"{method} {path}")
        for path in ("/", "/index.html", "/share.html", "/logo.png?v=3", "/catalog/lotus-diamond-ring.jpg", "/models/maya.jpg"):
            s, _, _ = self.call("GET", path)
            self.assertEqual(s, 200, path)

    def test_bad_requests(self):
        s, _, raw = self.call("POST", "/api/share/image", {"data": "data:image/jpeg;base64,bm90IGEgcGhvdG8="})
        self.assertEqual(s, 400)
        self.assertIn("JPG, PNG or WEBP", json.loads(raw)["message"])
        s, _, _ = self.call("POST", "/api/share", {"items": []})
        self.assertEqual(s, 400)
        s, _, _ = self.call("GET", "/api/share/img/" + "a" * 64 + ".jpg")
        self.assertEqual(s, 404)
        s, _, raw = self.call("GET", "/s/doesnotexist")
        self.assertEqual(s, 404)


if __name__ == "__main__":
    unittest.main()
