import json
import logging
import re
from difflib import SequenceMatcher

from fastapi import APIRouter, HTTPException, Request
from google.cloud import firestore as firestore_module
from google.cloud.firestore_v1.base_query import FieldFilter

from .. import razorpay_utils, whatsapp
from ..config import settings
from ..firebase import SERVER_TIMESTAMP, db
from ..notifications import send_payment_receipt
from ..schemas.order import OrderStatus


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/razorpay", tags=["Razorpay Webhook"])


async def _send_receipt_if_needed(order_id: str) -> None:
    """Fire-and-log notification dispatch.
    Idempotent via `notified_at` on the order doc."""
    try:
        order_ref = db().collection("orders").document(order_id)
        snap = order_ref.get()
        if not snap.exists:
            return
        data = snap.to_dict() or {}
        if data.get("status") != OrderStatus.paid.value:
            return
        if data.get("notified_at"):
            return
        data["razorpay_order_id"] = order_id
        result = await send_payment_receipt(data)
        order_ref.set(
            {
                "notified_at": SERVER_TIMESTAMP,
                "notification_result": result,
            },
            merge=True,
        )
        logger.info("Receipt dispatch for %s: %s", order_id, result)
    except Exception:
        logger.exception("Receipt dispatch failed for %s (suppressed)", order_id)


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
        order_id = (_payment_entity(payload).get("order_id"))
        if order_id:
            await _send_receipt_if_needed(order_id)
    elif event_type == "payment.failed":
        _handle_payment_failed(payload, event_id)
    elif event_type in ("refund.created", "refund.processed"):
        _handle_refund(payload, event_id, event_type)
    elif event_type == "order.paid":
        _handle_payment_captured(payload, event_id)
        order_id = (_payment_entity(payload).get("order_id"))
        if order_id:
            await _send_receipt_if_needed(order_id)
    elif event_type == "virtual_account.credited":
        await _handle_virtual_account_credited(payload, event_id)
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

    # WhatsApp confirmation — only fire if the verify_payment path hasn't
    # already done it for this order. Idempotency via whatsapp_confirmation_sent_at.
    if not order_data.get("whatsapp_confirmation_sent_at"):
        item = order_data.get("item") or {}
        cust = order_data.get("customer") or {}
        sent = whatsapp.send_order_confirmed(
            phone=cust.get("phone") or "",
            name=cust.get("full_name"),
            order_id=order_data.get("receipt") or order_id,
            item_text=f"{item.get('name', 'Mobile VR Box')} x {item.get('quantity', 1)}",
            amount_rupees=paid_amount // 100,
        )
        if sent:
            order_ref.set({"whatsapp_confirmation_sent_at": SERVER_TIMESTAMP}, merge=True)


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

    # Look up the order this refund belongs to (by razorpay_payment_id) and
    # mirror the latest refund status onto it. When the refund is fully
    # processed for the captured amount we also flip status to "refunded"
    # so the admin dashboard and customer /account reflect it without
    # depending on the admin-initiated code path having run.
    try:
        order_snaps = (
            db()
            .collection("orders")
            .where("razorpay_payment_id", "==", payment_id)
            .limit(1)
            .get()
        )
    except Exception:
        logger.exception("refund webhook: lookup by payment_id failed (event=%s)", event_id)
        return

    if not order_snaps:
        return
    order_snap = order_snaps[0]
    order_data = order_snap.to_dict() or {}
    captured = int(order_data.get("amount_paid") or order_data.get("amount") or 0)
    refund_amount = int(refund.get("amount", 0))

    update: dict = {
        "refund_id": refund_id,
        "refund_amount": refund_amount,
        "refund_status": refund.get("status"),
        "updated_at": SERVER_TIMESTAMP,
    }
    if refund.get("status") == "processed" and refund_amount >= captured and captured > 0:
        update["status"] = "refunded"
        if not order_data.get("refunded_at"):
            update["refunded_at"] = SERVER_TIMESTAMP

    order_snap.reference.set(update, merge=True)


