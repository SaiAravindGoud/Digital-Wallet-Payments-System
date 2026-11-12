
import hashlib
import io
import json
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4

import qrcode
from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Response,
)
from fastapi.responses import StreamingResponse
from sqlalchemy import desc
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.entities import (
    Account,
    Cashback,
    IdempotencyRecord,
    LedgerEntry,
    MerchantRefund,
    Role,
    Transaction,
    User,
    Wallet,
)
from app.schemas import MerchantPaymentIn, MerchantRefundIn
from app.security import current_user, verify_transaction_pin
from app.ledger import wallet_account, flag_suspicious_transaction
from app.notifications import send_transaction_notification


router = APIRouter(tags=["Merchant Payments"])

PLATFORM_FEE_RATE = Decimal("0.01")
CASHBACK_RATE = Decimal("0.02")
MAX_CASHBACK = Decimal("50.00")


def money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )


def calculate_cashback(amount: Decimal) -> Decimal:
    return min(
        money(amount * CASHBACK_RATE),
        MAX_CASHBACK,
    )


def make_idempotency_key(user_id: int, endpoint: str, key: str) -> str:
    raw = f"{user_id}:POST:{endpoint}:{key}"
    return hashlib.sha256(raw.encode()).hexdigest()


def make_request_hash(data: dict) -> str:
    raw = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def get_saved_response(record, request_hash, response: Response):
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
        response.headers["Idempotent-Replayed"] = "true"
        return json.loads(record.response_json)

    raise HTTPException(
        409,
        "Idempotency record is not completed",
    )


def find_or_expire_idempotency_record(db, stored_key):
    record = db.query(IdempotencyRecord).filter_by(
        key=stored_key
    ).first()

    if record:
        age = datetime.utcnow() - record.created_at

        if age < timedelta(hours=24):
            return record

        db.delete(record)
        db.flush()

    return None


def create_idempotency_record(
    db,
    *,
    stored_key,
    user_id,
    endpoint,
    request_hash,
):
    record = IdempotencyRecord(
        key=stored_key,
        user_id=user_id,
        endpoint=endpoint,
        request_hash=request_hash,
        response_json=None,
        status="Processing",
    )

    db.add(record)
    db.flush()

    return record


def require_merchant(user: User):
    if user.role != Role.Merchant:
        raise HTTPException(
            403,
            "Only merchants can access this endpoint",
        )


def get_wallet_cap(wallet: Wallet) -> Decimal:
    if wallet.kyc_level.value == "Minimum":
        return Decimal("10000.00")

    return Decimal("200000.00")


def create_ledger_entry(
    db,
    *,
    transaction_id,
    account_id,
    entry_type,
    amount,
):
    db.add(
        LedgerEntry(
            transaction_id=transaction_id,
            account_id=account_id,
            entry_type=entry_type,
            amount=amount,
            balance_after=Decimal("0.00"),
        )
    )


