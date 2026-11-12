
import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.database import get_db
from app.models.entities import (
    User,
    Wallet,
    Transaction,
    IdempotencyRecord,
    Cashback,
)
from app.schemas import TransferIn
from app.security import current_user, verify_transaction_pin
from app.ledger import wallet_account, post_transaction
from app.daily_limits import check_daily_outgoing_limit
from app.notifications import send_transaction_notification


router = APIRouter(prefix="/transfers", tags=["Transfers"])


CASHBACK_RATE = Decimal("0.02")
MAX_CASHBACK = Decimal("50.00")


def calculate_cashback(amount: Decimal) -> Decimal:
    cashback = (amount * CASHBACK_RATE).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    return min(cashback, MAX_CASHBACK)


def make_request_hash(data: TransferIn) -> str:
    request_data = {
        "receiver_phone": data.receiver_phone,
        "amount": str(data.amount),
        "note": data.note,
    }
    raw = json.dumps(request_data, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def make_idempotency_key(user_id: int, key: str) -> str:
    raw = f"{user_id}:POST:/transfers:{key}"
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
def transfer(
    data: TransferIn,
    idempotency_key: str = Header(
        ...,
        alias="Idempotency-Key",
        min_length=8,
        max_length=100,
    ),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    if data.amount < Decimal("1") or data.amount > Decimal("200000"):
        raise HTTPException(
            422,
            "Transfer amount must be between 1 and 200000",
        )

    request_hash = make_request_hash(data)
    stored_key = make_idempotency_key(user.id, idempotency_key)

    existing = db.query(IdempotencyRecord).filter_by(
        key=stored_key
    ).first()

    if existing:
        age = datetime.utcnow() - existing.created_at

        if age < timedelta(hours=24):
            if (
                existing.user_id != user.id
                or existing.endpoint != "/transfers"
            ):
                raise HTTPException(409, "Idempotency key conflict")

            return get_saved_response(db, existing, request_hash)

        db.delete(existing)
        db.flush()

    verify_transaction_pin(db, user, data.pin)

    receiver = db.query(User).filter_by(
        phone=data.receiver_phone
    ).first()

    if not receiver:
        raise HTTPException(404, "Receiver not found")

    if receiver.id == user.id:
        raise HTTPException(400, "Cannot send money to yourself")

    wallets = (
        db.query(Wallet)
        .filter(Wallet.owner_id.in_([user.id, receiver.id]))
        .order_by(Wallet.id)
        .with_for_update()
        .all()
    )

    by_owner = {
        wallet.owner_id: wallet
        for wallet in wallets
    }

    sender_w = by_owner.get(user.id)
    receiver_w = by_owner.get(receiver.id)

    if not sender_w or not receiver_w:
        raise HTTPException(404, "Wallet not found")

    if sender_w.status.value != "Active":
        raise HTTPException(403, "Sender wallet is frozen")

    if receiver_w.status.value != "Active":
        raise HTTPException(400, "Receiver wallet is not active")

    if sender_w.balance < data.amount:
        raise HTTPException(400, "Insufficient balance")

    sender_cap = (
        Decimal("10000")
        if sender_w.kyc_level.value == "Minimum"
        else Decimal("200000")
    )

    receiver_cap = (
        Decimal("10000")
        if receiver_w.kyc_level.value == "Minimum"
        else Decimal("200000")
    )

    if sender_w.balance > sender_cap:
        raise HTTPException(
            400,
            "Sender wallet KYC balance limit exceeded",
        )

    if receiver_w.balance + data.amount > receiver_cap:
        raise HTTPException(
            400,
            "Receiver KYC balance limit exceeded",
        )

    # Enforce the shared calendar-day outgoing limit.
    check_daily_outgoing_limit(
        db,
        user.id,
        data.amount,
    )

    record = IdempotencyRecord(
        key=stored_key,
        user_id=user.id,
        endpoint="/transfers",
        request_hash=request_hash,
        response_json=None,
        status="Processing",
    )

    db.add(record)

    try:
        db.flush()
    except IntegrityError:
        db.rollback()

        existing = db.query(IdempotencyRecord).filter_by(
            key=stored_key
        ).first()

        if existing:
            return get_saved_response(db, existing, request_hash)

        raise HTTPException(
            409,
            "Duplicate request detected. Please retry.",
        )

    try:
        sender_acc = wallet_account(db, sender_w.id)
        receiver_acc = wallet_account(db, receiver_w.id)

        tx = post_transaction(
            db,
            tx_type="P2P Transfer",
            amount=data.amount,
            initiated_by=user.id,
            debit=sender_acc,
            credit=receiver_acc,
            description=data.note,
        )

        sender_w.balance -= data.amount
        receiver_w.balance += data.amount

        # Create pending cashback for the sender.
        cashback_amount = calculate_cashback(data.amount)

        cashback = Cashback(
            transaction_id=tx.id,
            user_id=user.id,
            amount=cashback_amount,
            status="Pending",
        )

        db.add(cashback)

        response = {
            "transaction_ref": tx.transaction_ref,
            "status": "Success",
            "amount": str(data.amount),
            "sender_balance": str(sender_w.balance),
            "cashback": {
                "amount": str(cashback_amount),
                "status": "Pending",
                "release_after_hours": 24,
            },
        }

        record.response_json = json.dumps(response)
        record.status = "Completed"

        # Commit the financial transaction first.
        db.commit()

        # Refresh transaction data after committing.
        db.refresh(tx)

        # Notify sender and receiver.
        # Email failures are handled inside the notification helper.
        send_transaction_notification(
            user,
            tx,
            role="Sender",
        )

        send_transaction_notification(
            receiver,
            tx,
            role="Receiver",
        )

        return response

    except Exception:
        db.rollback()
        raise


@router.get("/{transaction_ref}")
def get_transfer(
    transaction_ref: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    tx = db.query(Transaction).filter_by(
        transaction_ref=transaction_ref,
        initiated_by=user.id,
    ).first()

    if not tx:
        raise HTTPException(404, "Transaction not found")

    return {
        "transaction_ref": tx.transaction_ref,
        "type": tx.type,
        "amount": str(tx.amount),
        "status": tx.status,
    }
