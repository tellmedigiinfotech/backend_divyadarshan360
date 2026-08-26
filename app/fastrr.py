"""Fastrr (Shiprocket Checkout) API client.

Auth for every outbound call is two headers:
  X-Api-Key:          <api key>
  X-Api-HMAC-SHA256:  Base64( HMAC-SHA256( request-body-bytes, api secret ) )

The HMAC is computed over the EXACT body string we send, so we serialize the
payload once and use that same string for both signing and the request.

Non-throwing style is NOT used here: these are called from request handlers that
should surface a 502 to the caller when Fastrr is unreachable.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

import httpx

from .config import settings

logger = logging.getLogger(__name__)

_STAGING = "https://fastrr-api-dev.pickrr.com"
_PRODUCTION = "https://checkout-api.shiprocket.com"


class FastrrError(Exception):
    pass


def base_url() -> str:
    return _STAGING if settings.fastrr_env == "staging" else _PRODUCTION


def is_configured() -> bool:
    return bool(settings.fastrr_api_key and settings.fastrr_api_secret)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sign(body: str) -> str:
    digest = hmac.new(
        settings.fastrr_api_secret.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def _post(path: str, payload: dict) -> dict:
    if not is_configured():
        raise FastrrError("Fastrr is not configured (missing api key/secret).")

    body = json.dumps(payload, separators=(",", ":"))
    headers = {
        "X-Api-Key": settings.fastrr_api_key,
        "X-Api-HMAC-SHA256": _sign(body),
        "Content-Type": "application/json",
    }
    url = f"{base_url()}{path}"
    try:
        with httpx.Client(timeout=15.0) as client:
            res = client.post(url, content=body, headers=headers)
    except httpx.HTTPError as exc:
        logger.exception("Fastrr request failed: %s", path)
        raise FastrrError(f"Fastrr request failed: {exc}") from exc

    if res.status_code >= 400:
        snippet = (res.text or "")[:400]
        logger.warning("Fastrr %s -> %s: %s", path, res.status_code, snippet)
        raise FastrrError(f"Fastrr {path} {res.status_code}: {snippet}")

    try:
        return res.json()
    except ValueError as exc:
        raise FastrrError(f"Fastrr {path} returned non-JSON body") from exc


def generate_checkout_token(items: list[dict], redirect_url: str) -> dict:
    """POST /api/v1/access-token/checkout.

    items: [{"variant_id": "...", "quantity": 1}]
    Returns the parsed response; the token is at result["result"]["token"] and
    the Fastrr order id at result["result"]["data"]["order_id"].
    """
    payload = {
        "cart_data": {"items": items},
        "redirect_url": redirect_url,
        "timestamp": _now_iso(),
    }
    return _post("/api/v1/access-token/checkout", payload)


def fetch_order_details(order_id: str) -> dict:
    """POST /api/v1/custom-platform-order/details for a single order."""
    payload = {"order_id": order_id, "timestamp": _now_iso()}
    return _post("/api/v1/custom-platform-order/details", payload)
