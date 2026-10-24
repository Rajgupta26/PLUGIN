"""Credit billing core for Jewelrex AI Studio.

Policy (customer side):
  * 1 successfully delivered final image = 1 credit (never based on pixels, size or resolution).
  * Credits are reserved before a job starts, charged per delivered image, released on failure.
  * Monthly (plan) credits are used first, then prepaid top-up credits. The two are kept separate.
  * Every balance change is an append-only ledger row; balances can never go negative.

Provider side is tracked separately in `provider_calls` (estimated and actual cost per call).

All writes run inside `BEGIN IMMEDIATE` transactions, so concurrent requests are serialised by SQLite
and cannot overspend.
"""
import contextlib
import hashlib
import hmac
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

DEFAULT_CONFIG = {
    # Plans and limits are editable by an administrator (PUT /api/admin/config).
    "plans": {
        "free_trial": {"name": "Free Trial", "monthly_credits": 10, "one_time": True, "price_inr": 0},
        "starter": {"name": "Starter", "monthly_credits": 80, "price_inr": None},
        "growth": {"name": "Growth", "monthly_credits": 220, "price_inr": None},
        "professional": {"name": "Professional", "monthly_credits": 500, "price_inr": None},
        "studio": {"name": "Studio", "monthly_credits": 1100, "price_inr": None},
        "enterprise": {"name": "Enterprise", "monthly_credits": 3000, "configurable": True, "price_inr": None},
    },
    "topup_packs": [
        {"id": "pack_50", "credits": 50, "price_inr": None},
        {"id": "pack_200", "credits": 200, "price_inr": None},
        {"id": "pack_500", "credits": 500, "price_inr": None},
    ],
    "warn_thresholds": [0.80, 0.95],
    "max_images_per_request": 4,
    "reservation_ttl_minutes": 15,
    # Provider billing units. The current route is a Hugging Face ZeroGPU Space: it is billed in GPU time
    # against the account's daily ZeroGPU quota (then $1 per 10 GPU-minutes of prepaid HF credit). The
    # Space runs on an "xlarge" GPU, which Hugging Face counts at 2x. Wall time is used as an upper-bound
    # estimate of GPU time because the Space does not report exact GPU seconds per call.
    "provider_rates": {
        "huggingface-zerogpu": {"unit": "gpu_second", "usd_per_unit": 1 / 600, "multiplier": 2.0},
    },
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts (
  id TEXT PRIMARY KEY,
  plan TEXT NOT NULL,
  custom_monthly INTEGER,
  cycle_id TEXT,
  cycle_start TEXT,
  cycle_end TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id TEXT NOT NULL REFERENCES accounts(id),
  bucket TEXT NOT NULL CHECK (bucket IN ('monthly', 'prepaid')),
  delta INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('grant', 'topup', 'charge', 'expire', 'adjust')),
  job_id TEXT,
  ref TEXT,
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ledger_ref_once ON ledger(account_id, kind, bucket, ref) WHERE ref IS NOT NULL;
CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES accounts(id),
  tool TEXT NOT NULL,
  requested INTEGER NOT NULL CHECK (requested > 0),
  hold_monthly INTEGER NOT NULL CHECK (hold_monthly >= 0),
  hold_prepaid INTEGER NOT NULL CHECK (hold_prepaid >= 0),
  status TEXT NOT NULL CHECK (status IN ('reserved', 'completed', 'failed', 'expired')),
  delivered INTEGER NOT NULL DEFAULT 0,
  charged INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  created_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_active ON jobs(account_id, status);
CREATE TABLE IF NOT EXISTS provider_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT REFERENCES jobs(id),
  provider TEXT NOT NULL,
  route TEXT NOT NULL,
  unit_type TEXT NOT NULL,
  units REAL NOT NULL,
  est_cost_usd REAL,
  actual_cost_usd REAL,
  succeeded INTEGER NOT NULL,
  detail TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS webhook_events (
  event_id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  payload TEXT NOT NULL,
  received_at TEXT NOT NULL
);
"""


class BillingError(Exception):
    status = 400


class InsufficientCredits(BillingError):
    status = 402

    def __init__(self, needed, available):
        super().__init__(f"Not enough credits: need {needed}, have {available}")
        self.needed, self.available = needed, available


class NotFound(BillingError):
    status = 404


class InvalidSignature(BillingError):
    status = 401


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat(timespec="seconds")


def connect(path):
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    return conn


@contextlib.contextmanager
def tx(conn):
    """Write transaction that takes the database write lock up front (serialises concurrent requests)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- config

def get_config(conn):
    row = conn.execute("SELECT value FROM config WHERE key = 'billing'").fetchone()
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if row:
        _deep_merge(cfg, json.loads(row["value"]))
    return cfg


def set_config(conn, patch):
    with tx(conn):
        cfg = get_config(conn)
        _deep_merge(cfg, patch)
        _validate_config(cfg)
        conn.execute("INSERT INTO config(key, value) VALUES ('billing', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (json.dumps(cfg),))
    return cfg


def _deep_merge(base, patch):
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def _validate_config(cfg):
    for pid, p in cfg["plans"].items():
        if not isinstance(p.get("monthly_credits"), int) or p["monthly_credits"] < 0:
            raise BillingError(f"plan {pid}: monthly_credits must be a non-negative integer")
    for pack in cfg["topup_packs"]:
        if not isinstance(pack.get("credits"), int) or pack["credits"] <= 0:
            raise BillingError("top-up packs need a positive integer 'credits'")
    if not 1 <= int(cfg["max_images_per_request"]) <= 20:
        raise BillingError("max_images_per_request must be between 1 and 20")


# ---------------------------------------------------------------- accounts

def ensure_account(conn, account_id, plan="free_trial"):
    """Create the account on first use; new accounts get the one-time Free Trial grant."""
    if conn.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id,)).fetchone():
        return
    cfg = get_config(conn)
    with tx(conn):
        if conn.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id,)).fetchone():
            return
        t = now()
        conn.execute("INSERT INTO accounts(id, plan, cycle_id, cycle_start, created_at) VALUES (?, ?, ?, ?, ?)",
                     (account_id, plan, "free_trial", iso(t), iso(t)))
        _ledger(conn, account_id, "monthly", cfg["plans"]["free_trial"]["monthly_credits"], "grant",
                ref="free_trial", note="Free Trial (one time)")


