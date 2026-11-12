
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.entities import Account, LedgerEntry, Transaction


SYSTEM_ACCOUNTS = [
    "Bank Settlement",
    "Cashback Pool",
    "Fees",
    "Withdrawal Clearing",
    "Hold",
]

HIGH_VALUE_LIMIT = Decimal("50000.00")
RAPID_TRANSACTION_LIMIT = 5
RAPID_TRANSACTION_WINDOW_MINUTES = 10


def ensure_system_accounts(db: Session):
    for name in SYSTEM_ACCOUNTS:
        if not db.query(Account).filter_by(name=name).first():
            db.add(
                Account(
                    name=name,
                    account_type=name,
                    wallet_id=None,
                )
            )
    db.commit()


def wallet_account(db: Session, wallet_id: int):
    name = f"WALLET-{wallet_id}"

    account = db.query(Account).filter_by(name=name).first()

    if not account:
        account = Account(
            name=name,
            account_type="User Wallet",
            wallet_id=wallet_id,
        )
        db.add(account)
        db.flush()

    return account


def flag_suspicious_transaction(
    db: Session,
    transaction: Transaction,
):
    """
    Flag high-value transactions and rapid transaction activity.
    Flagging does not block or reverse the transaction.
    """

    reasons = []

    if transaction.amount >= HIGH_VALUE_LIMIT:
        reasons.append("High-value transaction")

    cutoff = datetime.utcnow() - timedelta(
        minutes=RAPID_TRANSACTION_WINDOW_MINUTES
    )

    recent_count = (
        db.query(Transaction)
        .filter(
            Transaction.initiated_by == transaction.initiated_by,
            Transaction.created_at >= cutoff,
            Transaction.status.in_(["Success", "Pending"]),
        )
        .count()
    )

    if recent_count >= RAPID_TRANSACTION_LIMIT:
        reasons.append("Rapid transaction activity")

    if reasons:
        transaction.is_flagged = True

    return reasons


def post_transaction(
    db: Session,
    *,
    tx_type: str,
    amount: Decimal,
    initiated_by: int,
    debit: Account,
    credit: Account,
    description: str = "",
    status: str = "Success",
):
    amount = Decimal(amount).quantize(Decimal("0.01"))

    if amount <= 0:
        raise HTTPException(
            422,
            "Amount must be greater than zero",
        )

    tx = Transaction(
        transaction_ref=f"TXN-{uuid4().hex[:16].upper()}",
        type=tx_type,
        amount=amount,
        status=status,
        initiated_by=initiated_by,
        description=description,
    )

    db.add(tx)
    db.flush()

    db.add_all(
        [
            LedgerEntry(
                transaction_id=tx.id,
                account_id=debit.id,
                entry_type="Debit",
                amount=amount,
                balance_after=Decimal("0.00"),
            ),
            LedgerEntry(
                transaction_id=tx.id,
                account_id=credit.id,
                entry_type="Credit",
                amount=amount,
                balance_after=Decimal("0.00"),
            ),
        ]
    )

    flag_suspicious_transaction(db, tx)

    return tx
