import json
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, firestore

from .config import settings


_db = None


def _build_credentials() -> credentials.Base:
    if settings.firebase_credentials_json:
        info = json.loads(settings.firebase_credentials_json)
        return credentials.Certificate(info)

    if not settings.firebase_credentials_path:
        raise RuntimeError(
            "Firebase credentials not configured. Set FIREBASE_CREDENTIALS_PATH "
            "or FIREBASE_CREDENTIALS_JSON in the environment."
        )

    cred_path = Path(settings.firebase_credentials_path)
    if not cred_path.is_absolute():
        cred_path = (Path(__file__).resolve().parent.parent / cred_path).resolve()

    if not cred_path.exists():
        raise RuntimeError(f"Firebase service account file not found at {cred_path}")

    return credentials.Certificate(str(cred_path))


def init_firebase() -> None:
    global _db
    if not firebase_admin._apps:
        firebase_admin.initialize_app(_build_credentials())
    _db = firestore.client()


def db() -> firestore.Client:
    if _db is None:
        init_firebase()
    return _db


SERVER_TIMESTAMP = firestore.SERVER_TIMESTAMP
