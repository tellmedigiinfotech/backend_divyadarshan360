"""Firebase Functions entry point.

Wraps the FastAPI ASGI app (from app/main.py) as a 2nd-gen HTTPS callable
function deployed to Cloud Run in asia-south1.

For local development, run `python run_local.py` instead — that boots uvicorn
directly with hot-reload.
"""

from a2wsgi import ASGIMiddleware
from firebase_functions import https_fn, options
from werkzeug.wrappers import Response as WerkzeugResponse

from app.main import app as fastapi_app


# Firebase's deploy analyzer imports this module on the local venv to discover
# function decorators. If we eagerly init Firebase here, ADC fails locally and
# the analyzer aborts. firebase.py already inits lazily on first db() call, so
# leaving it out at module scope is safe on both the analyzer and the live
# function runtime.

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
