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


# --- Order webhook (called by Fastrr) ---


def _make_receipt() -> str:
    return f"{settings.receipt_prefix}-{int(time.time())}-{secrets.token_hex(3).upper()}"


@router.post("/webhook/order")
def order_webhook(payload: dict, token: Annotated[str | None, Query()] = None) -> dict:
    """Create the Firestore order from a successful Fastrr checkout.

    Verified by a shared secret on the URL (?token=) because Fastrr does not sign
    this webhook. Idempotent on fastrr_order_id.
    """
    expected = settings.fastrr_webhook_token
    if not expected or not token or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="Invalid webhook token.")

    if (payload.get("status") or "").upper() != "SUCCESS":
        # Ignore non-success callbacks but acknowledge so Fastrr stops retrying.
        return {"ok": True, "ignored": payload.get("status")}

    fastrr_order_id = payload.get("order_id")
    if not fastrr_order_id:
        raise HTTPException(status_code=400, detail="Missing order_id.")

    try:
        orders = db().collection("orders")
        # Idempotency: skip if we've already recorded this Fastrr order.
        existing = list(orders.where("fastrr_order_id", "==", fastrr_order_id).limit(1).stream())
        if existing:
            return {"ok": True, "duplicate": True}

        product = get_product("mobile-vr-box")
        unit_rupees = product.unit_price_paise / 100 if product else 699
        subtotal = float(payload.get("subtotal_price") or 0)
        qty = max(1, round(subtotal / unit_rupees)) if subtotal else 1

        payment_type = (payload.get("payment_type") or "").upper()
        is_cod = payment_type == "CASH_ON_DELIVERY"
        amount_paise = round(float(payload.get("total_amount_payable") or 0) * 100)

        addr = payload.get("shipping_address") or {}
        full_name = " ".join(
            x for x in [addr.get("first_name"), addr.get("last_name")] if x
        ).strip()

        receipt = _make_receipt()
        order_doc = {
            "fastrr_order_id": fastrr_order_id,
            "razorpay_order_id": None,
            "receipt": receipt,
            "source": "fastrr",
            "status": "cod_pending" if is_cod else "paid",
            "payment_method": "fastrr_cod" if is_cod else "fastrr_prepaid",
            "payment_type": payment_type,
            "amount": amount_paise,
            "amount_paid": 0 if is_cod else amount_paise,
            "currency": "INR",
            "cod_fee_paise": settings.cod_fee_paise if is_cod else 0,
            "item": {
                "sku": product.sku if product else "mobile-vr-box",
                "name": product.name if product else "Mobile VR Box",
                "quantity": qty,
            },
            "customer": {
                "full_name": full_name or "Customer",
                "phone": payload.get("phone") or "",
                "email": payload.get("email") or None,
            },
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
        orders.document().set(order_doc)
    except gax_exceptions.GoogleAPIError as exc:
        logger.exception("Firestore error creating Fastrr order")
        raise HTTPException(status_code=503, detail="Could not record order.") from exc

    # Team alert for COD orders (mirrors the existing COD flow). Non-fatal.
    if is_cod:
        try:
            alert = notifications.send_cod_team_alert(order_doc)
            if not alert.get("sent"):
                logger.warning("Fastrr COD team alert not sent: %s", alert.get("reason"))
        except Exception:
            logger.exception("Fastrr COD team alert error")

    return {"ok": True, "receipt": receipt}
