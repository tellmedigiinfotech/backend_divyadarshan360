import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from google.api_core import exceptions as gax_exceptions
from google.cloud import firestore as firestore_module

from .. import razorpay_utils, whatsapp
from ..auth import require_admin
from ..firebase import SERVER_TIMESTAMP, db
from ..schemas.order import (
    AdminCancelOrderInput,
    AdminMarkDeliveredInput,
    AdminOrderListItem,
    AdminRefundOrderInput,
    AdminShipOrderInput,
    OrderView,
)
from .orders import _doc_to_order_view, _ts_to_iso


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["Admin"])


@router.get("/me")
def admin_me(decoded: Annotated[dict, Depends(require_admin)]) -> dict:
    """Lightweight probe the frontend hits on /admin to check access."""
    return {
        "uid": decoded["uid"],
        "email": decoded.get("email"),
        "phone": decoded.get("phone_number"),
        "is_admin": True,
    }


@router.get("/orders", response_model=list[AdminOrderListItem])
def list_orders(
    decoded: Annotated[dict, Depends(require_admin)],
    status: Annotated[str | None, Query()] = None,
    fulfillment: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[AdminOrderListItem]:
    try:
        q = db().collection("orders")
        if status:
            q = q.where("status", "==", status)
        snaps = (
            q.order_by("created_at", direction=firestore_module.Query.DESCENDING)
            .limit(limit)
            .get()
        )
    except gax_exceptions.GoogleAPIError as exc:
        raise HTTPException(status_code=503, detail=f"Firestore error: {exc}") from exc

    out: list[AdminOrderListItem] = []
    for snap in snaps:
        data = snap.to_dict() or {}
        if fulfillment and (data.get("fulfillment_status") or "pending") != fulfillment:
            continue
        item = data.get("item", {}) or {}
        customer = data.get("customer", {}) or {}
        shipping = data.get("shipping_address", {}) or {}
        out.append(
            AdminOrderListItem(
                razorpay_order_id=snap.id,
                receipt=data.get("receipt", ""),
                status=data.get("status", "created"),
                fulfillment_status=data.get("fulfillment_status"),
                payment_method=data.get("payment_method"),
                amount=int(data.get("amount", 0)),
                amount_paid=int(data.get("amount_paid", 0)),
                currency=data.get("currency", "INR"),
                product_name=item.get("name", ""),
                quantity=int(item.get("quantity", 1)),
                customer_name=customer.get("full_name", ""),
                customer_phone=customer.get("phone", ""),
                customer_email=customer.get("email"),
                city=shipping.get("city"),
                pincode=shipping.get("pincode"),
                tracking_number=data.get("tracking_number"),
                courier=data.get("courier"),
                created_at=_ts_to_iso(data.get("created_at")),
                paid_at=_ts_to_iso(data.get("paid_at")),
                shipped_at=_ts_to_iso(data.get("shipped_at")),
            )
        )
    return out


@router.get("/orders/{order_id}", response_model=OrderView)
def get_order(
    order_id: str,
    decoded: Annotated[dict, Depends(require_admin)],
) -> OrderView:
    snap = db().collection("orders").document(order_id).get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Order not found")
    return _doc_to_order_view(order_id, snap.to_dict() or {})


@router.post("/orders/{order_id}/ship", response_model=OrderView)
def mark_shipped(
    order_id: str,
    payload: AdminShipOrderInput,
    decoded: Annotated[dict, Depends(require_admin)],
) -> OrderView:
    ref = db().collection("orders").document(order_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Order not found")
    data = snap.to_dict() or {}
    if data.get("status") not in ("paid", "cod_pending"):
        raise HTTPException(status_code=400, detail="Only paid or COD orders can be marked shipped")

    update: dict = {
        "fulfillment_status": "shipped",
        "tracking_number": payload.tracking_number.strip(),
        "courier": payload.courier.strip(),
        "shipped_at": SERVER_TIMESTAMP,
        "shipped_by": decoded.get("email") or decoded.get("phone_number") or decoded["uid"],
        "updated_at": SERVER_TIMESTAMP,
    }
    if payload.notes:
        update["admin_notes"] = payload.notes.strip()
    ref.set(update, merge=True)
    return _doc_to_order_view(order_id, (ref.get().to_dict() or {}))


@router.post("/orders/{order_id}/deliver", response_model=OrderView)
def mark_delivered(
    order_id: str,
    payload: AdminMarkDeliveredInput,
    decoded: Annotated[dict, Depends(require_admin)],
) -> OrderView:
    ref = db().collection("orders").document(order_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Order not found")
    data = snap.to_dict() or {}
    current_status = data.get("status")
    if current_status not in ("paid", "cod_pending"):
        raise HTTPException(status_code=400, detail="Only paid or COD orders can be marked delivered")

    update: dict = {
        "fulfillment_status": "delivered",
        "delivered_at": SERVER_TIMESTAMP,
        "delivered_by": decoded.get("email") or decoded.get("phone_number") or decoded["uid"],
        "updated_at": SERVER_TIMESTAMP,
    }
    # COD orders: cash collected on delivery — flip to paid.
    if current_status == "cod_pending":
        update["status"] = "paid"
        update["amount_paid"] = int(data.get("amount", 0))
        update["paid_at"] = SERVER_TIMESTAMP
        update["paid_via"] = "cod_delivery"
    if payload.notes:
        update["admin_notes"] = payload.notes.strip()
    ref.set(update, merge=True)
    return _doc_to_order_view(order_id, (ref.get().to_dict() or {}))


@router.post("/orders/{order_id}/cancel", response_model=OrderView)
def cancel_order(
    order_id: str,
    payload: AdminCancelOrderInput,
    decoded: Annotated[dict, Depends(require_admin)],
) -> OrderView:
    """Cancel a COD order before it's delivered.

    Only orders in `cod_pending` status can be cancelled here — paid orders
    must be refunded (different flow), and already-shipped COD orders should
    still be cancellable so RTO (Return To Origin) cases are covered.
    """
    ref = db().collection("orders").document(order_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Order not found")
    data = snap.to_dict() or {}

    if data.get("status") != "cod_pending":
        raise HTTPException(
            status_code=400,
            detail="Only COD orders awaiting delivery can be cancelled. "
            "For paid orders, issue a refund instead.",
        )

    update: dict = {
        "status": "cancelled",
        "cancellation_reason": payload.reason.strip(),
        "cancelled_at": SERVER_TIMESTAMP,
        "cancelled_by": decoded.get("email") or decoded.get("phone_number") or decoded["uid"],
        "updated_at": SERVER_TIMESTAMP,
    }
    ref.set(update, merge=True)
    return _doc_to_order_view(order_id, (ref.get().to_dict() or {}))


@router.post("/orders/{order_id}/refund", response_model=OrderView)
def refund_order(
    order_id: str,
    payload: AdminRefundOrderInput,
    decoded: Annotated[dict, Depends(require_admin)],
) -> OrderView:
    """Issue a Razorpay refund and mark the order refunded.

    - Razorpay-paid orders: calls the Razorpay refund API. `amount_paise=None`
      issues a full refund of the captured amount. Partial refunds keep
      status `paid` but record refund metadata; full refunds flip status to
      `refunded`.
    - COD orders (no razorpay_payment_id): rejected with 400 — cash refunds
      happen offline.
    """
    ref = db().collection("orders").document(order_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Order not found")
    data = snap.to_dict() or {}

    if data.get("status") == "refunded":
        raise HTTPException(status_code=400, detail="Order is already refunded")
    if data.get("status") != "paid":
        raise HTTPException(status_code=400, detail="Only paid orders can be refunded")
    payment_id = data.get("razorpay_payment_id")
    if not payment_id:
        raise HTTPException(
            status_code=400,
            detail="No Razorpay payment on this order — refund cash offline.",
        )

    captured_amount = int(data.get("amount_paid") or data.get("amount") or 0)
    refund_amount = int(payload.amount_paise) if payload.amount_paise else captured_amount
    if refund_amount > captured_amount:
        raise HTTPException(
            status_code=400,
            detail=f"Refund amount ({refund_amount}) exceeds captured amount ({captured_amount})",
        )

    try:
        rp_refund = razorpay_utils.refund_payment(
            payment_id,
            amount_paise=refund_amount,
            notes={
                "order_id": order_id,
                "reason": payload.reason[:240],
                "issued_by": decoded.get("email") or decoded.get("phone_number") or decoded["uid"],
            },
            speed="optimum" if payload.instant else "normal",
        )
    except Exception as exc:
        logger.exception("Razorpay refund failed for order %s", order_id)
        raise HTTPException(status_code=502, detail=f"Razorpay refund failed: {exc}") from exc

    refund_id = rp_refund.get("id")
    refund_status = rp_refund.get("status")
    is_full_refund = refund_amount == captured_amount

    # 1) Record in refunds/ collection (idempotent — refund_id is the doc id)
    if refund_id:
        db().collection("refunds").document(refund_id).set(
            {
                "refund_id": refund_id,
                "razorpay_payment_id": payment_id,
                "order_id": order_id,
                "amount": refund_amount,
                "currency": data.get("currency", "INR"),
                "status": refund_status,
                "reason": payload.reason.strip(),
                "razorpay_refund_body": rp_refund,
                "issued_by": decoded.get("email") or decoded.get("phone_number") or decoded["uid"],
                "source": "admin",
                "created_at": SERVER_TIMESTAMP,
            },
            merge=True,
        )

    # 2) Update the order
    update: dict = {
        "refund_id": refund_id,
        "refund_amount": refund_amount,
        "refund_status": refund_status,
        "refund_reason": payload.reason.strip(),
        "refunded_at": SERVER_TIMESTAMP,
        "refunded_by": decoded.get("email") or decoded.get("phone_number") or decoded["uid"],
        "updated_at": SERVER_TIMESTAMP,
    }
    if is_full_refund:
        update["status"] = "refunded"
    ref.set(update, merge=True)

    cust = data.get("customer") or {}
    whatsapp.send_refund_processed(
        phone=cust.get("phone") or "",
        name=cust.get("full_name"),
        order_id=data.get("receipt") or order_id,
        amount_rupees=refund_amount // 100,
        status=refund_status or "processed",
    )

    return _doc_to_order_view(order_id, (ref.get().to_dict() or {}))
