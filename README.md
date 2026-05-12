# Divya Darshan 360 — Backend (Firestore + Razorpay)

FastAPI service that powers the VR-headset checkout on `divyadarshan360.com`.
Persists orders, transactions, customers, and contact-form submissions to
**Cloud Firestore** and accepts payments through **Razorpay**.

> Lives alongside the Next.js frontend at `../frontend` and the legacy stock-
> media backend at `../backend_tellme`. Independent of both.

## Stack

- **FastAPI** (async) + **Uvicorn**
- **firebase-admin** (Firestore Native mode)
- **razorpay** Python SDK (HMAC-SHA256 signature verification + REST orders)
- **Pydantic v2** for validation
- Python **>= 3.11**

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/orders/create_order` | **Bearer** | Look up product + price, resolve customer from token (UID → phone fallback), create a Razorpay order, persist a pending order doc linked to `firebase_uid`. Returns `razorpay_order_id`, `razorpay_key_id`, `amount` (paise), `currency`, `receipt`. |
| `POST` | `/orders/verify_payment` | **Bearer** | Verify Razorpay signature + UID ownership, double-check amount/currency by fetching the payment from Razorpay, mark the order **paid**, and record a transaction. Idempotent with the webhook (never downgrades a paid order to failed). |
| `GET`  | `/orders/{razorpay_order_id}` | **Bearer** | Order view, scoped to the caller's UID. |
| `POST` | `/razorpay/webhook` | **HMAC** | Razorpay -> server. Verifies `X-Razorpay-Signature` against `RAZORPAY_WEBHOOK_SECRET`. Handles `payment.captured`, `payment.failed`, `refund.created`, `refund.processed`, `order.paid`. Idempotent. |
| `POST` | `/customers/contact` | — | Persist a contact-form submission. |
| `GET`  | `/auth/me` | **Bearer** | Verify the Firebase ID token, ensure a customer doc exists (creating one if needed), return both the token claims and the linked customer profile. |
| `GET`  | `/customers/me` | **Bearer** | Current user's customer profile. |
| `PUT`  | `/customers/me` | **Bearer** | Update full name, email, and/or last shipping address. |
| `GET`  | `/customers/me/orders` | **Bearer** | List the current user's orders (latest first). Supports `?limit=`. |
| `GET`  | `/health` | — | Liveness probe. |

### Razorpay webhook

Production correctness requires the webhook, because the browser-driven `/orders/verify_payment` call can be lost (network drop, tab close, refund happening async, etc). The webhook is the source of truth.

**Configure in Razorpay Dashboard:**

1. Dashboard → **Settings** → **Webhooks** → **Add new webhook**
2. Webhook URL: `https://<your-backend>/razorpay/webhook` (use [ngrok](https://ngrok.com) for local: `ngrok http 8001` → use the https URL)
3. Active events: at minimum `payment.captured`, `payment.failed`. Also useful: `refund.created`, `refund.processed`, `order.paid`.
4. Set a strong secret string → copy it → put in `.env` as `RAZORPAY_WEBHOOK_SECRET=<that string>`.

**Test locally:**

```bash
ngrok http 8001
# use the https forwarding URL in the Razorpay Dashboard
# Razorpay Dashboard -> Webhooks -> the new webhook -> "Send test webhook"
```

The handler writes to:
- `orders/{razorpay_order_id}` — flips `status` to `paid` or `failed`, sets `paid_at`, `paid_via`, `notified_at`.
- `transactions/{razorpay_payment_id}` — full payment body + `source: "webhook"`.
- `refunds/{refund_id}` — for refund events.

### Email + SMS receipts

After `payment.captured` (and `order.paid`), the webhook fires a transactional receipt to both the customer's email and phone. Idempotent via `notified_at` on the order doc — Razorpay retries (or duplicate events) won't double-send. Failures are logged and stored in `notification_result` but never block the webhook from returning 200.

**Email via Resend** — `https://resend.com`. 3 000 emails/month free, 100/day. Set in `.env`:
```
RESEND_API_KEY=re_xxxxxxxxxxxxxxxxxxxxxxxx
RESEND_FROM=Divya Darshan 360 <noreply@divyadarshan360.com>
```
Steps:
1. Sign up at resend.com → API Keys → create a key.
2. Domains → Add `divyadarshan360.com` → Resend shows you SPF + DKIM + DMARC DNS records → add them to your domain registrar → wait for "verified" status (usually a few minutes).
3. Until your domain is verified, leave `RESEND_FROM` as the default (`onboarding@resend.dev`). That sandbox sender can only deliver to your Resend account's email — fine for testing, not for real customers.

**SMS Striker** — same creds the legacy backend uses for OTP. Set in `.env`:
```
SMS_STRIKER_USERNAME=<your username>
SMS_STRIKER_PASSWORD=<your password>
SMS_STRIKER_CHANNEL=<your DLT-approved sender id>
SMS_STRIKER_ORDER_TEMPLATE_ID=<DLT template id for order confirmation>
```

