import hmac as _hmac
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from firebase_admin import auth as firebase_auth

from .config import settings
from .firebase import init_firebase


bearer_scheme = HTTPBearer(auto_error=False)


def verify_firebase_token(token: str) -> dict:
    # firebase_auth.verify_id_token() raises "default Firebase app does not
    # exist" when initialize_app() hasn't been called yet. Inside Firebase
    # Functions our FastAPI lifespan never runs (TestClient bridge skips it),
    # so we init here lazily. init_firebase() is idempotent.
    init_firebase()
    try:
        return firebase_auth.verify_id_token(token, check_revoked=False)
    except firebase_auth.ExpiredIdTokenError as exc:
        raise HTTPException(status_code=401, detail="ID token has expired") from exc
    except firebase_auth.RevokedIdTokenError as exc:
        raise HTTPException(status_code=401, detail="ID token has been revoked") from exc
    except firebase_auth.InvalidIdTokenError as exc:
        raise HTTPException(status_code=401, detail="Invalid ID token") from exc
    except firebase_auth.UserDisabledError as exc:
        raise HTTPException(status_code=403, detail="User account is disabled") from exc
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=f"ID token error: {exc}") from exc


def get_current_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> dict:
    if creds is None or not creds.credentials:
        raise HTTPException(status_code=401, detail="Missing Authorization Bearer token")
    return verify_firebase_token(creds.credentials)


def get_optional_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> dict | None:
    if creds is None or not creds.credentials:
        return None
    return verify_firebase_token(creds.credentials)


def require_admin(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> dict:
    """Gate /admin/* endpoints. Accepts either:

    1. An `X-Admin-Password` header matching settings.admin_password
       (the primary shared-password flow used by /admin/login), OR
    2. A Firebase Bearer token whose verified email/phone is in
       ADMIN_EMAILS / ADMIN_PHONES (kept as a fallback in case email-
       or Google-sign-in is added later).
    """
    # Path 1: shared admin password header
    pw = request.headers.get("x-admin-password")
    if pw and settings.admin_password and _hmac.compare_digest(pw, settings.admin_password):
        return {"uid": "admin-shared", "email": None, "phone_number": None, "via": "password"}

    # Path 2: Firebase token + email/phone allowlist
    if creds is not None and creds.credentials:
        decoded = verify_firebase_token(creds.credentials)
        email = (decoded.get("email") or "").strip().lower()
        phone = (decoded.get("phone_number") or "").strip()
        allowed_emails = {e.strip().lower() for e in settings.admin_emails if e.strip()}
        allowed_phones = {p.strip() for p in settings.admin_phones if p.strip()}
        if (email and email in allowed_emails) or (phone and phone in allowed_phones):
            decoded["via"] = "firebase"
            return decoded

    raise HTTPException(status_code=403, detail="Admin access required")
