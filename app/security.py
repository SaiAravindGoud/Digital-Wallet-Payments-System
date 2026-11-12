
from datetime import datetime, timedelta
from jose import jwt, JWTError
from passlib.context import CryptContext
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.entities import User


pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer = HTTPBearer()
ALGORITHM = "HS256"


def hash_password(value: str) -> str:
    return pwd.hash(value)


def verify_password(value: str, hashed: str) -> bool:
    return pwd.verify(value, hashed)


def hash_pin(value: str) -> str:
    return pwd.hash(value)


def verify_pin(value: str, hashed: str) -> bool:
    return pwd.verify(value, hashed)


def create_token(user_id: int):
    exp = datetime.utcnow() + timedelta(
        minutes=settings.access_token_expire_minutes
    )
    return jwt.encode(
        {"sub": str(user_id), "exp": exp},
        settings.secret_key,
        algorithm=ALGORITHM
    )


def current_user(
    creds: HTTPAuthorizationCredentials = Depends(bearer),
    db: Session = Depends(get_db)
):
    try:
        payload = jwt.decode(
            creds.credentials,
            settings.secret_key,
            algorithms=[ALGORITHM]
        )
        user_id = int(payload["sub"])
    except (JWTError, ValueError, KeyError):
        raise HTTPException(401, "Invalid or expired token")

    user = db.get(User, user_id)

    if not user or not user.is_active:
        raise HTTPException(401, "User is inactive or unavailable")

    return user


def verify_transaction_pin(db: Session, user: User, entered_pin: str):
    now = datetime.utcnow()

    if user.pin_locked_until and user.pin_locked_until > now:
        remaining = int((user.pin_locked_until - now).total_seconds())
        raise HTTPException(
            423,
            f"Payments are locked. Try again in {remaining} seconds."
        )

    if user.pin_locked_until and user.pin_locked_until <= now:
        user.pin_locked_until = None
        user.pin_failed_attempts = 0

    if not user.pin_hash:
        raise HTTPException(
            403,
            "Please set your transaction PIN before making payments."
        )

    if not verify_pin(entered_pin, user.pin_hash):
        user.pin_failed_attempts += 1

        if user.pin_failed_attempts >= 3:
            user.pin_failed_attempts = 0
            user.pin_locked_until = now + timedelta(minutes=30)
            db.commit()
            raise HTTPException(
                423,
                "Three incorrect PIN attempts. Payments locked for 30 minutes."
            )

        db.commit()
        raise HTTPException(
            400,
            f"Incorrect transaction PIN. "
            f"{3 - user.pin_failed_attempts} attempt(s) remaining."
        )

    user.pin_failed_attempts = 0
    db.commit()


def roles(*allowed):
    def checker(user=Depends(current_user)):
        if user.role.value not in allowed:
            raise HTTPException(403, "You do not have permission")
        return user

    return checker
