from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Column, Integer, Text
from sqlalchemy.orm import Session

from db import Base, get_db  # <- agora vem daqui

router = APIRouter(prefix="/estacoes", tags=["Estacoes"])

class owner(Base):
    __tablename__ = "owner"
    id      = Column(Integer, primary_key=True)  # ID gerado pelo banco
    owner = Column(Text, nullable=True)

class ownerFields(BaseModel):
    owner: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)

@router.post("/", status_code=status.HTTP_201_CREATED)
def create_owner(body: ownerFields, db: Session = Depends(get_db)):
    obj = owner(owner=body.owner)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return {"ok": True, "action": "created", "id": obj.id}
