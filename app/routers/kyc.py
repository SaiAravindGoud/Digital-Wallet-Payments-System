from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.entities import KYC
from app.schemas import KYCIn
from app.security import current_user

router=APIRouter(tags=["KYC"])
@router.post("/kyc",status_code=201)
def submit_kyc(data:KYCIn,db:Session=Depends(get_db),user=Depends(current_user)):
    if db.query(KYC).filter_by(user_id=user.id).first(): raise HTTPException(409,"KYC already submitted")
    item=KYC(user_id=user.id,pan_number=data.pan_number,aadhaar_last4=data.aadhaar_last4,full_name=data.full_name,date_of_birth=datetime.fromisoformat(data.date_of_birth))
    db.add(item); db.commit(); return {"id":item.id,"status":item.status}
@router.get("/kyc")
def get_kyc(db:Session=Depends(get_db),user=Depends(current_user)):
    item=db.query(KYC).filter_by(user_id=user.id).first()
    if not item: return {"status":"Not Submitted"}
    return {"id":item.id,"pan_number":item.pan_number,"status":item.status,"rejection_reason":item.rejection_reason}
