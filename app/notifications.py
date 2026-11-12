
import logging

from app.email_service import send_email


logger = logging.getLogger(__name__)


def send_transaction_notification(
    user,
    transaction,
    role="customer",
):
    """
    Send a transaction email without affecting financial processing.
    """

    if not user or not user.email:
        logger.warning("Transaction notification skipped: no email address")
        return False

    subject = "Digital Wallet - Transaction Notification"

    body = f"""
Hello {user.full_name},

A wallet transaction has been processed.

Transaction Type: {transaction.type}
Transaction Reference: {transaction.transaction_ref}
Amount: Rs. {transaction.amount}
Status: {transaction.status}
Account Role: {role}

Please keep this email for your records.

If you did not authorize this transaction, please contact support.

Regards,
Digital Wallet & Payments System
"""

    try:
        send_email(
            to_email=user.email,
            subject=subject,
            body=body,
        )
        return True

    except Exception:
        logger.exception(
            "Transaction notification failed for user ID %s",
            user.id,
        )
        return False
