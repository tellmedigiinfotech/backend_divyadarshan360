"""Phone-number blocklist.

A doc per blocked phone lives at blocked_phones/{phone}. Stored fields:
    blocked_at, blocked_by, reason

Order-creation endpoints call is_phone_blocked() before any side effects
(Razorpay order create, Firestore write) so spam phones get a clean 403.
"""

from __future__ import annotations

from .firebase import db


def _normalize(phone: str) -> str:
    """E.164 the way our orders store it: leading + plus digits only.
    Stays as-is if already E.164; strips spaces/dashes; assumes IN if 10 digits."""
    if not phone:
        return ""
    digits = "".join(ch for ch in phone if ch.isdigit())
    if not digits:
        return ""
    if phone.strip().startswith("+"):
        return f"+{digits}"
    if len(digits) == 10:
        return f"+91{digits}"
    if len(digits) == 12 and digits.startswith("91"):
        return f"+{digits}"
    return f"+{digits}"


def is_phone_blocked(phone: str | None) -> bool:
    p = _normalize(phone or "")
    if not p:
        return False
    snap = db().collection("blocked_phones").document(p).get()
    return snap.exists


def block_phone(phone: str, *, reason: str, blocked_by: str) -> str:
    """Add a phone to the blocklist. Returns the normalized phone."""
    from .firebase import SERVER_TIMESTAMP

    p = _normalize(phone)
    if not p:
        raise ValueError(f"Invalid phone: {phone!r}")
    db().collection("blocked_phones").document(p).set(
        {
            "phone": p,
            "blocked_at": SERVER_TIMESTAMP,
            "blocked_by": blocked_by,
            "reason": reason,
        }
    )
    return p


def unblock_phone(phone: str) -> str:
    p = _normalize(phone)
    if not p:
        raise ValueError(f"Invalid phone: {phone!r}")
    db().collection("blocked_phones").document(p).delete()
    return p
