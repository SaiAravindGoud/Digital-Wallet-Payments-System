
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.entities import (
    Account,
    Cashback,
    LedgerEntry,
    MerchantRefund,
    Transaction,
    User,
    Wallet,
    Withdrawal,
)
from app.security import current_user, roles


router = APIRouter(
    prefix="/reports",
    tags=["Reports & Dashboard"],
)


ZERO = Decimal("0.00")

# Cashback statuses that represent successfully released cashback.
PAID_CASHBACK_STATUSES = ("Paid", "Success", "Released")


def decimal_value(value):
    return Decimal(str(value or 0)).quantize(
        Decimal("0.01")
    )


def money_string(value):
    return str(decimal_value(value))


def date_range(start_date, end_date):
    if start_date > end_date:
        raise HTTPException(
            status_code=422,
            detail="Start date cannot be after end date",
        )

    start = datetime.combine(start_date, time.min)
    end = datetime.combine(
        end_date + timedelta(days=1),
        time.min,
    )
    return start, end


# --------------------------------------------------
# 1. ADMIN DASHBOARD
# --------------------------------------------------

@router.get("/admin-dashboard")
def admin_dashboard(
    db: Session = Depends(get_db),
    admin=Depends(roles("Admin")),
):
    today = date.today()
    start, end = date_range(today, today)

    total_wallet_balance = (
        db.query(
            func.coalesce(func.sum(Wallet.balance), 0)
        ).scalar()
    )

    today_transactions = db.query(Transaction).filter(
        Transaction.created_at >= start,
        Transaction.created_at < end,
    )

    transaction_count = today_transactions.count()

    transaction_volume = (
        db.query(
            func.coalesce(func.sum(Transaction.amount), 0)
        )
        .filter(
            Transaction.created_at >= start,
            Transaction.created_at < end,
            Transaction.status == "Success",
        )
        .scalar()
    )

    volume_rows = (
        db.query(
            Transaction.type,
            func.count(Transaction.id),
            func.coalesce(func.sum(Transaction.amount), 0),
        )
        .filter(
            Transaction.created_at >= start,
            Transaction.created_at < end,
            Transaction.status == "Success",
        )
        .group_by(Transaction.type)
        .all()
    )

    failed_count = today_transactions.filter(
        Transaction.status == "Failed"
    ).count()

    hold_count = today_transactions.filter(
        Transaction.status == "On Hold"
    ).count()

    cashback_paid = (
        db.query(
            func.coalesce(func.sum(Cashback.amount), 0)
        )
        .filter(
            Cashback.status.in_(PAID_CASHBACK_STATUSES),
            Cashback.created_at < end,
        )
        .scalar()
    )

    return {
        "date": today.isoformat(),
        "currency": "INR",
        "total_wallet_balance": money_string(
            total_wallet_balance
        ),
        "today": {
            "transaction_count": transaction_count,
            "transaction_volume": money_string(
                transaction_volume
            ),
        },
        "volume_by_type": {
            transaction_type: {
                "count": count,
                "amount": money_string(amount),
            }
            for transaction_type, count, amount in volume_rows
        },
        "failed_transactions": failed_count,
        "transactions_on_hold": hold_count,
        "cashback_paid": money_string(cashback_paid),
    }


# --------------------------------------------------
# 2. LEDGER SUMMARY
# --------------------------------------------------

