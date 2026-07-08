import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from google.api_core import exceptions as gax_exceptions

from .. import notifications, razorpay_utils, whatsapp
from ..auth import get_current_user
from ..blocklist import is_phone_blocked
from ..config import settings
from ..firebase import SERVER_TIMESTAMP, db
from ..products import get_product
from ..schemas.order import (
    CreateCodOrderInput,
    CreateCodOrderOutput,
    CreateOrderInput,
    CreateOrderOutput,
    OrderCustomerView,
    OrderItemView,
    OrderPayer,
    OrderStatus,
    OrderStatusOutput,
    OrderView,
    SmartCollectCreateInput,
    SmartCollectCreateOutput,
    VerifyPaymentInput,
    VerifyPaymentOutput,
)
from .auth import resolve_or_create_customer


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orders", tags=["Orders"])


def _make_receipt() -> str:
    ts = int(time.time())
    return f"{settings.receipt_prefix}-{ts}-{secrets.token_hex(3).upper()}"


def _doc_to_order_view(order_id: str, data: dict) -> OrderView:
    item = data.get("item", {})
    customer = data.get("customer", {})
    shipping = data.get("shipping_address", {})
    return OrderView(
        razorpay_order_id=order_id,
        receipt=data.get("receipt", ""),
        status=OrderStatus(data.get("status", "created")),
        amount=int(data.get("amount", 0)),
        amount_paid=int(data.get("amount_paid", 0)),
        currency=data.get("currency", "INR"),
        item=OrderItemView(
            sku=item.get("sku", ""),
            name=item.get("name", ""),
            quantity=int(item.get("quantity", 1)),
            unit_price_paise=int(item.get("unit_price_paise", 0)),
            line_total_paise=int(item.get("line_total_paise", 0)),
        ),
        customer=OrderCustomerView(
            full_name=customer.get("full_name", ""),
            phone=customer.get("phone", ""),
            email=customer.get("email"),
        ),
        shipping_address=shipping,
        created_at=_ts_to_iso(data.get("created_at")),
        paid_at=_ts_to_iso(data.get("paid_at")),
        razorpay_key_id=settings.razorpay_key_id,
        fulfillment_status=data.get("fulfillment_status"),
        tracking_number=data.get("tracking_number"),
        courier=data.get("courier"),
        shipped_at=_ts_to_iso(data.get("shipped_at")),
        delivered_at=_ts_to_iso(data.get("delivered_at")),
        admin_notes=data.get("admin_notes"),
        payment_method=data.get("payment_method"),
        razorpay_payment_id=data.get("razorpay_payment_id"),
        refund_id=data.get("refund_id"),
        refund_amount=data.get("refund_amount"),
        refund_status=data.get("refund_status"),
        refunded_at=_ts_to_iso(data.get("refunded_at")),
        refund_reason=data.get("refund_reason"),
        cancelled_at=_ts_to_iso(data.get("cancelled_at")),
        cancellation_reason=data.get("cancellation_reason"),
    )


def _ts_to_iso(value) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)


def _update_customer_from_form(customer_id: str, payload: CreateOrderInput) -> None:
    """Merge web-checkout supplied info onto users/{uid}.

    Uses camelCase to match the mobile app's existing user schema. Never
    touches mobile-managed fields (uid, displayName, isGuest, createdAt,
    phone) so concurrent mobile writes are safe.
    """
    update: dict = {
        "updatedAt": SERVER_TIMESTAMP,
        "lastShippingAddress": payload.shipping_address.model_dump(),
    }
    if payload.customer.full_name:
        update["name"] = payload.customer.full_name
    if payload.customer.email:
        update["email"] = payload.customer.email
    db().collection("users").document(customer_id).set(update, merge=True)


