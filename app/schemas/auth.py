from pydantic import BaseModel, EmailStr


class CurrentUser(BaseModel):
    uid: str
    phone_number: str | None = None
    email: EmailStr | None = None
    name: str | None = None
    picture: str | None = None
    sign_in_provider: str | None = None
    email_verified: bool = False


class CustomerProfile(BaseModel):
    customer_id: str
    full_name: str
    phone: str
    email: EmailStr | None = None
    firebase_uid: str | None = None
    last_shipping_address: dict | None = None


class MeResponse(BaseModel):
    user: CurrentUser
    customer: CustomerProfile