@router.get("/ledger-summary")
def ledger_summary(
    start_date: date = Query(
        default_factory=lambda: date.today() - timedelta(days=30)
    ),
    end_date: date = Query(
        default_factory=date.today
    ),
    db: Session = Depends(get_db),
    admin=Depends(roles("Admin")),
):
    start, end = date_range(start_date, end_date)

    accounts = (
        db.query(Account)
        .order_by(Account.id.asc())
        .all()
    )

    account_reports = []

    total_debits = ZERO
    total_credits = ZERO

    for account in accounts:

        # All-time account balance.
        balance_row = (
            db.query(
                func.coalesce(
                    func.sum(
                        LedgerEntry.amount
                    ),
                    0,
                )
            )
            .filter(
                LedgerEntry.account_id == account.id,
                LedgerEntry.entry_type == "Credit",
            )
            .scalar()
        )

        debit_balance = (
            db.query(
                func.coalesce(
                    func.sum(LedgerEntry.amount),
                    0,
                )
            )
            .filter(
                LedgerEntry.account_id == account.id,
                LedgerEntry.entry_type == "Debit",
            )
            .scalar()
        )

        # Period debit and credit movements.
        period_debits = (
            db.query(
                func.coalesce(
                    func.sum(LedgerEntry.amount),
                    0,
                )
            )
            .join(
                Transaction,
                LedgerEntry.transaction_id == Transaction.id,
            )
            .filter(
                LedgerEntry.account_id == account.id,
                LedgerEntry.entry_type == "Debit",
                Transaction.created_at >= start,
                Transaction.created_at < end,
            )
            .scalar()
        )

        period_credits = (
            db.query(
                func.coalesce(
                    func.sum(LedgerEntry.amount),
                    0,
                )
            )
            .join(
                Transaction,
                LedgerEntry.transaction_id == Transaction.id,
            )
            .filter(
                LedgerEntry.account_id == account.id,
                LedgerEntry.entry_type == "Credit",
                Transaction.created_at >= start,
                Transaction.created_at < end,
            )
            .scalar()
        )

        period_debits = decimal_value(period_debits)
        period_credits = decimal_value(period_credits)

        total_debits += period_debits
        total_credits += period_credits

        balance = (
            decimal_value(balance_row)
            - decimal_value(debit_balance)
        )

        account_reports.append({
            "account_id": account.id,
            "account_code": account.name,
            "account_name": account.name,
            "account_type": account.account_type,
            "balance": money_string(balance),
            "period_debits": money_string(period_debits),
            "period_credits": money_string(period_credits),
        })

    return {
        "period": {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        },
        "accounts": account_reports,
        "total_debits": money_string(total_debits),
        "total_credits": money_string(total_credits),
    }


# --------------------------------------------------
# 3. MERCHANT SALES REPORT
# --------------------------------------------------

@router.get("/merchant-sales")
def merchant_sales(
    start_date: date = Query(
        default_factory=lambda: date.today() - timedelta(days=30)
    ),
    end_date: date = Query(
        default_factory=date.today
    ),
    db: Session = Depends(get_db),
    merchant=Depends(roles("Merchant")),
):
    start, end = date_range(start_date, end_date)

    merchant_wallet = (
        db.query(Wallet)
        .filter(Wallet.owner_id == merchant.id)
        .first()
    )

    if not merchant_wallet:
        raise HTTPException(
            404,
            "Merchant wallet not found",
        )

    # Payments credited to this merchant's wallet.
    payment_query = (
        db.query(
            Transaction,
            LedgerEntry.amount.label("merchant_credit"),
        )
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
            Transaction.status == "Success",
            LedgerEntry.entry_type == "Credit",
            Account.wallet_id == merchant_wallet.id,
            Transaction.created_at >= start,
            Transaction.created_at < end,
        )
    )

    payments = payment_query.all()

    total_sales = ZERO
    total_merchant_credits = ZERO

    daily = {}

    for payment, merchant_credit in payments:
        amount = decimal_value(payment.amount)
        credit = decimal_value(merchant_credit)

        total_sales += amount
        total_merchant_credits += credit

        day = payment.created_at.date().isoformat()

        if day not in daily:
            daily[day] = {
                "sales": ZERO,
                "payments": 0,
                "refunds": ZERO,
            }

        daily[day]["sales"] += amount
        daily[day]["payments"] += 1

    # Merchant-specific successful refunds in the period.
    refund_records = (
        db.query(MerchantRefund)
        .filter(
            MerchantRefund.merchant_id == merchant.id,
            MerchantRefund.status == "Success",
            MerchantRefund.created_at >= start,
            MerchantRefund.created_at < end,
        )
        .all()
    )

    total_refunds = ZERO

    for refund in refund_records:
        amount = decimal_value(refund.amount)
        total_refunds += amount

        day = refund.created_at.date().isoformat()

        if day not in daily:
            daily[day] = {
                "sales": ZERO,
                "payments": 0,
                "refunds": ZERO,
            }

        daily[day]["refunds"] += amount

    platform_fees = (
        total_sales - total_merchant_credits
    )

    net_received = (
        total_merchant_credits - total_refunds
    )

    return {
        "merchant_id": merchant.id,
        "merchant_name": (
            merchant.business_name or merchant.full_name
        ),
        "period": {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        },
        "currency": "INR",
        "total_sales": money_string(total_sales),
        "total_payments": len(payments),
        "total_refunds": money_string(total_refunds),
        "platform_fees_paid": money_string(platform_fees),
        "net_amount_received": money_string(net_received),
        "daily_sales": [
            {
                "date": day,
                "sales": money_string(values["sales"]),
                "payments": values["payments"],
                "refunds": money_string(values["refunds"]),
            }
            for day, values in sorted(daily.items())
        ],
    }


