
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.entities import BankAccount, User, Withdrawal
from app.schemas import BankIn
from app.security import current_user
from app.otp_service import create_and_send_otp, verify_otp


router = APIRouter(
    prefix="/bank-accounts",
    tags=["Bank Accounts"],
)

MAX_BANK_ACCOUNTS = 3


def mask_account_number(account_number: str) -> str:
    """Show only the last four digits of a bank account number."""
    return "*" * (len(account_number) - 4) + account_number[-4:]


@router.post("/request-otp")
def request_bank_account_otp(
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    return create_and_send_otp(
        db=db,
        user_id=user.id,
        email=user.email,
        purpose="bank_account_link",
    )


@router.post("", status_code=status.HTTP_201_CREATED)
def add_bank_account(
    data: BankIn,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    try:
        # Lock the user row to reduce concurrent account-limit bypass.
        db.query(User).filter(User.id == user.id).with_for_update().first()

        existing = (
            db.query(BankAccount)
            .filter(
                BankAccount.user_id == user.id,
                BankAccount.account_number == data.account_number,
                BankAccount.ifsc_code == data.ifsc_code,
            )
            .first()
        )

        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This bank account is already linked",
            )

        account_count = (
            db.query(BankAccount)
            .filter(BankAccount.user_id == user.id)
            .count()
        )

        if account_count >= MAX_BANK_ACCOUNTS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Maximum 3 bank accounts allowed",
            )

        # Verify OTP after business validations.
        verify_otp(
            db=db,
            user_id=user.id,
            purpose="bank_account_link",
            otp_code=data.otp,
        )

        bank_account = BankAccount(
            user_id=user.id,
            account_holder_name=data.account_holder_name,
            account_number=data.account_number,
            ifsc_code=data.ifsc_code,
            bank_name=data.bank_name,
            is_primary=(account_count == 0),
        )

        db.add(bank_account)
        db.commit()
        db.refresh(bank_account)

        return {
            "message": "Bank account linked successfully",
            "id": bank_account.id,
            "account_holder_name": bank_account.account_holder_name,
            "account_number": mask_account_number(
                bank_account.account_number
            ),
            "ifsc_code": bank_account.ifsc_code,
            "bank_name": bank_account.bank_name,
            "is_primary": bank_account.is_primary,
        }

    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to link bank account",
        )


@router.get("")
def get_bank_accounts(
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    accounts = (
        db.query(BankAccount)
        .filter(BankAccount.user_id == user.id)
        .order_by(BankAccount.id.desc())
        .all()
    )

    return [
        {
            "id": account.id,
            "account_holder_name": account.account_holder_name,
            "account_number": mask_account_number(
                account.account_number
            ),
            "ifsc_code": account.ifsc_code,
            "bank_name": account.bank_name,
            "is_primary": account.is_primary,
        }
        for account in accounts
    ]


@router.delete("/{bank_account_id}")
def delete_bank_account(
    bank_account_id: int,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    account = (
        db.query(BankAccount)
        .filter(
            BankAccount.id == bank_account_id,
            BankAccount.user_id == user.id,
        )
        .with_for_update()
        .first()
    )

    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Bank account not found",
        )

    pending_withdrawal = (
        db.query(Withdrawal)
        .filter(
            Withdrawal.bank_account_id == account.id,
            Withdrawal.status == "Pending",
        )
        .first()
    )

    if pending_withdrawal:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete a bank account with a pending withdrawal",
        )

    was_primary = account.is_primary

    try:
        db.delete(account)
        db.flush()

        replacement = None

        if was_primary:
            replacement = (
                db.query(BankAccount)
                .filter(BankAccount.user_id == user.id)
                .order_by(BankAccount.id.desc())
                .first()
            )

            if replacement:
                replacement.is_primary = True

        db.commit()

        return {
            "message": "Bank account deleted successfully",
            "deleted_bank_account_id": bank_account_id,
            "new_primary_bank_account_id": (
                replacement.id if replacement else None
            ),
        }

    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to delete bank account",
        )


@router.put("/{bank_account_id}/primary")
def set_primary_bank_account(
    bank_account_id: int,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    account = (
        db.query(BankAccount)
        .filter(
            BankAccount.id == bank_account_id,
            BankAccount.user_id == user.id,
        )
        .first()
    )

    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Bank account not found",
        )

    try:
        (
            db.query(BankAccount)
            .filter(BankAccount.user_id == user.id)
            .update(
                {BankAccount.is_primary: False},
                synchronize_session=False,
            )
        )

        account.is_primary = True
        db.commit()

    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to update primary bank account",
        )

    return {
        "message": "Primary bank account updated successfully",
        "bank_account_id": account.id,
        "is_primary": account.is_primary,
    }
