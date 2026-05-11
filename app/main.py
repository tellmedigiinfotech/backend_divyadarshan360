from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .firebase import init_firebase
from .routers import auth, customers, orders


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_firebase()
    yield


app = FastAPI(
    title="Divya Darshan 360 Backend",
    description="Orders, payments, and customer records for divyadarshan360.com.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(orders.router)
app.include_router(customers.router)


@app.get("/health", tags=["Meta"])
def health() -> dict:
    return {"status": "ok", "environment": settings.environment}


@app.get("/", tags=["Meta"])
def root() -> dict:
    return {"service": "divyadarshan360-backend", "version": app.version}
