import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from google.api_core import exceptions as gax_exceptions
from google.cloud import firestore as firestore_module
from google.cloud.firestore_v1.base_query import FieldFilter

from ..auth import get_current_user
from ..firebase import SERVER_TIMESTAMP, db
from ..schemas.auth import CustomerProfile
from ..schemas.customer import (
    ContactFormInput,
    ContactFormOutput,
    CustomerOrderListItem,
    CustomerUpdateInput,
)
from .auth import _customer_profile, resolve_or_create_customer


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/customers", tags=["Customers"])


@router.post("/contact", response_model=ContactFormOutput)
def submit_contact_form(payload: ContactFormInput) -> ContactFormOutput:
    doc_ref = db().collection("contact_messages").document()
    doc_ref.set(
        {
            "full_name": payload.full_name,
            "email": payload.email,
            "phone": payload.phone,
            "subject": payload.subject,
            "message": payload.message,
            "created_at": SERVER_TIMESTAMP,
        }
    )
    return ContactFormOutput(id=doc_ref.id)


@router.get("/me", response_model=CustomerProfile)
def get_me(decoded: Annotated[dict, Depends(get_current_user)]) -> CustomerProfile:
    try:
        customer_id, data = resolve_or_create_customer(decoded)
    except gax_exceptions.PermissionDenied as exc:
        raise HTTPException(status_code=503, detail="Firestore not enabled or missing permission") from exc
    return _customer_profile(customer_id, data)


@router.put("/me", response_model=CustomerProfile)
def update_me(
    payload: CustomerUpdateInput,
    decoded: Annotated[dict, Depends(get_current_user)],
) -> CustomerProfile:
    try:
        customer_id, _ = resolve_or_create_customer(decoded)
    except gax_exceptions.PermissionDenied as exc:
        raise HTTPException(status_code=503, detail="Firestore not enabled or missing permission") from exc

    update: dict = {"updatedAt": SERVER_TIMESTAMP}
    if payload.full_name is not None:
        update["name"] = payload.full_name
    if payload.email is not None:
        update["email"] = payload.email
    if payload.last_shipping_address is not None:
        update["lastShippingAddress"] = payload.last_shipping_address.model_dump()

    user_ref = db().collection("users").document(customer_id)
    user_ref.set(update, merge=True)
    snap = user_ref.get()
    return _customer_profile(customer_id, snap.to_dict() or {})


@router.get("/me/orders", response_model=list[CustomerOrderListItem])
def list_my_orders(
    decoded: Annotated[dict, Depends(get_current_user)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[CustomerOrderListItem]:
    try:
        customer_id, _ = resolve_or_create_customer(decoded)
    except gax_exceptions.PermissionDenied as exc:
        raise HTTPException(status_code=503, detail="Firestore not enabled or missing permission") from exc

    try:
        snaps = (
            db()
            .collection("orders")
            .where(filter=FieldFilter("customer_id", "==", customer_id))
            .order_by("created_at", direction=firestore_module.Query.DESCENDING)
            .limit(limit)
            .get()
        )
    except gax_exceptions.GoogleAPIError as exc:
        raise HTTPException(status_code=503, detail=f"Firestore error: {exc}") from exc

    out: list[CustomerOrderListItem] = []
    for snap in snaps:
        data = snap.to_dict() or {}
        item = data.get("item", {}) or {}
        out.append(
            CustomerOrderListItem(
                razorpay_order_id=snap.id,
                receipt=data.get("receipt", ""),
                status=data.get("status", "created"),
                amount=int(data.get("amount", 0)),
                amount_paid=int(data.get("amount_paid", 0)),
                currency=data.get("currency", "INR"),
                product_name=item.get("name", ""),
                quantity=int(item.get("quantity", 1)),
                created_at=_ts_to_iso(data.get("created_at")),
                paid_at=_ts_to_iso(data.get("paid_at")),
                fulfillment_status=data.get("fulfillment_status"),
                tracking_number=data.get("tracking_number"),
                courier=data.get("courier"),
                shipped_at=_ts_to_iso(data.get("shipped_at")),
            )
        )
    return out


def _ts_to_iso(value) -> str | None:
    """Normalize Firestore Timestamp / DatetimeWithNanoseconds / SERVER_TIMESTAMP
    sentinel to an ISO-8601 string the frontend can pass to new Date(...)."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)
