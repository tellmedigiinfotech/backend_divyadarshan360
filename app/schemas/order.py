from enum import Enum
from typing import Annotated

from pydantic import BaseModel, Field

from .customer import CustomerInput, ShippingAddressInput


class OrderStatus(str, Enum):
    created = "created"
    paid = "paid"
    failed = "failed"


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
