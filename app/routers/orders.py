import logging
import secrets
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from google.api_core import exceptions as gax_exceptions

from .. import notifications, whatsapp
from ..auth import get_current_user
from ..blocklist import is_phone_blocked
from ..config import settings
from ..firebase import SERVER_TIMESTAMP, db
from ..products import get_product
from ..schemas.order import (
    CreateCodOrderInput,
    CreateCodOrderOutput,
    CustomerCancelOrderInput,
    OrderCustomerView,
    OrderItemView,
    OrderStatus,
    OrderView,
)
from .auth import resolve_or_create_customer
from .fastrr import phone10


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orders", tags=["Orders"])


def _make_receipt() -> str:
    ts = int(time.time())
    return f"{settings.receipt_prefix}-{ts}-{secrets.token_hex(3).upper()}"


def _doc_to_order_view(order_id: str, data: dict) -> OrderView:
    item = data.get("item", {})
    customer = data.get("customer", {})
    shipping = data.get("shipping_address", {})
    return OrderView(
        razorpay_order_id=order_id,
        receipt=data.get("receipt", ""),
        status=OrderStatus(data.get("status", "created")),
        amount=int(data.get("amount", 0)),
        amount_paid=int(data.get("amount_paid", 0)),
        currency=data.get("currency", "INR"),
        item=OrderItemView(
            sku=item.get("sku", ""),
            name=item.get("name", ""),
            quantity=int(item.get("quantity", 1)),
            unit_price_paise=int(item.get("unit_price_paise", 0)),
            line_total_paise=int(item.get("line_total_paise", 0)),
        ),
        customer=OrderCustomerView(
            full_name=customer.get("full_name", ""),
            phone=customer.get("phone", ""),
            email=customer.get("email"),
        ),
        shipping_address=shipping,
        created_at=_ts_to_iso(data.get("created_at")),
        paid_at=_ts_to_iso(data.get("paid_at")),
        fulfillment_status=data.get("fulfillment_status"),
        tracking_number=data.get("tracking_number"),
        courier=data.get("courier"),
        shipped_at=_ts_to_iso(data.get("shipped_at")),
        delivered_at=_ts_to_iso(data.get("delivered_at")),
        admin_notes=data.get("admin_notes"),
        payment_method=data.get("payment_method"),
        razorpay_payment_id=data.get("razorpay_payment_id"),
        refund_id=data.get("refund_id"),
        refund_amount=data.get("refund_amount"),
        refund_status=data.get("refund_status"),
        refunded_at=_ts_to_iso(data.get("refunded_at")),
        refund_reason=data.get("refund_reason"),
        cancelled_at=_ts_to_iso(data.get("cancelled_at")),
        cancellation_reason=data.get("cancellation_reason"),
        cancellation_requested=bool(data.get("cancellation_requested")),
        cancellation_requested_at=_ts_to_iso(data.get("cancellation_requested_at")),
        cancellation_request_reason=data.get("cancellation_request_reason"),
        source=data.get("source"),
        fastrr_order_id=data.get("fastrr_order_id"),
    )


def _ts_to_iso(value) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)


def _update_customer_from_form(customer_id: str, payload: CreateCodOrderInput) -> None:
    """Merge web-checkout supplied info onto users/{uid}.

    Uses camelCase to match the mobile app's existing user schema. Never
    touches mobile-managed fields (uid, displayName, isGuest, createdAt,
    phone) so concurrent mobile writes are safe.
    """
    update: dict = {
        "updatedAt": SERVER_TIMESTAMP,
        "lastShippingAddress": payload.shipping_address.model_dump(),
    }
    if payload.customer.full_name:
        update["name"] = payload.customer.full_name
    if payload.customer.email:
        update["email"] = payload.customer.email
    db().collection("users").document(customer_id).set(update, merge=True)