def plan_allowance(conn, account):
    cfg = get_config(conn)
    plan = cfg["plans"].get(account["plan"])
    if plan is None:
        raise BillingError(f"unknown plan {account['plan']}")
    if plan.get("configurable") and account["custom_monthly"]:
        return int(account["custom_monthly"])
    return int(plan["monthly_credits"])


def set_account_plan(conn, account_id, plan=None, custom_monthly=None):
    """Admin: change plan label / enterprise allowance. Credits change only on a verified renewal."""
    cfg = get_config(conn)
    with tx(conn):
        acc = _account(conn, account_id)
        if plan is not None and plan not in cfg["plans"]:
            raise BillingError(f"unknown plan {plan}")
        conn.execute("UPDATE accounts SET plan = COALESCE(?, plan), custom_monthly = COALESCE(?, custom_monthly) WHERE id = ?",
                     (plan, custom_monthly, acc["id"]))


def _account(conn, account_id):
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if not row:
        raise NotFound(f"account {account_id} not found")
    return row


def _ledger(conn, account_id, bucket, delta, kind, job_id=None, ref=None, note=None):
    conn.execute("INSERT INTO ledger(account_id, bucket, delta, kind, job_id, ref, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
                 (account_id, bucket, int(delta), kind, job_id, ref, note, iso(now())))


def _bucket_totals(conn, account_id):
    rows = conn.execute("SELECT bucket, COALESCE(SUM(delta), 0) AS bal FROM ledger WHERE account_id = ? GROUP BY bucket",
                        (account_id,)).fetchall()
    bal = {"monthly": 0, "prepaid": 0}
    bal.update({r["bucket"]: r["bal"] for r in rows})
    held = conn.execute("SELECT COALESCE(SUM(hold_monthly), 0) AS m, COALESCE(SUM(hold_prepaid), 0) AS p "
                        "FROM jobs WHERE account_id = ? AND status = 'reserved'", (account_id,)).fetchone()
    return bal, {"monthly": held["m"], "prepaid": held["p"]}


def balances(conn, account_id):
    acc = _account(conn, account_id)
    cfg = get_config(conn)
    bal, held = _bucket_totals(conn, account_id)
    avail = {b: max(0, bal[b] - held[b]) for b in bal}
    grant = conn.execute("SELECT COALESCE(SUM(delta), 0) AS g FROM ledger WHERE account_id = ? AND bucket = 'monthly' "
                         "AND kind = 'grant' AND ref = ?", (account_id, acc["cycle_id"] or "free_trial")).fetchone()["g"]
    allowance = grant or plan_allowance(conn, acc)
    used = max(0, allowance - avail["monthly"])
    ratio = used / allowance if allowance else 1.0
    warn = [t for t in sorted(cfg["warn_thresholds"]) if ratio >= t]
    return {
        "account_id": account_id,
        "plan": acc["plan"],
        "plan_name": cfg["plans"][acc["plan"]]["name"],
        "cycle": {"id": acc["cycle_id"], "start": acc["cycle_start"], "end": acc["cycle_end"]},
        "monthly": {"allowance": allowance, "used": used, "available": avail["monthly"], "held": held["monthly"]},
        "prepaid": {"available": avail["prepaid"], "held": held["prepaid"]},
        "available": avail["monthly"] + avail["prepaid"],
        "usage_ratio": round(ratio, 4),
        "warning": None if not warn else ("exhausted" if avail["monthly"] == 0 else f"{int(warn[-1] * 100)}%"),
        "blocked": avail["monthly"] + avail["prepaid"] == 0,
    }


