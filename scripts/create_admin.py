
import sys
from pathlib import Path

# Add the project root to Python's import path.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import getpass
import re

from app.database import SessionLocal
from app.models.entities import User, Role
from app.security import hash_password


def main():
    print("\n=== Create Digital Wallet Admin ===\n")

    full_name = input("Admin full name: ").strip()
    email = input("Admin email: ").strip().lower()
    phone = input("Admin phone (10 digits): ").strip()

    if not full_name or len(full_name) < 2:
        print("Error: Enter a valid full name.")
        return

    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        print("Error: Enter a valid email address.")
        return

    if not re.fullmatch(r"\d{10}", phone):
        print("Error: Phone number must contain 10 digits.")
        return

    password = getpass.getpass("Admin password (minimum 8 characters): ")
    confirm = getpass.getpass("Confirm admin password: ")

    if len(password) < 8:
        print("Error: Password must contain at least 8 characters.")
        return

    if password != confirm:
        print("Error: Passwords do not match.")
        return

    db = SessionLocal()

    try:
        existing = db.query(User).filter(
            (User.email == email) | (User.phone == phone)
        ).first()

        if existing:
            print("Error: This email or phone is already registered.")
            return

        admin = User(
            full_name=full_name,
            email=email,
            phone=phone,
            password_hash=hash_password(password),
            role=Role.Admin,
            is_active=True
        )

        db.add(admin)
        db.commit()
        db.refresh(admin)

        print("\nAdmin account created successfully.")
        print("Admin ID:", admin.id)
        print("Admin email:", admin.email)
        print("Role:", admin.role.value)

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()


if __name__ == "__main__":
    main()
