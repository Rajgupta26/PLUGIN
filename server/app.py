"""Jewelrex AI Studio server: serves the app and the credit-billing API.

Run:  python3 server/app.py          (http://localhost:5173)

Environment:
  PORT                 default 5173
  JEWELREX_DB          SQLite path, default server/data/jewelrex.db
  WEBHOOK_SECRET       HMAC secret for payment webhooks (required to accept webhooks)
  ADMIN_KEY            enables /api/admin/* (send as X-Admin-Key)
  HF_TOKEN             Hugging Face token used for generation (otherwise the browser's saved token is used)
  DEMO_PAYMENTS=1      lets Buy credits / Upgrade complete without a payment gateway (default on locally)

Accounts: until real sign-in exists, the browser sends X-Account-Id (default "demo-account").
"""
import json
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
import billing  # noqa: E402
import provider  # noqa: E402
import shares  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = os.environ.get("JEWELREX_DB", str(ROOT / "server" / "data" / "jewelrex.db"))
SHARES = shares.LocalStore(Path(DB_PATH).parent / "shares")   # shared catalog links (Vercel keeps them in Blob)
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
DEMO_PAYMENTS = os.environ.get("DEMO_PAYMENTS", "1") == "1"
DEFAULT_ACCOUNT = "demo-account"
AI_STYLE = "Anything2Real"
# Self-signed demo checkouts go through the same webhook path as real ones
_DEMO_SECRET = WEBHOOK_SECRET or uuid.uuid4().hex

# The only files the server hands out: the app's pages and images. Everything else (this server's code,
# its database, git and editor folders) is refused, however the path is written.
PUBLIC_FILES = {"index.html", "share.html", "styles.css", "logo.png"}
PUBLIC_DIRS = {"catalog", "models"}

ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def log_message(self, fmt, *args):
        if "/api/" in str(args[0] if args else ""):   # args[0] is a status code for send_error
            super().log_message(fmt, *args)

    # ---- dispatch
    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith("/api/share/img/"):
            return self._share_image(path.rsplit("/", 1)[1])
        if path.startswith("/s/"):
            return self._share_page(path[3:])
        if self.path.startswith("/api/"):
            return self._api("GET")
        if not self._public():
            return self.send_error(404)
        return super().do_GET()

    def do_HEAD(self):
        if not self._public():
            return self.send_error(404)
        return super().do_HEAD()

    def _public(self):
        # translate_path decodes %xx escapes and drops "." and ".." parts, so check the file it resolves to
        try:
            parts = Path(self.translate_path(self.path)).relative_to(ROOT).parts
        except ValueError:
            return False
        return not parts or (len(parts) == 1 and parts[0] in PUBLIC_FILES) or (len(parts) > 1 and parts[0] in PUBLIC_DIRS)

    def do_POST(self):
        return self._api("POST")

    def do_PUT(self):
        return self._api("PUT")

    def do_DELETE(self):
        return self._api("DELETE")

    def end_headers(self):
        if not self.path.startswith(("/api/", "/s/")):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _api(self, method):
        path = urlparse(self.path).path
        for m, rx, fn in ROUTES:
            match = rx.match(path)
            if m == method and match:
                conn = billing.connect(DB_PATH)
                try:
                    status, body = fn(self, conn, *match.groups())
                except billing.InsufficientCredits as e:
                    status, body = 402, {"error": "insufficient_credits", "message": str(e),
                                         "needed": e.needed, "available": e.available}
                except (billing.BillingError, shares.ShareError) as e:
                    status, body = e.status, {"error": type(e).__name__, "message": str(e)}
                except (ValueError, KeyError, json.JSONDecodeError) as e:
                    status, body = 400, {"error": "bad_request", "message": str(e)}
                finally:
                    conn.close()
                return self._json(status, body)
        return self._json(404, {"error": "not_found"})

    # ---- helpers
    def raw_body(self):
        if not hasattr(self, "_raw"):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 25_000_000:
                raise billing.BillingError("request too large")
            self._raw = self.rfile.read(n) if n else b""
        return self._raw

    def body(self):
        raw = self.raw_body()
        return json.loads(raw) if raw else {}

    def account(self, conn):
        acc = (self.headers.get("X-Account-Id") or DEFAULT_ACCOUNT).strip()[:64]
        if not re.fullmatch(r"[A-Za-z0-9_.@-]+", acc):
            raise billing.BillingError("invalid account id")
        billing.ensure_account(conn, acc)
        return acc

    def require_admin(self):
        if not ADMIN_KEY or self.headers.get("X-Admin-Key") != ADMIN_KEY:
            raise billing.InvalidSignature("admin key required")

    def _json(self, status, body):
        self._send(status, json.dumps(body).encode(), "application/json", "no-store")

    def _send(self, status, data, content_type, cache):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    def _share_image(self, name):
        found = SHARES.image(name)
        if not found:
            return self.send_error(404)
        # Photos are stored under the hash of their bytes, so they never change
        self._send(200, found[0], found[1], "public, max-age=31536000, immutable")

    def _share_page(self, share_id):
        """The client's view of a shared link: share.html with the link's details and preview tags filled in."""
        share = SHARES.get(share_id)
        share = share and shares.public(share, share_id)
        page = (ROOT / "share.html").read_text().replace(
            "<!--SHARE-->", shares.page_head(share, f"http://{self.headers.get('Host', 'localhost')}"), 1)
        self._send(200 if share else 404, page.encode(), "text/html; charset=utf-8", "no-store")