@router.post("/create_order", response_model=CreateOrderOutput)
def create_order(
    payload: CreateOrderInput,
    decoded: Annotated[dict, Depends(get_current_user)],
) -> CreateOrderOutput:
    token_phone_for_block = decoded.get("phone_number") or payload.customer.phone
    if is_phone_blocked(token_phone_for_block):
        raise HTTPException(status_code=403, detail="This phone is not allowed to place orders.")

    product = get_product(payload.item.sku)
    if product is None:
        raise HTTPException(status_code=404, detail=f"Unknown product sku: {payload.item.sku}")

    if payload.item.quantity > product.max_quantity:
        raise HTTPException(
            status_code=400,
            detail=f"Quantity exceeds the per-order limit of {product.max_quantity}.",
        )

    amount_paise = product.unit_price_paise * payload.item.quantity
    receipt = _make_receipt()

    try:
        customer_id, _ = resolve_or_create_customer(decoded)
        _update_customer_from_form(customer_id, payload)
    except gax_exceptions.PermissionDenied as exc:
        logger.exception("Firestore permission denied")
        raise HTTPException(
            status_code=503,
            detail="Firestore is not enabled or the service account lacks permission.",
        ) from exc
    except gax_exceptions.GoogleAPIError as exc:
        logger.exception("Firestore error")
        raise HTTPException(status_code=503, detail=f"Firestore error: {exc}") from exc

    # Phone comes from the verified token, not the request body.
    token_phone: str = decoded.get("phone_number") or payload.customer.phone

    try:
        rp_order = razorpay_utils.create_order(
            amount_paise=amount_paise,
            currency=product.currency,
            receipt=receipt,
            notes={
                "sku": product.sku,
                "quantity": str(payload.item.quantity),
                "customer_id": customer_id,
                "firebase_uid": decoded["uid"],
                "customer_phone": token_phone,
            },
        )
    except Exception as exc:
        logger.exception("Razorpay order creation failed")
        raise HTTPException(status_code=502, detail="Could not create payment order.") from exc

    order_doc = {
        "razorpay_order_id": rp_order["id"],
        "receipt": receipt,
        "status": OrderStatus.created.value,
        "amount": amount_paise,
        "amount_paid": 0,
        "currency": product.currency,
        "item": {
            "sku": product.sku,
            "name": product.name,
            "quantity": payload.item.quantity,
            "unit_price_paise": product.unit_price_paise,
            "line_total_paise": amount_paise,
        },
        "customer": {
            "full_name": payload.customer.full_name,
            "phone": token_phone,
            "email": payload.customer.email,
        },
        "customer_id": customer_id,
        "firebase_uid": decoded["uid"],
        "shipping_address": payload.shipping_address.model_dump(),
        "razorpay_order_response": rp_order,
        "created_at": SERVER_TIMESTAMP,
        "updated_at": SERVER_TIMESTAMP,
        "paid_at": None,
    }

    db().collection("orders").document(rp_order["id"]).set(order_doc)

    return CreateOrderOutput(
        razorpay_order_id=rp_order["id"],
        razorpay_key_id=settings.razorpay_key_id,
        amount=amount_paise,
        currency=product.currency,
        receipt=receipt,
        customer_id=customer_id,
        product_name=product.name,
    )


