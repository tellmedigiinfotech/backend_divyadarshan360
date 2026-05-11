import json
import logging

from fastapi import APIRouter, HTTPException, Request

from .. import razorpay_utils
from ..config import settings
from ..firebase import SERVER_TIMESTAMP, db
from ..schemas.order import OrderStatus


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/razorpay", tags=["Razorpay Webhook"])


@router.post("/webhook")
async def razorpay_webhook(request: Request) -> dict:
    if not settings.razorpay_webhook_secret:
        logger.error(
            "Razorpay webhook called but RAZORPAY_WEBHOOK_SECRET is not configured"
        )
        raise HTTPException(status_code=503, detail="Webhook not configured")

    body = await request.body()
    signature = request.headers.get("x-razorpay-signature", "")

    if not razorpay_utils.verify_webhook_signature(body, signature):
        logger.warning("Razorpay webhook: invalid signature")
        raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        event = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

    event_type: str = event.get("event", "")
    event_id: str = event.get("id", "")
    payload: dict = event.get("payload", {}) or {}

    logger.info("Razorpay webhook event=%s id=%s", event_type, event_id)

    if event_type == "payment.captured":
        _handle_payment_captured(payload, event_id)
    elif event_type == "payment.failed":
        _handle_payment_failed(payload, event_id)
    elif event_type in ("refund.created", "refund.processed"):
        _handle_refund(payload, event_id, event_type)
    elif event_type == "order.paid":
        # Some merchants enable this in addition to payment.captured.
        # The payment is already on the event.
        _handle_payment_captured(payload, event_id)
    else:
        logger.info("Razorpay webhook: unhandled event type %s", event_type)

    return {"status": "ok"}


def _payment_entity(payload: dict) -> dict:
    return ((payload.get("payment") or {}).get("entity")) or {}


def _refund_entity(payload: dict) -> dict:
    return ((payload.get("refund") or {}).get("entity")) or {}


def _handle_payment_captured(payload: dict, event_id: str) -> None:
    payment = _payment_entity(payload)
    order_id = payment.get("order_id")
    payment_id = payment.get("id")
    if not order_id or not payment_id:
        logger.warning(
            "payment.captured missing order_id or payment_id (event=%s)", event_id
        )
        return

    order_ref = db().collection("orders").document(order_id)
    order_snap = order_ref.get()
    if not order_snap.exists:
        logger.warning(
            "payment.captured: order %s not found in Firestore (event=%s)",
            order_id,
            event_id,
        )
        return

    order_data = order_snap.to_dict() or {}
    already_paid = order_data.get("status") == OrderStatus.paid.value

    paid_amount = int(payment.get("amount", order_data.get("amount", 0)))
    paid_currency = payment.get("currency", order_data.get("currency", "INR"))

    # Defense in depth: refuse to mark paid if amount/currency don't match.
    expected_amount = int(order_data.get("amount", 0))
    expected_currency = order_data.get("currency", "INR")
    if paid_amount != expected_amount or paid_currency != expected_currency:
        logger.error(
            "payment.captured amount/currency mismatch for order %s "
            "(expected %s %s, got %s %s)",
            order_id,
            expected_amount,
            expected_currency,
            paid_amount,
            paid_currency,
        )
        db().collection("transactions").document(payment_id).set(
            {
                "razorpay_order_id": order_id,
                "razorpay_payment_id": payment_id,
                "status": "amount_mismatch",
                "amount_paid": paid_amount,
                "currency": paid_currency,
                "razorpay_payment_body": payment,
                "source": "webhook",
                "webhook_event_id": event_id,
                "created_at": SERVER_TIMESTAMP,
            },
            merge=True,
        )
        return

    if not already_paid:
        order_ref.set(
            {
                "status": OrderStatus.paid.value,
                "amount_paid": paid_amount,
                "razorpay_payment_id": payment_id,
                "paid_at": SERVER_TIMESTAMP,
                "updated_at": SERVER_TIMESTAMP,
                "paid_via": order_data.get("paid_via") or "webhook",
            },
            merge=True,
        )

    db().collection("transactions").document(payment_id).set(
        {
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "status": payment.get("status", "captured"),
            "amount_paid": paid_amount,
            "currency": paid_currency,
            "razorpay_payment_body": payment,
            "source": "webhook",
            "webhook_event_id": event_id,
            "created_at": SERVER_TIMESTAMP,
        },
        merge=True,
    )


def _handle_payment_failed(payload: dict, event_id: str) -> None:
    payment = _payment_entity(payload)
    order_id = payment.get("order_id")
    payment_id = payment.get("id")
    if not order_id or not payment_id:
        return

    order_ref = db().collection("orders").document(order_id)
    order_snap = order_ref.get()

    # Never downgrade a paid order to failed (the verify_payment call may have
    # already marked it paid via a sibling path).
    if order_snap.exists:
        order_data = order_snap.to_dict() or {}
        if order_data.get("status") != OrderStatus.paid.value:
            order_ref.set(
                {
                    "status": OrderStatus.failed.value,
                    "updated_at": SERVER_TIMESTAMP,
                },
                merge=True,
            )

    db().collection("transactions").document(payment_id).set(
        {
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "status": "failed",
            "amount_paid": 0,
            "currency": payment.get("currency", "INR"),
            "razorpay_payment_body": payment,
            "error_code": payment.get("error_code"),
            "error_description": payment.get("error_description"),
            "source": "webhook",
            "webhook_event_id": event_id,
            "created_at": SERVER_TIMESTAMP,
        },
        merge=True,
    )


def _handle_refund(payload: dict, event_id: str, event_type: str) -> None:
    refund = _refund_entity(payload)
    refund_id = refund.get("id")
    payment_id = refund.get("payment_id")
    if not refund_id or not payment_id:
        logger.warning("%s missing refund_id or payment_id (event=%s)", event_type, event_id)
        return

    db().collection("refunds").document(refund_id).set(
        {
            "refund_id": refund_id,
            "razorpay_payment_id": payment_id,
            "amount": int(refund.get("amount", 0)),
            "currency": refund.get("currency", "INR"),
            "status": refund.get("status"),
            "razorpay_refund_body": refund,
            "source": "webhook",
            "event_type": event_type,
            "webhook_event_id": event_id,
            "created_at": SERVER_TIMESTAMP,
        },
        merge=True,
    )
