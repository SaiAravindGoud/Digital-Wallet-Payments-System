
from datetime import datetime, time
from decimal import Decimal

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.entities import Transaction, Withdrawal


DAILY_OUTGOING_LIMIT = Decimal("200000.00")


def get_daily_outgoing(db: Session, user_id: int) -> Decimal:
    """Calculate today's outgoing P2P transfers and withdrawals."""

    now = datetime.utcnow()
    day_start = datetime.combine(now.date(), time.min)

    p2p_total = (
        db.query(
            func.coalesce(func.sum(Transaction.amount), 0)
        )
        .filter(
            Transaction.initiated_by == user_id,
            Transaction.type == "P2P Transfer",
            Transaction.status == "Success",
            Transaction.created_at >= day_start,
        )
        .scalar()
    )

    withdrawal_total = (
        db.query(
            func.coalesce(func.sum(Withdrawal.amount), 0)
        )
        .join(
            Transaction,
            Withdrawal.transaction_id == Transaction.id,
        )
        .filter(
            Transaction.initiated_by == user_id,
            Transaction.type == "Withdrawal",
            Transaction.created_at >= day_start,
            Withdrawal.status.in_(["Pending", "Success"]),
        )
        .scalar()
    )

    return Decimal(str(p2p_total or 0)) + Decimal(
        str(withdrawal_total or 0)
    )


def check_daily_outgoing_limit(
    db: Session,
    user_id: int,
    amount: Decimal,
) -> Decimal:
    """Raise an error if this payment exceeds the daily limit."""

    from fastapi import HTTPException

    current_total = get_daily_outgoing(db, user_id)

    if current_total + amount > DAILY_OUTGOING_LIMIT:
        raise HTTPException(
            status_code=400,
            detail="Daily outgoing limit of 200000 exceeded",
        )

    return current_total