@router.post("/create_cod_order", response_model=CreateCodOrderOutput)
def create_cod_order(
    payload: CreateCodOrderInput,
    decoded: Annotated[dict, Depends(get_current_user)],
) -> CreateCodOrderOutput:
    """Cash-on-Delivery via WhatsApp. No Razorpay call — we just record the
    order in Firestore so it shows up in the admin dashboard and the user's
    /account list. Status stays `cod_pending` until the admin marks delivery
    (which flips it to `paid`).
    """
    token_phone_for_block = decoded.get("phone_number") or payload.customer.phone
    if is_phone_blocked(token_phone_for_block):
        raise HTTPException(status_code=403, detail="This phone is not allowed to place orders.")

    product = get_product(payload.item.sku)
    if product is None:
        raise HTTPException(status_code=404, detail=f"Unknown product sku: {payload.item.sku}")
    if payload.item.quantity > product.max_quantity:
        raise HTTPException(
            status_code=400,
            detail=f"Quantity exceeds the per-order limit of {product.max_quantity}.",
        )

    amount_paise = product.unit_price_paise * payload.item.quantity
    receipt = _make_receipt()

    try:
        customer_id, _ = resolve_or_create_customer(decoded)
        _update_customer_from_form(customer_id, payload)
    except gax_exceptions.PermissionDenied as exc:
        logger.exception("Firestore permission denied")
        raise HTTPException(
            status_code=503,
            detail="Firestore is not enabled or the service account lacks permission.",
        ) from exc
    except gax_exceptions.GoogleAPIError as exc:
        logger.exception("Firestore error")
        raise HTTPException(status_code=503, detail=f"Firestore error: {exc}") from exc

    token_phone: str = decoded.get("phone_number") or payload.customer.phone

    new_ref = db().collection("orders").document()
    order_doc = {
        "razorpay_order_id": None,
        "receipt": receipt,
        "status": OrderStatus.cod_pending.value,
        "payment_method": "cod_whatsapp",
        "amount": amount_paise,
        "amount_paid": 0,
        "currency": product.currency,
        "item": {
            "sku": product.sku,
            "name": product.name,
            "quantity": payload.item.quantity,
            "unit_price_paise": product.unit_price_paise,
            "line_total_paise": amount_paise,
        },
        "customer": {
            "full_name": payload.customer.full_name,
            "phone": token_phone,
            "email": payload.customer.email,
        },
        "customer_id": customer_id,
        "firebase_uid": decoded["uid"],
        "shipping_address": payload.shipping_address.model_dump(),
        "created_at": SERVER_TIMESTAMP,
        "updated_at": SERVER_TIMESTAMP,
        "paid_at": None,
    }
    new_ref.set(order_doc)

    sent = whatsapp.send_cod_received(
        phone=token_phone,
        name=payload.customer.full_name,
        order_id=receipt,
        item_text=f"{product.name} x {payload.item.quantity}",
        amount_rupees=amount_paise // 100,
    )
    if sent:
        new_ref.set({"whatsapp_confirmation_sent_at": SERVER_TIMESTAMP}, merge=True)

    # Notify the internal team of the new COD order. Non-throwing — a mail
    # failure must not fail the order.
    try:
        alert = notifications.send_cod_team_alert(order_doc)
        if not alert.get("sent"):
            logger.warning("COD team alert not sent: %s", alert.get("reason"))
    except Exception:
        logger.exception("Unexpected error sending COD team alert")

    return CreateCodOrderOutput(
        order_id=new_ref.id,
        receipt=receipt,
        amount=amount_paise,
        currency=product.currency,
        product_name=product.name,
    )


def _assert_order_ownership(order: dict, decoded: dict) -> None:
    owner = order.get("firebase_uid")
    # Legacy orders created before auth was required have no firebase_uid.
    # Reject only if the order DOES have one and it differs from the caller.
    if owner and owner != decoded["uid"]:
        raise HTTPException(status_code=403, detail="Order does not belong to current user")


