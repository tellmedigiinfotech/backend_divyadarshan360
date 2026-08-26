from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parent.parent / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: str = "development"
    host: str = "127.0.0.1"
    port: int = 8001

    # NoDecode keeps pydantic-settings from JSON-parsing the env value, so the
    # @field_validator below can split a plain `a,b,c` string. Without NoDecode,
    # pydantic-settings v2 tries json.loads() first and errors before the
    # validator runs.
    allowed_origins: Annotated[list[str], NoDecode, Field(default_factory=lambda: ["*"])]

    razorpay_key_id: str
    razorpay_key_secret: str
    razorpay_webhook_secret: str | None = None
    receipt_prefix: str = "DD360"

    # Flat handling fee added to Cash-on-Delivery orders, in paise (5000 = ₹50).
    # Keep the frontend COD_FEE (checkout-client.tsx) in sync with this value.
    cod_fee_paise: int = 5000

    # Fastrr by Shiprocket — Checkout integration.
    # Auth: X-Api-Key + X-Api-HMAC-SHA256 (Base64 HMAC-SHA256 of the request body,
    # signed with the secret). fastrr_env switches the base URL between the
    # staging (pickrr) and production (shiprocket) hosts.
    fastrr_api_key: str | None = None
    fastrr_api_secret: str | None = None
    fastrr_env: str = "production"  # "staging" | "production"
    # Shared secret we append to the order-webhook URL and check on inbound calls,
    # since Fastrr's order webhook is not HMAC-signed. Set to a long random string.
    fastrr_webhook_token: str | None = None

    # Admin allowlists. A signed-in Firebase user whose verified phone_number
    # is in ADMIN_PHONES (E.164, e.g. +919049921850) — or whose email is in
    # ADMIN_EMAILS — may access /admin. Everyone else gets a 404.
    admin_emails: Annotated[list[str], NoDecode, Field(default_factory=list)]
    admin_phones: Annotated[list[str], NoDecode, Field(default_factory=list)]

    # Gmail SMTP. Requires a Google App Password (NOT the regular login):
    # https://myaccount.google.com/apppasswords (needs 2-Step Verification on).
    smtp_server: str = "smtp.gmail.com"
    smtp_port: int = 465  # 465=SSL on connect, 587=STARTTLS
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from_name: str = "Divya Darshan 360"

    sms_striker_username: str | None = None
    sms_striker_password: str | None = None
    sms_striker_channel: str | None = None
    sms_striker_order_template_id: str | None = None
    sms_striker_url: str = "https://www.smsstriker.com/API/sms.php"

    # WhatsApp via Meta Cloud API (Graph API).
    # Setup: developers.facebook.com -> create app -> WhatsApp product ->
    # copy Phone Number ID + a permanent access token. Register an
    # `order_confirmation` template with 4 body placeholders.
    whatsapp_access_token: str | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_template_name: str = "order_confirmation"
    whatsapp_template_language: str = "en"
    whatsapp_graph_version: str = "v22.0"

    merchant_support_email: str = "connect@youtellme.ai"
    merchant_support_phone: str = "+91 90499 21850"

    # Internal inbox that receives an alert whenever a new COD order is placed.
    team_notification_email: str = "team.divyadarshan@gmail.com"

    # Razorpay Smart Collect (virtual account) payee details shown to customers
    # when they pay via direct UPI/bank transfer outside the standard checkout
    # modal. These match the assigned values from the Razorpay dashboard.
    smart_collect_upi_id: str = "rpy.payto000009179099666@icici"
    smart_collect_account_number: str = "2223006306876848"
    smart_collect_ifsc: str = "UTIB000RAZP"
    smart_collect_beneficiary: str = "TELLME DIGIINFOTECH PRIVATE LIMITED"
    smart_collect_order_expiry_minutes: int = 30

    # Firebase reserves env vars beginning with FIREBASE_/X_GOOGLE_/EXT_ in
    # Cloud Functions, so we use neutral SERVICE_ACCOUNT_* names instead.
    service_account_path: str | None = None
    service_account_json: str | None = None

    @field_validator("allowed_origins", "admin_emails", "admin_phones", mode="before")
    @classmethod
    def _split_csv(cls, value):
        if isinstance(value, str):
            return [o.strip() for o in value.split(",") if o.strip()]
        return value


settings = Settings()
