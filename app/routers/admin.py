
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.entities import (
    User,
    Wallet,
    KYC,
    KycLevel,
    WalletStatus,
    Transaction,
)
from app.security import roles


router = APIRouter(prefix="/admin", tags=["Admin"])


@router.get("/kyc")
def pending_kyc(
    db: Session = Depends(get_db),
    admin=Depends(roles("Admin"))
):
    applications = (
        db.query(KYC)
        .filter_by(status="Pending")
        .order_by(KYC.created_at.asc())
        .all()
    )

    return [
        {
            "id": item.id,
            "user_id": item.user_id,
            "full_name": item.full_name,
            "pan_last4": item.pan_number[-4:],
            "status": item.status,
            "created_at": item.created_at.isoformat()
        }
        for item in applications
    ]


@router.put("/kyc/{kyc_id}/approve")
def approve_kyc(
    kyc_id: int,
    db: Session = Depends(get_db),
    admin=Depends(roles("Admin"))
):
    try:
        item = (
            db.query(KYC)
            .filter(KYC.id == kyc_id)
            .with_for_update()
            .first()
        )

        if not item:
            raise HTTPException(404, "KYC not found")

        if item.status != "Pending":
            raise HTTPException(
                409,
                f"KYC application is already {item.status}"
            )

        wallet = (
            db.query(Wallet)
            .filter_by(owner_id=item.user_id)
            .with_for_update()
            .first()
        )

        if not wallet:
            raise HTTPException(404, "User wallet not found")

        item.status = "Approved"
        item.rejection_reason = None
        wallet.kyc_level = KycLevel.Full

        db.commit()

        return {
            "message": "KYC approved successfully",
            "kyc_id": item.id,
            "user_id": item.user_id,
            "kyc_level": "Full"
        }

    except Exception:
        db.rollback()
        raise


@router.put("/kyc/{kyc_id}/reject")
def reject_kyc(
    kyc_id: int,
    reason: str,
    db: Session = Depends(get_db),
    admin=Depends(roles("Admin"))
):
    if not reason.strip():
        raise HTTPException(422, "Rejection reason is required")

    try:
        item = (
            db.query(KYC)
            .filter(KYC.id == kyc_id)
            .with_for_update()
            .first()
        )

        if not item:
            raise HTTPException(404, "KYC not found")

        if item.status != "Pending":
            raise HTTPException(
                409,
                f"KYC application is already {item.status}"
            )

        item.status = "Rejected"
        item.rejection_reason = reason.strip()

        db.commit()

        return {
            "message": "KYC rejected",
            "kyc_id": item.id,
            "reason": item.rejection_reason
        }

    except Exception:
        db.rollback()
        raise


@router.put("/wallets/{wallet_id}/freeze")
def freeze_wallet(
    wallet_id: int,
    reason: str,
    db: Session = Depends(get_db),
    admin=Depends(roles("Admin"))
):
    if not reason.strip():
        raise HTTPException(422, "Reason is required")

    wallet = (
        db.query(Wallet)
        .filter(Wallet.id == wallet_id)
        .with_for_update()
        .first()
    )

    if not wallet:
        raise HTTPException(404, "Wallet not found")

    if wallet.status == WalletStatus.Frozen:
        raise HTTPException(409, "Wallet is already frozen")

    wallet.status = WalletStatus.Frozen
    db.commit()

    return {
        "message": "Wallet frozen",
        "wallet_id": wallet.id,
        "reason": reason.strip()
    }


@router.put("/wallets/{wallet_id}/unfreeze")
def unfreeze_wallet(
    wallet_id: int,
    db: Session = Depends(get_db),
    admin=Depends(roles("Admin"))
):
    wallet = (
        db.query(Wallet)
        .filter(Wallet.id == wallet_id)
        .with_for_update()
        .first()
    )

    if not wallet:
        raise HTTPException(404, "Wallet not found")

    if wallet.status == WalletStatus.Active:
        raise HTTPException(409, "Wallet is already active")

    wallet.status = WalletStatus.Active
    db.commit()

    return {
        "message": "Wallet unfrozen",
        "wallet_id": wallet.id
    }


@router.get("/transactions/flagged")
def flagged_transactions(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    admin=Depends(roles("Admin"))
):
    transactions = (
        db.query(Transaction)
        .filter(Transaction.is_flagged.is_(True))
        .order_by(Transaction.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )

    return [
        {
            "id": item.id,
            "transaction_ref": item.transaction_ref,
            "type": item.type,
            "amount": str(item.amount),
            "status": item.status,
            "initiated_by": item.initiated_by,
            "description": item.description,
            "is_flagged": item.is_flagged,
            "created_at": (
                item.created_at.isoformat()
                if item.created_at else None
            )
        }
        for item in transactions
    ]