@router.post("/verify_payment", response_model=VerifyPaymentOutput)
def verify_payment(
    payload: VerifyPaymentInput,
    decoded: Annotated[dict, Depends(get_current_user)],
) -> VerifyPaymentOutput:
    order_ref = db().collection("orders").document(payload.razorpay_order_id)
    order_snap = order_ref.get()
    if not order_snap.exists:
        raise HTTPException(status_code=404, detail="Order not found")
    order = order_snap.to_dict() or {}
    _assert_order_ownership(order, decoded)

    already_paid = order.get("status") == OrderStatus.paid.value

    is_valid = razorpay_utils.verify_signature(
        payload.razorpay_order_id,
        payload.razorpay_payment_id,
        payload.razorpay_signature,
    )

    if not is_valid:
        db().collection("transactions").document(payload.razorpay_payment_id).set(
            {
                "razorpay_order_id": payload.razorpay_order_id,
                "razorpay_payment_id": payload.razorpay_payment_id,
                "razorpay_signature": payload.razorpay_signature,
                "status": "signature_failed",
                "amount_paid": 0,
                "currency": order.get("currency", "INR"),
                "source": "verify_payment",
                "created_at": SERVER_TIMESTAMP,
            },
            merge=True,
        )
        # Do not downgrade an order the webhook may have already marked paid.
        if not already_paid:
            order_ref.set(
                {"status": OrderStatus.failed.value, "updated_at": SERVER_TIMESTAMP},
                merge=True,
            )
        raise HTTPException(status_code=400, detail="Invalid payment signature")

    try:
        payment = razorpay_utils.fetch_payment(payload.razorpay_payment_id)
    except Exception as exc:
        logger.exception("Razorpay payment fetch failed")
        raise HTTPException(status_code=502, detail="Could not verify payment with Razorpay.") from exc

    expected_amount = int(order.get("amount", 0))
    paid_amount = int(payment.get("amount", 0))
    paid_currency = payment.get("currency", "INR")

    if paid_amount != expected_amount or paid_currency != order.get("currency", "INR"):
        db().collection("transactions").document(payload.razorpay_payment_id).set(
            {
                "razorpay_order_id": payload.razorpay_order_id,
                "razorpay_payment_id": payload.razorpay_payment_id,
                "razorpay_signature": payload.razorpay_signature,
                "status": "amount_mismatch",
                "amount_paid": paid_amount,
                "currency": paid_currency,
                "razorpay_payment_body": payment,
                "source": "verify_payment",
                "created_at": SERVER_TIMESTAMP,
            },
            merge=True,
        )
        if not already_paid:
            order_ref.set(
                {"status": OrderStatus.failed.value, "updated_at": SERVER_TIMESTAMP},
                merge=True,
            )
        raise HTTPException(status_code=403, detail="Payment amount or currency mismatch")

    db().collection("transactions").document(payload.razorpay_payment_id).set(
        {
            "razorpay_order_id": payload.razorpay_order_id,
            "razorpay_payment_id": payload.razorpay_payment_id,
            "razorpay_signature": payload.razorpay_signature,
            "status": payment.get("status", "captured"),
            "amount_paid": paid_amount,
            "currency": paid_currency,
            "razorpay_payment_body": payment,
            "source": "verify_payment",
            "created_at": SERVER_TIMESTAMP,
        },
        merge=True,
    )

    order_update = {
        "status": OrderStatus.paid.value,
        "amount_paid": paid_amount,
        "razorpay_payment_id": payload.razorpay_payment_id,
        "updated_at": SERVER_TIMESTAMP,
    }
    if not already_paid:
        order_update["paid_at"] = SERVER_TIMESTAMP
        order_update["paid_via"] = "verify_payment"
    order_ref.set(order_update, merge=True)

    if not already_paid and not order.get("whatsapp_confirmation_sent_at"):
        item = order.get("item") or {}
        cust = order.get("customer") or {}
        sent = whatsapp.send_order_confirmed(
            phone=cust.get("phone") or "",
            name=cust.get("full_name"),
            order_id=order.get("receipt") or payload.razorpay_order_id,
            item_text=f"{item.get('name', 'Mobile VR Box')} x {item.get('quantity', 1)}",
            amount_rupees=paid_amount // 100,
        )
        if sent:
            order_ref.set({"whatsapp_confirmation_sent_at": SERVER_TIMESTAMP}, merge=True)

    return VerifyPaymentOutput(
        status=OrderStatus.paid,
        razorpay_order_id=payload.razorpay_order_id,
        razorpay_payment_id=payload.razorpay_payment_id,
        amount_paid=paid_amount,
        currency=paid_currency,
    )


@router.get("/{razorpay_order_id}", response_model=OrderView)
def get_order(
    razorpay_order_id: str,
    decoded: Annotated[dict, Depends(get_current_user)],
) -> OrderView:
    snap = db().collection("orders").document(razorpay_order_id).get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Order not found")
    data = snap.to_dict() or {}
    _assert_order_ownership(data, decoded)
    return _doc_to_order_view(razorpay_order_id, data)


# --- Smart Collect (UPI virtual account) flow ---


def _make_reference() -> str:
    """Short customer-typeable reference, e.g. DD360-A7K2N9."""
    return f"{settings.receipt_prefix}-{secrets.token_hex(3).upper()}"


