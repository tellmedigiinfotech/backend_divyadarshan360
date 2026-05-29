"""WhatsApp Cloud API (Meta Graph) integration.

Send approved template messages to customers for:
  - order_confirmed       (Razorpay payment captured)
  - cod_order_received    (Cash on Delivery order placed)
  - refund_processed      (admin issued a refund)

WhatsApp is OPTIONAL. If WHATSAPP_ACCESS_TOKEN or WHATSAPP_PHONE_NUMBER_ID
is unset, every helper silently no-ops so the rest of the order flow keeps
working. Failures are logged and swallowed — never break checkout because
WhatsApp errored.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import settings


logger = logging.getLogger(__name__)


class WhatsAppError(Exception):
    pass


def _normalize_phone(phone: str) -> str:
    """Meta expects digits only, no leading '+'. Strip everything else."""
    return "".join(ch for ch in (phone or "") if ch.isdigit())


def _first_name(name: str | None) -> str:
    if not name:
        return "devotee"
    parts = name.strip().split()
    return parts[0] if parts else "devotee"


def _is_configured() -> bool:
    return bool(settings.whatsapp_access_token and settings.whatsapp_phone_number_id)


def send_template(
    to_phone: str,
    template_name: str,
    body_params: list[str],
    language: str | None = None,
) -> dict | None:
    """Send a WhatsApp template message via Meta Cloud API.

    Returns the JSON response on success.
    Returns None if WhatsApp is not configured (silently skipped).
    Raises WhatsAppError on API failure so the caller can log + decide.
    """
    if not _is_configured():
        logger.info(
            "WhatsApp not configured; skipping %s -> %s", template_name, to_phone
        )
        return None

    to = _normalize_phone(to_phone)
    if not to:
        raise WhatsAppError(f"Invalid recipient phone: {to_phone!r}")

    url = (
        f"https://graph.facebook.com/{settings.whatsapp_graph_version}"
        f"/{settings.whatsapp_phone_number_id}/messages"
    )
    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language or settings.whatsapp_template_language},
            "components": [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": p} for p in body_params],
                }
            ],
        },
    }
    headers = {
        "Authorization": f"Bearer {settings.whatsapp_access_token}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            body = resp.text[:500]
            raise WhatsAppError(
                f"WhatsApp API {resp.status_code}: {body}"
            )
        return resp.json()
    except httpx.HTTPError as exc:
        raise WhatsAppError(f"WhatsApp request failed: {exc}") from exc


# --- Convenience helpers (fire-and-forget; never raise) ---


def send_order_confirmed(
    *, phone: str, name: str | None, order_id: str, item_text: str, amount_rupees: int
) -> bool:
    """True on success, False on (logged) failure or unconfigured."""
    try:
        result = send_template(
            to_phone=phone,
            template_name="order_confirmed",
            body_params=[_first_name(name), order_id, item_text, str(amount_rupees)],
        )
        return result is not None
    except WhatsAppError as exc:
        logger.warning("WhatsApp order_confirmed failed for %s: %s", order_id, exc)
        return False


def send_cod_received(
    *, phone: str, name: str | None, order_id: str, item_text: str, amount_rupees: int
) -> bool:
    try:
        result = send_template(
            to_phone=phone,
            template_name="cod_order_received",
            body_params=[_first_name(name), order_id, item_text, str(amount_rupees)],
        )
        return result is not None
    except WhatsAppError as exc:
        logger.warning("WhatsApp cod_order_received failed for %s: %s", order_id, exc)
        return False


def send_refund_processed(
    *,
    phone: str,
    name: str | None,
    order_id: str,
    amount_rupees: int,
    status: str,
) -> bool:
    try:
        result = send_template(
            to_phone=phone,
            template_name="refund_processed",
            body_params=[
                _first_name(name),
                order_id,
                str(amount_rupees),
                status or "processed",
            ],
        )
        return result is not None
    except WhatsAppError as exc:
        logger.warning("WhatsApp refund_processed failed for %s: %s", order_id, exc)
        return False
