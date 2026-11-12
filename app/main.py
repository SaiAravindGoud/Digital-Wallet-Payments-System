
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import settings
from app.database import SessionLocal
from app.ledger import ensure_system_accounts
from app.jobs.scheduler import start_scheduler, stop_scheduler

from app.routers import (
    auth,
    wallet,
    transfers,
    admin,
    kyc,
    transactions,
    bank_accounts,
    money_requests,
    cashbacks,
    merchant_payments,
    withdrawals,
    analytics,
    reports,
)


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = SessionLocal()

    try:
        ensure_system_accounts(db)
    finally:
        db.close()

    start_scheduler()

    yield

    stop_scheduler()


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
):
    errors = []

    for error in exc.errors():
        errors.append({
            "field": ".".join(str(part) for part in error["loc"]),
            "message": error["msg"],
            "type": error["type"],
        })

    return JSONResponse(
        status_code=422,
        content={
            "success": False,
            "message": "Request validation failed",
            "errors": errors,
        },
    )


@app.exception_handler(Exception)
async def global_exception_handler(
    request: Request,
    exc: Exception,
):
    logger.exception(
        "Unhandled server error on %s %s",
        request.method,
        request.url.path,
    )

    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "message": "Internal server error",
        },
    )


app.include_router(auth.router)
app.include_router(wallet.router)
app.include_router(transfers.router)
app.include_router(admin.router)
app.include_router(kyc.router)
app.include_router(transactions.router)
app.include_router(bank_accounts.router)
app.include_router(money_requests.router)
app.include_router(cashbacks.router)
app.include_router(merchant_payments.router)
app.include_router(withdrawals.router)
app.include_router(analytics.router)
app.include_router(reports.router)

@app.get("/")
def root():
    return {
        "message": "Digital Wallet API is running",
        "docs": "/docs",
    }


@app.get("/health")
def health():
    return {"status": "healthy"}
