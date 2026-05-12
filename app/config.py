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

    smtp_server: str | None = None
    smtp_port: int = 465
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from_name: str = "Divya Darshan 360"

    sms_striker_username: str | None = None
    sms_striker_password: str | None = None
    sms_striker_channel: str | None = None
    sms_striker_order_template_id: str | None = None
    sms_striker_url: str = "https://www.smsstriker.com/API/sms.php"

    merchant_support_email: str = "connect@youtellme.ai"
    merchant_support_phone: str = "+91 90499 21850"

    # Firebase reserves env vars beginning with FIREBASE_/X_GOOGLE_/EXT_ in
    # Cloud Functions, so we use neutral SERVICE_ACCOUNT_* names instead.
    service_account_path: str | None = None
    service_account_json: str | None = None

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value):
        if isinstance(value, str):
            return [o.strip() for o in value.split(",") if o.strip()]
        return value


settings = Settings()
