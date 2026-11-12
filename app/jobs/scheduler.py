
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.entities import (
    Cashback,
    Wallet,
    Account,
    Withdrawal,
    Transaction,
    BankAccount,
    LedgerEntry,
    User,
)
from app.ledger import wallet_account, post_transaction
from app.email_service import send_email
from app.config import settings
from app.notifications import send_transaction_notification


logger = logging.getLogger("wallet.scheduler")

scheduler = BackgroundScheduler()


def reconciliation_job():
    """Audit double-entry ledger records and report imbalances."""

    db: Session = SessionLocal()

    try:
        ledger_rows = (
            db.query(
                LedgerEntry.transaction_id,
                LedgerEntry.entry_type,
                LedgerEntry.amount,
            )
            .order_by(LedgerEntry.transaction_id)
            .all()
        )

        totals = defaultdict(
            lambda: {
                "debit": Decimal("0.00"),
                "credit": Decimal("0.00"),
                "invalid_entries": 0,
            }
        )

        for row in ledger_rows:
            transaction_totals = totals[row.transaction_id]

            if row.entry_type == "Debit":
                transaction_totals["debit"] += row.amount
            elif row.entry_type == "Credit":
                transaction_totals["credit"] += row.amount
            else:
                transaction_totals["invalid_entries"] += 1

        discrepancies = []

        for transaction_id, values in totals.items():
            debit = values["debit"]
            credit = values["credit"]

            if (
                debit != credit
                or debit == Decimal("0.00")
                or credit == Decimal("0.00")
                or values["invalid_entries"] > 0
            ):
                discrepancies.append(
                    {
                        "transaction_id": transaction_id,
                        "debit": debit,
                        "credit": credit,
                        "invalid_entries": values["invalid_entries"],
                    }
                )

        logger.info(
            "Ledger reconciliation completed. "
            "Transactions audited: %s, discrepancies: %s",
            len(totals),
            len(discrepancies),
        )

        if not discrepancies:
            return

        transaction_ids = [
            item["transaction_id"] for item in discrepancies
        ]

        transactions = (
            db.query(Transaction)
            .filter(Transaction.id.in_(transaction_ids))
            .all()
        )

        transaction_refs = {
            tx.id: tx.transaction_ref for tx in transactions
        }

        report_lines = [
            "Digital Wallet Ledger Reconciliation Alert",
            "",
            f"Audit time: {datetime.utcnow()}",
            f"Discrepancies found: {len(discrepancies)}",
            "",
        ]

        for item in discrepancies[:100]:
            transaction_id = item["transaction_id"]
            reference = transaction_refs.get(
                transaction_id,
                f"Transaction ID {transaction_id}",
            )

            report_lines.append(
                f"Reference: {reference} | "
                f"Debit: {item['debit']} | "
                f"Credit: {item['credit']} | "
                f"Invalid entries: {item['invalid_entries']}"
            )

        if len(discrepancies) > 100:
            report_lines.append(
                f"\nAdditional discrepancies: "
                f"{len(discrepancies) - 100}"
            )

        report_lines.extend(
            [
                "",
                "No financial records were automatically modified.",
                "Please investigate these discrepancies.",
            ]
        )

        recipient = (
            settings.admin_email
            or settings.smtp_from
            or settings.smtp_username
        )

        if recipient:
            try:
                send_email(
                    recipient,
                    "Digital Wallet - Ledger Reconciliation Alert",
                    "\n".join(report_lines),
                )
            except Exception:
                logger.exception(
                    "Failed to send reconciliation alert"
                )
        else:
            logger.error(
                "No admin notification email is configured"
            )

        logger.error(
            "Ledger discrepancies detected: %s",
            len(discrepancies),
        )

    except Exception:
        logger.exception("Ledger reconciliation job failed")

    finally:
        db.close()


def release_pending_cashbacks():
    db: Session = SessionLocal()

    try:
        cutoff_time = datetime.utcnow() - timedelta(hours=24)

        pending_cashbacks = (
            db.query(Cashback)
            .filter(
                Cashback.status == "Pending",
                Cashback.created_at <= cutoff_time,
            )
            .order_by(Cashback.id)
            .with_for_update()
            .all()
        )

        if not pending_cashbacks:
            return

        cashback_pool = (
            db.query(Account)
            .filter_by(name="Cashback Pool")
            .first()
        )

        if not cashback_pool:
            logger.error("Cashback Pool account not found")
            return

        released_notifications = []
        released_count = 0

        for cashback in pending_cashbacks:
            try:
                wallet = (
                    db.query(Wallet)
                    .filter_by(owner_id=cashback.user_id)
                    .with_for_update()
                    .first()
                )

                if not wallet:
                    logger.error(
                        "Wallet not found for cashback ID %s",
                        cashback.id,
                    )
                    continue

                if wallet.status.value != "Active":
                    logger.info(
                        "Cashback ID %s postponed: wallet is not active",
                        cashback.id,
                    )
                    continue

                cashback_account = wallet_account(db, wallet.id)

                transaction = post_transaction(
                    db,
                    tx_type="Cashback",
                    amount=cashback.amount,
                    initiated_by=cashback.user_id,
                    debit=cashback_pool,
                    credit=cashback_account,
                    description=f"Cashback release #{cashback.id}",
                )

                wallet.balance += cashback.amount
                cashback.status = "Success"

                released_notifications.append(
                    (cashback.user_id, transaction.id)
                )

                released_count += 1

            except Exception:
                logger.exception(
                    "Failed to release cashback ID %s",
                    cashback.id,
                )
                raise

        db.commit()

        logger.info(
            "Released %s pending cashback records",
            released_count,
        )

        # Send notifications only after the financial commit.
        for user_id, transaction_id in released_notifications:
            try:
                customer = (
                    db.query(User)
                    .filter(User.id == user_id)
                    .first()
                )

                transaction = (
                    db.query(Transaction)
                    .filter(Transaction.id == transaction_id)
                    .first()
                )

                if customer and transaction:
                    send_transaction_notification(
                        customer,
                        transaction,
                        role="Customer",
                    )

            except Exception:
                logger.exception(
                    "Cashback notification failed for user ID %s",
                    user_id,
                )

    except Exception:
        db.rollback()
        logger.exception("Cashback release job failed")

    finally:
        db.close()


