# saidas_routes.py
from __future__ import annotations

from typing import Optional
from datetime import datetime, date
import re

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
    codigo: str = Field(min_length=1)

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

# ---------- HELPERS ----------
def _resolve_user_base(db: Session, current_user: User) -> str:
    """
    Determina a sub_base (v2) do usuário, com fallback para base (retrocompat).
    Tenta por id, depois por email e por username.
    """
    def pick_base(u: User) -> str | None:
        # v2: usa sub_base; se não existir/preenchida, usa base
        return getattr(u, "sub_base", None) or getattr(u, "base", None)

    # 1) por ID
    user_id = getattr(current_user, "id", None)
    if user_id is not None:
        u = db.get(User, user_id)
        if u:
            b = pick_base(u)
            if b:
                return b

    # 2) por email
    email = getattr(current_user, "email", None)
    if email:
        u = db.scalars(select(User).where(User.email == email)).first()
        if u:
            b = pick_base(u)
            if b:
                return b

    # 3) por username
    username = getattr(current_user, "username", None)
    if username:
        u = db.scalars(select(User).where(User.username == username)).first()
        if u:
            b = pick_base(u)
            if b:
                return b

    raise HTTPException(status_code=401, detail="Usuário sem sub_base/base definida em 'users'.")

def _get_owner_for_base_or_user(
    db: Session,
    sub_base_user: str,
    email: str | None,
    username: str | None
) -> Owner:
    """
    Retorna o Owner responsável pela sub_base do usuário.
    Prioriza Owner.sub_base (v2); faz fallback para Owner.base (retrocompat),
    depois tenta por email e username.
    """
    owner = None

    # v2: se o modelo tiver coluna sub_base, tenta primeiro por ela
    if hasattr(Owner, "sub_base"):
        owner = db.scalars(select(Owner).where(Owner.sub_base == sub_base_user)).first()

    # Fallback: ainda não migrado? tentar pela coluna base
    if not owner and hasattr(Owner, "base"):
        owner = db.scalars(select(Owner).where(Owner.base == sub_base_user)).first()

    # Fallbacks finais por email / username
    if not owner and email:
        owner = db.scalars(select(Owner).where(Owner.email == email)).first()
    if not owner and username:
        owner = db.scalars(select(Owner).where(Owner.username == username)).first()

    if owner:
        return owner

    raise HTTPException(status_code=404, detail="Owner não encontrado para esta sub_base/usuário.")

# ---------- CLASSIFICADOR DE SERVIÇO ----------
_SHOPEE_RE = re.compile(r"^BR\d{12,14}[A-Z]?$", re.IGNORECASE)

def _classificar_servico(codigo: str) -> str:
    """
    Define o serviço a partir do formato do código:
    - NF-e: 44 dígitos numéricos => 'nfe'
    - Shopee: BR + 12–14 dígitos + letra opcional => 'shopee'
    - Mercado Livre: exatamente 10 ou 11 dígitos numéricos => 'mercado_livre'
    - Caso contrário => 'avulso'
    """
    raw = (codigo or "").strip()
    if not raw:
        return "avulso"

    digits_only = re.sub(r"\D", "", raw)

    if len(digits_only) == 44:
        return "nfe"
    if _SHOPEE_RE.match(raw):
        return "shopee"
    if re.fullmatch(r"\d{10,11}", raw):
        return "mercado_livre"
    return "avulso"

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

    # Base e owner (usados apenas para fins de cobrança)
    base_user = _resolve_user_base(db, current_user)
    owner = _get_owner_for_base_or_user(db, base_user, email, username)

    # Regras de cobrança
    try:
        cobranca = int(str(owner.cobranca or "0"))
    except Exception:
        cobranca = 0

    valor_un = float(owner.valor or 0.0)
    creditos = float(owner.creditos or 0.0)
    mensalidade = owner.mensalidade

    # Sempre 1 código por requisição
    codigo = payload.codigo.strip()
    entregador = payload.entregador.strip()
    servico = _classificar_servico(codigo)

    # 🔎 Checa duplicidade antes de prosseguir
    existente = db.scalars(
        select(Saida).where(Saida.base == base_user, Saida.codigo == codigo)
    ).first()
    if existente:
        raise HTTPException(
            status_code=409,
            detail=f"O código '{codigo}' já foi registrado anteriormente."
        )

    try:
        # 1) Cobrança
        if cobranca == 0:  # pré-pago
            custo = round(valor_un * 1, 2)
            if creditos < custo:
                raise HTTPException(
                    status_code=409,
                    detail=f"Créditos insuficientes. Necessário {custo:.2f}, saldo {creditos:.2f}."
                )
            owner.creditos = round(creditos - custo, 2)
            db.add(owner)

        elif cobranca == 1:  # mensalidade
            if not mensalidade or date.today() > mensalidade:
                raise HTTPException(status_code=402, detail="Mensalidade vencida ou não configurada.")
        else:
            raise HTTPException(status_code=422, detail="Valor inválido em 'cobranca' (use 0 ou 1).")

        # 2) Insert único
        row = Saida(
            base=base_user,
            username=username,
            entregador=entregador,
            codigo=codigo,
            servico=servico,
            status="saiu",
        )
        db.add(row)
        db.commit()
        db.refresh(row)

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Erro ao registrar saída: {e}")

    return SaidaOut.model_validate(row)
