"""Fastrr (Shiprocket Checkout) integration endpoints.

- Catalog APIs (Fetch Products / Collections / Products-by-Collection): read-only
  endpoints Shiprocket calls to sync our catalog. We have a single product, so
  these are small. Response schema matches Fastrr's Seller API docs; every field
  is present (blank string when unknown) and pagination is honoured.
- Checkout token: our frontend calls this; we sign + call Fastrr and return the
  token that opens the checkout iframe.
- Order webhook: Fastrr calls this on a successful order; we create the Firestore
  order and send the team alert. Fastrr's webhook is not HMAC-signed, so we
  verify a shared secret passed as ?token= on the registered URL.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from google.api_core import exceptions as gax_exceptions
from google.cloud.firestore_v1.base_query import FieldFilter

from .. import fastrr, notifications
from ..config import settings
from ..firebase import SERVER_TIMESTAMP, db
from ..products import CATALOG, get_product, get_product_by_fastrr_variant

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/fastrr", tags=["Fastrr"])

# The single collection we expose so Products-by-Collection is meaningful.
_COLLECTION_ID = "1"
_COLLECTION_TITLE = "VR Headsets"
_SITE = "https://divyadarshan360.com"
_PRODUCT_IMG = f"{_SITE}/vr_set1.png"


def _price_str(paise: int) -> str:
    return f"{paise / 100:.2f}"


def _product_json(product) -> dict:
    return {
        "id": product.fastrr_product_id,
        "title": product.name,
        "body_html": product.description,
        "vendor": "Divya Darshan 360",
        "product_type": _COLLECTION_TITLE,
        "status": "active",
        "image": {"src": _PRODUCT_IMG},
        "variants": [
            {
                "id": product.fastrr_variant_id,
                "title": product.name,
                "price": _price_str(product.unit_price_paise),
                "sku": product.sku,
                "quantity": 1000,
                "weight": product.weight_kg,
                "image": {"src": _PRODUCT_IMG},
            }
        ],
    }


def _paginate(items: list, page: int, limit: int) -> list:
    start = (page - 1) * limit
    return items[start : start + limit]


@router.get("/products")
def fetch_products(
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=250)] = 100,
) -> dict:
    all_products = [_product_json(p) for p in CATALOG.values()]
    return {"data": {"total": len(all_products), "products": _paginate(all_products, page, limit)}}


@router.get("/collections")
def fetch_collections(
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=250)] = 100,
) -> dict:
    collections = [
        {
            "id": _COLLECTION_ID,
            "title": _COLLECTION_TITLE,
            "body_html": "VR headsets for immersive 360° temple darshan.",
            "image": {"src": _PRODUCT_IMG},
        }
    ]
    return {"data": {"total": len(collections), "collections": _paginate(collections, page, limit)}}


@router.get("/products-by-collection")
def fetch_products_by_collection(
    collection_id: Annotated[str, Query(alias="collection_id")] = "",
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=250)] = 100,
) -> dict:
    products = (
        [_product_json(p) for p in CATALOG.values()] if collection_id == _COLLECTION_ID else []
    )
    return {"data": {"total": len(products), "products": _paginate(products, page, limit)}}


# --- Checkout token (called by our frontend) ---


@router.post("/checkout-token")
def checkout_token(payload: dict) -> dict:
    """Generate a Fastrr checkout token for the VR box.

    Body: {"quantity": int}. Returns {"token": "...", "order_id": "..."} for the
    frontend to pass into HeadlessCheckout.addToCart.
    """
    if not fastrr.is_configured():
        raise HTTPException(status_code=503, detail="Fastrr is not configured.")

    product = get_product("mobile-vr-box")
    if product is None:
        raise HTTPException(status_code=500, detail="Product not found.")

    try:
        quantity = int(payload.get("quantity", 1))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid quantity.")
    if quantity < 1 or quantity > product.max_quantity:
        raise HTTPException(status_code=400, detail=f"Quantity must be 1–{product.max_quantity}.")

    items = [{"variant_id": product.fastrr_variant_id, "quantity": quantity}]
    redirect_url = f"{_SITE}/vr-headset/checkout/success"

    try:
        result = fastrr.generate_checkout_token(items, redirect_url)
    except fastrr.FastrrError as exc:
        logger.exception("Fastrr token generation failed")
        raise HTTPException(status_code=502, detail="Could not start checkout.") from exc

    inner = result.get("result") or {}
    token = inner.get("token")
    if not token:
        logger.warning("Fastrr token response missing token: %s", result)
        raise HTTPException(status_code=502, detail="Checkout token unavailable.")

    return {"token": token, "order_id": (inner.get("data") or {}).get("order_id")}


# --- Order status (polled by our success page to fire the conversion) ---


@router.get("/order-status/{fastrr_order_id}")
def order_status(fastrr_order_id: str) -> dict:
    """Look up the order the webhook created, by Fastrr's order id.

    The success page polls this after checkout to read the amount and fire the
    Google Ads conversion. Returns found=false until the webhook has landed.
    """
    try:
        snaps = list(
            db().collection("orders").where("fastrr_order_id", "==", fastrr_order_id).limit(1).stream()
        )
    except gax_exceptions.GoogleAPIError as exc:
        raise HTTPException(status_code=503, detail="Lookup failed.") from exc

    if not snaps:
        return {"found": False}

    data = snaps[0].to_dict() or {}
    return {
        "found": True,
        "status": data.get("status"),
        "payment_method": data.get("payment_method"),
        "amount_paise": int(data.get("amount", 0)),
        "receipt": data.get("receipt"),
    }


# --- Order persistence + pull-based sync (webhook-independent) ---


def _make_receipt() -> str:
    return f"{settings.receipt_prefix}-{int(time.time())}-{secrets.token_hex(3).upper()}"


def phone10(raw: str | None) -> str:
    """Last 10 digits of a phone, so +91XXXXXXXXXX and bare XXXXXXXXXX match.

    Fastrr gives us a bare 10-digit number; users store E.164 (+91...). We key
    both on the last 10 digits to link a Fastrr order to its customer account.
    """
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else ""


def resolve_customer_id(phone_10: str) -> str | None:
    """Find the users/{uid} whose phone matches this 10-digit number, if any.

    All accounts are Indian (+91). Returns the firebase uid (== customer_id) or
    None when the buyer has no account yet (guest checkout).
    """
    if not phone_10:
        return None
    try:
        snaps = list(
            db()
            .collection("users")
            .where(filter=FieldFilter("phone", "==", f"+91{phone_10}"))
            .limit(1)
            .stream()
        )
    except gax_exceptions.GoogleAPIError:
        logger.exception("resolve_customer_id lookup failed for %s", phone_10)
        return None
    return snaps[0].id if snaps else None


def has_active_cod_order(phone_10: str) -> bool:
    """True if this customer already has an OPEN COD order.

    "Open" = a COD order still in `cod_pending` (placed or shipped but not yet
    delivered/cancelled). Enforces the rule: one active COD order per customer.
    Prepaid orders don't count. Matched on customer_phone (last 10 digits).
    """
    if not phone_10:
        return False
    try:
        snaps = (
            db()
            .collection("orders")
            .where(filter=FieldFilter("customer_phone", "==", phone_10))
            .stream()
        )
    except gax_exceptions.GoogleAPIError:
        logger.exception("has_active_cod_order lookup failed for %s", phone_10)
        return False
    for snap in snaps:
        d = snap.to_dict() or {}
        pm = d.get("payment_method") or ""
        if d.get("status") == "cod_pending" and pm in ("fastrr_cod", "cod_whatsapp"):
            return True
    return False


def _persist_fastrr_order(
    *,
    fastrr_order_id: str,
    is_cod: bool,
    amount_paise: int,
    cod_fee_paise: int,
    qty: int,
    full_name: str,
    phone: str,
    email: str | None,
    addr: dict,
) -> tuple[str, bool, str]:
    """Create the Firestore order if it doesn't exist. Returns (receipt, created, status).

    Idempotent on fastrr_order_id, so the webhook and the pull-based sync can both
    run without creating duplicates.

    COD limit: a customer may have only ONE active (cod_pending) COD order. If a
    second COD order arrives while one is still open, it's recorded already
    `cancelled` (Fastrr gives us no way to block COD at checkout) and the customer
    is asked to pay online. Prepaid orders are unlimited.
    """
    orders = db().collection("orders")
    existing = list(orders.where("fastrr_order_id", "==", fastrr_order_id).limit(1).stream())
    if existing:
        data = existing[0].to_dict() or {}
        return data.get("receipt", ""), False, data.get("status") or ""

    product = get_product("mobile-vr-box")
    receipt = _make_receipt()
    cust_phone10 = phone10(phone)
    customer_id = resolve_customer_id(cust_phone10)

    # Enforce one active COD per customer (only on genuinely new COD orders).
    cod_blocked = is_cod and has_active_cod_order(cust_phone10)

    if is_cod:
        status = "cancelled" if cod_blocked else "cod_pending"
    else:
        status = "paid"

    order_doc = {
        "fastrr_order_id": fastrr_order_id,
        "razorpay_order_id": None,
        "receipt": receipt,
        "source": "fastrr",
        "status": status,
        "payment_method": "fastrr_cod" if is_cod else "fastrr_prepaid",
        "payment_type": "CASH_ON_DELIVERY" if is_cod else "PREPAID",
        "amount": amount_paise,
        "amount_paid": 0 if is_cod else amount_paise,
        "currency": "INR",
        "cod_fee_paise": cod_fee_paise,
        # Link to the customer account: customer_id when the buyer already has one,
        # customer_phone always, so /account can match guest-then-signup orders.
        "customer_id": customer_id,
        "customer_phone": cust_phone10,
        "item": {
            "sku": product.sku if product else "mobile-vr-box",
            "name": product.name if product else "Mobile VR Box",
            "quantity": qty,
        },
        "customer": {"full_name": full_name or "Customer", "phone": phone, "email": email or None},
        "shipping_address": {
            "line1": addr.get("line1") or "",
            "city": addr.get("city") or "",
            "state": addr.get("state") or "",
            "pincode": addr.get("pincode") or "",
            "country": addr.get("country") or "IN",
        },
        "created_at": SERVER_TIMESTAMP,
        "updated_at": SERVER_TIMESTAMP,
        "paid_at": None if is_cod else SERVER_TIMESTAMP,
    }
    if cod_blocked:
        order_doc["cancellation_reason"] = (
            "COD limit: one active COD order per customer. Please pay online for additional orders."
        )
        order_doc["cancelled_via"] = "system_cod_limit"
        order_doc["cancelled_at"] = SERVER_TIMESTAMP

    orders.document().set(order_doc)

    if cod_blocked:
        # Don't send the "ship this" alert. Tell the team not to ship, and nudge
        # the customer to pay online (best-effort; Fastrr emails are placeholders).
        logger.info("COD limit hit for %s (%s) — auto-cancelled %s", cust_phone10, receipt, fastrr_order_id)
        try:
            notifications.send_order_cancel_alert(order_doc)
        except Exception:
            logger.exception("COD-limit team alert error")
        try:
            notifications.send_cod_limit_customer_notice(order_doc)
        except Exception:
            logger.exception("COD-limit customer notice error")
    elif is_cod:
        try:
            alert = notifications.send_cod_team_alert(order_doc)
            if alert.get("sent"):
                logger.info("Fastrr COD team alert sent for %s", receipt)
            else:
                logger.warning("Fastrr COD team alert not sent for %s: %s", receipt, alert.get("reason"))
        except Exception:
            logger.exception("Fastrr COD team alert error")

    return receipt, True, status


def _sync_one(order_id: str) -> dict:
    """Fetch one Fastrr order by id and persist it if it's a real SUCCESS order.

    Shared by the success-page pull (/sync-order) and the reconciliation cron.
    Returns the same shape as /sync-order. Raises gax on Firestore failure.
    """
    try:
        result = fastrr.fetch_order_details(order_id)
    except fastrr.FastrrError as exc:
        logger.warning("Fastrr fetch_order_details failed for %s: %s", order_id, exc)
        return {"found": False}

    r = result.get("result") or {}
    if (r.get("status") or "").upper() != "SUCCESS":
        return {"found": False, "status": r.get("status")}

    fastrr_order_id = r.get("order_id") or order_id
    is_cod = (r.get("payment_type") or "").upper() == "CASH_ON_DELIVERY"
    amount_paise = round(float(r.get("total_amount_payable") or 0) * 100)
    cod_fee_paise = round(float(r.get("cod_charges") or 0) * 100)
    items = (r.get("cart_data") or {}).get("items") or []
    qty = sum(int(i.get("quantity", 1)) for i in items) or 1
    addr = r.get("shipping_address") or {}
    full_name = " ".join(x for x in [addr.get("first_name"), addr.get("last_name")] if x).strip()

    receipt, created, status = _persist_fastrr_order(
        fastrr_order_id=fastrr_order_id,
        is_cod=is_cod,
        amount_paise=amount_paise,
        cod_fee_paise=cod_fee_paise,
        qty=qty,
        full_name=full_name,
        phone=r.get("phone") or "",
        email=r.get("email"),
        addr=addr,
    )
    # A COD order auto-cancelled by the one-active-COD rule.
    cod_limit_blocked = is_cod and status == "cancelled"
    return {
        "found": True,
        "created": created,
        "receipt": receipt,
        "amount_paise": amount_paise,
        "status": status,
        "cod_limit_blocked": cod_limit_blocked,
    }


@router.post("/sync-order")
def sync_order(payload: dict) -> dict:
    """Pull an order from Fastrr by id and record it — no webhook required.

    Fastrr's push webhook has proven unreliable, so the success page calls this
    with the order_id captured at checkout. We fetch the order and, if it's a real
    SUCCESS order, create the Firestore order (idempotent) + team alert, then
    return the amount so the page can fire the Google Ads conversion.
    """
    order_id = (payload or {}).get("order_id")
    if not order_id:
        raise HTTPException(status_code=400, detail="Missing order_id.")

    try:
        return _sync_one(order_id)
    except gax_exceptions.GoogleAPIError as exc:
        logger.exception("Firestore error persisting Fastrr order")
        raise HTTPException(status_code=503, detail="Could not record order.") from exc


# --- Reconciliation: pull ALL recent Fastrr orders (webhook-independent) ---


def reconcile_recent_orders(days: int = 2, page_limit: int = 50, max_pages: int = 20) -> dict:
    """List every SUCCESS order in the last `days` and persist any we're missing.

    The success-page pull only records orders where the customer actually lands on
    /success. This closes the gap: a scheduled run walks Fastrr's order list and
    syncs anything not already in Firestore. Idempotent via _persist_fastrr_order.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    end = now + timedelta(minutes=5)  # small forward pad for clock skew

    seen = 0
    created = 0
    errors = 0
    page = 1
    while page <= max_pages:
        try:
            resp = fastrr.fetch_order_list(start, end, page=page, limit=page_limit)
        except fastrr.FastrrError as exc:
            logger.warning("Fastrr order-list page %d failed: %s", page, exc)
            errors += 1
            break

        result = resp.get("result") or {}
        rows = result.get("data") or []
        if not rows:
            break

        for row in rows:
            if (row.get("status") or "").upper() != "SUCCESS":
                continue
            oid = row.get("id")
            if not oid:
                continue
            seen += 1
            try:
                out = _sync_one(oid)
                if out.get("created"):
                    created += 1
                    logger.info("Reconcile: created order for Fastrr %s (%s)", oid, out.get("receipt"))
            except gax_exceptions.GoogleAPIError:
                logger.exception("Reconcile: Firestore error for %s", oid)
                errors += 1

        total = int(result.get("total") or 0)
        if page * page_limit >= total:
            break
        page += 1

    logger.info("reconcile_recent_orders: success_seen=%d created=%d errors=%d", seen, created, errors)
    return {"success_seen": seen, "created": created, "errors": errors}


