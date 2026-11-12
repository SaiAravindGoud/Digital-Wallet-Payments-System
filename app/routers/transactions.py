
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from io import BytesIO
from html import escape

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import asc, desc
from sqlalchemy.orm import Session

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from app.database import get_db
from app.models.entities import (
    Transaction,
    Wallet,
    LedgerEntry,
    Account,
)
from app.security import current_user


router = APIRouter(
    prefix="/transactions",
    tags=["Transactions"],
)


def get_user_wallet(db: Session, user_id: int):
    wallet = db.query(Wallet).filter_by(
        owner_id=user_id
    ).first()

    if not wallet:
        raise HTTPException(
            status_code=404,
            detail="Wallet not found",
        )

    accounts = db.query(Account).filter_by(
        wallet_id=wallet.id
    ).all()

    if not accounts:
        raise HTTPException(
            status_code=404,
            detail="Wallet ledger account not found",
        )

    return wallet, accounts


def validate_statement_dates(
    start_date: date | None,
    end_date: date | None,
):
    if start_date and end_date and start_date > end_date:
        raise HTTPException(
            status_code=422,
            detail="Start date cannot be after end date",
        )


def get_statement_data(
    db: Session,
    user,
    start_date: date | None = None,
    end_date: date | None = None,
):
    validate_statement_dates(start_date, end_date)

    wallet, accounts = get_user_wallet(db, user.id)

    account_ids = [account.id for account in accounts]

    query = (
        db.query(LedgerEntry, Transaction)
        .join(
            Transaction,
            Transaction.id == LedgerEntry.transaction_id,
        )
        .filter(
            LedgerEntry.account_id.in_(account_ids)
        )
    )

    if start_date:
        start_datetime = datetime.combine(
            start_date,
            time.min,
        )

        query = query.filter(
            Transaction.created_at >= start_datetime
        )

    if end_date:
        end_exclusive = datetime.combine(
            end_date + timedelta(days=1),
            time.min,
        )

        query = query.filter(
            Transaction.created_at < end_exclusive
        )

    rows = (
        query
        .order_by(
            Transaction.created_at.asc(),
            Transaction.id.asc(),
            LedgerEntry.id.asc(),
        )
        .all()
    )

    opening_balance = Decimal("0.00")

    if start_date:
        start_datetime = datetime.combine(
            start_date,
            time.min,
        )

        previous_entries = (
            db.query(LedgerEntry)
            .join(
                Transaction,
                Transaction.id == LedgerEntry.transaction_id,
            )
            .filter(
                LedgerEntry.account_id.in_(account_ids),
                Transaction.created_at < start_datetime,
            )
            .all()
        )

        for entry in previous_entries:
            amount = Decimal(entry.amount)

            if entry.entry_type == "Credit":
                opening_balance += amount
            elif entry.entry_type == "Debit":
                opening_balance -= amount

    running_balance = opening_balance
    grouped = {}

    for entry, tx in rows:
        if tx.id not in grouped:
            grouped[tx.id] = {
                "transaction_ref": tx.transaction_ref,
                "type": tx.type,
                "status": tx.status,
                "description": tx.description or "",
                "date": (
                    tx.created_at.isoformat()
                    if tx.created_at else None
                ),
                "debit": Decimal("0.00"),
                "credit": Decimal("0.00"),
            }

        amount = Decimal(entry.amount)

        if entry.entry_type == "Debit":
            grouped[tx.id]["debit"] += amount
        elif entry.entry_type == "Credit":
            grouped[tx.id]["credit"] += amount

    transactions = []

    for item in grouped.values():
        running_balance += item["credit"]
        running_balance -= item["debit"]

        transactions.append({
            "transaction_ref": item["transaction_ref"],
            "type": item["type"],
            "status": item["status"],
            "description": item["description"],
            "date": item["date"],
            "debit": str(item["debit"]),
            "credit": str(item["credit"]),
            "balance": str(running_balance),
        })

    return {
        "wallet_id": wallet.id,
        "account_holder": user.full_name,
        "start_date": (
            start_date.isoformat() if start_date else None
        ),
        "end_date": (
            end_date.isoformat() if end_date else None
        ),
        "opening_balance": str(opening_balance),
        "closing_balance": str(running_balance),
        "total_transactions": len(transactions),
        "transactions": transactions,
    }


