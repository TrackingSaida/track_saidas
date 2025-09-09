# saidas_routes.py
from __future__ import annotations

from typing import Optional, List, Literal
from datetime import datetime, date

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from models import User, Owner, Saida

router = APIRouter(prefix="/saidas", tags=["Saídas"])

# ---------- SCHEMAS ----------
class SaidaCreate(BaseModel):
    entregador: str = Field(min_length=1)
    codigo: Optional[str] = None
    codigos: Optional[List[str]] = None
    servico: Optional[str] = None

    @property
    def lista_codigos(self) -> List[str]:
        # prioriza lista; senão, usa 'codigo' único
        raw = []
        if self.codigos:
            raw.extend(self.codigos)
        if self.codigo:
            raw.append(self.codigo)
        # trim, remove vazios e duplica removendo order-preserving
        seen: set[str] = set()
        out: list[str] = []
        for c in (s.strip() for s in raw if s and isinstance(s, str)):
            if c and c not in seen:
                seen.add(c)
                out.append(c)
        return out

class SaidaOut(BaseModel):
    id_saida: int
    timestamp: datetime
    data: date
    base: Optional[str]
    username: Optional[str]
    entregador: Optional[str]
    codigo: Optional[str]
    servico: Optional[str]
    status: Optional[str]
    model_config = ConfigDict(from_attributes=True)

class ResumoBatchOut(BaseModel):
    base: str
    username: str
    entregador: str
    servico: str
    qtd_codigos: int
    codigos_inseridos: List[str]
    modo_cobranca: Literal[0, 1]
    saldo_restante: Optional[float] = None

# ---------- HELPERS ----------
def _resolve_user_base(db: Session, current_user: User) -> str:
    base_user = getattr(current_user, "base", None)
    if base_user:
        return base_user

    # fallback por email/username
    email = getattr(current_user, "email", None)
    username = getattr(current_user, "username", None)

    if email:
        u = db.scalars(select(User).where(User.email == email)).first()
        if u and u.base:
            return u.base
    if username:
        u = db.scalars(select(User).where(User.username == username)).first()
        if u and u.base:
            return u.base

    raise HTTPException(status_code=401, detail="Usuário sem 'base' definida.")

def _get_owner_for_base_or_user(db: Session, base_user: str, email: str | None, username: str | None) -> Owner:
    # prioriza base
    owner = db.scalars(select(Owner).where(Owner.base == base_user)).first()
    if owner:
        return owner
    # fallback por email/username
    if email:
        owner = db.scalars(select(Owner).where(Owner.email == email)).first()
        if owner:
            return owner
    if username:
        owner = db.scalars(select(Owner).where(Owner.username == username)).first()
        if owner:
            return owner
    raise HTTPException(status_code=404, detail="Owner não encontrado para esta base/usuário.")

# ---------- ENDPOINT ----------
@router.post("/registrar", status_code=status.HTTP_201_CREATED)
def registrar_saida(
    payload: SaidaCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    username = getattr(current_user, "username", None)
    email = getattr(current_user, "email", None)
    if not username:
        raise HTTPException(status_code=401, detail="Usuário sem 'username'.")

    base_user = _resolve_user_base(db, current_user)
    owner = _get_owner_for_base_or_user(db, base_user, email, username)

    # Normalizações de cobrança
    try:
        cobranca = int(str(owner.cobranca or "0"))
    except Exception:
        cobranca = 0

    valor_un = float(owner.valor or 0.0)          # valor unitário na cobrança 0
    creditos = float(owner.creditos or 0.0)       # saldo pré-pago
    mensalidade = owner.mensalidade               # date

    codigos = payload.lista_codigos
    if not codigos:
        raise HTTPException(status_code=422, detail="Informe 'codigo' ou 'codigos'.")

    entregador = payload.entregador.strip()
    servico = (payload.servico or "padrao").strip()
    qtd = len(codigos)

    rows: list[Saida] = []
    try:
        # 1) Cobrança
        if cobranca == 0:
            custo = round(valor_un * qtd, 2)
            if creditos < custo:
                raise HTTPException(
                    status_code=409,
                    detail=f"Créditos insuficientes. Necessário {custo:.2f}, saldo {creditos:.2f}."
                )
            owner.creditos = round(creditos - custo, 2)
            db.add(owner)

        elif cobranca == 1:
            if not mensalidade or date.today() > mensalidade:
                raise HTTPException(status_code=402, detail="Mensalidade vencida ou não configurada.")
        else:
            raise HTTPException(status_code=422, detail="Valor inválido em 'cobranca' (use 0 ou 1).")

        # 2) Inserts em SAIDAS (uma transação)
        for codigo in codigos:
            row = Saida(
                base=base_user,
                username=username,
                entregador=entregador,
                codigo=codigo,
                servico=servico,
                status="saiu",  # redundante ao default do DB, mas explícito
            )
            db.add(row)
            rows.append(row)

        db.commit()

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Erro ao registrar saídas: {e}")

    # Resposta
    if len(rows) == 1:
        db.refresh(rows[0])
        return SaidaOut.model_validate(rows[0])

    saldo_restante = float(owner.creditos or 0.0) if cobranca == 0 else None
    return ResumoBatchOut(
        base=base_user,
        username=username,
        entregador=entregador,
        servico=servico,
        qtd_codigos=len(rows),
        codigos_inseridos=[r.codigo for r in rows],
        modo_cobranca=cobranca,
        saldo_restante=saldo_restante,
    )
