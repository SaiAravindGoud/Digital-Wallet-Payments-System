
import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Header,
    status,
)
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.database import get_db
from app.models.entities import (
    User,
    Wallet,
    MoneyRequest,
    Transaction,
    IdempotencyRecord,
    Cashback,
)
from app.schemas import MoneyRequestIn, MoneyRequestPayIn
from app.security import current_user, verify_transaction_pin
from app.otp_service import create_and_send_otp, verify_otp
from app.ledger import wallet_account, post_transaction
from app.daily_limits import check_daily_outgoing_limit
from app.notifications import send_transaction_notification


router = APIRouter(
    prefix="/money-requests",
    tags=["Money Requests"],
)


REQUEST_EXPIRY_HOURS = 48
MAX_PENDING_REQUESTS = 5

CASHBACK_RATE = Decimal("0.02")
MAX_CASHBACK = Decimal("50.00")

IDEMPOTENCY_HOURS = 24


def calculate_cashback(amount: Decimal) -> Decimal:
    cashback = (amount * CASHBACK_RATE).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    return min(cashback, MAX_CASHBACK)


def serialize_request(item, requester, payer):
    return {
        "id": item.id,
        "requester_id": item.requester_id,
        "requester_name": requester.full_name,
        "requester_phone": requester.phone,
        "payer_id": item.payer_id,
        "payer_name": payer.full_name,
        "payer_phone": payer.phone,
        "amount": str(item.amount),
        "note": item.note,
        "status": item.status,
        "expires_at": item.expires_at.isoformat(),
        "created_at": item.created_at.isoformat(),
    }


def expire_request(item):
    if (
        item.status == "Pending"
        and item.expires_at <= datetime.utcnow()
    ):
        item.status = "Expired"
        return True

    return False


def make_idempotency_key(user_id: int, key: str) -> str:
    raw = f"{user_id}:PUT:/money-requests/pay:{key}"
    return hashlib.sha256(raw.encode()).hexdigest()


def make_request_hash(request_id: int, user_id: int) -> str:
    raw = json.dumps(
        {
            "request_id": request_id,
            "payer_id": user_id,
        },
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def get_saved_response(db, record, request_hash):
    if record.request_hash != request_hash:
        raise HTTPException(
            status_code=422,
            detail="Idempotency key was already used with a different request",
        )

    if record.status == "Processing":
        raise HTTPException(
            status_code=409,
            detail="A request with this idempotency key is still processing",
        )

    if record.status == "Completed" and record.response_json:
        return json.loads(record.response_json)

    raise HTTPException(
        status_code=409,
        detail="Idempotency record is not completed",
    )


def get_request_or_404(db, request_id):
    item = (
        db.query(MoneyRequest)
        .filter(MoneyRequest.id == request_id)
        .with_for_update()
        .first()
    )

    if not item:
        raise HTTPException(
            status_code=404,
            detail="Money request not found",
        )

    return item


def check_pending_request(item):
    if item.status != "Pending":
        raise HTTPException(
            status_code=400,
            detail=f"Request is already {item.status}",
        )

    if item.expires_at <= datetime.utcnow():
        item.status = "Expired"
        return False

    return True


def list_user_requests(db, user, request_filter):
    requests = (
        db.query(MoneyRequest)
        .filter(request_filter)
        .order_by(MoneyRequest.created_at.desc())
        .all()
    )

    result = []

    for item in requests:
        expire_request(item)

        requester = (
            db.query(User)
            .filter_by(id=item.requester_id)
            .first()
        )

        payer = (
            db.query(User)
            .filter_by(id=item.payer_id)
            .first()
        )

        if requester and payer:
            result.append(
                serialize_request(item, requester, payer)
            )

    db.commit()

    return result


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
)
def create_money_request(
    data: MoneyRequestIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    if data.amount < Decimal("1") or data.amount > Decimal("200000"):
        raise HTTPException(
            status_code=422,
            detail="Request amount must be between 1 and 200000",
        )

    payer = (
        db.query(User)
        .filter(User.phone == data.payer_phone)
        .first()
    )

    if not payer:
        raise HTTPException(
            status_code=404,
            detail="Payer not found",
        )

    if payer.id == user.id:
        raise HTTPException(
            status_code=400,
            detail="Cannot request money from yourself",
        )

    if not payer.is_active:
        raise HTTPException(
            status_code=400,
            detail="Payer account is inactive",
        )

    payer_wallet = (
        db.query(Wallet)
        .filter_by(owner_id=payer.id)
        .first()
    )

    requester_wallet = (
        db.query(Wallet)
        .filter_by(owner_id=user.id)
        .first()
    )

    if not payer_wallet:
        raise HTTPException(
            status_code=404,
            detail="Payer wallet not found",
        )

    if not requester_wallet:
        raise HTTPException(
            status_code=404,
            detail="Requester wallet not found",
        )

    now = datetime.utcnow()

    # Expire old requests before checking the pending limit.
    (
        db.query(MoneyRequest)
        .filter(
            MoneyRequest.requester_id == user.id,
            MoneyRequest.payer_id == payer.id,
            MoneyRequest.status == "Pending",
            MoneyRequest.expires_at <= now,
        )
        .update(
            {"status": "Expired"},
            synchronize_session=False,
        )
    )

    pending_count = (
        db.query(MoneyRequest)
        .filter(
            MoneyRequest.requester_id == user.id,
            MoneyRequest.payer_id == payer.id,
            MoneyRequest.status == "Pending",
            MoneyRequest.expires_at > now,
        )
        .count()
    )

    if pending_count >= MAX_PENDING_REQUESTS:
        db.commit()
        raise HTTPException(
            status_code=400,
            detail="Maximum 5 pending requests allowed for this payer",
        )

    item = MoneyRequest(
        requester_id=user.id,
        payer_id=payer.id,
        amount=data.amount,
        note=data.note.strip(),
        status="Pending",
        expires_at=now + timedelta(
            hours=REQUEST_EXPIRY_HOURS
        ),
    )

    try:
        db.add(item)
        db.commit()
        db.refresh(item)

    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail="Unable to create money request",
        )

    return {
        "message": "Money request created successfully",
        "request": serialize_request(item, user, payer),
    }


