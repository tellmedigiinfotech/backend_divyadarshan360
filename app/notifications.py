"""Transactional email + SMS for order receipts.

Both senders are non-throwing: they log failures and return a dict the caller
can inspect, but they never raise. This is important because the webhook caller
must return 200 to Razorpay even when a notification side-effect fails.
"""

from __future__ import annotations

import logging
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any

import aiosmtplib
import httpx
from jinja2 import Template

from .config import settings


logger = logging.getLogger(__name__)


_EMAIL_TEMPLATE = Template(
    """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Order confirmation - {{ order_id }}</title>
</head>
<body style="margin:0;padding:0;background:#fff7ed;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:#1f2937;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#fff7ed;padding:32px 16px;">
    <tr><td align="center">
      <table role="presentation" width="600" cellspacing="0" cellpadding="0" border="0" style="max-width:600px;background:#ffffff;border-radius:20px;overflow:hidden;box-shadow:0 4px 24px rgba(212,175,55,0.12);">
        <tr><td style="background:linear-gradient(135deg,#d4af37 0%,#b8860b 100%);padding:32px 32px 28px;text-align:center;">
          <div style="font-family:Georgia,serif;font-size:28px;color:#ffffff;letter-spacing:0.5px;">Divya Darshan <em style="font-style:italic;">360</em></div>
          <div style="margin-top:8px;color:rgba(255,255,255,0.85);font-size:13px;letter-spacing:2px;text-transform:uppercase;">Payment Received</div>
        </td></tr>
        <tr><td style="padding:32px;">
          <p style="margin:0 0 16px;font-size:16px;">Namaste {{ customer_name or 'devotee' }},</p>
          <p style="margin:0 0 24px;font-size:15px;line-height:1.6;color:#4b5563;">
            Thank you for your order. Your payment was received and we'll arrange
            delivery shortly. A confirmation has also been sent to your registered phone.
          </p>

          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="border:1px solid #f3e8d2;border-radius:14px;margin-bottom:24px;">
            <tr><td style="padding:18px 20px;border-bottom:1px solid #f3e8d2;">
              <div style="font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#9ca3af;margin-bottom:4px;">Order ID</div>
              <div style="font-family:Menlo,Consolas,monospace;font-size:14px;color:#b8860b;font-weight:600;">{{ order_id }}</div>
            </td></tr>
            <tr><td style="padding:18px 20px;border-bottom:1px solid #f3e8d2;">
              <div style="font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#9ca3af;margin-bottom:4px;">Payment ID</div>
              <div style="font-family:Menlo,Consolas,monospace;font-size:13px;color:#374151;">{{ payment_id }}</div>
            </td></tr>
            <tr><td style="padding:18px 20px;">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
                <tr>
                  <td style="font-size:15px;color:#1f2937;">{{ product_name }} &times; {{ quantity }}</td>
                  <td align="right" style="font-size:15px;color:#1f2937;font-weight:600;">{{ currency_symbol }}{{ amount_display }}</td>
                </tr>
                <tr>
                  <td style="font-size:13px;color:#6b7280;padding-top:8px;">Shipping</td>
                  <td align="right" style="font-size:13px;color:#16a34a;padding-top:8px;">Free</td>
                </tr>
                <tr><td colspan="2" style="border-top:1px solid #f3e8d2;padding-top:12px;margin-top:12px;"></td></tr>
                <tr>
                  <td style="font-size:15px;color:#1f2937;font-weight:600;padding-top:12px;">Total paid</td>
                  <td align="right" style="font-size:18px;color:#b8860b;font-weight:700;padding-top:12px;">{{ currency_symbol }}{{ amount_display }}</td>
                </tr>
              </table>
            </td></tr>
          </table>

          {% if shipping %}
          <div style="margin-bottom:24px;">
            <div style="font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#9ca3af;margin-bottom:8px;">Shipping to</div>
            <div style="font-size:14px;color:#374151;line-height:1.6;">
              {{ shipping.line1 or '' }}<br />
              {{ shipping.city or '' }}{% if shipping.state %}, {{ shipping.state }}{% endif %} - {{ shipping.pincode or '' }}<br />
              {{ shipping.country or 'IN' }}
            </div>
          </div>
          {% endif %}

          <div style="background:#fff7ed;border-radius:12px;padding:16px 18px;margin-bottom:24px;">
            <div style="font-size:13px;color:#92400e;">
              <strong>Ships within 24 hours</strong> &middot; 7-day replacement &middot; Free in India
            </div>
          </div>

          <p style="margin:0;font-size:13px;color:#6b7280;line-height:1.6;">
            Need help? Reach us at
            <a href="mailto:{{ support_email }}" style="color:#b8860b;text-decoration:none;">{{ support_email }}</a>
            or {{ support_phone }}. Keep this email for your records.
          </p>
        </td></tr>
        <tr><td style="background:#1f2937;padding:18px 32px;text-align:center;">
          <div style="font-size:11px;color:rgba(255,255,255,0.55);letter-spacing:1px;">DivyaDarshan360.com &middot; TellMe Digi Infotech Pvt Ltd</div>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>
"""
)

_PLAIN_TEMPLATE = Template(
    """Namaste {{ customer_name or 'devotee' }},

Thank you for your order with Divya Darshan 360.

Order ID:    {{ order_id }}
Payment ID:  {{ payment_id }}
Item:        {{ product_name }} x {{ quantity }}
Amount paid: {{ currency_symbol }}{{ amount_display }}
{% if shipping %}
Shipping to:
  {{ shipping.line1 or '' }}
  {{ shipping.city or '' }}{% if shipping.state %}, {{ shipping.state }}{% endif %} - {{ shipping.pincode or '' }}
  {{ shipping.country or 'IN' }}
{% endif %}
Ships within 24 hours. Free shipping in India. 7-day replacement.

Need help? {{ support_email }} or {{ support_phone }}.

- Divya Darshan 360
"""
)


