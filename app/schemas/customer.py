from typing import Annotated

from pydantic import BaseModel, EmailStr, Field, field_validator


PhoneStr = Annotated[str, Field(min_length=10, max_length=15)]


class CustomerInput(BaseModel):
    full_name: Annotated[str, Field(min_length=2, max_length=100)]
    phone: PhoneStr
    email: EmailStr | None = None

    @field_validator("phone")
    @classmethod
    def _normalize_phone(cls, v: str) -> str:
        digits = "".join(ch for ch in v if ch.isdigit())
        if len(digits) == 10:
            return f"+91{digits}"
        if len(digits) == 12 and digits.startswith("91"):
            return f"+{digits}"
        if v.startswith("+") and 11 <= len(digits) <= 15:
            return f"+{digits}"
        raise ValueError("Enter a valid 10-digit Indian mobile number or E.164 format")


class ShippingAddressInput(BaseModel):
    line1: Annotated[str, Field(min_length=4, max_length=300)]
    city: Annotated[str, Field(min_length=2, max_length=80)]
    state: Annotated[str, Field(min_length=2, max_length=80)]
    pincode: Annotated[str, Field(pattern=r"^\d{6}$")]
    country: str = "IN"
    notes: Annotated[str, Field(default="", max_length=500)]


class ContactFormInput(BaseModel):
    full_name: Annotated[str, Field(min_length=2, max_length=100)]
    email: EmailStr
    phone: PhoneStr | None = None
    subject: Annotated[str, Field(min_length=2, max_length=200)]
    message: Annotated[str, Field(min_length=2, max_length=4000)]

    @field_validator("phone")
    @classmethod
    def _normalize_phone(cls, v):
        if v is None:
            return v
        return CustomerInput._normalize_phone(v)


class ContactFormOutput(BaseModel):
    id: str
    message: str = "Thank you. We will get back to you shortly."


class CustomerUpdateInput(BaseModel):
    full_name: Annotated[str | None, Field(default=None, min_length=2, max_length=100)] = None
    email: EmailStr | None = None
    last_shipping_address: ShippingAddressInput | None = None


class CustomerOrderListItem(BaseModel):
    razorpay_order_id: str
    receipt: str
    status: str
    amount: int
    amount_paid: int
    currency: str
    product_name: str
    quantity: int
    created_at: str | None = None
    paid_at: str | None = None