@router.post("/create_cod_order", response_model=CreateCodOrderOutput)
def create_cod_order(
    payload: CreateCodOrderInput,
    decoded: Annotated[dict, Depends(get_current_user)],
) -> CreateCodOrderOutput:
    """Cash-on-Delivery via WhatsApp. No Razorpay call — we just record the
    order in Firestore so it shows up in the admin dashboard and the user's
    /account list. Status stays `cod_pending` until the admin marks delivery
    (which flips it to `paid`).
    """
    token_phone_for_block = decoded.get("phone_number") or payload.customer.phone
    if is_phone_blocked(token_phone_for_block):
        raise HTTPException(status_code=403, detail="This phone is not allowed to place orders.")

    product = get_product(payload.item.sku)
    if product is None:
        raise HTTPException(status_code=404, detail=f"Unknown product sku: {payload.item.sku}")
    if payload.item.quantity > product.max_quantity:
        raise HTTPException(
            status_code=400,
            detail=f"Quantity exceeds the per-order limit of {product.max_quantity}.",
        )

    amount_paise = product.unit_price_paise * payload.item.quantity
    receipt = _make_receipt()

    try:
        customer_id, _ = resolve_or_create_customer(decoded)
        _update_customer_from_form(customer_id, payload)
    except gax_exceptions.PermissionDenied as exc:
        logger.exception("Firestore permission denied")
        raise HTTPException(
            status_code=503,
            detail="Firestore is not enabled or the service account lacks permission.",
        ) from exc
    except gax_exceptions.GoogleAPIError as exc:
        logger.exception("Firestore error")
        raise HTTPException(status_code=503, detail=f"Firestore error: {exc}") from exc

    token_phone: str = decoded.get("phone_number") or payload.customer.phone

    # Flat COD handling fee on top of the product total.
    cod_fee_paise = settings.cod_fee_paise
    amount_paise += cod_fee_paise

    new_ref = db().collection("orders").document()
    order_doc = {
        "razorpay_order_id": None,
        "receipt": receipt,
        "status": OrderStatus.cod_pending.value,
        "payment_method": "cod_whatsapp",
        "amount": amount_paise,
        "cod_fee_paise": cod_fee_paise,
        "amount_paid": 0,
        "currency": product.currency,
        "item": {
            "sku": product.sku,
            "name": product.name,
            "quantity": payload.item.quantity,
            "unit_price_paise": product.unit_price_paise,
            "line_total_paise": amount_paise,
        },
        "customer": {
            "full_name": payload.customer.full_name,
            "phone": token_phone,
            "email": payload.customer.email,
        },
        "customer_id": customer_id,
        "firebase_uid": decoded["uid"],
        "shipping_address": payload.shipping_address.model_dump(),
        "created_at": SERVER_TIMESTAMP,
        "updated_at": SERVER_TIMESTAMP,
        "paid_at": None,
    }
    new_ref.set(order_doc)

    sent = whatsapp.send_cod_received(
        phone=token_phone,
        name=payload.customer.full_name,
        order_id=receipt,
        item_text=f"{product.name} x {payload.item.quantity}",
        amount_rupees=amount_paise // 100,
    )
    if sent:
        new_ref.set({"whatsapp_confirmation_sent_at": SERVER_TIMESTAMP}, merge=True)

    # Notify the internal team of the new COD order. Non-throwing — a mail
    # failure must not fail the order.
    try:
        alert = notifications.send_cod_team_alert(order_doc)
        if alert.get("sent"):
            logger.info("COD team alert sent for %s", receipt)
        else:
            logger.warning(
                "COD team alert not sent for %s: reason=%s error=%s",
                receipt,
                alert.get("reason"),
                alert.get("error"),
            )
    except Exception:
        logger.exception("Unexpected error sending COD team alert")

    return CreateCodOrderOutput(
        order_id=new_ref.id,
        receipt=receipt,
        amount=amount_paise,
        currency=product.currency,
        product_name=product.name,
    )


def _assert_order_ownership(order: dict, decoded: dict) -> None:
    owner = order.get("firebase_uid")
    # Legacy orders created before auth was required have no firebase_uid.
    # Reject only if the order DOES have one and it differs from the caller.
    if owner and owner != decoded["uid"]:
        raise HTTPException(status_code=403, detail="Order does not belong to current user")


def _owns_order(order: dict, uid: str, caller_phone10: str) -> bool:
    """True if this order belongs to the caller.

    Covers all linkage shapes: firebase_uid (Razorpay/app), customer_id (linked
    Fastrr), and customer_phone (guest Fastrr matched by phone).
    """
    if order.get("firebase_uid") == uid or order.get("customer_id") == uid:
        return True
    order_phone10 = phone10(order.get("customer_phone") or (order.get("customer") or {}).get("phone"))
    return bool(caller_phone10) and order_phone10 == caller_phone10


