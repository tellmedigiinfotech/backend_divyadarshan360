import uvicorn

from app.config import settings


if __name__ == "__main__":
    if settings.environment == "development":
        uvicorn.run(
            "app.main:app",
            host=settings.host,
            port=settings.port,
            log_level="info",
            reload=True,
        )
    else:
        uvicorn.run(
            "app.main:app",
            host=settings.host,
            port=settings.port,
            workers=2,
        )