def process_pending_withdrawals():
    """Process withdrawals pending for at least ten minutes."""

    db: Session = SessionLocal()

    try:
        cutoff_time = datetime.utcnow() - timedelta(minutes=10)

        pending_ids = [
            row.id
            for row in (
                db.query(Withdrawal.id)
                .filter(
                    Withdrawal.status == "Pending",
                    Withdrawal.created_at <= cutoff_time,
                )
                .order_by(Withdrawal.id)
                .all()
            )
        ]

        for withdrawal_id in pending_ids:
            try:
                withdrawal = (
                    db.query(Withdrawal)
                    .filter(
                        Withdrawal.id == withdrawal_id,
                        Withdrawal.status == "Pending",
                    )
                    .with_for_update()
                    .first()
                )

                if not withdrawal:
                    db.rollback()
                    continue

                transaction = (
                    db.query(Transaction)
                    .filter(
                        Transaction.id == withdrawal.transaction_id
                    )
                    .with_for_update()
                    .first()
                )

                bank_account = (
                    db.query(BankAccount)
                    .filter(
                        BankAccount.id == withdrawal.bank_account_id
                    )
                    .first()
                )

                if not transaction or not bank_account:
                    logger.error(
                        "Missing transaction or bank account "
                        "for withdrawal %s",
                        withdrawal.id,
                    )
                    db.rollback()
                    continue

                wallet = (
                    db.query(Wallet)
                    .filter(
                        Wallet.owner_id == transaction.initiated_by
                    )
                    .with_for_update()
                    .first()
                )

                clearing_account = (
                    db.query(Account)
                    .filter_by(name="Withdrawal Clearing")
                    .first()
                )

                if not wallet or not clearing_account:
                    logger.error(
                        "Missing wallet or Withdrawal Clearing account "
                        "for withdrawal %s",
                        withdrawal.id,
                    )
                    db.rollback()
                    continue

                if bank_account.account_number.endswith("0000"):
                    user_account = wallet_account(db, wallet.id)

                    post_transaction(
                        db,
                        tx_type="Withdrawal Reversal",
                        amount=withdrawal.amount,
                        initiated_by=transaction.initiated_by,
                        debit=clearing_account,
                        credit=user_account,
                        description=(
                            f"Reversal of failed withdrawal "
                            f"#{withdrawal.id}"
                        ),
                    )

                    wallet.balance += withdrawal.amount

                    withdrawal.status = "Failed"
                    withdrawal.failure_reason = (
                        "Simulated bank processing failure"
                    )
                    transaction.status = "Reversed"

                    logger.info(
                        "Withdrawal %s failed; funds returned to wallet",
                        withdrawal.id,
                    )

                else:
                    bank_settlement = (
                        db.query(Account)
                        .filter_by(name="Bank Settlement")
                        .first()
                    )

                    if not bank_settlement:
                        logger.error(
                            "Bank Settlement account not found"
                        )
                        db.rollback()
                        continue

                    db.add_all(
                        [
                            LedgerEntry(
                                transaction_id=transaction.id,
                                account_id=clearing_account.id,
                                entry_type="Debit",
                                amount=withdrawal.amount,
                                balance_after=Decimal("0.00"),
                            ),
                            LedgerEntry(
                                transaction_id=transaction.id,
                                account_id=bank_settlement.id,
                                entry_type="Credit",
                                amount=withdrawal.amount,
                                balance_after=Decimal("0.00"),
                            ),
                        ]
                    )

                    withdrawal.status = "Success"
                    transaction.status = "Success"

                    logger.info(
                        "Withdrawal %s completed successfully",
                        withdrawal.id,
                    )

                withdrawal.processed_at = datetime.utcnow()

                # Capture IDs before commit for post-commit notification.
                customer_id = transaction.initiated_by
                committed_transaction_id = transaction.id

                db.commit()

                # Notify customer after the financial changes are committed.
                try:
                    customer = (
                        db.query(User)
                        .filter(User.id == customer_id)
                        .first()
                    )

                    committed_transaction = (
                        db.query(Transaction)
                        .filter(
                            Transaction.id == committed_transaction_id
                        )
                        .first()
                    )

                    if customer and committed_transaction:
                        send_transaction_notification(
                            customer,
                            committed_transaction,
                            role="Customer",
                        )

                except Exception:
                    logger.exception(
                        "Withdrawal notification failed for withdrawal %s",
                        withdrawal_id,
                    )

            except Exception:
                db.rollback()
                logger.exception(
                    "Failed to process withdrawal ID %s",
                    withdrawal_id,
                )

    except Exception:
        db.rollback()
        logger.exception("Withdrawal processing job failed")

    finally:
        db.close()


def start_scheduler():
    if scheduler.running:
        return

    scheduler.add_job(
        reconciliation_job,
        "cron",
        hour=2,
        id="reconciliation",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.add_job(
        release_pending_cashbacks,
        "interval",
        hours=1,
        id="cashback_release",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.add_job(
        process_pending_withdrawals,
        "interval",
        minutes=5,
        id="withdrawal_processing",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()

    logger.info("Digital Wallet background scheduler started")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Digital Wallet background scheduler stopped")