@router.post("/{order_id}/cancel", response_model=OrderView)
def cancel_my_order(
    order_id: str,
    payload: CustomerCancelOrderInput,
    decoded: Annotated[dict, Depends(get_current_user)],
) -> OrderView:
    """Let a customer cancel their own COD order while it's still pending.

    Mirrors the admin cancel rules: only `cod_pending` orders can be self-cancelled
    (a paid order needs a refund, which stays an admin/support action). Ownership is
    checked across firebase_uid / customer_id / customer_phone so Fastrr orders —
    including guest checkouts linked only by phone — are cancellable too. Notifies
    the ops team so a cancelled order is never dispatched.
    """
    uid = decoded["uid"]
    caller_phone10 = phone10(decoded.get("phone_number"))

    ref = db().collection("orders").document(order_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Order not found")
    order = snap.to_dict() or {}

    if not _owns_order(order, uid, caller_phone10):
        raise HTTPException(status_code=403, detail="Order does not belong to current user")

    status = order.get("status")
    if status == "cancelled":
        # Idempotent: already cancelled, just return current state.
        return _doc_to_order_view(order_id, order)
    if status != "cod_pending":
        raise HTTPException(
            status_code=400,
            detail="This order can no longer be cancelled online. "
            "Please contact support and we'll help you.",
        )

    reason = (payload.reason or "").strip() or "Cancelled by customer"
    update = {
        "status": "cancelled",
        "cancellation_reason": reason,
        "cancelled_at": SERVER_TIMESTAMP,
        "cancelled_by": decoded.get("phone_number") or uid,
        "cancelled_via": "customer",
        "updated_at": SERVER_TIMESTAMP,
    }
    ref.set(update, merge=True)
    updated = ref.get().to_dict() or {}

    try:
        alert = notifications.send_order_cancel_alert(updated)
        if not alert.get("sent"):
            logger.warning("Order cancel team alert not sent for %s: %s", order_id, alert.get("reason"))
    except Exception:
        logger.exception("Order cancel team alert error for %s", order_id)

    return _doc_to_order_view(order_id, updated)


@router.post("/{order_id}/request-cancellation", response_model=OrderView)
def request_order_cancellation(
    order_id: str,
    payload: CustomerCancelOrderInput,
    decoded: Annotated[dict, Depends(get_current_user)],
) -> OrderView:
    """Let a customer request cancellation of a PAID order (needs a refund).

    A paid order can't be self-cancelled — the money has moved, so a refund is an
    admin/support action. This records the request on the order and emails the ops
    team to review + refund from the admin dashboard. The order status is left
    unchanged until a human acts. Idempotent per order.
    """
    uid = decoded["uid"]
    caller_phone10 = phone10(decoded.get("phone_number"))

    ref = db().collection("orders").document(order_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Order not found")
    order = snap.to_dict() or {}

    if not _owns_order(order, uid, caller_phone10):
        raise HTTPException(status_code=403, detail="Order does not belong to current user")

    status = order.get("status")
    if status == "cod_pending":
        # COD isn't paid yet — cancel it directly instead.
        raise HTTPException(
            status_code=400,
            detail="This order can be cancelled directly. Please use Cancel order.",
        )
    if status != "paid":
        raise HTTPException(
            status_code=400,
            detail="Only a paid order can be requested for cancellation. "
            "Please contact support for help.",
        )

    if order.get("cancellation_requested"):
        # Idempotent: already requested, return current state.
        return _doc_to_order_view(order_id, order)

    reason = (payload.reason or "").strip() or "Requested by customer"
    update = {
        "cancellation_requested": True,
        "cancellation_request_reason": reason,
        "cancellation_requested_at": SERVER_TIMESTAMP,
        "cancellation_requested_by": decoded.get("phone_number") or uid,
        "updated_at": SERVER_TIMESTAMP,
    }
    ref.set(update, merge=True)
    updated = ref.get().to_dict() or {}

    try:
        alert = notifications.send_cancellation_request_alert(updated)
        if not alert.get("sent"):
            logger.warning("Cancellation request alert not sent for %s: %s", order_id, alert.get("reason"))
    except Exception:
        logger.exception("Cancellation request alert error for %s", order_id)

    return _doc_to_order_view(order_id, updated)


@router.get("/{razorpay_order_id}", response_model=OrderView)
def get_order(
    razorpay_order_id: str,
    decoded: Annotated[dict, Depends(get_current_user)],
) -> OrderView:
    snap = db().collection("orders").document(razorpay_order_id).get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Order not found")
    data = snap.to_dict() or {}
    _assert_order_ownership(data, decoded)
    return _doc_to_order_view(razorpay_order_id, data)