@router.get("")
def get_money_requests(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return list_user_requests(
        db,
        user,
        (
            (MoneyRequest.requester_id == user.id)
            | (MoneyRequest.payer_id == user.id)
        ),
    )


@router.get("/sent")
def get_sent_requests(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return list_user_requests(
        db,
        user,
        MoneyRequest.requester_id == user.id,
    )


@router.get("/received")
def get_received_requests(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return list_user_requests(
        db,
        user,
        MoneyRequest.payer_id == user.id,
    )


@router.post("/{request_id}/request-otp")
def request_money_payment_otp(
    request_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    item = (
        db.query(MoneyRequest)
        .filter(MoneyRequest.id == request_id)
        .first()
    )

    if not item:
        raise HTTPException(
            status_code=404,
            detail="Money request not found",
        )

    if item.payer_id != user.id:
        raise HTTPException(
            status_code=403,
            detail="Only the payer can request a payment OTP",
        )

    if item.status != "Pending":
        raise HTTPException(
            status_code=400,
            detail=f"Request is already {item.status}",
        )

    if item.expires_at <= datetime.utcnow():
        item.status = "Expired"
        db.commit()

        raise HTTPException(
            status_code=400,
            detail="Money request has expired",
        )

    return create_and_send_otp(
        db=db,
        user_id=user.id,
        email=user.email,
        purpose=f"money_request_pay_{request_id}",
    )


@router.put("/{request_id}/pay")
@router.post(
    "/{request_id}/pay",
    include_in_schema=False,
)
def pay_money_request(
    request_id: int,
    data: MoneyRequestPayIn,
    idempotency_key: str = Header(
        ...,
        alias="Idempotency-Key",
        min_length=8,
        max_length=100,
    ),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    stored_key = make_idempotency_key(
        user.id,
        idempotency_key,
    )

    request_hash = make_request_hash(
        request_id,
        user.id,
    )

    existing = (
        db.query(IdempotencyRecord)
        .filter_by(key=stored_key)
        .first()
    )

    if existing:
        age = datetime.utcnow() - existing.created_at

        if age < timedelta(hours=IDEMPOTENCY_HOURS):
            if (
                existing.user_id != user.id
                or existing.endpoint != f"/money-requests/{request_id}/pay"
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency key conflict",
                )

            return get_saved_response(
                db,
                existing,
                request_hash,
            )

        db.delete(existing)
        db.commit()

    # Verify the payer's transaction PIN.
    verify_transaction_pin(
        db,
        user,
        data.pin,
    )

    # OTP is specific to this money request.
    verify_otp(
        db=db,
        user_id=user.id,
        purpose=f"money_request_pay_{request_id}",
        otp_code=data.otp,
    )

    # Lock the request before processing the payment.
    item = get_request_or_404(
        db,
        request_id,
    )

    if item.payer_id != user.id:
        raise HTTPException(
            status_code=403,
            detail="You are not authorized to pay this request",
        )

    if not check_pending_request(item):
        db.commit()

        raise HTTPException(
            status_code=400,
            detail="Money request has expired",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=403,
            detail="Inactive users cannot make payments",
        )

    # Lock both wallets in a consistent order.
    wallets = (
        db.query(Wallet)
        .filter(
            Wallet.owner_id.in_([
                user.id,
                item.requester_id,
            ])
        )
        .order_by(Wallet.id)
        .with_for_update()
        .all()
    )

    wallet_by_owner = {
        wallet.owner_id: wallet
        for wallet in wallets
    }

    payer_wallet = wallet_by_owner.get(user.id)
    requester_wallet = wallet_by_owner.get(item.requester_id)

    if not payer_wallet or not requester_wallet:
        raise HTTPException(
            status_code=404,
            detail="Wallet not found",
        )

    if payer_wallet.status.value != "Active":
        raise HTTPException(
            status_code=403,
            detail="Your wallet is frozen",
        )

    if requester_wallet.status.value != "Active":
        raise HTTPException(
            status_code=400,
            detail="Requester wallet is not active",
        )

    if payer_wallet.balance < item.amount:
        raise HTTPException(
            status_code=400,
            detail="Insufficient wallet balance",
        )

    payer_cap = (
        Decimal("10000")
        if payer_wallet.kyc_level.value == "Minimum"
        else Decimal("200000")
    )

    requester_cap = (
        Decimal("10000")
        if requester_wallet.kyc_level.value == "Minimum"
        else Decimal("200000")
    )

    if payer_wallet.balance > payer_cap:
        raise HTTPException(
            status_code=400,
            detail="Payer wallet KYC balance limit exceeded",
        )

    if requester_wallet.balance + item.amount > requester_cap:
        raise HTTPException(
            status_code=400,
            detail="Requester wallet KYC balance limit exceeded",
        )

    # Enforce the shared daily outgoing limit.
    check_daily_outgoing_limit(
        db,
        user.id,
        item.amount,
    )

    record = IdempotencyRecord(
        key=stored_key,
        user_id=user.id,
        endpoint=f"/money-requests/{request_id}/pay",
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
            return get_saved_response(
                db,
                existing,
                request_hash,
            )

        raise HTTPException(
            status_code=409,
            detail="Duplicate payment request. Please retry.",
        )

    try:
        payer_account = wallet_account(
            db,
            payer_wallet.id,
        )

        requester_account = wallet_account(
            db,
            requester_wallet.id,
        )

        tx = post_transaction(
            db,
            tx_type="P2P Transfer",
            amount=item.amount,
            initiated_by=user.id,
            debit=payer_account,
            credit=requester_account,
            description=f"Money request payment #{item.id}",
        )

        payer_wallet.balance -= item.amount
        requester_wallet.balance += item.amount

        item.status = "Paid"

        cashback_amount = calculate_cashback(
            item.amount,
        )

        cashback = Cashback(
            transaction_id=tx.id,
            user_id=user.id,
            amount=cashback_amount,
            status="Pending",
        )

        db.add(cashback)

        response = {
            "message": "Money request paid successfully",
            "request_id": item.id,
            "transaction_ref": tx.transaction_ref,
            "amount": str(item.amount),
            "status": "Paid",
            "payer_balance": str(payer_wallet.balance),
            "cashback": {
                "amount": str(cashback_amount),
                "status": "Pending",
                "release_after_hours": 24,
            },
        }

        record.response_json = json.dumps(response)
        record.status = "Completed"

        db.commit()

        # Send notifications only after the payment is committed.
        send_transaction_notification(
            user,
            tx,
            role="Customer",
        )

        requester = (
            db.query(User)
            .filter(User.id == item.requester_id)
            .first()
        )

        if requester:
            send_transaction_notification(
                requester,
                tx,
                role="Recipient",
            )

        return response

    except Exception:
        db.rollback()
        raise


@router.put("/{request_id}/decline")
@router.post(
    "/{request_id}/reject",
    include_in_schema=False,
)
def decline_money_request(
    request_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    item = get_request_or_404(
        db,
        request_id,
    )

    if item.payer_id != user.id:
        raise HTTPException(
            status_code=403,
            detail="You are not authorized to decline this request",
        )

    if not check_pending_request(item):
        db.commit()

        raise HTTPException(
            status_code=400,
            detail="Money request has expired",
        )

    item.status = "Declined"

    db.commit()

    return {
        "message": "Money request declined successfully",
        "request_id": item.id,
        "status": item.status,
    }


@router.put("/{request_id}/cancel")
def cancel_money_request(
    request_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    item = get_request_or_404(
        db,
        request_id,
    )

    if item.requester_id != user.id:
        raise HTTPException(
            status_code=403,
            detail="Only the requester can cancel this request",
        )

    if not check_pending_request(item):
        db.commit()

        raise HTTPException(
            status_code=400,
            detail="Money request has expired",
        )

    item.status = "Cancelled"

    db.commit()

    return {
        "message": "Money request cancelled successfully",
        "request_id": item.id,
        "status": item.status,
    }