# --- Smart Collect (virtual account / UPI) flow ---


_REFERENCE_RE = re.compile(
    rf"{re.escape(settings.receipt_prefix)}-[A-Z0-9]+", re.IGNORECASE
)
_NAME_SIMILARITY_THRESHOLD = 0.80


def _normalize_name(s: str) -> str:
    return " ".join(s.lower().split())


def _phone_from_vpa(vpa: str) -> str | None:
    """Extract a 10-digit Indian phone from a VPA like `9876543210@paytm`."""
    if not vpa or "@" not in vpa:
        return None
    local = vpa.split("@", 1)[0]
    digits = "".join(ch for ch in local if ch.isdigit())
    if len(digits) == 10 and digits[0] in "6789":
        return digits
    return None


def _match_smart_collect_payment(
    amount: int,
    vpa: str,
    payer_name: str,
    notes: dict,
) -> tuple[str | None, str | None]:
    """Return (order_id, strategy) for the matched awaiting order, or
    (None, None) if no confident match across all three strategies."""
    orders = db().collection("orders")

    # STRATEGY A — reference in UPI note
    note_text = " ".join(str(v) for v in (notes or {}).values() if v)
    match = _REFERENCE_RE.search(note_text)
    if match:
        ref = match.group(0).upper()
        try:
            candidates = (
                orders
                .where(filter=FieldFilter("reference", "==", ref))
                .where(filter=FieldFilter(
                    "status", "==", OrderStatus.awaiting_payment.value
                ))
                .limit(1)
                .get()
            )
            if candidates:
                return candidates[0].id, "reference"
        except Exception:
            logger.exception("Strategy A (reference) failed")

    # STRATEGY B — phone extracted from payer VPA
    digits = _phone_from_vpa(vpa)
    if digits:
        phone_e164 = "+91" + digits
        try:
            candidates = (
                orders
                .where(filter=FieldFilter("customer.phone", "==", phone_e164))
                .where(filter=FieldFilter(
                    "status", "==", OrderStatus.awaiting_payment.value
                ))
                .order_by("created_at", direction=firestore_module.Query.DESCENDING)
                .limit(1)
                .get()
            )
            if candidates:
                return candidates[0].id, "phone_in_vpa"
        except Exception:
            logger.exception("Strategy B (phone_in_vpa) failed")

    # STRATEGY C — name fuzzy match (only if exactly one strong match)
    if payer_name and payer_name.strip():
        try:
            awaiting = (
                orders
                .where(filter=FieldFilter(
                    "status", "==", OrderStatus.awaiting_payment.value
                ))
                .order_by("created_at", direction=firestore_module.Query.DESCENDING)
                .limit(50)
                .get()
            )
            normalized_payer = _normalize_name(payer_name)
            strong: list[tuple[str, float]] = []
            for doc in awaiting:
                data = doc.to_dict() or {}
                customer = data.get("customer") or {}
                order_name = customer.get("full_name") or ""
                if not order_name:
                    continue
                sim = SequenceMatcher(
                    None,
                    normalized_payer,
                    _normalize_name(order_name),
                ).ratio()
                if sim >= _NAME_SIMILARITY_THRESHOLD:
                    strong.append((doc.id, sim))
            if len(strong) == 1:
                return strong[0][0], "name_fuzzy"
            if len(strong) > 1:
                logger.info(
                    "Strategy C: %d strong name matches; refusing to auto-match",
                    len(strong),
                )
        except Exception:
            logger.exception("Strategy C (name_fuzzy) failed")

    return None, None


def _record_unmatched_smart_collect(
    payment_id: str,
    payment: dict,
    amount: int,
    vpa: str,
    payer_name: str,
    notes: dict,
    event_id: str,
) -> None:
    db().collection("unmatched_payments").document(payment_id).set(
        {
            "payment_id": payment_id,
            "amount": amount,
            "currency": payment.get("currency", "INR"),
            "vpa": vpa,
            "payer_name": payer_name,
            "notes": notes,
            "method": payment.get("method"),
            "razorpay_payment_body": payment,
            "status": "needs_match",
            "received_at": SERVER_TIMESTAMP,
            "webhook_event_id": event_id,
        },
        merge=True,
    )