def account_view(conn, acc):
    cfg = billing.get_config(conn)
    return {**billing.balances(conn, acc), "history": billing.history(conn, acc, 15),
            "plans": cfg["plans"], "topup_packs": cfg["topup_packs"], "warn_thresholds": cfg["warn_thresholds"],
            "demo_payments": DEMO_PAYMENTS}


# ---------------------------------------------------------------- customer API

@route("GET", "/api/account")
def get_account(h, conn):
    return 200, account_view(conn, h.account(conn))


@route("POST", "/api/jobs")
def create_job(h, conn):
    """Reserve credits for a job the browser runs itself (Enhance: no paid provider involved)."""
    acc, b = h.account(conn), h.body()
    if b.get("tool") != "enhance":
        raise billing.BillingError("use /api/generate for AI tools")
    return 201, {"job": billing.reserve(conn, acc, b["tool"], int(b.get("images", 1))), "account": billing.balances(conn, acc)}


def _own_job(conn, acc, job_id):
    j = billing.job(conn, job_id)
    if j["account_id"] != acc:
        raise billing.NotFound("job not found")
    return j


@route("POST", r"/api/jobs/([A-Za-z0-9_]+)/complete")
def complete_job(h, conn, job_id):
    acc = h.account(conn)
    j = _own_job(conn, acc, job_id)
    if j["tool"] != "enhance":
        raise billing.BillingError("AI jobs are completed by the server")
    return 200, {"job": billing.complete(conn, job_id, int(h.body().get("delivered", 1))), "account": billing.balances(conn, acc)}


@route("POST", r"/api/jobs/([A-Za-z0-9_]+)/fail")
def fail_job(h, conn, job_id):
    acc = h.account(conn)
    _own_job(conn, acc, job_id)
    return 200, {"job": billing.fail(conn, job_id, h.body().get("error", "failed in browser")), "account": billing.balances(conn, acc)}


@route("POST", "/api/generate")
def generate(h, conn):
    """Show on Model / Mannequin / Blend: reserve 1 credit, call the provider, charge only if an image comes back."""
    acc, b = h.account(conn), h.body()
    tool = b.get("tool")
    if tool not in ("model", "mannequin", "blend"):
        raise billing.BillingError("tool must be model, mannequin or blend")
    j = billing.reserve(conn, acc, tool, 1)
    try:
        image, call = provider.edit_image(b["images_b64_json"], b["prompt"], b.get("style") or AI_STYLE, b.get("hf_token"))
    except provider.ProviderError as e:
        billing.fail(conn, j["id"], str(e), [e.call])
        return (429 if e.quota else 502), {"error": "provider_quota" if e.quota else "provider_failed",
                                            "message": str(e)[:400], "account": billing.balances(conn, acc)}
    except Exception as e:  # noqa: BLE001
        billing.fail(conn, j["id"], str(e))
        raise
    billing.complete(conn, j["id"], 1, [call])
    return 200, {"image": image, "job_id": j["id"], "account": billing.balances(conn, acc)}


