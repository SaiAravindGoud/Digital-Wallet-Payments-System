from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.entities import Cashback, Transaction, User
from app.security import current_user


router = APIRouter(prefix="/cashbacks", tags=["Cashbacks"])


@router.get("")
def get_cashbacks(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    query = (
        db.query(Cashback, Transaction.transaction_ref)
        .join(
            Transaction,
            Cashback.transaction_id == Transaction.id,
        )
        .filter(Cashback.user_id == user.id)
    )

    total = query.count()

    records = (
        query
        .order_by(Cashback.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "cashbacks": [
            {
                "id": cashback.id,
                "transaction_ref": transaction_ref,
                "amount": str(cashback.amount),
                "status": cashback.status,
                "created_at": cashback.created_at.isoformat(),
            }
            for cashback, transaction_ref in records
        ],
    }