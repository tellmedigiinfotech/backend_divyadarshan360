import json
import logging
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, firestore

from .config import settings


logger = logging.getLogger(__name__)

_db = None


def _build_credentials() -> credentials.Base | None:
    """Resolve a Firebase Admin credential, in priority order:

    1. `FIREBASE_CREDENTIALS_JSON` (inline JSON string — convenient for env vars).
    2. `FIREBASE_CREDENTIALS_PATH` (path to a service-account JSON file).
    3. None → caller initializes Firebase Admin with no args, which makes
       firebase_admin use Application Default Credentials (ADC). This is the
       correct path inside GCP runtimes like Firebase Functions / Cloud Run,
       where the platform's built-in service account is already attached.
    """
    if settings.service_account_json:
        info = json.loads(settings.service_account_json)
        return credentials.Certificate(info)

    if settings.service_account_path:
        cred_path = Path(settings.service_account_path)
        if not cred_path.is_absolute():
            cred_path = (Path(__file__).resolve().parent.parent / cred_path).resolve()
        if cred_path.exists():
            return credentials.Certificate(str(cred_path))
        logger.warning(
            "SERVICE_ACCOUNT_PATH=%s does not exist; falling back to ADC",
            cred_path,
        )

    return None


def init_firebase() -> None:
    global _db
    if not firebase_admin._apps:
        cred = _build_credentials()
        if cred is not None:
            firebase_admin.initialize_app(cred)
        else:
            # ADC path — works on Firebase Functions / Cloud Run / Cloud Build /
            # any GCP runtime, and on developer machines that have run
            # `gcloud auth application-default login`.
            firebase_admin.initialize_app()
    _db = firestore.client()


def db() -> firestore.Client:
    if _db is None:
        init_firebase()
    return _db


SERVER_TIMESTAMP = firestore.SERVER_TIMESTAMP