# ---------------------------------------------------------------- jobs: reserve / complete / fail

def _expire_stale(conn, account_id, cfg):
    cutoff = iso(now() - timedelta(minutes=int(cfg["reservation_ttl_minutes"])))
    conn.execute("UPDATE jobs SET status = 'expired', finished_at = ?, error = 'reservation timed out' "
                 "WHERE account_id = ? AND status = 'reserved' AND created_at < ?", (iso(now()), account_id, cutoff))


def reserve(conn, account_id, tool, images=1):
    """Hold `images` credits before a generation starts. Monthly credits first, then prepaid."""
    cfg = get_config(conn)
    images = int(images)
    if not 1 <= images <= int(cfg["max_images_per_request"]):
        raise BillingError(f"images must be between 1 and {cfg['max_images_per_request']}")
    with tx(conn):
        _account(conn, account_id)
        _expire_stale(conn, account_id, cfg)
        bal, held = _bucket_totals(conn, account_id)
        avail_m, avail_p = max(0, bal["monthly"] - held["monthly"]), max(0, bal["prepaid"] - held["prepaid"])
        if avail_m + avail_p < images:
            raise InsufficientCredits(images, avail_m + avail_p)
        hold_m = min(images, avail_m)
        job_id = "job_" + uuid.uuid4().hex
        conn.execute("INSERT INTO jobs(id, account_id, tool, requested, hold_monthly, hold_prepaid, status, created_at) "
                     "VALUES (?,?,?,?,?,?, 'reserved', ?)", (job_id, account_id, tool, images, hold_m, images - hold_m, iso(now())))
    return job(conn, job_id)


def complete(conn, job_id, delivered, provider_calls=()):
    """Charge one credit per usable final image delivered; release the rest of the hold."""
    delivered = int(delivered)
    with tx(conn):
        j = _job(conn, job_id)
        if j["status"] != "reserved":
            raise BillingError(f"job is already {j['status']}")
        if delivered < 0 or delivered > j["requested"]:
            raise BillingError(f"delivered must be between 0 and {j['requested']}")
        from_m = min(delivered, j["hold_monthly"])
        from_p = delivered - from_m
        if from_m:
            _ledger(conn, j["account_id"], "monthly", -from_m, "charge", job_id=job_id, ref=job_id, note=j["tool"])
        if from_p:
            _ledger(conn, j["account_id"], "prepaid", -from_p, "charge", job_id=job_id, ref=job_id, note=j["tool"])
        status = "completed" if delivered else "failed"
        conn.execute("UPDATE jobs SET status = ?, delivered = ?, charged = ?, finished_at = ?, error = ? WHERE id = ?",
                     (status, delivered, delivered, iso(now()), None if delivered else "no usable image delivered", job_id))
        _record_provider_calls(conn, job_id, provider_calls)
    return job(conn, job_id)


def fail(conn, job_id, error="generation failed", provider_calls=()):
    """Release the reservation and charge nothing. Provider costs are still recorded."""
    with tx(conn):
        j = _job(conn, job_id)
        if j["status"] != "reserved":
            raise BillingError(f"job is already {j['status']}")
        conn.execute("UPDATE jobs SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
                     (iso(now()), str(error)[:500], job_id))
        _record_provider_calls(conn, job_id, provider_calls)
    return job(conn, job_id)


def _record_provider_calls(conn, job_id, calls):
    cfg = get_config(conn)
    for c in calls:
        rate = cfg["provider_rates"].get(c["provider"], {})
        est = c.get("est_cost_usd")
        if est is None and rate:
            est = float(c["units"]) * rate.get("multiplier", 1) * rate["usd_per_unit"]
        conn.execute("INSERT INTO provider_calls(job_id, provider, route, unit_type, units, est_cost_usd, actual_cost_usd, "
                     "succeeded, detail, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (job_id, c["provider"], c["route"], c.get("unit_type") or rate.get("unit", "call"), float(c["units"]),
                      est, c.get("actual_cost_usd"), 1 if c.get("succeeded") else 0,
                      json.dumps(c.get("detail")) if c.get("detail") is not None else None, iso(now())))


def _job(conn, job_id):
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        raise NotFound(f"job {job_id} not found")
    return row


def job(conn, job_id):
    return dict(_job(conn, job_id))


# ---------------------------------------------------------------- top-ups, renewals, webhooks

