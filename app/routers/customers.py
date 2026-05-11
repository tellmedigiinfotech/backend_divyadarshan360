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

    update: dict = {"updated_at": SERVER_TIMESTAMP}
    if payload.full_name is not None:
        update["full_name"] = payload.full_name
    if payload.email is not None:
        update["email"] = payload.email
    if payload.last_shipping_address is not None:
        update["last_shipping_address"] = payload.last_shipping_address.model_dump()

    customer_ref = db().collection("customers").document(customer_id)
    customer_ref.set(update, merge=True)
    snap = customer_ref.get()
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
                created_at=str(data.get("created_at")) if data.get("created_at") else None,
                paid_at=str(data.get("paid_at")) if data.get("paid_at") else None,
            )
        )
    return out
