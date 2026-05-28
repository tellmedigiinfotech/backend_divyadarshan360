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


def require_admin(decoded: Annotated[dict, Depends(get_current_user)]) -> dict:
    """Gate /admin/* endpoints. Matches the verified token email OR phone
    against the comma-separated allowlists in settings (ADMIN_EMAILS /
    ADMIN_PHONES). Either match is sufficient — so an admin who signs in
    by phone OTP can be allowlisted by phone without needing email auth.
    """
    email = (decoded.get("email") or "").strip().lower()
    phone = (decoded.get("phone_number") or "").strip()

    allowed_emails = {e.strip().lower() for e in settings.admin_emails if e.strip()}
    allowed_phones = {p.strip() for p in settings.admin_phones if p.strip()}

    if (email and email in allowed_emails) or (phone and phone in allowed_phones):
        return decoded
    raise HTTPException(status_code=403, detail="Admin access required")
