
from datetime import datetime
from decimal import Decimal
from sqlalchemy import (
    String,
    Integer,
    Boolean,
    DateTime,
    ForeignKey,
    Numeric,
    Text,
    Enum,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base
import enum


class Role(str, enum.Enum):
    Admin = "Admin"
    User = "User"
    Merchant = "Merchant"


class WalletStatus(str, enum.Enum):
    Active = "Active"
    Frozen = "Frozen"


class KycLevel(str, enum.Enum):
    Minimum = "Minimum"
    Full = "Full"


class TxStatus(str, enum.Enum):
    Pending = "Pending"
    Success = "Success"
    Failed = "Failed"
    On_Hold = "On Hold"
    Reversed = "Reversed"


class TxType(str, enum.Enum):
    Add_Money = "Add Money"
    P2P_Transfer = "P2P Transfer"
    Merchant_Payment = "Merchant Payment"
    Merchant_Refund = "Merchant Refund"
    Cashback = "Cashback"
    Withdrawal = "Withdrawal"
    Withdrawal_Reversal = "Withdrawal Reversal"
    Reversal = "Reversal"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    phone: Mapped[str] = mapped_column(String(10), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.User)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    pin_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pin_failed_attempts: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    pin_locked_until: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )

    business_name: Mapped[str | None] = mapped_column(
        String(150),
        nullable=True,
    )
    business_category: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    merchant_code: Mapped[str | None] = mapped_column(
        String(20),
        unique=True,
        nullable=True,
    )

    wallet: Mapped["Wallet"] = relationship(
        back_populates="owner",
        uselist=False,
    )
    kyc: Mapped["KYC | None"] = relationship(
        back_populates="user",
        uselist=False,
    )


class Wallet(Base, TimestampMixin):
    __tablename__ = "wallets"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    wallet_number: Mapped[str] = mapped_column(String(20), unique=True)
    balance: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        default=Decimal("0.00"),
    )
    kyc_level: Mapped[KycLevel] = mapped_column(
        Enum(KycLevel),
        default=KycLevel.Minimum,
    )
    status: Mapped[WalletStatus] = mapped_column(
        Enum(WalletStatus),
        default=WalletStatus.Active,
    )

    owner: Mapped[User] = relationship(back_populates="wallet")


class KYC(Base, TimestampMixin):
    __tablename__ = "kyc"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    pan_number: Mapped[str] = mapped_column(String(10), unique=True)
    aadhaar_last4: Mapped[str] = mapped_column(String(4))
    full_name: Mapped[str] = mapped_column(String(120))
    date_of_birth: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="Pending")
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped[User] = relationship(back_populates="kyc")


class Account(Base, TimestampMixin):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_type: Mapped[str] = mapped_column(String(40))
    wallet_id: Mapped[int | None] = mapped_column(
        ForeignKey("wallets.id"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(100), unique=True)


class Transaction(Base, TimestampMixin):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_ref: Mapped[str] = mapped_column(
        String(40),
        unique=True,
        index=True,
    )
    type: Mapped[str] = mapped_column(String(40))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    status: Mapped[str] = mapped_column(String(20))
    initiated_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    description: Mapped[str] = mapped_column(String(255), default="")
    is_flagged: Mapped[bool] = mapped_column(Boolean, default=False)

    ledger_entries: Mapped[list["LedgerEntry"]] = relationship(
        back_populates="transaction",
    )


class LedgerEntry(Base, TimestampMixin):
    __tablename__ = "ledger_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    entry_type: Mapped[str] = mapped_column(String(10))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    balance_after: Mapped[Decimal] = mapped_column(Numeric(12, 2))

    transaction: Mapped[Transaction] = relationship(
        back_populates="ledger_entries",
    )


class IdempotencyRecord(Base, TimestampMixin):
    __tablename__ = "idempotency_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(100), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    endpoint: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="Processing")


class BankAccount(Base, TimestampMixin):
    __tablename__ = "bank_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    account_holder_name: Mapped[str] = mapped_column(String(120))
    account_number: Mapped[str] = mapped_column(String(18))
    ifsc_code: Mapped[str] = mapped_column(String(11))
    bank_name: Mapped[str] = mapped_column(String(100))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)


class MoneyRequest(Base, TimestampMixin):
    __tablename__ = "money_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    requester_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    payer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    note: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(20), default="Pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class Cashback(Base, TimestampMixin):
    __tablename__ = "cashbacks"

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    status: Mapped[str] = mapped_column(String(20), default="Pending")


class MerchantRefund(Base, TimestampMixin):
    __tablename__ = "merchant_refunds"

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"))
    merchant_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    status: Mapped[str] = mapped_column(String(20), default="Success")


class Withdrawal(Base, TimestampMixin):
    __tablename__ = "withdrawals"

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"))
    bank_account_id: Mapped[int] = mapped_column(ForeignKey("bank_accounts.id"))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    status: Mapped[str] = mapped_column(String(20), default="Pending")
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class OTP(Base, TimestampMixin):
    __tablename__ = "otps"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    purpose: Mapped[str] = mapped_column(String(40))
    otp_hash: Mapped[str] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    is_used: Mapped[bool] = mapped_column(Boolean, default=False)
