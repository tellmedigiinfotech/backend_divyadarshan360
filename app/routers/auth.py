import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from google.api_core import exceptions as gax_exceptions
from google.cloud.firestore_v1.base_query import FieldFilter

from ..auth import get_current_user
from ..firebase import SERVER_TIMESTAMP, db
from ..schemas.auth import CurrentUser, CustomerProfile, MeResponse


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Auth"])


def _to_current_user(decoded: dict) -> CurrentUser:
    firebase_info = decoded.get("firebase", {}) or {}
    return CurrentUser(
        uid=decoded["uid"],
        phone_number=decoded.get("phone_number"),
        email=decoded.get("email"),
        name=decoded.get("name"),
        picture=decoded.get("picture"),
        sign_in_provider=firebase_info.get("sign_in_provider"),
        email_verified=bool(decoded.get("email_verified", False)),
    )


def resolve_or_create_customer(decoded: dict) -> tuple[str, dict]:
    """Return (customer_doc_id, customer_data). Backfills firebase_uid on legacy docs."""
    uid: str = decoded["uid"]
    phone: str | None = decoded.get("phone_number")
    name: str | None = decoded.get("name") or ""
    email: str | None = decoded.get("email")

    customers = db().collection("customers")

    by_uid = (
        customers.where(filter=FieldFilter("firebase_uid", "==", uid)).limit(1).get()
    )
    if by_uid:
        doc = by_uid[0]
        return doc.id, doc.to_dict()

    if phone:
        by_phone = (
            customers.where(filter=FieldFilter("phone", "==", phone)).limit(1).get()
        )
        if by_phone:
            doc = by_phone[0]
            doc.reference.set(
                {"firebase_uid": uid, "updated_at": SERVER_TIMESTAMP},
                merge=True,
            )
            data = doc.to_dict() or {}
            data["firebase_uid"] = uid
            return doc.id, data

    new_ref = customers.document()
    payload = {
        "firebase_uid": uid,
        "full_name": name,
        "phone": phone or "",
        "email": email,
        "created_at": SERVER_TIMESTAMP,
        "updated_at": SERVER_TIMESTAMP,
    }
    new_ref.set(payload)
    return new_ref.id, payload


def _customer_profile(customer_id: str, data: dict) -> CustomerProfile:
    return CustomerProfile(
        customer_id=customer_id,
        full_name=data.get("full_name", "") or "",
        phone=data.get("phone", "") or "",
        email=data.get("email"),
        firebase_uid=data.get("firebase_uid"),
        last_shipping_address=data.get("last_shipping_address"),
    )


@router.get("/me", response_model=MeResponse)
def me(decoded: Annotated[dict, Depends(get_current_user)]) -> MeResponse:
    try:
        customer_id, customer_data = resolve_or_create_customer(decoded)
    except gax_exceptions.PermissionDenied as exc:
        logger.exception("Firestore permission denied")
        raise HTTPException(
            status_code=503,
            detail="Firestore is not enabled or the service account lacks permission.",
        ) from exc
    except gax_exceptions.GoogleAPIError as exc:
        logger.exception("Firestore error")
        raise HTTPException(status_code=503, detail=f"Firestore error: {exc}") from exc

    return MeResponse(
        user=_to_current_user(decoded),
        customer=_customer_profile(customer_id, customer_data),
    )