async def _confirm_smart_collect_order(
    order_id: str,
    payment: dict,
    payment_id: str,
    strategy: str,
    event_id: str,
) -> None:
    """Idempotently mark an awaiting order as paid + dispatch receipt email."""
    order_ref = db().collection("orders").document(order_id)
    snap = order_ref.get()
    if not snap.exists:
        logger.warning("smart_collect: order %s not found", order_id)
        return

    order_data = snap.to_dict() or {}
    if order_data.get("status") == OrderStatus.paid.value:
        logger.info("smart_collect: order %s already paid", order_id)
        return

    amount_paid = int(payment.get("amount", 0))
    bank_tx = payment.get("bank_transaction") or {}
    payer_info = {
        "name": bank_tx.get("payer_name") or (order_data.get("customer") or {}).get("full_name"),
        "vpa": payment.get("vpa"),
        "method": payment.get("method"),
    }

    order_ref.set(
        {
            "status": OrderStatus.paid.value,
            "amount_paid": amount_paid,
            "razorpay_payment_id": payment_id,
            "payer": payer_info,
            "paid_at": SERVER_TIMESTAMP,
            "updated_at": SERVER_TIMESTAMP,
            "paid_via": "smart_collect_webhook",
            "matched_via": strategy,
        },
        merge=True,
    )

    db().collection("transactions").document(payment_id).set(
        {
            "razorpay_payment_id": payment_id,
            "linked_order_id": order_id,
            "status": payment.get("status", "captured"),
            "amount_paid": amount_paid,
            "currency": payment.get("currency", "INR"),
            "razorpay_payment_body": payment,
            "source": "smart_collect_webhook",
            "payer": payer_info,
            "webhook_event_id": event_id,
            "created_at": SERVER_TIMESTAMP,
        },
        merge=True,
    )

    receipt_data = order_ref.get().to_dict() or {}
    receipt_data["razorpay_order_id"] = order_id
    receipt_data["razorpay_payment_id"] = payment_id
    receipt_data["amount_paid"] = amount_paid

    try:
        result = await send_payment_receipt(receipt_data)
        order_ref.set(
            {"notified_at": SERVER_TIMESTAMP, "notification_result": result},
            merge=True,
        )
        logger.info(
            "smart_collect: receipt dispatched for %s via %s -> %s",
            order_id, strategy, result,
        )
    except Exception:
        logger.exception("smart_collect: receipt dispatch failed for %s", order_id)


async def _handle_virtual_account_credited(payload: dict, event_id: str) -> None:
    payment = _payment_entity(payload)
    payment_id = payment.get("id")
    if not payment_id:
        logger.warning(
            "virtual_account.credited missing payment id (event=%s)", event_id
        )
        return

    # Idempotency — payment already recorded means we've already processed.
    if db().collection("transactions").document(payment_id).get().exists:
        logger.info("smart_collect: payment %s already processed", payment_id)
        return

    amount = int(payment.get("amount", 0))
    vpa = payment.get("vpa", "") or ""
    notes = payment.get("notes") or {}
    bank_tx = payment.get("bank_transaction") or {}
    payer_name = bank_tx.get("payer_name") or ""

    matched_order_id, strategy = _match_smart_collect_payment(
        amount=amount, vpa=vpa, payer_name=payer_name, notes=notes,
    )

    if matched_order_id and strategy:
        await _confirm_smart_collect_order(
            matched_order_id, payment, payment_id, strategy, event_id,
        )
    else:
        logger.info(
            "smart_collect: no match for payment %s amount=%d vpa=%s payer=%s",
            payment_id, amount, vpa, payer_name,
        )
        _record_unmatched_smart_collect(
            payment_id, payment, amount, vpa, payer_name, notes, event_id,
        )