def _format_amount_display(amount_paise: int) -> str:
    if amount_paise % 100 == 0:
        return str(amount_paise // 100)
    return f"{amount_paise / 100:.2f}"


@router.post("/create_smart_collect", response_model=SmartCollectCreateOutput)
def create_smart_collect(
    payload: SmartCollectCreateInput,
    decoded: Annotated[dict, Depends(get_current_user)],
) -> SmartCollectCreateOutput:
    """Create an `awaiting_payment` order for the UPI/bank-transfer flow.

    No Razorpay API call is made here. The customer pays directly to the
    Razorpay-issued virtual account; the razorpay_webhook later matches
    the incoming payment against this order using the reference / phone /
    name strategies.
    """
    product = get_product(payload.item.sku)
    if product is None:
        raise HTTPException(status_code=404, detail=f"Unknown product sku: {payload.item.sku}")
    if payload.item.quantity > product.max_quantity:
        raise HTTPException(
            status_code=400,
            detail=f"Quantity exceeds the per-order limit of {product.max_quantity}.",
        )

    amount_paise = product.unit_price_paise * payload.item.quantity
    reference = _make_reference()

    try:
        customer_id, _ = resolve_or_create_customer(decoded)
        _update_customer_from_form(customer_id, payload)
    except gax_exceptions.PermissionDenied as exc:
        logger.exception("Firestore permission denied")
        raise HTTPException(status_code=503, detail="Firestore not enabled / missing permission") from exc
    except gax_exceptions.GoogleAPIError as exc:
        logger.exception("Firestore error")
        raise HTTPException(status_code=503, detail=f"Firestore error: {exc}") from exc

    token_phone = decoded.get("phone_number") or payload.customer.phone

    expires_at_dt = datetime.now(timezone.utc) + timedelta(
        minutes=settings.smart_collect_order_expiry_minutes
    )

    new_ref = db().collection("orders").document()
    order_doc = {
        "razorpay_order_id": None,
        "reference": reference,
        "status": OrderStatus.awaiting_payment.value,
        "amount": amount_paise,
        "amount_paid": 0,
        "currency": product.currency,
        "item": {
            "sku": product.sku,
            "name": product.name,
            "quantity": payload.item.quantity,
            "unit_price_paise": product.unit_price_paise,
            "line_total_paise": amount_paise,
        },
        "customer": {
            "full_name": payload.customer.full_name,
            "phone": token_phone,
            "email": payload.customer.email,
        },
        "customer_id": customer_id,
        "firebase_uid": decoded["uid"],
        "shipping_address": payload.shipping_address.model_dump(),
        "payment_method": "smart_collect",
        "created_at": SERVER_TIMESTAMP,
        "updated_at": SERVER_TIMESTAMP,
        "expires_at": expires_at_dt,
        "paid_at": None,
    }
    new_ref.set(order_doc)

    return SmartCollectCreateOutput(
        order_id=new_ref.id,
        reference=reference,
        amount_paise=amount_paise,
        amount_display=_format_amount_display(amount_paise),
        currency=product.currency,
        upi_id=settings.smart_collect_upi_id,
        account_number=settings.smart_collect_account_number,
        ifsc=settings.smart_collect_ifsc,
        beneficiary_name=settings.smart_collect_beneficiary,
        expires_at_iso=expires_at_dt.isoformat(),
    )


@router.get("/{order_id}/status", response_model=OrderStatusOutput)
def get_order_status(
    order_id: str,
    decoded: Annotated[dict, Depends(get_current_user)],
) -> OrderStatusOutput:
    """Lightweight polling endpoint for the pay-instructions page.

    Returns both the dynamic status (paid? still waiting?) and the static
    UPI/account info the page renders, so the client can render entirely
    from one fetch + poll.
    """
    snap = db().collection("orders").document(order_id).get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Order not found")
    data = snap.to_dict() or {}
    _assert_order_ownership(data, decoded)

    amount_paise = int(data.get("amount", 0))
    payer_data = data.get("payer") or {}
    payer = OrderPayer(
        name=payer_data.get("name"),
        vpa=payer_data.get("vpa"),
        method=payer_data.get("method"),
    ) if payer_data else None

    expires_at = data.get("expires_at")
    expires_at_iso = None
    if expires_at is not None:
        try:
            expires_at_iso = expires_at.isoformat() if hasattr(expires_at, "isoformat") else str(expires_at)
        except Exception:
            expires_at_iso = str(expires_at)

    return OrderStatusOutput(
        order_id=order_id,
        status=OrderStatus(data.get("status", "awaiting_payment")),
        amount_paise=amount_paise,
        amount_display=_format_amount_display(amount_paise),
        currency=data.get("currency", "INR"),
        reference=data.get("reference"),
        upi_id=settings.smart_collect_upi_id,
        account_number=settings.smart_collect_account_number,
        ifsc=settings.smart_collect_ifsc,
        beneficiary_name=settings.smart_collect_beneficiary,
        paid_at=str(data.get("paid_at")) if data.get("paid_at") else None,
        payer=payer,
        expires_at_iso=expires_at_iso,
    )
