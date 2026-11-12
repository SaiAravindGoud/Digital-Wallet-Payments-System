
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.entities import (
    User,
    Wallet,
    Transaction,
    LedgerEntry,
    Account,
)
from app.security import roles


router = APIRouter(
    prefix="/admin/analytics",
    tags=["Admin Analytics"],
)


@router.get("/overview")
def analytics_overview(
    db: Session = Depends(get_db),
    admin=Depends(roles("Admin")),
):
    total_users = db.query(User).count()

    total_merchants = (
        db.query(User)
        .filter(User.role == "Merchant")
        .count()
    )

    total_wallets = db.query(Wallet).count()

    active_wallets = (
        db.query(Wallet)
        .filter(Wallet.status == "Active")
        .count()
    )

    frozen_wallets = (
        db.query(Wallet)
        .filter(Wallet.status == "Frozen")
        .count()
    )

    total_transactions = db.query(Transaction).count()

    successful_transactions = (
        db.query(Transaction)
        .filter(Transaction.status == "Success")
        .count()
    )

    failed_transactions = (
        db.query(Transaction)
        .filter(Transaction.status == "Failed")
        .count()
    )

    pending_transactions = (
        db.query(Transaction)
        .filter(Transaction.status == "Pending")
        .count()
    )

    flagged_transactions = (
        db.query(Transaction)
        .filter(Transaction.is_flagged.is_(True))
        .count()
    )

    successful_volume = (
        db.query(func.coalesce(func.sum(Transaction.amount), 0))
        .filter(Transaction.status == "Success")
        .scalar()
    )

    total_wallet_balance = (
        db.query(func.coalesce(func.sum(Wallet.balance), 0))
        .scalar()
    )

    return {
        "generated_at": datetime.utcnow().isoformat(),
        "users": {
            "total": total_users,
            "merchants": total_merchants,
            "customers": total_users - total_merchants,
        },
        "wallets": {
            "total": total_wallets,
            "active": active_wallets,
            "frozen": frozen_wallets,
            "total_balance": str(
                Decimal(str(total_wallet_balance))
            ),
        },
        "transactions": {
            "total": total_transactions,
            "successful": successful_transactions,
            "failed": failed_transactions,
            "pending": pending_transactions,
            "flagged": flagged_transactions,
            "successful_volume": str(
                Decimal(str(successful_volume))
            ),
        },
    }


@router.get("/transaction-types")
def transaction_type_report(
    db: Session = Depends(get_db),
    admin=Depends(roles("Admin")),
):
    rows = (
        db.query(
            Transaction.type,
            func.count(Transaction.id),
            func.coalesce(func.sum(Transaction.amount), 0),
        )
        .group_by(Transaction.type)
        .order_by(Transaction.type.asc())
        .all()
    )

    return {
        "total_types": len(rows),
        "reports": [
            {
                "type": transaction_type,
                "count": count,
                "total_amount": str(
                    Decimal(str(total_amount))
                ),
            }
            for transaction_type, count, total_amount in rows
        ],
    }


@router.get("/daily")
def daily_transaction_report(
    start_date: date = Query(
        default_factory=lambda: date.today() - timedelta(days=6)
    ),
    end_date: date = Query(
        default_factory=date.today
    ),
    db: Session = Depends(get_db),
    admin=Depends(roles("Admin")),
):
    if start_date > end_date:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=422,
            detail="Start date cannot be after end date",
        )

    start_datetime = datetime.combine(
        start_date,
        time.min,
    )

    end_exclusive = datetime.combine(
        end_date + timedelta(days=1),
        time.min,
    )

    rows = (
        db.query(
            func.date(Transaction.created_at),
            func.count(Transaction.id),
            func.coalesce(func.sum(Transaction.amount), 0),
        )
        .filter(
            Transaction.created_at >= start_datetime,
            Transaction.created_at < end_exclusive,
        )
        .group_by(func.date(Transaction.created_at))
        .order_by(func.date(Transaction.created_at).asc())
        .all()
    )

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "reports": [
            {
                "date": (
                    report_date.isoformat()
                    if hasattr(report_date, "isoformat")
                    else str(report_date)
                ),
                "transaction_count": count,
                "total_amount": str(
                    Decimal(str(total_amount))
                ),
            }
            for report_date, count, total_amount in rows
        ],
    }
