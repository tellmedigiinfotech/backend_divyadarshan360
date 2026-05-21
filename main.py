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

from fastapi.testclient import TestClient
from firebase_functions import https_fn, options

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
    https://asia-south1-<project>.cloudfunctions.net/api/razorpay/webhook
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
