# Jewelrex AI Studio

AI studio for jewellery businesses: product photo clean-up, try-on photos and a credit-based billing system.

- **Enhance**: removes the background (automatic, with an in-browser AI cutout for difficult photos) and shows the piece on white, with yellow / rose / white gold previews
- **Show on Model**: creates a realistic photo of the piece being worn, using the open Qwen-Image-Edit model on a Hugging Face Space. Model photos live in `models/` (Maya, Meera, Zara); rings and bracelets use a hand photo. For earrings, rings and bracelets the AI works on a close-up of the ear, fingers or wrist, which is blended back into the full photo
- **Show on Mannequin**: the same, on display busts, an earring stand and a mannequin hand
- **Blend**: combines two designs into a new one
- **Inventory Match**: searches stock from a client's reference photo (shows a "connect inventory" state until the ERP is linked)
- **Raw / Catalog**: original uploads for the session, and finished results saved with "Add to Catalog" (kept in the browser), plus starter catalog photos in `catalog/`
- **Share catalog**: pick pieces from the catalog (or one piece with its link icon), give the link a title and note, and send it to a client. The client opens `/s/<id>` on any phone without signing in: a gallery of the pieces with their details (never prices), a full-size view, and an "Enquire on WhatsApp" button when a phone number is set in Profile settings. "Stop sharing" turns a link off for everyone
- **Credits**: live meter in the header, Credits & usage, Buy credits and Upgrade plan in the profile menu

## Run locally

```bash
python3 server/app.py
```

Then open http://localhost:5173. The server serves the app and the billing API (Python 3.11+, standard library only; `gradio_client` is needed for Show on Model / Mannequin). It only hands out the app's own pages and images, never its code, database or other folders.

The pages use `styles.css`, built with Tailwind from the classes in `index.html` and `share.html`. After changing classes, rebuild it:

```bash
npx tailwindcss@3.4.5 -i styles.src.css -o styles.css --minify
```

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

Shared catalog links (no account needed; served by `server/app.py` locally and `api/share.mjs` on Vercel):

- `POST /api/share/image` upload one photo `{"data": "data:image/jpeg;base64,..."}`; photos are stored once, under the SHA-256 of their bytes
- `POST /api/share` create a link from uploaded photos: returns `{id, key, url}`; the key is needed to stop sharing
- `GET /api/share/{id}` the link's details, `DELETE /api/share/{id}` with header `X-Share-Key` stops sharing
- `GET /s/{id}` the client's page (`share.html`, with WhatsApp / social preview tags), `GET /api/share/img/{name}` a photo
- `GET /api/share/status` whether sharing is switched on (`{"on": true, ...}`)

Payment gateway: `POST /api/webhooks/payment`, header `X-Jewelrex-Signature` = hex HMAC-SHA256 of the raw body. Each event `id` is processed once; renewals grant credits once per `cycle_id`, top-ups once per `payment_id`.

```json
{"id": "evt_1", "type": "subscription.renewed", "data": {"account_id": "...", "plan": "growth", "cycle_id": "sub_123-2026-11", "period_start": "...", "period_end": "..."}}
{"id": "evt_2", "type": "topup.paid", "data": {"account_id": "...", "credits": 200, "payment_id": "pay_456"}}
```

Admin (`X-Admin-Key`): `GET/PUT /api/admin/config` (plans, packs, warning thresholds, provider rates, limits), `PUT /api/admin/accounts/{id}` (plan, Enterprise allowance), `GET /api/admin/provider-usage`, `PUT /api/admin/provider-calls/{id}` (record the actual cost).

## Provider costs

Show on Model / Mannequin call the Hugging Face Space `prithivMLmods/Qwen-Image-Edit-2511-LoRAs-Fast` on **ZeroGPU**. It is billed in GPU time against the token owner's daily ZeroGPU quota (beyond that, $1 per 10 GPU-minutes of prepaid Hugging Face credit), and the Space's xlarge GPU counts double. Each call is stored with its measured duration (an upper bound for GPU seconds) and an estimated cost; the actual cost is filled in from the Hugging Face bill. Customer credits and provider costs are tracked separately.

## Switching on shared links on Vercel (one time)

Links made on `localhost` only open on that computer. On the live site, links are stored in Vercel Blob:

1. In the Vercel dashboard, open the project, then **Storage** → **Create** → **Blob** (Public or Private both work)
2. **Connect** the store to the project for all environments
3. Redeploy (Deployments → ⋯ → Redeploy), so the new settings reach the site

Links always point at the project's production address (`VERCEL_PROJECT_PRODUCTION_URL`), even when made from a preview or branch address such as `plugin-git-main-….vercel.app`: Vercel asks visitors of those addresses to log in. Work from the production address too, since the catalog is saved per address in the browser.

Until then, Create link shows "Sharing isn't switched on for this site yet". To check, open `/api/share/status` on the live site: it says `"on": true` once the store is connected and the site redeployed. `.vercelignore` keeps the Python server and other non-site files out of the deployment. Free (Hobby) limits per month: 1 GB stored, 2,000 uploads (each new photo and each link is one), 10,000 reads (each time a client opens a link is one), 10 GB downloads. If a limit is passed, Vercel pauses Blob for 30 days instead of charging.

## Tests

```bash
cd server && python3 -m unittest discover -s tests -v
```

Covers successful generation, multi-image requests, failed jobs, top-ups, exhausted limits, prepaid order, warnings, renewals, duplicate webhooks, signatures, admin limits, the append-only ledger and concurrent requests, plus shared links (photo checks, link details, the client page, stopping a link).

## Not production-ready yet

- The live site on Vercel has no billing server, so it runs in test mode: credits are free test credits kept in each visitor's browser (anyone can reset them), and AI photos are made by the visitor's browser on the free Hugging Face Space. Selling credits needs `server/app.py` (or an equivalent) deployed with the items below.
- No sign-in: accounts are identified by a header. Add real authentication before going live.
- No payment gateway: `DEMO_PAYMENTS` simulates payments. Connect e.g. Razorpay and point its webhook at `/api/webhooks/payment`.
- Enhance and Blend run in the browser, so their completion is reported by the browser; AI generation is enforced by the server.
- Anyone can call the share upload API until sign-in exists, and stopped links keep their photos in storage (other links may use the same photo).
- The free Hugging Face Space has daily limits and no uptime guarantee.

`visualizer-v1.html` is the earlier single-tool version.
