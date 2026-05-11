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

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/orders/create_order` | Look up product + price, upsert customer by phone, create a Razorpay order, persist a pending order doc. Returns `razorpay_order_id`, `razorpay_key_id`, `amount` (paise), `currency`, `receipt`. |
| `POST` | `/orders/verify_payment` | Verify Razorpay signature, double-check amount/currency by fetching the payment from Razorpay, mark the order **paid**, and record a transaction. |
| `GET`  | `/orders/{razorpay_order_id}` | Public-safe order view (for the thank-you page / order lookup). |
| `POST` | `/customers/contact` | Persist a contact-form submission. |
| `GET`  | `/health` | Liveness probe. |

Auto-docs at `http://localhost:8001/docs` and `/redoc`.

## Firestore collections

| Collection | Doc id | Notes |
|---|---|---|
| `orders` | Razorpay order id (`order_xxx`) | Status, amount, item snapshot, customer snapshot, shipping address, raw Razorpay response. |
| `transactions` | Razorpay payment id (`pay_xxx`) | Per-payment record. Also written on signature/amount failures with a `status` of `signature_failed` / `amount_mismatch`. |
| `customers` | Auto-id | Keyed by normalized E.164 phone. Last shipping address cached for reorders. |
| `contact_messages` | Auto-id | Contact-form submissions. |

No composite indexes required. Single-field index on `customers.phone` is automatic.

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