@route("POST", "/api/checkout")
def checkout(h, conn):
    """Buy credits / change plan. Without a payment gateway (demo mode) the payment is simulated by
    sending a signed event through the same webhook handler a real gateway would call."""
    acc, b = h.account(conn), h.body()
    if not DEMO_PAYMENTS:
        return 501, {"error": "payments_not_configured", "message": "Connect a payment gateway to sell credits."}
    cfg = billing.get_config(conn)
    eid = "demo_" + uuid.uuid4().hex
    if b.get("kind") == "topup":
        pack = next((p for p in cfg["topup_packs"] if p["id"] == b.get("id")), None)
        if not pack:
            raise billing.BillingError("unknown top-up pack")
        event = {"id": eid, "type": "topup.paid", "data": {"account_id": acc, "credits": pack["credits"], "payment_id": eid}}
    elif b.get("kind") == "plan":
        if b.get("id") not in cfg["plans"] or cfg["plans"][b["id"]].get("one_time"):
            raise billing.BillingError("unknown plan")
        start = datetime.now(timezone.utc)
        event = {"id": eid, "type": "subscription.renewed", "data": {
            "account_id": acc, "plan": b["id"], "cycle_id": f"{b['id']}-{eid}", "period_start": start.isoformat(timespec="seconds"),
            "period_end": (start + timedelta(days=30)).isoformat(timespec="seconds")}}
    else:
        raise billing.BillingError("kind must be topup or plan")
    raw = json.dumps(event).encode()
    billing.process_webhook(conn, raw, billing.sign(raw, _DEMO_SECRET), _DEMO_SECRET)
    return 200, {"account": account_view(conn, acc)}


@route("POST", "/api/share/image")
def share_image(h, conn):
    """Upload one photo for a shared link (JSON {"data": "data:image/jpeg;base64,..."})."""
    return 201, SHARES.save_image(h.body().get("data"))


@route("POST", "/api/share")
def share_create(h, conn):
    """Create a link from uploaded photos. The key it returns is needed to stop sharing."""
    return 201, SHARES.create(h.body())


@route("GET", "/api/share/status")
def share_status(h, conn):
    return 200, {"on": True, "via": "local", "site": None}   # same answer shape as the Vercel function


@route("GET", r"/api/share/([A-Za-z0-9]{10})")
def share_get(h, conn, share_id):
    share = SHARES.get(share_id)
    if not share:
        raise shares.NotFound("this link is no longer shared")
    return 200, shares.public(share, share_id)


@route("DELETE", r"/api/share/([A-Za-z0-9]{10})")
def share_delete(h, conn, share_id):
    SHARES.delete(share_id, h.headers.get("X-Share-Key"))
    return 200, {"ok": True}


@route("POST", "/api/webhooks/payment")
def webhook(h, conn):
    """Real payment gateway webhook (HMAC-SHA256 of the raw body in X-Jewelrex-Signature)."""
    return 200, billing.process_webhook(conn, h.raw_body(), h.headers.get("X-Jewelrex-Signature", ""), WEBHOOK_SECRET)


# ---------------------------------------------------------------- admin API

@route("GET", "/api/admin/config")
def admin_get_config(h, conn):
    h.require_admin()
    return 200, billing.get_config(conn)


@route("PUT", "/api/admin/config")
def admin_set_config(h, conn):
    h.require_admin()
    return 200, billing.set_config(conn, h.body())


@route("PUT", r"/api/admin/accounts/([A-Za-z0-9_.@-]+)")
def admin_account(h, conn, acc):
    h.require_admin()
    b = h.body()
    billing.ensure_account(conn, acc)
    billing.set_account_plan(conn, acc, b.get("plan"), b.get("custom_monthly"))
    return 200, billing.balances(conn, acc)


@route("GET", "/api/admin/provider-usage")
def admin_usage(h, conn):
    h.require_admin()
    return 200, {"by_provider": billing.provider_usage(conn)}


@route("PUT", r"/api/admin/provider-calls/(\d+)")
def admin_actual_cost(h, conn, call_id):
    h.require_admin()
    billing.set_actual_cost(conn, call_id, h.body()["actual_cost_usd"])
    return 200, {"ok": True}


def main():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    billing.connect(DB_PATH).close()
    port = int(os.environ.get("PORT", "5173"))
    print(f"Jewelrex AI Studio on http://localhost:{port}  (db: {DB_PATH}, demo payments: {DEMO_PAYMENTS})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
