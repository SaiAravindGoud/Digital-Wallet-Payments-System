
import re
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import (
    BaseModel,
    EmailStr,
    Field,
    field_validator,
)


class RegisterIn(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    phone: str
    password: str = Field(min_length=8)
    role: Literal["User", "Merchant"] = "User"
    business_name: str | None = None
    business_category: str | None = None

    @field_validator("full_name", mode="before")
    @classmethod
    def clean_full_name(cls, value):
        if isinstance(value, str):
            value = value.strip()
        return value

    @field_validator("phone", mode="before")
    @classmethod
    def valid_phone(cls, value):
        if isinstance(value, str):
            value = value.strip()

        if not isinstance(value, str) or not re.fullmatch(r"\d{10}", value):
            raise ValueError("Phone must be 10 digits")

        return value


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class MoneyIn(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    source: str = "UPI"
    simulate_status: Literal["Success", "Failed"] = "Success"


class TransferIn(BaseModel):
    receiver_phone: str
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    note: str = Field(default="", max_length=255)
    pin: str = Field(pattern=r"^\d{4}$")

    @field_validator("receiver_phone", mode="before")
    @classmethod
    def valid_receiver_phone(cls, value):
        if isinstance(value, str):
            value = value.strip()

        if not isinstance(value, str) or not re.fullmatch(r"\d{10}", value):
            raise ValueError("Receiver phone must be 10 digits")

        return value


class MerchantPaymentIn(BaseModel):
    merchant_code: str = Field(min_length=5, max_length=20)
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    pin: str = Field(pattern=r"^\d{4}$")

    @field_validator("merchant_code")
    @classmethod
    def valid_merchant_code(cls, value):
        value = value.strip().upper()

        if not re.fullmatch(r"MER-\d{5,}", value):
            raise ValueError("Invalid merchant code")

        return value


class MerchantRefundIn(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    pin: str = Field(pattern=r"^\d{4}$")


class MoneyRequestIn(BaseModel):
    payer_phone: str
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    note: str = Field(default="", max_length=255)

    @field_validator("payer_phone", mode="before")
    @classmethod
    def valid_payer_phone(cls, value):
        if isinstance(value, str):
            value = value.strip()

        if not isinstance(value, str) or not re.fullmatch(r"\d{10}", value):
            raise ValueError("Payer phone must be 10 digits")

        return value


class PinIn(BaseModel):
    pin: str = Field(pattern=r"^\d{4}$")


class MoneyRequestPayIn(BaseModel):
    pin: str = Field(pattern=r"^\d{4}$")
    otp: str = Field(pattern=r"^\d{6}$")


class PinChangeIn(BaseModel):
    old_pin: str = Field(pattern=r"^\d{4}$")
    new_pin: str = Field(pattern=r"^\d{4}$")


class KYCIn(BaseModel):
    pan_number: str = Field(pattern=r"^[A-Z]{5}\d{4}[A-Z]$")
    aadhaar_last4: str = Field(pattern=r"^\d{4}$")
    full_name: str = Field(min_length=2, max_length=120)
    date_of_birth: str

    @field_validator("pan_number", mode="before")
    @classmethod
    def normalize_pan(cls, value):
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator("full_name", mode="before")
    @classmethod
    def clean_kyc_name(cls, value):
        if isinstance(value, str):
            return value.strip()
        return value


class BankIn(BaseModel):
    account_holder_name: str = Field(min_length=2, max_length=120)
    account_number: str = Field(pattern=r"^\d{9,18}$")
    ifsc_code: str = Field(pattern=r"^[A-Z]{4}0[A-Z0-9]{6}$")
    bank_name: str = Field(min_length=2, max_length=100)
    otp: str = Field(pattern=r"^\d{6}$")

    @field_validator(
        "account_holder_name",
        "bank_name",
        "account_number",
        mode="before",
    )
    @classmethod
    def clean_bank_fields(cls, value):
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("ifsc_code", mode="before")
    @classmethod
    def uppercase_ifsc(cls, value):
        if isinstance(value, str):
            return value.strip().upper()
        return value


class WithdrawalIn(BaseModel):
    bank_account_id: int = Field(gt=0)
    amount: Decimal = Field(
        ge=Decimal("100.00"),
        max_digits=12,
        decimal_places=2,
    )
    pin: str = Field(pattern=r"^\d{4}$")
