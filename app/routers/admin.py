import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from google.api_core import exceptions as gax_exceptions
from google.cloud import firestore as firestore_module

from ..auth import require_admin
from ..firebase import SERVER_TIMESTAMP, db
from ..schemas.order import (
    AdminMarkDeliveredInput,
    AdminOrderListItem,
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
    if data.get("status") != "paid":
        raise HTTPException(status_code=400, detail="Only paid orders can be marked shipped")

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
    if data.get("status") != "paid":
        raise HTTPException(status_code=400, detail="Only paid orders can be marked delivered")

    update: dict = {
        "fulfillment_status": "delivered",
        "delivered_at": SERVER_TIMESTAMP,
        "delivered_by": decoded.get("email") or decoded.get("phone_number") or decoded["uid"],
        "updated_at": SERVER_TIMESTAMP,
    }
    if payload.notes:
        update["admin_notes"] = payload.notes.strip()
    ref.set(update, merge=True)
    return _doc_to_order_view(order_id, (ref.get().to_dict() or {}))