@router.post("/reconcile")
def reconcile(
    token: Annotated[str | None, Query()] = None,
    days: Annotated[int, Query(ge=1, le=30)] = 2,
) -> dict:
    """Manually trigger reconciliation (also run on a schedule).

    Protected by the same shared secret as the webhook (?token=).
    """
    expected = settings.fastrr_webhook_token
    if not expected or not token or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="Invalid token.")
    return reconcile_recent_orders(days=days)


# --- Order webhook (called by Fastrr) ---


@router.post("/webhook/order")
def order_webhook(payload: dict, token: Annotated[str | None, Query()] = None) -> dict:
    """Create the Firestore order from a successful Fastrr checkout.

    Verified by a shared secret on the URL (?token=) because Fastrr does not sign
    this webhook. Idempotent on fastrr_order_id.
    """
    logger.info(
        "Fastrr webhook hit: status=%s order_id=%s payment_type=%s payment_status=%s has_token=%s",
        payload.get("status"),
        payload.get("order_id"),
        payload.get("payment_type"),
        payload.get("payment_status"),
        bool(token),
    )

    expected = settings.fastrr_webhook_token
    if not expected or not token or not secrets.compare_digest(token, expected):
        logger.warning("Fastrr webhook rejected: invalid/missing token")
        raise HTTPException(status_code=403, detail="Invalid webhook token.")

    if (payload.get("status") or "").upper() != "SUCCESS":
        # Ignore non-success callbacks but acknowledge so Fastrr stops retrying.
        logger.info("Fastrr webhook ignored (status != SUCCESS): %s", payload.get("status"))
        return {"ok": True, "ignored": payload.get("status")}

    fastrr_order_id = payload.get("order_id")
    if not fastrr_order_id:
        raise HTTPException(status_code=400, detail="Missing order_id.")

    product = get_product("mobile-vr-box")
    unit_rupees = product.unit_price_paise / 100 if product else 699
    subtotal = float(payload.get("subtotal_price") or 0)
    qty = max(1, round(subtotal / unit_rupees)) if subtotal else 1

    payment_type = (payload.get("payment_type") or "").upper()
    is_cod = payment_type == "CASH_ON_DELIVERY"
    amount_paise = round(float(payload.get("total_amount_payable") or 0) * 100)

    addr = payload.get("shipping_address") or {}
    full_name = " ".join(x for x in [addr.get("first_name"), addr.get("last_name")] if x).strip()

    # Route through the shared persister so idempotency, account linking, the COD
    # limit, and all team/customer alerts behave identically to the pull path.
    try:
        receipt, created, status = _persist_fastrr_order(
            fastrr_order_id=fastrr_order_id,
            is_cod=is_cod,
            amount_paise=amount_paise,
            cod_fee_paise=settings.cod_fee_paise if is_cod else 0,
            qty=qty,
            full_name=full_name,
            phone=payload.get("phone") or "",
            email=payload.get("email"),
            addr=addr,
        )
    except gax_exceptions.GoogleAPIError as exc:
        logger.exception("Firestore error creating Fastrr order")
        raise HTTPException(status_code=503, detail="Could not record order.") from exc

    return {"ok": True, "receipt": receipt, "created": created, "status": status}
