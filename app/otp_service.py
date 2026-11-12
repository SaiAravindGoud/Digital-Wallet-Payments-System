
import secrets
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.email_service import send_email
from app.models.entities import OTP
from app.security import hash_password, verify_password


OTP_EXPIRY_MINUTES = 5
OTP_RESEND_SECONDS = 60
OTP_MAX_ATTEMPTS = 3


def create_and_send_otp(
    db: Session,
    user_id: int,
    email: str,
    purpose: str,
):
    now = datetime.utcnow()

    latest_otp = (
        db.query(OTP)
        .filter(
            OTP.user_id == user_id,
            OTP.purpose == purpose,
        )
        .order_by(OTP.created_at.desc())
        .first()
    )

    if latest_otp:
        elapsed = (now - latest_otp.created_at).total_seconds()

        if elapsed < OTP_RESEND_SECONDS:
            remaining = int(OTP_RESEND_SECONDS - elapsed) + 1
            raise HTTPException(
                status_code=429,
                detail=f"Please wait {remaining} seconds before requesting another OTP",
            )

    otp_code = f"{secrets.randbelow(1000000):06d}"

    otp_record = OTP(
        user_id=user_id,
        purpose=purpose,
        otp_hash=hash_password(otp_code),
        expires_at=now + timedelta(minutes=OTP_EXPIRY_MINUTES),
        attempts=0,
        is_used=False,
    )

    db.add(otp_record)

    try:
        db.flush()

        email_body = (
            f"Your Digital Wallet verification OTP is: {otp_code}\n\n"
            f"This OTP expires in {OTP_EXPIRY_MINUTES} minutes.\n"
            "You have a maximum of 3 verification attempts.\n"
            "Do not share this OTP with anyone."
        )

        send_email(
            to_email=email,
            subject="Digital Wallet - Verification OTP",
            body=email_body,
        )

        db.commit()

    except RuntimeError:
        db.rollback()
        raise HTTPException(
            status_code=503,
            detail="Unable to send OTP email. Please try again later.",
        )

    except Exception:
        db.rollback()
        raise

    return {
        "message": "OTP sent successfully to your registered email",
        "expires_in_seconds": OTP_EXPIRY_MINUTES * 60,
        "resend_after_seconds": OTP_RESEND_SECONDS,
    }


def verify_otp(
    db: Session,
    user_id: int,
    purpose: str,
    otp_code: str,
):
    now = datetime.utcnow()

    otp_record = (
        db.query(OTP)
        .filter(
            OTP.user_id == user_id,
            OTP.purpose == purpose,
            OTP.is_used.is_(False),
        )
        .order_by(OTP.created_at.desc())
        .first()
    )

    if not otp_record:
        raise HTTPException(
            status_code=400,
            detail="No valid OTP found. Please request a new OTP.",
        )

    if otp_record.expires_at <= now:
        otp_record.is_used = True
        db.commit()

        raise HTTPException(
            status_code=400,
            detail="OTP has expired. Please request a new OTP.",
        )

    if otp_record.attempts >= OTP_MAX_ATTEMPTS:
        otp_record.is_used = True
        db.commit()

        raise HTTPException(
            status_code=429,
            detail="Maximum OTP attempts reached. Please request a new OTP.",
        )

    if not verify_password(otp_code, otp_record.otp_hash):
        otp_record.attempts += 1

        if otp_record.attempts >= OTP_MAX_ATTEMPTS:
            otp_record.is_used = True
            db.commit()

            raise HTTPException(
                status_code=429,
                detail="Maximum OTP attempts reached. Please request a new OTP.",
            )

        remaining = OTP_MAX_ATTEMPTS - otp_record.attempts
        db.commit()

        raise HTTPException(
            status_code=400,
            detail=f"Invalid OTP. {remaining} attempt(s) remaining.",
        )

    otp_record.is_used = True
    db.commit()

    return True