def render_receipt(order: dict[str, Any]) -> tuple[str, str, str]:
    """Returns (subject, html_body, text_body)."""
    item = order.get("item") or {}
    customer = order.get("customer") or {}
    shipping = order.get("shipping_address") or {}
    currency = order.get("currency", "INR")
    amount_paise = int(order.get("amount_paid", order.get("amount", 0)))
    amount_rupees = amount_paise / 100
    amount_display = f"{amount_rupees:,.2f}".rstrip("0").rstrip(".") if amount_rupees % 1 else f"{int(amount_rupees):,}"
    ctx = {
        "order_id": order.get("razorpay_order_id", ""),
        "payment_id": order.get("razorpay_payment_id", ""),
        "customer_name": customer.get("full_name"),
        "product_name": item.get("name", "Order"),
        "quantity": int(item.get("quantity", 1)),
        "amount_display": amount_display,
        "currency_symbol": "₹" if currency == "INR" else f"{currency} ",
        "shipping": shipping,
        "support_email": settings.merchant_support_email,
        "support_phone": settings.merchant_support_phone,
    }
    subject = f"Order confirmation · {ctx['order_id']}"
    return subject, _EMAIL_TEMPLATE.render(**ctx), _PLAIN_TEMPLATE.render(**ctx)


async def send_email_async(recipient: str, subject: str, html_body: str, text_body: str) -> dict:
    if not settings.smtp_server or not settings.smtp_username or not settings.smtp_password:
        return {"sent": False, "reason": "smtp_not_configured"}

    message = EmailMessage()
    message["From"] = formataddr((settings.smtp_from_name, settings.smtp_username))
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    use_tls = settings.smtp_port == 465
    start_tls = settings.smtp_port == 587

    try:
        await aiosmtplib.send(
            message,
            hostname=settings.smtp_server,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            use_tls=use_tls,
            start_tls=start_tls,
            timeout=30,
        )
        return {"sent": True}
    except Exception as exc:
        logger.exception("send_email_async failed for %s", recipient)
        return {"sent": False, "reason": "smtp_error", "error": str(exc)}


def _strip_e164(phone: str) -> str:
    return "".join(ch for ch in (phone or "") if ch.isdigit())


def render_sms_text(order: dict[str, Any]) -> str:
    item = order.get("item") or {}
    amount_paise = int(order.get("amount_paid", order.get("amount", 0)))
    amount_rupees = amount_paise // 100
    order_id = order.get("razorpay_order_id", "")
    short_id = order_id[-8:] if order_id else ""
    name = item.get("name", "your order")
    return (
        f"Divya Darshan 360: Payment of Rs.{amount_rupees} received for {name}. "
        f"Order #{short_id}. Ships within 24h. Help: {settings.merchant_support_phone}"
    )


async def send_sms_async(phone_e164_or_local: str, message: str) -> dict:
    if (
        not settings.sms_striker_username
        or not settings.sms_striker_password
        or not settings.sms_striker_channel
    ):
        return {"sent": False, "reason": "sms_not_configured"}

    digits = _strip_e164(phone_e164_or_local)
    if len(digits) == 10:
        digits = "91" + digits

    params = {
        "username": settings.sms_striker_username,
        "password": settings.sms_striker_password,
        "from": settings.sms_striker_channel,
        "to": digits,
        "msg": message,
        "type": "1",
    }
    if settings.sms_striker_order_template_id:
        params["template_id"] = settings.sms_striker_order_template_id

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.get(settings.sms_striker_url, params=params)
    except Exception as exc:
        logger.exception("SMS Striker request failed for %s", digits)
        return {"sent": False, "reason": "sms_network_error", "error": str(exc)}

    if res.status_code != 200:
        logger.warning("SMS Striker non-200 (status=%s body=%s)", res.status_code, res.text[:200])
        return {"sent": False, "reason": "sms_http_error", "status": res.status_code, "body": res.text[:200]}

    body = (res.text or "").lower()
    if "invalid" in body or "error" in body or "fail" in body:
        logger.warning("SMS Striker reported failure: %s", res.text[:200])
        return {"sent": False, "reason": "sms_provider_error", "body": res.text[:200]}

    return {"sent": True, "body": res.text[:200]}


async def send_payment_receipt(order: dict[str, Any]) -> dict:
    """Returns {"email": {...}, "sms": {...}}. Never raises."""
    customer = order.get("customer") or {}
    email_addr = customer.get("email")
    phone = customer.get("phone")

    result: dict = {"email": {"sent": False, "reason": "skipped"}, "sms": {"sent": False, "reason": "skipped"}}

    try:
        subject, html_body, text_body = render_receipt(order)
    except Exception as exc:
        logger.exception("Failed to render receipt template")
        return {"email": {"sent": False, "reason": "render_error", "error": str(exc)},
                "sms": {"sent": False, "reason": "render_error", "error": str(exc)}}

    if email_addr:
        try:
            result["email"] = await send_email_async(email_addr, subject, html_body, text_body)
        except Exception as exc:
            logger.exception("Unexpected email send error")
            result["email"] = {"sent": False, "reason": "unexpected_error", "error": str(exc)}
    else:
        result["email"] = {"sent": False, "reason": "no_email_address"}

    if phone:
        try:
            result["sms"] = await send_sms_async(phone, render_sms_text(order))
        except Exception as exc:
            logger.exception("Unexpected SMS send error")
            result["sms"] = {"sent": False, "reason": "unexpected_error", "error": str(exc)}
    else:
        result["sms"] = {"sent": False, "reason": "no_phone_number"}

    return result
