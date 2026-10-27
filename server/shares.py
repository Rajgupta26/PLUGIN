"""Shared catalog links: a snapshot of chosen catalog photos that a client can open without signing in.

Locally the photos and link details are kept in server/data/shares/. On Vercel the same API is served by
api/share.mjs with Vercel Blob storage. Photos are stored under the SHA-256 of their bytes, so a photo that
is in several links is stored once. Stopping a link deletes its details, after which the page and the API
answer "not found".
"""
import base64
import binascii
import hashlib
import html
import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

MAX_IMAGE_BYTES = 3_000_000
MAX_ITEMS = 60
ID_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"   # no look-alike characters
ID_RX = re.compile(r"[A-Za-z0-9]{10}")
IMAGE_RX = re.compile(r"[a-f0-9]{64}\.(?:jpg|png|webp)")
IMAGE_URL_RX = re.compile(r"/api/share/img/([a-f0-9]{64}\.(?:jpg|png|webp))")
IMAGE_TYPES = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


class ShareError(Exception):
    status = 400


class NotFound(ShareError):
    status = 404


class Forbidden(ShareError):
    status = 403


def text(value, limit):
    return " ".join(str(value or "").split())[:limit]


def image_kind(data):
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    raise ShareError("photos must be JPG, PNG or WEBP")


def decode_image(data_url):
    """data:image/...;base64,... -> (file name, bytes). The name is the SHA-256 of the bytes plus the file type."""
    if not isinstance(data_url, str) or not data_url.startswith("data:image/") or "," not in data_url:
        raise ShareError("missing photo")
    try:
        data = base64.b64decode(data_url.split(",", 1)[1], validate=True)
    except binascii.Error:
        raise ShareError("photo is not valid base64") from None
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ShareError("photo is empty or larger than 3 MB")
    return f"{hashlib.sha256(data).hexdigest()}.{image_kind(data)}", data


def build(body):
    """Check a new link sent by the browser and return its details (without id and key)."""
    items = body.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise ShareError(f"choose between 1 and {MAX_ITEMS} photos")
    out = []
    for it in items:
        if not isinstance(it, dict) or not IMAGE_URL_RX.fullmatch(str(it.get("img", ""))):
            raise ShareError("every photo must be uploaded first")
        details = [[text(k, 24), text(v, 80)] for k, v in (it.get("details") or [])[:8]
                   if isinstance(k, str) and text(v, 80)]
        size = [int(n) if isinstance(n, (int, float)) and 1 <= n <= 20000 else 0 for n in (it.get("w"), it.get("h"))]
        out.append({"title": text(it.get("title"), 120) or "Untitled", "kind": text(it.get("kind"), 40),
                    "sku": text(it.get("sku"), 40), "details": details, "img": it["img"],
                    "w": size[0] or 4, "h": size[1] or 5})
    return {"title": text(body.get("title"), 80) or "Selected designs", "note": text(body.get("note"), 300),
            "business": text(body.get("business"), 80), "phone": text(body.get("phone"), 20),
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "items": out}


def public(share, share_id):
    """What clients see: everything except the hash of the stop-sharing key."""
    return {k: v for k, v in share.items() if k != "keyHash"} | {"id": share_id}


def page_head(share, origin):
    """<head> additions for /s/<id>: the link preview (WhatsApp etc.) and the catalog data for share.html."""
    if not share:
        return "<title>Catalog not available</title><script>window.SHARE=null</script>"
    esc = lambda s: html.escape(str(s), quote=True)  # noqa: E731
    title = share["title"] + (f" · {share['business']}" if share["business"] else "")
    n = len(share["items"])
    desc = share["note"] or f"{n} piece{'' if n == 1 else 's'} – tap to view"
    image = f'<meta property="og:image" content="{esc(origin + share["items"][0]["img"])}">' \
            '<meta name="twitter:card" content="summary_large_image">'
    data = json.dumps(share, ensure_ascii=False).replace("<", "\\u003c")
    return (f'<title>{esc(title)}</title><meta property="og:title" content="{esc(title)}">'
            f'<meta property="og:description" content="{esc(desc)}"><meta property="og:type" content="website">'
            f"{image}<script>window.SHARE={data}</script>")


class LocalStore:
    """Links and photos as files in one folder (the local server; Vercel uses Blob storage instead)."""

    def __init__(self, folder):
        self.dir = Path(folder)
        (self.dir / "img").mkdir(parents=True, exist_ok=True)

    def save_image(self, data_url):
        name, data = decode_image(data_url)
        path = self.dir / "img" / name
        if not path.exists():
            path.write_bytes(data)
        return {"name": name, "url": f"/api/share/img/{name}"}

    def image(self, name):
        """(bytes, content type) of a stored photo, or None."""
        path = self.dir / "img" / name
        if not IMAGE_RX.fullmatch(name) or not path.is_file():
            return None
        return path.read_bytes(), IMAGE_TYPES[name.rsplit(".", 1)[1]]

    def create(self, body):
        share = build(body)
        for it in share["items"]:
            if not (self.dir / "img" / IMAGE_URL_RX.fullmatch(it["img"]).group(1)).is_file():
                raise ShareError("every photo must be uploaded first")
        key = secrets.token_urlsafe(24)
        while True:
            share_id = "".join(secrets.choice(ID_CHARS) for _ in range(10))
            path = self.dir / f"{share_id}.json"
            if not path.exists():
                break
        path.write_text(json.dumps(share | {"keyHash": hashlib.sha256(key.encode()).hexdigest()}))
        return {"id": share_id, "key": key, "url": f"/s/{share_id}"}

    def get(self, share_id):
        """The full stored link (including keyHash), or None."""
        path = self.dir / f"{share_id}.json"
        if not ID_RX.fullmatch(share_id or "") or not path.is_file():
            return None
        return json.loads(path.read_text())

    def delete(self, share_id, key):
        share = self.get(share_id)
        if not share:
            raise NotFound("this link is no longer shared")
        if not secrets.compare_digest(hashlib.sha256((key or "").encode()).hexdigest(), share["keyHash"]):
            raise Forbidden("only the person who created this link can stop sharing it")
        (self.dir / f"{share_id}.json").unlink()