> **DLT note**: Indian telcos require every transactional SMS to use a pre-registered template id. The OTP template id `1407162495551105851` is for OTPs only — register a separate template for order-confirmation text under your DLT portal and put its id in `SMS_STRIKER_ORDER_TEMPLATE_ID`. The template text the system sends is: `"Divya Darshan 360: Payment of Rs.{amount} received for {item}. Order #{short_id}. Ships within 24h. Help: {phone}"`.

If either set of creds is missing, that channel is skipped (logged with `reason: smtp_not_configured` or `sms_not_configured`). The other channel still tries.

### Authentication

Endpoints marked **Bearer** require an `Authorization: Bearer <firebaseIdToken>` header. Tokens come from the Firebase Auth web SDK on the client (phone OTP flow); the backend verifies them with `firebase_admin.auth.verify_id_token`.

Frontend flow (phone OTP, sketch):

```ts
import { initializeApp } from "firebase/app"
import { getAuth, RecaptchaVerifier, signInWithPhoneNumber } from "firebase/auth"

const auth = getAuth(initializeApp({ /* firebaseConfig */ }))

// 1. Send OTP
const verifier = new RecaptchaVerifier(auth, "recaptcha-container", { size: "invisible" })
const confirmation = await signInWithPhoneNumber(auth, "+919876543210", verifier)

// 2. User enters OTP
const cred = await confirmation.confirm(code)
const idToken = await cred.user.getIdToken()

// 3. Call protected backend endpoints
const res = await fetch(`${API_BASE}/auth/me`, {
  headers: { Authorization: `Bearer ${idToken}` },
})
```

On the backend, the dependency lives in `app/auth.py`. The first time a Firebase user calls `/auth/me`, the resolver either backfills `firebase_uid` onto an existing phone-keyed customer doc, or creates a fresh one.

Auto-docs at `http://localhost:8001/docs` and `/redoc`.

## Firestore collections

| Collection | Doc id | Notes |
|---|---|---|
| `orders` | Razorpay order id (`order_xxx`) | Status, amount, item snapshot, customer snapshot, shipping address, raw Razorpay response. |
| `transactions` | Razorpay payment id (`pay_xxx`) | Per-payment record. Also written on signature/amount failures with a `status` of `signature_failed` / `amount_mismatch`. |
| `customers` | Auto-id | Keyed by normalized E.164 phone. Last shipping address cached for reorders. |
| `contact_messages` | Auto-id | Contact-form submissions. |

No composite indexes required. Single-field index on `customers.phone` is automatic.

## Deploying to Firebase Functions (production)

This backend is set up to deploy as a single 2nd-gen HTTPS Firebase Function in
`asia-south1` (Mumbai). Under the hood Firebase deploys it as a Cloud Run
service. Cost: free up to 2M invocations / 400k GB-sec / 200k CPU-sec per month
on the Blaze plan.

### One-time setup

1. **Install Firebase CLI** (Node 18+ required):
   ```bash
   npm install -g firebase-tools
   firebase login
   ```
2. The repo already contains `firebase.json` (function config) and `.firebaserc`
   (project binding to `divyadarshanweb-3c2d7`). No `firebase init` needed.
3. Make sure your local `.env` has the production values you want deployed
   (Razorpay live keys, real SMTP creds, etc.). Firebase will upload `.env`
   alongside the function source — it's not committed to git.

### Deploy

```bash
cd backend_dd360
firebase deploy --only functions
```

The CLI will:
- Build a container from the source (Python 3.12 runtime).
- Push it to Artifact Registry.
- Create / update a Cloud Run service in `asia-south1`.
- Print the function URL: `https://api-<hash>-asia-south1.run.app`.

Hit `https://<that-url>/health` — should return `{"status":"ok"}`.

### After first deploy — three things to update

1. **Razorpay Dashboard → Settings → Webhooks** → change the webhook URL to
   `https://api-<hash>-asia-south1.run.app/razorpay/webhook`. The secret stays
   the same.
2. **Vercel → frontend project → Settings → Environment Variables** →
   `NEXT_PUBLIC_API_BASE = https://api-<hash>-asia-south1.run.app` → redeploy.
3. **Firebase Console → Authentication → Settings → Authorized domains** →
   confirm your Vercel domain is listed (so the OTP flow's reCAPTCHA accepts
   tokens from production).

### Credentials — what changes vs local

| Thing | Locally | On Firebase Functions |
|---|---|---|
| Firebase Admin (Firestore access) | service-account JSON file at `FIREBASE_CREDENTIALS_PATH` | **Auto** — the function uses the runtime's attached service account (ADC). No file needed. |
| Razorpay keys, webhook secret | `.env` | `.env` (deployed with the function) |
| SMTP, SMS Striker creds | `.env` | `.env` |
| CORS allowlist | `.env` (`ALLOWED_ORIGINS=...`) | `.env` — make sure your production Vercel domain is in the list |

