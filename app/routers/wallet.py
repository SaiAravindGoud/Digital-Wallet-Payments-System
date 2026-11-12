
import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.entities import (
    Wallet,
    Account,
    Transaction,
    IdempotencyRecord,
    User,
)
from app.schemas import MoneyIn
from app.security import current_user
from app.ledger import wallet_account, post_transaction
from app.notifications import send_transaction_notification


router = APIRouter(tags=["Wallet"])


def make_request_hash(data: MoneyIn) -> str:
    request_data = {
        "amount": str(data.amount),
        "source": data.source,
        "simulate_status": data.simulate_status,
    }
    raw = json.dumps(request_data, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def make_idempotency_key(user_id: int, key: str) -> str:
    raw = f"{user_id}:POST:/wallet/add-money:{key}"
    return hashlib.sha256(raw.encode()).hexdigest()


def get_saved_response(record, request_hash):
    if record.request_hash != request_hash:
        raise HTTPException(
            422,
            "Idempotency key was already used with a different request"
        )

    if record.status == "Processing":
        raise HTTPException(
            409,
            "A request with this idempotency key is still processing"
        )

    if record.status == "Completed" and record.response_json:
        return json.loads(record.response_json)

    raise HTTPException(409, "Idempotency record is not completed")


@router.get("/wallet")
def get_wallet(
    db: Session = Depends(get_db),
    user: User = Depends(current_user)
):
    w = db.query(Wallet).filter_by(owner_id=user.id).first()

    if not w:
        raise HTTPException(404, "Wallet not found")

    return {
        "wallet_number": w.wallet_number,
        "balance": str(w.balance),
        "kyc_level": w.kyc_level.value,
        "status": w.status.value
    }


@router.post("/wallet/add-money", status_code=201)
def add_money(
    data: MoneyIn,
    idempotency_key: str = Header(
        ...,
        alias="Idempotency-Key",
        min_length=8,
        max_length=100
    ),
    db: Session = Depends(get_db),
    user: User = Depends(current_user)
):
    if data.amount < Decimal("10") or data.amount > Decimal("50000"):
        raise HTTPException(
            422,
            "Amount must be between 10 and 50000"
        )

    request_hash = make_request_hash(data)
    stored_key = make_idempotency_key(user.id, idempotency_key)

    existing = db.query(IdempotencyRecord).filter_by(
        key=stored_key
    ).first()

    if existing:
        age = datetime.utcnow() - existing.created_at

        if age < timedelta(hours=24):
            if existing.user_id != user.id or existing.endpoint != "/wallet/add-money":
                raise HTTPException(409, "Idempotency key conflict")

            return get_saved_response(existing, request_hash)

        db.delete(existing)
        db.flush()

    wallet = (
        db.query(Wallet)
        .filter_by(owner_id=user.id)
        .with_for_update()
        .first()
    )

    if not wallet:
        raise HTTPException(404, "Wallet not found")

    if wallet.status.value != "Active":
        raise HTTPException(403, "Wallet is frozen")

    cap = (
        Decimal("10000")
        if wallet.kyc_level.value == "Minimum"
        else Decimal("200000")
    )

    if data.simulate_status == "Success":
        if wallet.balance + data.amount > cap:
            raise HTTPException(
                400,
                "KYC wallet balance limit exceeded"
            )

    record = IdempotencyRecord(
        key=stored_key,
        user_id=user.id,
        endpoint="/wallet/add-money",
        request_hash=request_hash,
        response_json=None,
        status="Processing"
    )

    db.add(record)

    try:
        db.flush()
    except Exception:
        db.rollback()
        existing = db.query(IdempotencyRecord).filter_by(
            key=stored_key
        ).first()

        if existing:
            return get_saved_response(existing, request_hash)

        raise HTTPException(
            409,
            "Duplicate request detected. Please retry."
        )

    try:
        if data.simulate_status == "Failed":
            tx = Transaction(
                transaction_ref="TXN-ADD-FAIL-" + stored_key[:16],
                type="Add Money",
                amount=data.amount,
                status="Failed",
                initiated_by=user.id,
                description="Simulated failed add-money"
            )

            db.add(tx)

            response = {
                "status": "Failed",
                "transaction_ref": tx.transaction_ref
            }

        else:
            bank_account = db.query(Account).filter_by(
                name="Bank Settlement"
            ).first()

            if not bank_account:
                raise HTTPException(
                    500,
                    "Bank Settlement system account is unavailable"
                )

            credit_account = wallet_account(db, wallet.id)

            tx = post_transaction(
                db,
                tx_type="Add Money",
                amount=data.amount,
                initiated_by=user.id,
                debit=bank_account,
                credit=credit_account,
                description=data.source
            )

            wallet.balance += data.amount

            response = {
                "status": "Success",
                "transaction_ref": tx.transaction_ref,
                "balance": str(wallet.balance)
            }

        record.response_json = json.dumps(response)
        record.status = "Completed"

        db.commit()

        # Send notification only after a successful Add Money commit.
        # The notification helper handles email errors without
        # affecting the completed wallet transaction.
        if data.simulate_status != "Failed":
            send_transaction_notification(
                user,
                tx,
                role="Customer"
            )

        return response

    except Exception:
        db.rollback()
        raise
