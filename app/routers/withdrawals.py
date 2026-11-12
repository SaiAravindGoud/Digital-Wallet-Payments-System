
import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.database import get_db
from app.models.entities import (
    User,
    Wallet,
    BankAccount,
    Withdrawal,
    Transaction,
    IdempotencyRecord,
    Account,
)
from app.schemas import WithdrawalIn
from app.security import current_user, verify_transaction_pin
from app.ledger import wallet_account, post_transaction
from app.daily_limits import check_daily_outgoing_limit
from app.notifications import send_transaction_notification


router = APIRouter(
    prefix="/withdrawals",
    tags=["Withdrawals"],
)


def make_request_hash(data: WithdrawalIn) -> str:
    request_data = {
        "bank_account_id": data.bank_account_id,
        "amount": str(data.amount),
    }
    raw = json.dumps(request_data, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def make_idempotency_key(user_id: int, key: str) -> str:
    raw = f"{user_id}:POST:/withdrawals:{key}"
    return hashlib.sha256(raw.encode()).hexdigest()


def get_saved_response(db, record, request_hash):
    if record.request_hash != request_hash:
        raise HTTPException(
            422,
            "Idempotency key was already used with a different request",
        )

    if record.status == "Processing":
        raise HTTPException(
            409,
            "A request with this idempotency key is still processing",
        )

    if record.status == "Completed" and record.response_json:
        return json.loads(record.response_json)

    raise HTTPException(409, "Idempotency record is not completed")


@router.post("", status_code=201)
def request_withdrawal(
    data: WithdrawalIn,
    idempotency_key: str = Header(
        ...,
        alias="Idempotency-Key",
        min_length=8,
        max_length=100,
    ),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    request_hash = make_request_hash(data)
    stored_key = make_idempotency_key(user.id, idempotency_key)

    existing = (
        db.query(IdempotencyRecord)
        .filter_by(key=stored_key)
        .first()
    )

    if existing:
        age = datetime.utcnow() - existing.created_at

        if age < timedelta(hours=24):
            if (
                existing.user_id != user.id
                or existing.endpoint != "/withdrawals"
            ):
                raise HTTPException(409, "Idempotency key conflict")

            return get_saved_response(db, existing, request_hash)

        db.delete(existing)
        db.flush()

    if user.role.value not in ("User", "Merchant"):
        raise HTTPException(403, "This account cannot request withdrawals")

    verify_transaction_pin(db, user, data.pin)

    wallet = (
        db.query(Wallet)
        .filter(Wallet.owner_id == user.id)
        .with_for_update()
        .first()
    )

    if not wallet:
        raise HTTPException(404, "Wallet not found")

    if wallet.status.value != "Active":
        raise HTTPException(403, "Wallet is frozen")

    if wallet.kyc_level.value != "Full":
        raise HTTPException(
            403,
            "Full KYC verification is required for withdrawals",
        )

    bank_account = (
        db.query(BankAccount)
        .filter(
            BankAccount.id == data.bank_account_id,
            BankAccount.user_id == user.id,
        )
        .first()
    )

    if not bank_account:
        raise HTTPException(404, "Bank account not found")

    if wallet.balance < data.amount:
        raise HTTPException(400, "Insufficient wallet balance")

    check_daily_outgoing_limit(
        db,
        user.id,
        data.amount,
    )

    record = IdempotencyRecord(
        key=stored_key,
        user_id=user.id,
        endpoint="/withdrawals",
        request_hash=request_hash,
        response_json=None,
        status="Processing",
    )

    db.add(record)

    try:
        db.flush()
    except IntegrityError:
        db.rollback()

        existing = (
            db.query(IdempotencyRecord)
            .filter_by(key=stored_key)
            .first()
        )

        if existing:
            return get_saved_response(db, existing, request_hash)

        raise HTTPException(
            409,
            "Duplicate withdrawal request detected. Please retry.",
        )

    try:
        clearing_account = (
            db.query(Account)
            .filter_by(name="Withdrawal Clearing")
            .first()
        )

        if not clearing_account:
            raise HTTPException(
                500,
                "Withdrawal Clearing system account not found",
            )

        user_account = wallet_account(db, wallet.id)

        transaction = post_transaction(
            db,
            tx_type="Withdrawal",
            amount=data.amount,
            initiated_by=user.id,
            debit=user_account,
            credit=clearing_account,
            description=(
                f"Withdrawal requested to bank account "
                f"ending {bank_account.account_number[-4:]}"
            ),
            status="Pending",
        )

        wallet.balance -= data.amount

        withdrawal = Withdrawal(
            transaction_id=transaction.id,
            bank_account_id=bank_account.id,
            amount=data.amount,
            status="Pending",
        )

        db.add(withdrawal)
        db.flush()

        response = {
            "withdrawal_id": withdrawal.id,
            "transaction_ref": transaction.transaction_ref,
            "amount": str(data.amount),
            "status": "Pending",
            "message": "Withdrawal requested successfully",
            "processing_note": "Withdrawal will be processed by the scheduler",
        }

        record.response_json = json.dumps(response)
        record.status = "Completed"

        db.commit()
        db.refresh(transaction)

        # Notify the customer only after the withdrawal is committed.
        # The notification reports the current Pending status.
        send_transaction_notification(
            user,
            transaction,
            role="Customer",
        )

        return response

    except Exception:
        db.rollback()
        raise


@router.get("")
def get_withdrawals(
    skip: int = 0,
    limit: int = 20,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    if skip < 0 or limit < 1 or limit > 100:
        raise HTTPException(
            422,
            "Invalid pagination values",
        )

    query = (
        db.query(Withdrawal, Transaction)
        .join(
            Transaction,
            Withdrawal.transaction_id == Transaction.id,
        )
        .filter(Transaction.initiated_by == user.id)
        .order_by(Withdrawal.id.desc())
    )

    total = query.count()

    results = query.offset(skip).limit(limit).all()

    return {
        "total": total,
        "skip": skip,
        "limit": limit,
        "items": [
            {
                "withdrawal_id": withdrawal.id,
                "transaction_ref": transaction.transaction_ref,
                "amount": str(withdrawal.amount),
                "status": withdrawal.status,
                "bank_account_id": withdrawal.bank_account_id,
                "created_at": withdrawal.created_at,
                "processed_at": withdrawal.processed_at,
                "failure_reason": withdrawal.failure_reason,
            }
            for withdrawal, transaction in results
        ],
    }
