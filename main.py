"""Firebase Functions entry point.

Wraps the FastAPI ASGI app (from app/main.py) as a 2nd-gen HTTPS callable
function deployed to Cloud Run in asia-south1.

For local development, run `python run_local.py` instead — that boots uvicorn
directly with hot-reload.
"""

from a2wsgi import ASGIMiddleware
from firebase_functions import https_fn, options
from werkzeug.wrappers import Response as WerkzeugResponse

from app.firebase import init_firebase
from app.main import app as fastapi_app


# Firebase Functions doesn't run FastAPI's lifespan hooks, so initialize the
# Firestore client eagerly at module import time.
init_firebase()

# Convert ASGI -> WSGI so Firebase Functions can drive the request.
_wsgi_app = ASGIMiddleware(fastapi_app)


@https_fn.on_request(
    region="asia-south1",
    memory=options.MemoryOption.MB_512,
    timeout_sec=300,
    min_instances=0,
    max_instances=10,
    cpu=1,
)
def api(req: https_fn.Request) -> WerkzeugResponse:
    """All HTTP traffic for the backend flows through this function.

    Path examples after deploy (replace <hash>/<region> as Firebase shows):
      https://api-<hash>-asia-south1.run.app/health
      https://api-<hash>-asia-south1.run.app/orders/create_order
      https://api-<hash>-asia-south1.run.app/razorpay/webhook
    """
    return WerkzeugResponse.from_app(_wsgi_app, req.environ)
