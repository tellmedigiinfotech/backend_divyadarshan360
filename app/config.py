from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    allowed_origins: Annotated[list[str], Field(default_factory=lambda: ["*"])]

    razorpay_key_id: str
    razorpay_key_secret: str
    razorpay_webhook_secret: str | None = None
    receipt_prefix: str = "DD360"

    firebase_credentials_path: str | None = None
    firebase_credentials_json: str | None = None

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value):
        if isinstance(value, str):
            return [o.strip() for o in value.split(",") if o.strip()]
        return value


settings = Settings()
