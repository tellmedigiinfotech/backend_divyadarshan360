"""Firebase Functions entry point.

Wraps the FastAPI ASGI app (from app/main.py) as a 2nd-gen HTTPS callable
function deployed to Cloud Run in asia-south1.

The wrapper uses Starlette/FastAPI's TestClient, which has a battle-tested
sync->async bridge built on httpx. Earlier versions used a2wsgi which
deadlocks under Firebase Functions Python's gunicorn-backed runtime
(requests time out at function timeout_sec).

For local development, run `python run_local.py` instead.
"""

import logging
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from firebase_functions import https_fn, options, scheduler_fn
from google.cloud.firestore_v1.base_query import FieldFilter

from app.firebase import SERVER_TIMESTAMP, db, init_firebase
from app.main import app as fastapi_app


logger = logging.getLogger(__name__)

# TestClient construction triggers ASGI lifespan startup (which runs
# init_firebase via FastAPI's lifespan handler). Instantiating once at module
# scope is the right move — Cloud Run reuses the warm container across
# requests, and we don't want to pay startup cost per call.
_client = TestClient(fastapi_app, raise_server_exceptions=False)


@https_fn.on_request(
    region="asia-south1",
    memory=options.MemoryOption.MB_512,
    timeout_sec=60,
    min_instances=0,
    max_instances=10,
    cpu=1,
)
def api(req: https_fn.Request) -> https_fn.Response:
    """All HTTP traffic for the backend flows through this function.

    https://asia-south1-<project>.cloudfunctions.net/api/health
    https://asia-south1-<project>.cloudfunctions.net/api/orders/create_order
    https://asia-south1-<project>.cloudfunctions.net/api/fastrr/webhook/order
    """
    # Pass body bytes verbatim; FastAPI parses JSON itself.
    body = req.get_data(cache=False, as_text=False)

    # Strip hop-by-hop headers + ones TestClient sets itself.
    incoming_headers = {
        k: v
        for k, v in req.headers.items()
        if k.lower() not in {"host", "content-length", "transfer-encoding"}
    }

    res = _client.request(
        method=req.method,
        url=req.path or "/",
        params=req.args.to_dict(flat=False) if req.args else None,
        headers=incoming_headers,
        content=body if body else None,
        follow_redirects=False,
    )

    response_headers = {
        k: v
        for k, v in res.headers.items()
        if k.lower() not in {"transfer-encoding", "content-encoding", "content-length"}
    }

    return https_fn.Response(
        res.content,
        status=res.status_code,
        headers=response_headers,
        content_type=res.headers.get("content-type", "application/octet-stream"),
    )


# --- Scheduled maintenance: expire stale pending orders ---


@scheduler_fn.on_schedule(
    schedule="every 24 hours",
    region="asia-south1",
    memory=options.MemoryOption.MB_256,
    timeout_sec=120,
)
def expire_stale_orders(_event: scheduler_fn.ScheduledEvent) -> None:
    """Daily job that:

    1. Marks orders in `created` or `awaiting_payment` status that are
       older than 24 hours as `expired` (so the /account "Continue payment"
       button can flip to a final "Order expired" state).
    2. Deletes orders that have been `expired` for more than 7 days
       (keeps the data set small without losing customer history of
       successful purchases).
    """
    init_firebase()
    fs = db()

    now = datetime.now(timezone.utc)
    expire_cutoff = now - timedelta(hours=24)
    delete_cutoff = now - timedelta(days=7)

    expired_count = 0
    for status_value in ("created", "awaiting_payment"):
        snaps = (
            fs.collection("orders")
            .where(filter=FieldFilter("status", "==", status_value))
            .where(filter=FieldFilter("created_at", "<", expire_cutoff))
            .stream()
        )
        for doc in snaps:
            doc.reference.set(
                {"status": "expired", "updated_at": SERVER_TIMESTAMP},
                merge=True,
            )
            expired_count += 1

    deleted_count = 0
    old_expired = (
        fs.collection("orders")
        .where(filter=FieldFilter("status", "==", "expired"))
        .where(filter=FieldFilter("updated_at", "<", delete_cutoff))
        .stream()
    )
    for doc in old_expired:
        doc.reference.delete()
        deleted_count += 1

    logger.info(
        "expire_stale_orders: expired=%d, deleted=%d", expired_count, deleted_count
    )


# --- Scheduled reconciliation: pull any Fastrr orders the webhook/success-page missed ---


@scheduler_fn.on_schedule(
    schedule="every 30 minutes",
    region="asia-south1",
    memory=options.MemoryOption.MB_256,
    timeout_sec=120,
)
def reconcile_fastrr_orders(_event: scheduler_fn.ScheduledEvent) -> None:
    """Walk Fastrr's recent order list and record any orders we're missing.

    Fastrr's push webhook has never fired for us, and the success-page pull only
    catches customers who reach /success. This scheduled sweep is the safety net
    that guarantees every placed (SUCCESS) order lands in Firestore + triggers the
    COD team alert. Idempotent, so re-running is harmless.
    """
    init_firebase()
    from app.routers.fastrr import reconcile_recent_orders

    summary = reconcile_recent_orders(days=2)
    logger.info("reconcile_fastrr_orders: %s", summary)