@router.get("/merchants/me/qr")
def get_merchant_qr(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    require_merchant(user)

    if not user.merchant_code:
        raise HTTPException(
            404,
            "Merchant code is not available",
        )

    qr = qrcode.QRCode(
        version=1,
        box_size=10,
        border=4,
    )

    qr.add_data(user.merchant_code)
    qr.make(fit=True)

    image = qr.make_image(
        fill_color="black",
        back_color="white",
    )

    image_buffer = io.BytesIO()
    image.save(image_buffer, format="PNG")
    image_buffer.seek(0)

    return StreamingResponse(
        image_buffer,
        media_type="image/png",
        headers={
            "Content-Disposition": "inline; filename=merchant-qr.png",
        },
    )


@router.post("/payments/merchant", status_code=201)
def pay_merchant(
    data: MerchantPaymentIn,
    response: Response,
    idempotency_key: str = Header(
        ...,
        alias="Idempotency-Key",
        min_length=8,
        max_length=100,
    ),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    if user.role != Role.User:
        raise HTTPException(
            403,
            "Only customer accounts can make merchant payments",
        )

    if data.amount < Decimal("1.00") or data.amount > Decimal("200000.00"):
        raise HTTPException(
            422,
            "Payment amount must be between 1 and 200000",
        )

    endpoint = "/payments/merchant"

    request_hash = make_request_hash({
        "merchant_code": data.merchant_code,
        "amount": str(data.amount),
    })

    stored_key = make_idempotency_key(
        user.id,
        endpoint,
        idempotency_key,
    )

    existing = find_or_expire_idempotency_record(
        db,
        stored_key,
    )

    if existing:
        if (
            existing.user_id != user.id
            or existing.endpoint != endpoint
        ):
            raise HTTPException(
                409,
                "Idempotency key conflict",
            )

        return get_saved_response(
            existing,
            request_hash,
            response,
        )

    verify_transaction_pin(db, user, data.pin)

    merchant = db.query(User).filter_by(
        merchant_code=data.merchant_code,
        role=Role.Merchant,
        is_active=True,
    ).first()

    if not merchant:
        raise HTTPException(
            404,
            "Active merchant not found",
        )

    if merchant.id == user.id:
        raise HTTPException(
            400,
            "Cannot pay your own merchant account",
        )

    wallets = (
        db.query(Wallet)
        .filter(
            Wallet.owner_id.in_([user.id, merchant.id])
        )
        .order_by(Wallet.id)
        .with_for_update()
        .all()
    )

    wallet_by_owner = {
        wallet.owner_id: wallet
        for wallet in wallets
    }

    customer_wallet = wallet_by_owner.get(user.id)
    merchant_wallet = wallet_by_owner.get(merchant.id)

    if not customer_wallet or not merchant_wallet:
        raise HTTPException(
            404,
            "Customer or merchant wallet not found",
        )

    if customer_wallet.status.value != "Active":
        raise HTTPException(
            403,
            "Customer wallet is frozen",
        )

    if merchant_wallet.status.value != "Active":
        raise HTTPException(
            403,
            "Merchant wallet is not active",
        )

    if customer_wallet.balance < data.amount:
        raise HTTPException(
            400,
            "Insufficient wallet balance",
        )

    merchant_fee = money(
        data.amount * PLATFORM_FEE_RATE
    )

    merchant_credit = money(
        data.amount - merchant_fee
    )

    cashback_amount = calculate_cashback(data.amount)

    merchant_cap = get_wallet_cap(merchant_wallet)

    if merchant_wallet.balance + merchant_credit > merchant_cap:
        raise HTTPException(
            400,
            "Merchant KYC wallet balance limit exceeded",
        )

    fee_account = db.query(Account).filter_by(
        name="Fees"
    ).first()

    if not fee_account:
        raise HTTPException(
            500,
            "Fees system account is unavailable",
        )

    record = create_idempotency_record(
        db,
        stored_key=stored_key,
        user_id=user.id,
        endpoint=endpoint,
        request_hash=request_hash,
    )

    try:
        customer_account = wallet_account(
            db,
            customer_wallet.id,
        )

        merchant_account = wallet_account(
            db,
            merchant_wallet.id,
        )

        transaction = Transaction(
            transaction_ref=f"TXN-{uuid4().hex[:16].upper()}",
            type="Merchant Payment",
            amount=data.amount,
            status="Success",
            initiated_by=user.id,
            description=f"Payment to {merchant.merchant_code}",
        )

        db.add(transaction)
        db.flush()

        # Check merchant payment for suspicious activity.
        flag_suspicious_transaction(db, transaction)

        # Debit customer by the complete payment amount.
        create_ledger_entry(
            db,
            transaction_id=transaction.id,
            account_id=customer_account.id,
            entry_type="Debit",
            amount=data.amount,
        )

        # Credit merchant after deducting the platform fee.
        create_ledger_entry(
            db,
            transaction_id=transaction.id,
            account_id=merchant_account.id,
            entry_type="Credit",
            amount=merchant_credit,
        )

        # Credit the platform Fees account.
        if merchant_fee > 0:
            create_ledger_entry(
                db,
                transaction_id=transaction.id,
                account_id=fee_account.id,
                entry_type="Credit",
                amount=merchant_fee,
            )

        customer_wallet.balance -= data.amount
        merchant_wallet.balance += merchant_credit

        cashback = Cashback(
            transaction_id=transaction.id,
            user_id=user.id,
            amount=cashback_amount,
            status="Pending",
        )

        db.add(cashback)

        result = {
            "transaction_ref": transaction.transaction_ref,
            "status": "Success",
            "merchant_code": merchant.merchant_code,
            "merchant_name": merchant.business_name or merchant.full_name,
            "amount": str(data.amount),
            "platform_fee": str(merchant_fee),
            "merchant_received": str(merchant_credit),
            "customer_balance": str(customer_wallet.balance),
            "cashback": {
                "amount": str(cashback_amount),
                "status": "Pending",
                "release_after_hours": 24,
            },
        }

        record.response_json = json.dumps(result)
        record.status = "Completed"

        db.commit()

        # Notify customer and merchant after successful payment commit.
        send_transaction_notification(
            user,
            transaction,
            role="Customer",
        )

        send_transaction_notification(
            merchant,
            transaction,
            role="Merchant",
        )

        return result

    except IntegrityError:
        db.rollback()

        existing = db.query(IdempotencyRecord).filter_by(
            key=stored_key
        ).first()

        if existing:
            if existing.user_id != user.id:
                raise HTTPException(
                    409,
                    "Idempotency key conflict",
                )

            return get_saved_response(
                existing,
                request_hash,
                response,
            )

        raise HTTPException(
            409,
            "Duplicate payment request. Please retry.",
        )

    except Exception:
        db.rollback()
        raise


@router.post("/payments/{transaction_ref}/refund", status_code=201)
def refund_merchant_payment(
    transaction_ref: str,
    data: MerchantRefundIn,
    response: Response,
    idempotency_key: str = Header(
        ...,
        alias="Idempotency-Key",
        min_length=8,
        max_length=100,
    ),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    require_merchant(user)

    if data.amount < Decimal("1.00"):
        raise HTTPException(
            422,
            "Refund amount must be at least 1",
        )

    endpoint = f"/payments/{transaction_ref}/refund"

    request_hash = make_request_hash({
        "transaction_ref": transaction_ref,
        "amount": str(data.amount),
    })

    stored_key = make_idempotency_key(
        user.id,
        endpoint,
        idempotency_key,
    )

    existing = find_or_expire_idempotency_record(
        db,
        stored_key,
    )

    if existing:
        if (
            existing.user_id != user.id
            or existing.endpoint != endpoint
        ):
            raise HTTPException(
                409,
                "Idempotency key conflict",
            )

        return get_saved_response(
            existing,
            request_hash,
            response,
        )

    verify_transaction_pin(db, user, data.pin)

    original_transaction = db.query(Transaction).filter_by(
        transaction_ref=transaction_ref,
        type="Merchant Payment",
        status="Success",
    ).first()

    if not original_transaction:
        raise HTTPException(
            404,
            "Successful merchant payment not found",
        )

    if datetime.utcnow() - original_transaction.created_at > timedelta(days=7):
        raise HTTPException(
            400,
            "Refund period of 7 days has expired",
        )

    merchant_wallet = db.query(Wallet).filter_by(
        owner_id=user.id
    ).first()

    customer = db.query(User).filter_by(
        id=original_transaction.initiated_by
    ).first()

    if not merchant_wallet or not customer:
        raise HTTPException(
            404,
            "Merchant or customer account not found",
        )

    if customer.id == user.id:
        raise HTTPException(
            400,
            "Invalid refund customer",
        )

    # Verify that this merchant actually received the original payment.
    merchant_received_entry = (
        db.query(LedgerEntry)
        .join(Account, LedgerEntry.account_id == Account.id)
        .filter(
            LedgerEntry.transaction_id == original_transaction.id,
            LedgerEntry.entry_type == "Credit",
            Account.wallet_id == merchant_wallet.id,
        )
        .first()
    )

    if not merchant_received_entry:
        raise HTTPException(
            403,
            "You are not the merchant who received this payment",
        )

    # Lock both wallets in ascending ID order.
    wallets = (
        db.query(Wallet)
        .filter(
            Wallet.owner_id.in_([user.id, customer.id])
        )
        .order_by(Wallet.id)
        .with_for_update()
        .all()
    )

    wallet_by_owner = {
        wallet.owner_id: wallet
        for wallet in wallets
    }

    merchant_wallet = wallet_by_owner.get(user.id)
    customer_wallet = wallet_by_owner.get(customer.id)

    if not merchant_wallet or not customer_wallet:
        raise HTTPException(
            404,
            "Refund wallet not found",
        )

    if merchant_wallet.status.value != "Active":
        raise HTTPException(
            403,
            "Merchant wallet is frozen",
        )

    if customer_wallet.status.value != "Active":
        raise HTTPException(
            403,
            "Customer wallet is not active",
        )

    refunded_amount = (
        db.query(MerchantRefund)
        .filter(
            MerchantRefund.transaction_id == original_transaction.id,
            MerchantRefund.merchant_id == user.id,
            MerchantRefund.status == "Success",
        )
        .with_for_update()
        .all()
    )

    total_refunded = sum(
        (item.amount for item in refunded_amount),
        Decimal("0.00"),
    )

    remaining_amount = money(
        original_transaction.amount - total_refunded
    )

    if data.amount > remaining_amount:
        raise HTTPException(
            400,
            f"Refund exceeds remaining refundable amount: {remaining_amount}",
        )

    if merchant_wallet.balance < data.amount:
        raise HTTPException(
            400,
            "Insufficient merchant wallet balance for refund",
        )

    customer_cap = get_wallet_cap(customer_wallet)

    if customer_wallet.balance + data.amount > customer_cap:
        raise HTTPException(
            400,
            "Customer KYC wallet balance limit exceeded",
        )

    record = create_idempotency_record(
        db,
        stored_key=stored_key,
        user_id=user.id,
        endpoint=endpoint,
        request_hash=request_hash,
    )

    try:
        merchant_account = wallet_account(
            db,
            merchant_wallet.id,
        )

        customer_account = wallet_account(
            db,
            customer_wallet.id,
        )

        refund_transaction = Transaction(
            transaction_ref=f"TXN-{uuid4().hex[:16].upper()}",
            type="Merchant Refund",
            amount=data.amount,
            status="Success",
            initiated_by=user.id,
            description=f"Refund for {transaction_ref}",
        )

        db.add(refund_transaction)
        db.flush()

        create_ledger_entry(
            db,
            transaction_id=refund_transaction.id,
            account_id=merchant_account.id,
            entry_type="Debit",
            amount=data.amount,
        )

        create_ledger_entry(
            db,
            transaction_id=refund_transaction.id,
            account_id=customer_account.id,
            entry_type="Credit",
            amount=data.amount,
        )

        merchant_wallet.balance -= data.amount
        customer_wallet.balance += data.amount

        refund_record = MerchantRefund(
            transaction_id=original_transaction.id,
            merchant_id=user.id,
            amount=data.amount,
            status="Success",
        )

        db.add(refund_record)

        # Cancel pending cashback for the original payment.
        pending_cashback = db.query(Cashback).filter_by(
            transaction_id=original_transaction.id,
            status="Pending",
        ).first()

        if pending_cashback:
            pending_cashback.status = "Cancelled"

        result = {
            "refund_transaction_ref": refund_transaction.transaction_ref,
            "original_transaction_ref": transaction_ref,
            "status": "Success",
            "refund_amount": str(data.amount),
            "total_refunded": str(total_refunded + data.amount),
            "remaining_refundable": str(
                remaining_amount - data.amount
            ),
            "merchant_balance": str(merchant_wallet.balance),
            "customer_balance": str(customer_wallet.balance),
            "cashback_status": (
                "Cancelled"
                if pending_cashback
                else "Already released or not applicable"
            ),
        }

        record.response_json = json.dumps(result)
        record.status = "Completed"

        db.commit()

        # Notify customer and merchant after successful refund commit.
        send_transaction_notification(
            customer,
            refund_transaction,
            role="Customer",
        )

        send_transaction_notification(
            user,
            refund_transaction,
            role="Merchant",
        )

        return result

    except IntegrityError:
        db.rollback()

        existing = db.query(IdempotencyRecord).filter_by(
            key=stored_key
        ).first()

        if existing:
            return get_saved_response(
                existing,
                request_hash,
                response,
            )

        raise HTTPException(
            409,
            "Duplicate refund request. Please retry.",
        )

    except Exception:
        db.rollback()
        raise


@router.get("/merchants/me/payments")
def get_merchant_payments(
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    require_merchant(user)

    if page < 1:
        raise HTTPException(
            422,
            "Page must be at least 1",
        )

    if page_size < 1 or page_size > 100:
        raise HTTPException(
            422,
            "Page size must be between 1 and 100",
        )

    merchant_wallet = db.query(Wallet).filter_by(
        owner_id=user.id
    ).first()

    if not merchant_wallet:
        raise HTTPException(
            404,
            "Merchant wallet not found",
        )

    payments_query = (
        db.query(Transaction)
        .join(
            LedgerEntry,
            LedgerEntry.transaction_id == Transaction.id,
        )
        .join(
            Account,
            Account.id == LedgerEntry.account_id,
        )
        .filter(
            Transaction.type == "Merchant Payment",
            LedgerEntry.entry_type == "Credit",
            Account.wallet_id == merchant_wallet.id,
        )
    )

    total = payments_query.count()

    payments = (
        payments_query
        .order_by(desc(Transaction.created_at))
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    results = []

    for payment in payments:
        refunds = db.query(MerchantRefund).filter_by(
            transaction_id=payment.id,
            merchant_id=user.id,
            status="Success",
        ).all()

        refunded_total = sum(
            (item.amount for item in refunds),
            Decimal("0.00"),
        )

        results.append({
            "transaction_ref": payment.transaction_ref,
            "amount": str(payment.amount),
            "refunded_amount": str(refunded_total),
            "remaining_refundable": str(
                money(payment.amount - refunded_total)
            ),
            "status": payment.status,
            "created_at": payment.created_at.isoformat(),
            "description": payment.description,
        })

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "payments": results,
    }