@router.get("/statement")
def account_statement(
    start_date: date | None = Query(
        default=None,
        description="Statement start date (YYYY-MM-DD)",
    ),
    end_date: date | None = Query(
        default=None,
        description="Statement end date (YYYY-MM-DD)",
    ),
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    return get_statement_data(
        db,
        user,
        start_date,
        end_date,
    )


@router.get("/statement/pdf")
def download_account_statement(
    start_date: date | None = Query(
        default=None,
        description="Statement start date (YYYY-MM-DD)",
    ),
    end_date: date | None = Query(
        default=None,
        description="Statement end date (YYYY-MM-DD)",
    ),
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    statement = get_statement_data(
        db,
        user,
        start_date,
        end_date,
    )

    buffer = BytesIO()

    document = SimpleDocTemplate(
        buffer,
        pagesize=landscape(letter),
        rightMargin=15 * mm,
        leftMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "StatementTitle",
        parent=styles["Title"],
        alignment=TA_CENTER,
        fontSize=18,
        spaceAfter=12,
    )

    normal_style = styles["Normal"]
    elements = []

    elements.append(
        Paragraph(
            "DIGITAL WALLET ACCOUNT STATEMENT",
            title_style,
        )
    )

    elements.append(
        Paragraph(
            f"Account Holder: {escape(str(statement['account_holder']))}",
            normal_style,
        )
    )

    elements.append(
        Paragraph(
            f"Wallet ID: {statement['wallet_id']}",
            normal_style,
        )
    )

    elements.append(
        Paragraph(
            f"Period: {statement['start_date'] or 'Beginning'} "
            f"to {statement['end_date'] or 'Present'}",
            normal_style,
        )
    )

    elements.append(Spacer(1, 10))

    summary_data = [
        ["Opening Balance", "Closing Balance", "Total Transactions"],
        [
            f"Rs. {statement['opening_balance']}",
            f"Rs. {statement['closing_balance']}",
            str(statement["total_transactions"]),
        ],
    ]

    summary_table = Table(
        summary_data,
        colWidths=[75 * mm, 75 * mm, 75 * mm],
    )

    summary_table.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("PADDING", (0, 0), (-1, -1), 7),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ])
    )

    elements.append(summary_table)
    elements.append(Spacer(1, 15))

    table_data = [
        [
            "Date",
            "Reference",
            "Type",
            "Status",
            "Debit (Rs.)",
            "Credit (Rs.)",
            "Balance (Rs.)",
        ]
    ]

    for tx in statement["transactions"]:
        transaction_date = (
            tx["date"][:19].replace("T", " ")
            if tx["date"]
            else ""
        )

        table_data.append([
            transaction_date,
            tx["transaction_ref"],
            tx["type"],
            tx["status"],
            tx["debit"],
            tx["credit"],
            tx["balance"],
        ])

    if len(table_data) == 1:
        table_data.append([
            "No transactions",
            "",
            "",
            "",
            "",
            "",
            "",
        ])

    transaction_table = Table(
        table_data,
        repeatRows=1,
        colWidths=[
            35 * mm,
            42 * mm,
            32 * mm,
            25 * mm,
            28 * mm,
            28 * mm,
            30 * mm,
        ],
    )

    transaction_table.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#263B5A")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("PADDING", (0, 0), (-1, -1), 5),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ])
    )

    elements.append(transaction_table)
    elements.append(Spacer(1, 12))

    elements.append(
        Paragraph(
            "This statement is generated electronically by the "
            "Digital Wallet &amp; Payments System.",
            normal_style,
        )
    )

    document.build(elements)
    buffer.seek(0)

    return StreamingResponse(
        buffer,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                'attachment; filename="wallet-statement.pdf"'
            )
        },
    )


@router.get("")
def list_transactions(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    search: str | None = Query(default=None, max_length=40),
    transaction_type: str | None = Query(
        default=None,
        alias="type",
        max_length=40,
    ),
    status: str | None = Query(
        default=None,
        max_length=20,
    ),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    sort_by: str = Query(
        default="date",
        pattern=r"^(date|amount)$",
    ),
    sort_order: str = Query(
        default="desc",
        pattern=r"^(asc|desc)$",
    ),
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    if start_date and end_date and start_date > end_date:
        raise HTTPException(
            status_code=422,
            detail="Start date cannot be after end date",
        )

    wallet, accounts = get_user_wallet(db, user.id)
    account_ids = [account.id for account in accounts]

    query = (
        db.query(Transaction)
        .join(
            LedgerEntry,
            LedgerEntry.transaction_id == Transaction.id,
        )
        .filter(
            LedgerEntry.account_id.in_(account_ids)
        )
        .distinct()
    )

    if search:
        query = query.filter(
            Transaction.transaction_ref.ilike(
                f"%{search.strip()}%"
            )
        )

    if transaction_type:
        query = query.filter(
            Transaction.type == transaction_type.strip()
        )

    if status:
        query = query.filter(
            Transaction.status == status.strip()
        )

    if start_date:
        start_datetime = datetime.combine(
            start_date,
            time.min,
        )
        query = query.filter(
            Transaction.created_at >= start_datetime
        )

    if end_date:
        end_exclusive = datetime.combine(
            end_date + timedelta(days=1),
            time.min,
        )
        query = query.filter(
            Transaction.created_at < end_exclusive
        )

    total = query.count()

    sort_column = (
        Transaction.amount
        if sort_by == "amount"
        else Transaction.created_at
    )

    ordering = (
        asc(sort_column)
        if sort_order == "asc"
        else desc(sort_column)
    )

    rows = (
        query
        .order_by(ordering, Transaction.id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )

    return {
        "total": total,
        "skip": skip,
        "limit": limit,
        "has_more": skip + len(rows) < total,
        "transactions": [
            {
                "transaction_ref": tx.transaction_ref,
                "type": tx.type,
                "amount": str(tx.amount),
                "status": tx.status,
                "description": tx.description,
                "date": (
                    tx.created_at.isoformat()
                    if tx.created_at else None
                ),
                "is_flagged": tx.is_flagged,
            }
            for tx in rows
        ],
    }


@router.get("/{transaction_ref}")
def transaction_detail(
    transaction_ref: str,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    wallet = db.query(Wallet).filter_by(
        owner_id=user.id
    ).first()

    if not wallet:
        raise HTTPException(
            status_code=404,
            detail="Transaction not found",
        )

    tx = (
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
            Account.wallet_id == wallet.id,
            Transaction.transaction_ref == transaction_ref,
        )
        .first()
    )

    if not tx:
        raise HTTPException(
            status_code=404,
            detail="Transaction not found",
        )

    return {
        "transaction_ref": tx.transaction_ref,
        "type": tx.type,
        "amount": str(tx.amount),
        "status": tx.status,
        "description": tx.description,
        "date": tx.created_at.isoformat(),
    }
