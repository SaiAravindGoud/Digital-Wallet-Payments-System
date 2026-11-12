
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.entities import User, Wallet, Role
from app.schemas import RegisterIn, LoginIn, PinIn, PinChangeIn
from app.security import (
    hash_password,
    verify_password,
    create_token,
    current_user,
    hash_pin,
    verify_transaction_pin,
)

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/register", status_code=201)
def register(data: RegisterIn, db: Session = Depends(get_db)):
    if data.role == "Merchant" and (
        not data.business_name or not data.business_category
    ):
        raise HTTPException(
            422,
            "Merchants must provide business_name and business_category"
        )

    existing = db.query(User).filter(
        (User.email == data.email) | (User.phone == data.phone)
    ).first()

    if existing:
        raise HTTPException(409, "Email or phone already registered")

    user = User(
        full_name=data.full_name,
        email=data.email,
        phone=data.phone,
        password_hash=hash_password(data.password),
        role=Role(data.role),
        business_name=data.business_name,
        business_category=data.business_category,
    )

    db.add(user)
    db.flush()

    # Generate a unique merchant code after obtaining the user ID.
    if user.role == Role.Merchant:
        user.merchant_code = f"MER-{user.id:05d}"

    wallet = Wallet(
        owner_id=user.id,
        wallet_number=f"WAL-{1000000 + user.id}",
        balance=0
    )

    db.add(wallet)
    db.commit()
    db.refresh(user)
    db.refresh(wallet)

    response = {
        "message": "Registration successful",
        "user_id": user.id,
        "wallet_number": wallet.wallet_number
    }

    if user.role == Role.Merchant:
        response["merchant_code"] = user.merchant_code

    return response


@router.post("/login")
def login(data: LoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(email=data.email).first()

    if not user or not verify_password(
        data.password, user.password_hash
    ):
        raise HTTPException(401, "Invalid email or password")

    if not user.is_active:
        raise HTTPException(403, "Inactive users cannot log in")

    return {
        "access_token": create_token(user.id),
        "token_type": "bearer",
        "expires_in_minutes": 30
    }


@router.get("/me")
def me(user=Depends(current_user)):
    response = {
        "id": user.id,
        "full_name": user.full_name,
        "email": user.email,
        "phone": user.phone,
        "role": user.role.value
    }

    if user.role == Role.Merchant:
        response["merchant_code"] = user.merchant_code
        response["business_name"] = user.business_name

    return response


@router.post("/set-pin")
def set_pin(
    data: PinIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user)
):
    if user.pin_hash:
        raise HTTPException(
            409,
            "Transaction PIN already exists. Use change-pin instead."
        )

    user.pin_hash = hash_pin(data.pin)
    user.pin_failed_attempts = 0
    user.pin_locked_until = None

    db.commit()

    return {"message": "Transaction PIN set successfully"}


@router.post("/change-pin")
def change_pin(
    data: PinChangeIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user)
):
    verify_transaction_pin(db, user, data.old_pin)

    if data.old_pin == data.new_pin:
        raise HTTPException(
            400,
            "New PIN must be different from the old PIN"
        )

    user.pin_hash = hash_pin(data.new_pin)
    user.pin_failed_attempts = 0
    user.pin_locked_until = None

    db.commit()

    return {"message": "Transaction PIN changed successfully"}