For real secrets (Razorpay live secret, SMTP password, webhook secret) you can
later promote them to Google Secret Manager via:
```bash
firebase functions:secrets:set RAZORPAY_KEY_SECRET
```
and bind them to the function — but `.env` is fine for v1.

### Local development (unchanged)

```bash
python run_local.py
```
This bypasses Firebase Functions entirely and runs FastAPI under uvicorn with
hot-reload. `main.py` is for production deploy only; running it directly does
nothing useful in dev.

---

## Setup

```bash
cd backend_dd360
python -m venv .venv
.venv\Scripts\activate          # PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### Firebase credentials

1. In Firebase console: **Project settings → Service accounts → Generate new private key**.
2. Save the downloaded JSON as `backend_dd360/service-account.json`
   *(already in `.gitignore`)*.
3. Make sure **Cloud Firestore** is enabled for the project (Native mode).

For container/serverless deploys you can instead pass the JSON as a single-line
env var: `FIREBASE_CREDENTIALS_JSON='{...}'`.

### Razorpay credentials

Get keys from <https://dashboard.razorpay.com/app/keys>. Use `rzp_test_*` while
developing — payments won't be charged. Switch to live keys only after the
flow is end-to-end verified.

### Environment

```bash
copy .env.example .env          # PowerShell: cp .env.example .env
# then fill in RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, FIREBASE_CREDENTIALS_PATH
```

### Run

```bash
python main.py
# → http://127.0.0.1:8001
```

## Pricing & products

Server is the **only** source of truth for prices. The client sends an `sku`
and a `quantity`; the server multiplies by `unit_price_paise` from
`app/products.py`. The browser never gets to choose the amount.

To add or change a product, edit `CATALOG` in `app/products.py`:

```python
CATALOG = {
    "mobile-vr-box": Product(
        sku="mobile-vr-box",
        name="Mobile VR Box",
        unit_price_paise=59900,   # ₹599
        mrp_paise=99900,
        max_quantity=10,
    ),
}
```

## Frontend integration

Replace the WhatsApp-only flow in
`frontend/app/vr-headset/checkout/checkout-client.tsx` with:

```ts
// 1. Load Razorpay checkout.js once (e.g. in app/layout.tsx <head> or via next/script)
//    <script src="https://checkout.razorpay.com/v1/checkout.js" />

// 2. On "Place order":
const res = await fetch(`${NEXT_PUBLIC_API_BASE}/orders/create_order`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    item: { sku: "mobile-vr-box", quantity: form.qty },
    customer: {
      full_name: form.fullName,
      phone: form.phone,
      email: form.email || null,
    },
    shipping_address: {
      line1: form.address,
      city: form.city,
      state: form.state,
      pincode: form.pincode,
      country: "IN",
      notes: form.notes,
    },
  }),
})
const order = await res.json()

const rzp = new (window as any).Razorpay({
  key: order.razorpay_key_id,
  amount: order.amount,
  currency: order.currency,
  order_id: order.razorpay_order_id,
  name: "Divya Darshan 360",
  description: order.product_name,
  prefill: { name: form.fullName, email: form.email, contact: form.phone },
  theme: { color: "#d4af37" },
  handler: async (resp: any) => {
    const verify = await fetch(`${NEXT_PUBLIC_API_BASE}/orders/verify_payment`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        razorpay_order_id: resp.razorpay_order_id,
        razorpay_payment_id: resp.razorpay_payment_id,
        razorpay_signature: resp.razorpay_signature,
      }),
    })
    if (verify.ok) router.push(`/vr-headset/order-confirmed/${resp.razorpay_order_id}`)
    else /* show failure UI */
  },
  modal: {
    ondismiss: () => { /* user closed the modal — order stays "created" */ },
  },
})
rzp.open()
```

Set `NEXT_PUBLIC_API_BASE` (e.g. `http://localhost:8001` in dev) in
`frontend/.env.local`.

## Security notes

- **Never trust client-side prices** — already enforced by looking up the SKU
  server-side.
- **Always verify the Razorpay signature** before marking an order paid — the
  HMAC check happens inside `razorpay_utils.verify_signature`.
- The endpoints are **unauthenticated** in v1 (matching the existing flow).
  Add Firebase Auth + `verify_id_token` middleware before exposing admin
  endpoints.
- CORS is locked to `ALLOWED_ORIGINS`; do not use `*` with `allow_credentials=True`
  in production.

## Not in v1

The following are deliberately out of scope and easy to add later:

- Razorpay webhook endpoint (`payment.captured`, `refund.processed`, …)
- Admin endpoints (list orders, mark shipped, refund)
- Email confirmation on payment success
- User accounts via Firebase Auth
- Inventory tracking
