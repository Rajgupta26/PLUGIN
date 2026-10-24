# Jewelrex AI Studio

AI studio for jewellery businesses: product photo clean-up, try-on photos and a credit-based billing system.

- **Enhance**: removes the background (automatic, with an in-browser AI cutout for difficult photos) and shows the piece on white, with yellow / rose / white gold previews
- **Show on Model**: creates a realistic photo of the piece being worn, using the open Qwen-Image-Edit model on a Hugging Face Space. Model photos live in `models/` (Maya, Meera, Zara); rings and bracelets use a hand photo. For earrings, rings and bracelets the AI works on a close-up of the ear, fingers or wrist, which is blended back into the full photo
- **Show on Mannequin**: the same, on display busts, an earring stand and a mannequin hand
- **Blend**: combines two designs into a new one
- **Inventory Match**: searches stock from a client's reference photo (shows a "connect inventory" state until the ERP is linked)
- **Raw / Catalog**: original uploads for the session, and finished results saved with "Add to Catalog" (kept in the browser), plus starter catalog photos in `catalog/`
- **Credits**: live meter in the header, Credits & usage, Buy credits and Upgrade plan in the profile menu

## Run locally

```bash
python3 server/app.py
```

Then open http://localhost:5173. The server serves the app and the billing API (Python 3.11+, standard library only; `gradio_client` is needed for Show on Model / Mannequin).

## Credit policy

- **1 credit = 1 usable final image delivered**, for every tool. Never based on pixels, resolution or file size.
- Credits are **reserved** before a job starts, **charged** per image delivered, and **released** if the job fails (failed jobs cost 0).
- If several provider calls produce one image, the customer pays 1 credit; every provider call is still recorded.
- Monthly plan credits are used first, then prepaid top-up credits. The two balances are kept separate; unused monthly credits expire at renewal, top-ups do not.
- Warnings at 80% and 95% of the monthly allowance; generation is blocked at 0 with Buy credits / Upgrade plan.
- Every balance change is a row in an append-only ledger (`server/data/jewelrex.db`); balances cannot go negative and concurrent requests cannot overspend.

| Plan | Credits |
|---|---|
| Free Trial | 10, one time |
| Starter | 80 / month |
| Growth | 220 / month |
| Professional | 500 / month |
| Studio | 1,100 / month |
| Enterprise | from 3,000 / month, configurable per account |

Prices are not set yet (`price_inr: null`); an administrator sets them with the config API.

## Server settings (environment variables)

| Variable | Purpose |
|---|---|
| `PORT` | default 5173 |
| `JEWELREX_DB` | SQLite file, default `server/data/jewelrex.db` |
| `WEBHOOK_SECRET` | HMAC-SHA256 secret for payment webhooks |
| `ADMIN_KEY` | enables `/api/admin/*` (send as `X-Admin-Key`) |
| `HF_TOKEN` | Hugging Face token for generation (otherwise the browser's saved token is forwarded) |
| `DEMO_PAYMENTS` | `1` (default) lets Buy credits / Upgrade complete without a payment gateway; set `0` in production |

## API

Customer (account from `X-Account-Id` until real sign-in exists):

- `GET /api/account` balances, warning level, plans, packs, recent ledger
- `POST /api/generate` Show on Model / Mannequin: reserves 1 credit, calls the provider, charges only if an image is delivered
- `POST /api/jobs` + `/api/jobs/{id}/complete|fail` browser-side tools (Enhance, Blend)
- `POST /api/checkout` buy a pack or change plan (demo mode only until a gateway is connected)

Payment gateway: `POST /api/webhooks/payment`, header `X-Jewelrex-Signature` = hex HMAC-SHA256 of the raw body. Each event `id` is processed once; renewals grant credits once per `cycle_id`, top-ups once per `payment_id`.

```json
{"id": "evt_1", "type": "subscription.renewed", "data": {"account_id": "...", "plan": "growth", "cycle_id": "sub_123-2026-11", "period_start": "...", "period_end": "..."}}
{"id": "evt_2", "type": "topup.paid", "data": {"account_id": "...", "credits": 200, "payment_id": "pay_456"}}
```

Admin (`X-Admin-Key`): `GET/PUT /api/admin/config` (plans, packs, warning thresholds, provider rates, limits), `PUT /api/admin/accounts/{id}` (plan, Enterprise allowance), `GET /api/admin/provider-usage`, `PUT /api/admin/provider-calls/{id}` (record the actual cost).

## Provider costs

Show on Model / Mannequin call the Hugging Face Space `prithivMLmods/Qwen-Image-Edit-2511-LoRAs-Fast` on **ZeroGPU**. It is billed in GPU time against the token owner's daily ZeroGPU quota (beyond that, $1 per 10 GPU-minutes of prepaid Hugging Face credit), and the Space's xlarge GPU counts double. Each call is stored with its measured duration (an upper bound for GPU seconds) and an estimated cost; the actual cost is filled in from the Hugging Face bill. Customer credits and provider costs are tracked separately.

## Tests

```bash
cd server && python3 -m unittest discover -s tests -v
```

Covers successful generation, multi-image requests, failed jobs, top-ups, exhausted limits, prepaid order, warnings, renewals, duplicate webhooks, signatures, admin limits, the append-only ledger and concurrent requests.

## Not production-ready yet

- No sign-in: accounts are identified by a header. Add real authentication before going live.
- No payment gateway: `DEMO_PAYMENTS` simulates payments. Connect e.g. Razorpay and point its webhook at `/api/webhooks/payment`.
- Enhance and Blend run in the browser, so their completion is reported by the browser; AI generation is enforced by the server.
- The free Hugging Face Space has daily limits and no uptime guarantee.

`visualizer-v1.html` is the earlier single-tool version.
