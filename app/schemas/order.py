from enum import Enum
from typing import Annotated

from pydantic import BaseModel, Field

from .customer import CustomerInput, ShippingAddressInput


class OrderStatus(str, Enum):
    awaiting_payment = "awaiting_payment"
    created = "created"
    paid = "paid"
    failed = "failed"
    expired = "expired"
    cod_pending = "cod_pending"
    refunded = "refunded"
    cancelled = "cancelled"


class CartLineInput(BaseModel):
    sku: Annotated[str, Field(min_length=1, max_length=64)]
    quantity: Annotated[int, Field(ge=1, le=10)] = 1


class CreateOrderInput(BaseModel):
    item: CartLineInput
    customer: CustomerInput
    shipping_address: ShippingAddressInput


class CreateOrderOutput(BaseModel):
    razorpay_order_id: str
    razorpay_key_id: str
    amount: int
    currency: str
    receipt: str
    customer_id: str
    product_name: str


class CreateCodOrderInput(BaseModel):
    item: CartLineInput
    customer: CustomerInput
    shipping_address: ShippingAddressInput


class CreateCodOrderOutput(BaseModel):
    order_id: str
    receipt: str
    amount: int
    currency: str
    product_name: str


class VerifyPaymentInput(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class VerifyPaymentOutput(BaseModel):
    status: OrderStatus
    razorpay_order_id: str
    razorpay_payment_id: str
    amount_paid: int
    currency: str


class OrderItemView(BaseModel):
    sku: str
    name: str
    quantity: int
    unit_price_paise: int
    line_total_paise: int


class OrderCustomerView(BaseModel):
    full_name: str
    phone: str
    email: str | None = None


class OrderView(BaseModel):
    razorpay_order_id: str
    receipt: str
    status: OrderStatus
    amount: int
    amount_paid: int
    currency: str
    item: OrderItemView
    customer: OrderCustomerView
    shipping_address: ShippingAddressInput
    created_at: str | None = None
    paid_at: str | None = None
    # Sent so the /account "Continue payment" button can re-open the Razorpay
    # modal for a pending order without making the backend roundtrip again.
    razorpay_key_id: str | None = None
    fulfillment_status: str | None = None
    tracking_number: str | None = None
    courier: str | None = None
    shipped_at: str | None = None
    delivered_at: str | None = None
    admin_notes: str | None = None
    payment_method: str | None = None
    razorpay_payment_id: str | None = None
    refund_id: str | None = None
    refund_amount: int | None = None
    refund_status: str | None = None
    refunded_at: str | None = None
    refund_reason: str | None = None
    cancelled_at: str | None = None
    cancellation_reason: str | None = None


# --- Smart Collect (UPI virtual account) flow ---


class SmartCollectCreateInput(BaseModel):
    item: CartLineInput
    customer: CustomerInput
    shipping_address: ShippingAddressInput


class SmartCollectCreateOutput(BaseModel):
    order_id: str
    reference: str
    amount_paise: int
    amount_display: str
    currency: str
    upi_id: str
    account_number: str
    ifsc: str
    beneficiary_name: str
    expires_at_iso: str | None = None


class OrderPayer(BaseModel):
    name: str | None = None
    vpa: str | None = None
    method: str | None = None


# --- Admin schemas ---


class AdminOrderListItem(BaseModel):
    razorpay_order_id: str
    receipt: str
    status: str
    fulfillment_status: str | None = None
    payment_method: str | None = None
    amount: int
    amount_paid: int
    currency: str
    product_name: str
    quantity: int
    customer_name: str
    customer_phone: str
    customer_email: str | None = None
    city: str | None = None
    pincode: str | None = None
    tracking_number: str | None = None
    courier: str | None = None
    created_at: str | None = None
    paid_at: str | None = None
    shipped_at: str | None = None


class AdminShipOrderInput(BaseModel):
    tracking_number: Annotated[str, Field(min_length=2, max_length=80)]
    courier: Annotated[str, Field(min_length=2, max_length=80)]
    notes: Annotated[str | None, Field(default=None, max_length=500)] = None


class AdminMarkDeliveredInput(BaseModel):
    notes: Annotated[str | None, Field(default=None, max_length=500)] = None


class AdminRefundOrderInput(BaseModel):
    # amount_paise None = full refund. Allow partial down to Re 1 (100 paise).
    amount_paise: Annotated[int | None, Field(default=None, ge=100)] = None
    reason: Annotated[str, Field(min_length=2, max_length=500)]
    instant: bool = False


class AdminCancelOrderInput(BaseModel):
    reason: Annotated[str, Field(min_length=2, max_length=500)]


class CustomerCancelOrderInput(BaseModel):
    # Optional — customers aren't required to justify cancelling their own order.
    reason: Annotated[str | None, Field(default=None, max_length=500)] = None


class OrderStatusOutput(BaseModel):
    order_id: str
    status: OrderStatus
    amount_paise: int
    amount_display: str
    currency: str
    reference: str | None = None
    upi_id: str | None = None
    account_number: str | None = None
    ifsc: str | None = None
    beneficiary_name: str | None = None
    paid_at: str | None = None
    payer: OrderPayer | None = None
    expires_at_iso: str | None = None
