import logging
import secrets
import time

from fastapi import APIRouter, HTTPException
from google.api_core import exceptions as gax_exceptions
from google.cloud.firestore_v1.base_query import FieldFilter

from .. import razorpay_utils
from ..config import settings
from ..firebase import SERVER_TIMESTAMP, db
from ..products import get_product
from ..schemas.order import (
    CreateOrderInput,
    CreateOrderOutput,
    OrderCustomerView,
    OrderItemView,
    OrderStatus,
    OrderView,
    VerifyPaymentInput,
    VerifyPaymentOutput,
)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orders", tags=["Orders"])


def _make_receipt() -> str:
    ts = int(time.time())
    return f"{settings.receipt_prefix}-{ts}-{secrets.token_hex(3).upper()}"


def _upsert_customer(payload: CreateOrderInput) -> str:
    customers = db().collection("customers")
    phone = payload.customer.phone

    existing = (
        customers.where(filter=FieldFilter("phone", "==", phone)).limit(1).get()
    )

    if existing:
        doc = existing[0]
        doc.reference.set(
            {
                "full_name": payload.customer.full_name,
                "email": payload.customer.email,
                "last_shipping_address": payload.shipping_address.model_dump(),
                "updated_at": SERVER_TIMESTAMP,
            },
            merge=True,
        )
        return doc.id

    new_ref = customers.document()
    new_ref.set(
        {
            "full_name": payload.customer.full_name,
            "phone": phone,
            "email": payload.customer.email,
            "last_shipping_address": payload.shipping_address.model_dump(),
            "created_at": SERVER_TIMESTAMP,
            "updated_at": SERVER_TIMESTAMP,
        }
    )
    return new_ref.id


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
        created_at=str(data.get("created_at")) if data.get("created_at") else None,
        paid_at=str(data.get("paid_at")) if data.get("paid_at") else None,
    )


@router.post("/create_order", response_model=CreateOrderOutput)
def create_order(payload: CreateOrderInput) -> CreateOrderOutput:
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
        customer_id = _upsert_customer(payload)
    except gax_exceptions.PermissionDenied as exc:
        logger.exception("Firestore permission denied")
        raise HTTPException(
            status_code=503,
            detail="Firestore is not enabled or the service account lacks permission. "
                   "Enable it at https://console.cloud.google.com/apis/api/firestore.googleapis.com",
        ) from exc
    except gax_exceptions.GoogleAPIError as exc:
        logger.exception("Firestore error")
        raise HTTPException(status_code=503, detail=f"Firestore error: {exc}") from exc

    try:
        rp_order = razorpay_utils.create_order(
            amount_paise=amount_paise,
            currency=product.currency,
            receipt=receipt,
            notes={
                "sku": product.sku,
                "quantity": str(payload.item.quantity),
                "customer_id": customer_id,
                "customer_phone": payload.customer.phone,
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
        "customer": payload.customer.model_dump(),
        "customer_id": customer_id,
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


@router.post("/verify_payment", response_model=VerifyPaymentOutput)
def verify_payment(payload: VerifyPaymentInput) -> VerifyPaymentOutput:
    order_ref = db().collection("orders").document(payload.razorpay_order_id)
    order_snap = order_ref.get()
    if not order_snap.exists:
        raise HTTPException(status_code=404, detail="Order not found")

    order = order_snap.to_dict()

    is_valid = razorpay_utils.verify_signature(
        payload.razorpay_order_id, payload.razorpay_payment_id, payload.razorpay_signature
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
                "created_at": SERVER_TIMESTAMP,
            }
        )
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
        order_ref.set(
            {"status": OrderStatus.failed.value, "updated_at": SERVER_TIMESTAMP},
            merge=True,
        )
        db().collection("transactions").document(payload.razorpay_payment_id).set(
            {
                "razorpay_order_id": payload.razorpay_order_id,
                "razorpay_payment_id": payload.razorpay_payment_id,
                "razorpay_signature": payload.razorpay_signature,
                "status": "amount_mismatch",
                "amount_paid": paid_amount,
                "currency": paid_currency,
                "razorpay_payment_body": payment,
                "created_at": SERVER_TIMESTAMP,
            }
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
            "created_at": SERVER_TIMESTAMP,
        }
    )

    order_ref.set(
        {
            "status": OrderStatus.paid.value,
            "amount_paid": paid_amount,
            "razorpay_payment_id": payload.razorpay_payment_id,
            "paid_at": SERVER_TIMESTAMP,
            "updated_at": SERVER_TIMESTAMP,
        },
        merge=True,
    )

    return VerifyPaymentOutput(
        status=OrderStatus.paid,
        razorpay_order_id=payload.razorpay_order_id,
        razorpay_payment_id=payload.razorpay_payment_id,
        amount_paid=paid_amount,
        currency=paid_currency,
    )


@router.get("/{razorpay_order_id}", response_model=OrderView)
def get_order(razorpay_order_id: str) -> OrderView:
    snap = db().collection("orders").document(razorpay_order_id).get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Order not found")
    return _doc_to_order_view(razorpay_order_id, snap.to_dict())
