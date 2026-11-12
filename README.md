# Digital Wallet & Payments System

FastAPI + MySQL backend starter based on the supplied 17-level assignment.

## Important implementation status

This repository is a runnable foundation, **not yet a fully completed implementation of every assignment requirement**. Implemented baseline: project configuration, SQLAlchemy models, Alembic integration, JWT registration/login/me, role dependency, wallet creation, KYC submission and admin approval/rejection, wallet freeze/unfreeze, add-money ledger posting, basic P2P ledger posting, transaction listing, and a scheduler startup hook.

The following assignment areas still require implementation and verification before claiming full submission completion: transaction PIN lifecycle and OTP delivery/verification; complete idempotency replay and conflict semantics; full daily/monthly limits; merchant QR/payments/refunds/cashback; money requests; bank-account OTP and withdrawals/reversals; fraud holds and admin decisions; PDF/CSV statements; email notifications; all scheduled jobs and reconciliation; filters/sorting; complete dashboard reports; WebSockets; scheduled/bill payments; Razorpay; password-protected PDFs; Docker; and automated test coverage. Do not represent placeholder jobs or missing routes as complete.

## Windows setup

1. Install Python 3.9+ and MySQL 8.
2. Create database:
   ```sql
   CREATE DATABASE digital_wallet_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
   ```
3. Open PowerShell in this folder and create environment:
   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   ```
4. Create `.env` from `.env.example` and set your MySQL password and a long random `SECRET_KEY`.
5. Create Alembic migration:
   ```powershell
   python -m alembic revision --autogenerate -m "initial wallet schema"
   python -m alembic upgrade head
   ```
6. Run:
   ```powershell
   python -m uvicorn app.main:app --reload
   ```
7. Open `http://127.0.0.1:8000/docs`.

## Alembic commands

```powershell
python -m alembic revision --autogenerate -m "describe change"
python -m alembic upgrade head
python -m alembic current
python -m alembic history
```

## API baseline

- `POST /auth/register`
- `POST /auth/login`
- `GET /auth/me`
- `GET /wallet`
- `POST /wallet/add-money` (requires `Idempotency-Key`)
- `POST /transfers` (requires `Idempotency-Key`)
- `GET /transfers/{transaction_ref}`
- `POST /kyc`, `GET /kyc`
- Admin KYC approve/reject and wallet freeze/unfreeze routes
- `GET /transactions`, `GET /transactions/{transaction_ref}`

## Ledger design

Every posted money movement creates a transaction and two ledger entries: one Debit and one Credit for the same amount. Example: adding ₹500 debits the Bank Settlement system account and credits the user's wallet account. Wallet balance is updated in the same SQLAlchemy transaction. Ledger entries are intended to be append-only. A production implementation must additionally enforce balanced multi-entry postings, reconciliation, idempotency, concurrency, and reversal controls.

## Security and configuration

Never commit `.env`. Only commit `.env.example`. Use a dedicated SMTP app password, never your normal email password. Create the first Admin account manually through a controlled database seed procedure; public registration intentionally permits only User and Merchant.

## Submission checklist

- [ ] Complete every remaining feature listed in the implementation status section
- [ ] Add and run pytest tests, including concurrent transfers and idempotency replay
- [ ] Capture Swagger screenshots
- [ ] Capture idempotency replay screenshot
- [ ] Capture PDF and CSV statements
- [ ] Capture reconciliation result
- [ ] Verify `.env` and generated `statements/` are excluded from Git
- [ ] Push source, migrations, tests, and README to GitHub
