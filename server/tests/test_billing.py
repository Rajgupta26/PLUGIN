"""Tests for the credit policy. Run: python3 -m unittest discover -s server/tests -v"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import billing  # noqa: E402

SECRET = "test-secret"


class BillingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.db")
        self.conn = billing.connect(self.db)
        billing.ensure_account(self.conn, "acc")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def bal(self):
        return billing.balances(self.conn, "acc")

    def webhook(self, event):
        raw = json.dumps(event).encode()
        return billing.process_webhook(self.conn, raw, billing.sign(raw, SECRET), SECRET)

    def use(self, n):
        """Generate and deliver n images (in requests of up to 4)."""
        while n:
            k = min(4, n)
            j = billing.reserve(self.conn, "acc", "model", k)
            billing.complete(self.conn, j["id"], k)
            n -= k

    def renew(self, plan="starter", cycle="c1", eid=None):
        return self.webhook({"id": eid or f"evt-{cycle}", "type": "subscription.renewed", "data": {
            "account_id": "acc", "plan": plan, "cycle_id": cycle, "period_start": "2026-10-01", "period_end": "2026-11-01"}})

    # ---- free trial / basics
    def test_new_account_gets_one_time_free_trial(self):
        b = self.bal()
        self.assertEqual(b["plan"], "free_trial")
        self.assertEqual(b["available"], 10)
        billing.ensure_account(self.conn, "acc")  # second call grants nothing more
        self.assertEqual(self.bal()["available"], 10)

    # ---- successful generation
    def test_success_charges_one_credit_per_delivered_image(self):
        j = billing.reserve(self.conn, "acc", "model", 1)
        self.assertEqual(self.bal()["available"], 9)  # held
        billing.complete(self.conn, j["id"], 1)
        b = self.bal()
        self.assertEqual(b["available"], 9)
        self.assertEqual(b["monthly"]["held"], 0)
        self.assertEqual(billing.job(self.conn, j["id"])["charged"], 1)

    def test_three_images_in_one_request_cost_three(self):
        j = billing.reserve(self.conn, "acc", "model", 3)
        billing.complete(self.conn, j["id"], 3)
        self.assertEqual(self.bal()["available"], 7)

    def test_partial_delivery_charges_only_delivered_images(self):
        j = billing.reserve(self.conn, "acc", "model", 3)
        billing.complete(self.conn, j["id"], 2)
        self.assertEqual(self.bal()["available"], 8)

    def test_many_provider_calls_for_one_image_charge_one_credit_but_record_all_costs(self):
        j = billing.reserve(self.conn, "acc", "model", 1)
        calls = [{"provider": "huggingface-zerogpu", "route": "space/edit", "units": 10, "succeeded": False},
                 {"provider": "huggingface-zerogpu", "route": "space/edit", "units": 12, "succeeded": True}]
        billing.complete(self.conn, j["id"], 1, calls)
        self.assertEqual(self.bal()["available"], 9)
        usage = billing.provider_usage(self.conn)[0]
        self.assertEqual(usage["calls"], 2)
        self.assertAlmostEqual(usage["units"], 22)
        self.assertAlmostEqual(usage["est_cost_usd"], 22 * 2 / 600)  # xlarge GPU counts double, $1 / 10 GPU-min
        self.assertEqual(usage["unreconciled"], 2)

    # ---- failed jobs
    def test_failed_job_releases_reservation_and_charges_nothing(self):
        j = billing.reserve(self.conn, "acc", "model", 1)
        billing.fail(self.conn, j["id"], "timeout", [{"provider": "huggingface-zerogpu", "route": "r", "units": 5}])
        b = self.bal()
        self.assertEqual(b["available"], 10)
        self.assertEqual(billing.job(self.conn, j["id"])["status"], "failed")
        self.assertEqual(billing.provider_usage(self.conn)[0]["calls"], 1)  # provider cost still recorded

    def test_zero_delivered_is_treated_as_failure(self):
        j = billing.reserve(self.conn, "acc", "model", 2)
        billing.complete(self.conn, j["id"], 0)
        self.assertEqual(self.bal()["available"], 10)
        self.assertEqual(billing.job(self.conn, j["id"])["status"], "failed")

    def test_job_cannot_be_settled_twice(self):
        j = billing.reserve(self.conn, "acc", "model", 1)
        billing.complete(self.conn, j["id"], 1)
        with self.assertRaises(billing.BillingError):
            billing.complete(self.conn, j["id"], 1)
        with self.assertRaises(billing.BillingError):
            billing.fail(self.conn, j["id"])
        self.assertEqual(self.bal()["available"], 9)

    def test_cannot_deliver_more_than_reserved(self):
        j = billing.reserve(self.conn, "acc", "model", 1)
        with self.assertRaises(billing.BillingError):
            billing.complete(self.conn, j["id"], 2)

    # ---- exhausted limits + prepaid order
    def test_exhausted_credits_block_generation(self):
        j = billing.reserve(self.conn, "acc", "model", 4)
        billing.complete(self.conn, j["id"], 4)
        j = billing.reserve(self.conn, "acc", "model", 4)
        billing.complete(self.conn, j["id"], 4)
        j = billing.reserve(self.conn, "acc", "model", 2)
        billing.complete(self.conn, j["id"], 2)
        b = self.bal()
        self.assertEqual(b["available"], 0)
        self.assertTrue(b["blocked"])
        with self.assertRaises(billing.InsufficientCredits):
            billing.reserve(self.conn, "acc", "model", 1)

    def test_monthly_credits_used_before_prepaid(self):
        billing.add_topup(self.conn, "acc", 5, ref="pay_1")
        j = billing.reserve(self.conn, "acc", "model", 4)  # 10 monthly available
        self.assertEqual((j["hold_monthly"], j["hold_prepaid"]), (4, 0))
        billing.complete(self.conn, j["id"], 4)
        j = billing.reserve(self.conn, "acc", "model", 4)
        billing.complete(self.conn, j["id"], 4)
        j = billing.reserve(self.conn, "acc", "model", 4)  # 2 monthly + 2 prepaid
        self.assertEqual((j["hold_monthly"], j["hold_prepaid"]), (2, 2))
        billing.complete(self.conn, j["id"], 4)
        b = self.bal()
        self.assertEqual((b["monthly"]["available"], b["prepaid"]["available"]), (0, 3))

    def test_reservation_needs_enough_for_all_requested_images(self):
        self.use(8)
        with self.assertRaises(billing.InsufficientCredits) as ctx:
            billing.reserve(self.conn, "acc", "model", 3)
        self.assertEqual(ctx.exception.available, 2)

    def test_stale_reservations_expire_and_release_credits(self):
        j = billing.reserve(self.conn, "acc", "model", 4)
        self.conn.execute("UPDATE jobs SET created_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (j["id"],))
        billing.reserve(self.conn, "acc", "model", 1)  # triggers clean-up
        self.assertEqual(billing.job(self.conn, j["id"])["status"], "expired")
        self.assertEqual(self.bal()["available"], 9)

    # ---- warnings
    def test_usage_warnings_at_80_and_95_percent(self):
        self.renew("starter")  # 80 credits
        for n, expect in [(63, None), (1, "80%"), (12, "95%"), (4, "exhausted")]:
            self.use(n)
            self.assertEqual(self.bal()["warning"], expect, f"after using {self.bal()['monthly']['used']}")

    # ---- top-ups and webhooks
    def test_topup_adds_prepaid_credits_once(self):
        r = self.webhook({"id": "evt-1", "type": "topup.paid", "data": {"account_id": "acc", "credits": 50, "payment_id": "pay_9"}})
        self.assertEqual(r["status"], "processed")
        self.assertEqual(self.bal()["prepaid"]["available"], 50)
        # Same payment re-sent under a different event id must not add credits again
        self.webhook({"id": "evt-2", "type": "topup.paid", "data": {"account_id": "acc", "credits": 50, "payment_id": "pay_9"}})
        self.assertEqual(self.bal()["prepaid"]["available"], 50)

    def test_duplicate_webhook_is_ignored(self):
        self.assertEqual(self.renew(eid="evt-dup")["status"], "processed")
        self.assertEqual(self.renew(eid="evt-dup")["status"], "duplicate")
        self.assertEqual(self.bal()["monthly"]["available"], 80)

    def test_repeated_renewal_for_same_cycle_grants_once(self):
        self.renew("growth", cycle="2026-10", eid="a")
        self.renew("growth", cycle="2026-10", eid="b")  # different event, same billing cycle
        self.assertEqual(self.bal()["monthly"]["available"], 220)

    def test_renewal_expires_unused_monthly_but_keeps_prepaid(self):
        billing.add_topup(self.conn, "acc", 30, ref="pay_x")
        self.renew("starter", cycle="2026-10")
        j = billing.reserve(self.conn, "acc", "model", 4)
        billing.complete(self.conn, j["id"], 4)
        self.renew("starter", cycle="2026-11")
        b = self.bal()
        self.assertEqual(b["monthly"]["available"], 80)
        self.assertEqual(b["prepaid"]["available"], 30)

    def test_invalid_webhook_signature_is_rejected(self):
        raw = json.dumps({"id": "x", "type": "topup.paid", "data": {}}).encode()
        with self.assertRaises(billing.InvalidSignature):
            billing.process_webhook(self.conn, raw, "bad", SECRET)

    def test_enterprise_allowance_is_configurable(self):
        billing.set_account_plan(self.conn, "acc", custom_monthly=4500)
        self.renew("enterprise", cycle="e1")
        self.assertEqual(self.bal()["monthly"]["available"], 4500)

    def test_admin_can_change_plan_limits(self):
        billing.set_config(self.conn, {"plans": {"starter": {"monthly_credits": 90}}})
        self.renew("starter", cycle="s1")
        self.assertEqual(self.bal()["monthly"]["available"], 90)
        with self.assertRaises(billing.BillingError):
            billing.set_config(self.conn, {"plans": {"starter": {"monthly_credits": -1}}})

    # ---- ledger integrity
    def test_ledger_is_append_only(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE ledger SET delta = 1000")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM ledger")

    def test_balance_never_negative(self):
        total = self.conn.execute("SELECT SUM(delta) FROM ledger WHERE account_id='acc'").fetchone()[0]
        self.assertGreaterEqual(total, 0)

    # ---- concurrency
    def test_concurrent_requests_cannot_overspend(self):
        results = []
        barrier = threading.Barrier(25)

        def worker():
            conn = billing.connect(self.db)
            barrier.wait()
            try:
                j = billing.reserve(conn, "acc", "model", 1)
                billing.complete(conn, j["id"], 1)
                results.append("ok")
            except billing.InsufficientCredits:
                results.append("blocked")
            finally:
                conn.close()

        threads = [threading.Thread(target=worker) for _ in range(25)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count("ok"), 10)
        self.assertEqual(results.count("blocked"), 15)
        self.assertEqual(self.bal()["available"], 0)
        self.assertEqual(self.conn.execute("SELECT SUM(delta) FROM ledger WHERE account_id='acc'").fetchone()[0], 0)


class ApiTest(unittest.TestCase):
    """End-to-end through the HTTP server with the provider stubbed (no real AI calls)."""

    @classmethod
    def setUpClass(cls):
        import importlib
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["JEWELREX_DB"] = os.path.join(cls.tmp.name, "api.db")
        os.environ["DEMO_PAYMENTS"] = "1"
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

    def call(self, method, path, body=None, account="api-acc"):
        import urllib.request
        import urllib.error
        req = urllib.request.Request(self.base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", "X-Account-Id": account})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_generate_success_and_failure(self):
        import provider
        ok_call = {"provider": "huggingface-zerogpu", "route": "r", "unit_type": "gpu_second", "units": 8, "succeeded": True}
        provider.edit_image = lambda *a, **k: ("data:image/png;base64,AAAA", ok_call)
        s, r = self.call("POST", "/api/generate", {"tool": "model", "images_b64_json": "[]", "prompt": "p"}, "gen")
        self.assertEqual(s, 200)
        self.assertEqual(r["account"]["available"], 9)

        def boom(*a, **k):
            raise provider.ProviderError("You have exceeded your ZeroGPU quota", {**ok_call, "units": 0, "succeeded": False}, quota=True)
        provider.edit_image = boom
        s, r = self.call("POST", "/api/generate", {"tool": "model", "images_b64_json": "[]", "prompt": "p"}, "gen")
        self.assertEqual(s, 429)
        self.assertEqual(r["account"]["available"], 9)  # failed generation charged nothing

    def test_exhausted_returns_402_and_checkout_unblocks(self):
        for _ in range(10):
            s, r = self.call("POST", "/api/jobs", {"tool": "enhance"}, "ex")
            self.call("POST", f"/api/jobs/{r['job']['id']}/complete", {"delivered": 1}, "ex")
        s, r = self.call("POST", "/api/jobs", {"tool": "enhance"}, "ex")
        self.assertEqual(s, 402)
        s, r = self.call("POST", "/api/checkout", {"kind": "topup", "id": "pack_50"}, "ex")
        self.assertEqual(r["account"]["prepaid"]["available"], 50)
        s, r = self.call("POST", "/api/jobs", {"tool": "enhance"}, "ex")
        self.assertEqual(s, 201)

    def test_browser_cannot_complete_ai_jobs(self):
        for tool in ("model", "blend"):
            s, r = self.call("POST", "/api/jobs", {"tool": tool}, "cheat")
            self.assertEqual(s, 400)

    def test_webhook_requires_signature(self):
        s, r = self.call("POST", "/api/webhooks/payment", {"id": "x", "type": "topup.paid", "data": {}})
        self.assertEqual(s, 401)


if __name__ == "__main__":
    unittest.main()