# --------------------------------------------------
# 4. USER MONTHLY SPENDING REPORT
# --------------------------------------------------

@router.get("/my-spending")
def my_spending(
    month: int = Query(default_factory=lambda: date.today().month, ge=1, le=12),
    year: int = Query(default_factory=lambda: date.today().year, ge=2000, le=2100),
    db: Session = Depends(get_db),
    user=Depends(roles("User")),
):
    start_date = date(year, month, 1)

    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)

    start = datetime.combine(start_date, time.min)
    end = datetime.combine(next_month, time.min)

    # Successful outgoing P2P transfers.
    transfers = (
        db.query(
            func.coalesce(func.sum(Transaction.amount), 0)
        )
        .filter(
            Transaction.initiated_by == user.id,
            Transaction.type == "P2P Transfer",
            Transaction.status == "Success",
            Transaction.created_at >= start,
            Transaction.created_at < end,
        )
        .scalar()
    )

    # Successful merchant payments made by this user.
    merchant_payments = (
        db.query(
            func.coalesce(func.sum(Transaction.amount), 0)
        )
        .filter(
            Transaction.initiated_by == user.id,
            Transaction.type == "Merchant Payment",
            Transaction.status == "Success",
            Transaction.created_at >= start,
            Transaction.created_at < end,
        )
        .scalar()
    )

    # Merchant payments broken down by business category.
    category_rows = (
        db.query(
            User.business_category,
            func.coalesce(
                func.sum(Transaction.amount),
                0,
            ),
        )
        .join(
            Wallet,
            Wallet.owner_id == User.id,
        )
        .join(
            Account,
            Account.wallet_id == Wallet.id,
        )
        .join(
            LedgerEntry,
            LedgerEntry.account_id == Account.id,
        )
        .join(
            Transaction,
            Transaction.id == LedgerEntry.transaction_id,
        )
        .filter(
            Transaction.initiated_by == user.id,
            Transaction.type == "Merchant Payment",
            Transaction.status == "Success",
            LedgerEntry.entry_type == "Credit",
            Transaction.created_at >= start,
            Transaction.created_at < end,
        )
        .group_by(User.business_category)
        .all()
    )

    # Successful withdrawals by this user.
    withdrawals = (
        db.query(
            func.coalesce(
                func.sum(Withdrawal.amount),
                0,
            )
        )
        .join(
            Transaction,
            Transaction.id == Withdrawal.transaction_id,
        )
        .filter(
            Transaction.initiated_by == user.id,
            Transaction.type == "Withdrawal",
            Transaction.status == "Success",
            Transaction.created_at >= start,
            Transaction.created_at < end,
        )
        .scalar()
    )

    # Successfully released cashback earned by this user.
    cashback_earned = (
        db.query(
            func.coalesce(
                func.sum(Cashback.amount),
                0,
            )
        )
        .filter(
            Cashback.user_id == user.id,
            Cashback.status.in_(PAID_CASHBACK_STATUSES),
            Cashback.created_at >= start,
            Cashback.created_at < end,
        )
        .scalar()
    )

    transfers = decimal_value(transfers)
    merchant_payments = decimal_value(merchant_payments)
    withdrawals = decimal_value(withdrawals)

    total_spending = (
        transfers + merchant_payments + withdrawals
    )

    return {
        "user_id": user.id,
        "month": month,
        "year": year,
        "currency": "INR",
        "total_spending": money_string(total_spending),
        "spending_by_type": {
            "transfers": money_string(transfers),
            "merchant_payments": money_string(
                merchant_payments
            ),
            "withdrawals": money_string(withdrawals),
        },
        "merchant_categories": {
            category or "Uncategorized": money_string(amount)
            for category, amount in category_rows
        },
        "cashback_earned": money_string(cashback_earned),
    }
