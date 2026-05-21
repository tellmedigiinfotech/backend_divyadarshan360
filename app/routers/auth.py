import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from google.api_core import exceptions as gax_exceptions

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
    """Return (firebase_uid, user_data) reading users/{uid} in Firestore.

    The mobile app stores user records at users/{firebase_auth_uid} with
    camelCase fields (uid, phone, displayName, isGuest, createdAt, updatedAt).
    We adopt the same collection so web and mobile share user records.

    Creates the doc on first web sign-in (mirroring mobile's create shape).
    """
    uid: str = decoded["uid"]
    phone: str = decoded.get("phone_number") or ""

    user_ref = db().collection("users").document(uid)
    snap = user_ref.get()

    if snap.exists:
        return uid, snap.to_dict() or {}

    new_data = {
        "uid": uid,
        "phone": phone,
        "displayName": phone,
        "isGuest": False,
        "createdAt": SERVER_TIMESTAMP,
        "updatedAt": SERVER_TIMESTAMP,
    }
    user_ref.set(new_data)
    return uid, new_data


def _customer_profile(customer_id: str, data: dict) -> CustomerProfile:
    return CustomerProfile(
        customer_id=customer_id,
        full_name=(data.get("name") or data.get("displayName") or "").strip(),
        phone=data.get("phone", "") or "",
        email=data.get("email"),
        firebase_uid=data.get("uid") or customer_id,
        last_shipping_address=data.get("lastShippingAddress"),
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