def add_topup(conn, account_id, credits, ref, note="Prepaid top-up"):
    """Add prepaid credits once per payment reference (repeat calls with the same ref do nothing)."""
    if int(credits) <= 0:
        raise BillingError("credits must be positive")
    with tx(conn):
        _account(conn, account_id)
        if conn.execute("SELECT 1 FROM ledger WHERE account_id = ? AND kind = 'topup' AND bucket = 'prepaid' AND ref = ?",
                        (account_id, ref)).fetchone():
            return False
        _ledger(conn, account_id, "prepaid", credits, "topup", ref=ref, note=note)
    return True


def renew_cycle(conn, account_id, plan, cycle_id, start, end):
    """Start a verified billing cycle: unused monthly credits expire, the plan's credits are granted once."""
    cfg = get_config(conn)
    if plan not in cfg["plans"] or cfg["plans"][plan].get("one_time"):
        raise BillingError(f"cannot renew plan {plan}")
    with tx(conn):
        acc = _account(conn, account_id)
        if conn.execute("SELECT 1 FROM ledger WHERE account_id = ? AND kind = 'grant' AND bucket = 'monthly' AND ref = ?",
                        (account_id, cycle_id)).fetchone():
            return False
        conn.execute("UPDATE accounts SET plan = ? WHERE id = ?", (plan, account_id))
        acc = _account(conn, account_id)
        bal, held = _bucket_totals(conn, account_id)
        leftover = bal["monthly"] - held["monthly"]
        if leftover > 0:
            _ledger(conn, account_id, "monthly", -leftover, "expire", ref=cycle_id, note="unused monthly credits expired")
        _ledger(conn, account_id, "monthly", plan_allowance(conn, acc), "grant", ref=cycle_id,
                note=f"{cfg['plans'][plan]['name']} plan credits")
        conn.execute("UPDATE accounts SET cycle_id = ?, cycle_start = ?, cycle_end = ? WHERE id = ?",
                     (cycle_id, start, end, account_id))
    return True


def sign(payload_bytes, secret):
    return hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()


def process_webhook(conn, raw_body, signature, secret):
    """Verified payment webhook. Each event id is processed exactly once.

    Events:
      {"id": "...", "type": "subscription.renewed", "data": {"account_id", "plan", "cycle_id", "period_start", "period_end"}}
      {"id": "...", "type": "topup.paid", "data": {"account_id", "credits", "payment_id"}}
    """
    if not secret or not signature or not hmac.compare_digest(sign(raw_body, secret), signature):
        raise InvalidSignature("invalid webhook signature")
    event = json.loads(raw_body)
    eid, etype, data = event.get("id"), event.get("type"), event.get("data") or {}
    if not eid or not etype:
        raise BillingError("event needs id and type")
    with tx(conn):
        try:
            conn.execute("INSERT INTO webhook_events(event_id, type, payload, received_at) VALUES (?,?,?,?)",
                         (eid, etype, raw_body.decode(), iso(now())))
        except sqlite3.IntegrityError:
            return {"status": "duplicate", "event_id": eid}
    if etype == "subscription.renewed":
        ensure_account(conn, data["account_id"])
        renew_cycle(conn, data["account_id"], data["plan"], data["cycle_id"], data["period_start"], data["period_end"])
    elif etype == "topup.paid":
        ensure_account(conn, data["account_id"])
        add_topup(conn, data["account_id"], int(data["credits"]), ref=data["payment_id"])
    else:
        return {"status": "ignored", "event_id": eid}
    return {"status": "processed", "event_id": eid}


# ---------------------------------------------------------------- reporting

def history(conn, account_id, limit=30):
    rows = conn.execute("SELECT l.*, j.tool FROM ledger l LEFT JOIN jobs j ON j.id = l.job_id WHERE l.account_id = ? "
                        "ORDER BY l.id DESC LIMIT ?", (account_id, limit)).fetchall()
    return [dict(r) for r in rows]


def provider_usage(conn, since=None):
    q = ("SELECT provider, route, unit_type, COUNT(*) AS calls, SUM(succeeded) AS succeeded, SUM(units) AS units, "
         "SUM(est_cost_usd) AS est_cost_usd, SUM(actual_cost_usd) AS actual_cost_usd, "
         "SUM(CASE WHEN actual_cost_usd IS NULL THEN 1 ELSE 0 END) AS unreconciled FROM provider_calls")
    args = ()
    if since:
        q += " WHERE created_at >= ?"
        args = (since,)
    q += " GROUP BY provider, route, unit_type"
    return [dict(r) for r in conn.execute(q, args).fetchall()]


def set_actual_cost(conn, call_id, usd):
    with tx(conn):
        cur = conn.execute("UPDATE provider_calls SET actual_cost_usd = ? WHERE id = ?", (float(usd), int(call_id)))
        if not cur.rowcount:
            raise NotFound(f"provider call {call_id} not found")
