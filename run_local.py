"""Local development runner.

Boots uvicorn directly against app.main:app for fast iteration with --reload.
This file is NOT deployed to Firebase Functions (see firebase.json `ignore`).

In production, main.py is the Firebase Functions entrypoint; this file is dev-only.
"""

import os

import uvicorn

from app.config import settings


if __name__ == "__main__":
    is_dev = settings.environment == "development"
    host = settings.host if is_dev else "0.0.0.0"
    port = int(os.environ.get("PORT", settings.port))

    if is_dev:
        uvicorn.run(
            "app.main:app",
            host=host,
            port=port,
            log_level="info",
            reload=True,
        )
    else:
        uvicorn.run(
            "app.main:app",
            host=host,
            port=port,
            workers=int(os.environ.get("WEB_CONCURRENCY", "2")),
        )
