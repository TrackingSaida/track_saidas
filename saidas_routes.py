from __future__ import annotations

from typing import Optional, List, Literal
from datetime import datetime, date

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import Column, BigInteger, Text, Date, DateTime, func
from sqlalchemy.orm import Session

# DB e modelos
from db import Base, get_db
from models import User
from auth import get_current_user

router = APIRouter(prefix="/saidas", tags=["Saídas"])

# =========================
# MODELO TABELA SAIDAS
# =========================
class Saida(Base):
    __tablename__ = "saidas"

    id_saida  = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    timestamp = Column(DateTime(timezone=False), server_default=func.now())
    data      = Column(Date, server_default=func.current_date())

    base       = Column(Text, nullable=True)
    username   = Column(Text, nullable=True)
    entregador = Column(Text, nullable=True)
    codigo     = Column(Text, nullable=True)
    servico    = Column(Text, nullable=True)
    status     = Column(Text, nullable=True)  # sempre "saiu" neste step

# =========================
# SCHEMAS
# =========================
class SaidaCreate(BaseModel):
    """Aceita um único código OU uma lista de códigos."""
    entregador: str = Field(min_length=1)
    codigo: Optional[str] = None
    codigos: Optional[List[str]] = None
    servico: Optional[str] = None

    @property
    def lista_codigos(self) -> List[str]:
        if self.codigos and len(self.codigos) > 0:
            return [c.strip() for c in self.codigos if c and c.strip()]
        if self.codigo and self.codigo.strip():
            return [self.codigo.strip()]
        return []

class SaidaOut(BaseModel):
    id_saida: int
    timestamp: datetime
    data: date
    base: Optional[str] = None
    username: Optional[str] = None
    entregador: Optional[str] = None
    codigo: Optional[str] = None
    servico: Optional[str] = None
    status: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

class ResumoBatchOut(BaseModel):
    """Resposta quando vier mais de um código."""
    base: str
    username: str
    entregador: str
    servico: str
    qtd_codigos: int
    codigos_inseridos: List[str]
    modo_cobranca: Literal[0, 1]
    saldo_restante: Optional[float] = None  # só informa quando cobranca==0


# =========================
# UTILS
# =========================
def _parse_date_any(value) -> Optional[date]:
    """Tenta interpretar 'value' como date (aceita date, 'YYYY-MM-DD', 'DD/MM/YYYY')."""
    if isinstance(value, date):
        return value
    if value in (None, ""):
        return None
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            pass
    return None


# =========================
# ENDPOINT
# =========================
@router.post("/registrar", status_code=status.HTTP_201_CREATED)
def registrar_saida(
    payload: SaidaCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Step 2: registra 1..N códigos e aplica regras de cobrança.
    - cobranca==0: pré-pago por valor -> debita (valor * qtd) de 'creditos'
    - cobranca==1: mensalidade -> valida que hoje <= 'mensalidade'
    - NÃO grava valores na tabela 'saidas'
    - Retorna a linha única (compat step 1) ou um resumo quando batch
    """
    base_user = getattr(current_user, "base", None)
    username  = getattr(current_user, "username", None)
    if not base_user or not username:
        raise HTTPException(status_code=401, detail="Usuário sem 'base' ou 'username' configurados.")

    codigos = payload.lista_codigos
    if not codigos:
        raise HTTPException(status_code=422, detail="Informe 'codigo' ou 'codigos'.")

    # Normalizações vindas do usuário
    raw_cobranca   = getattr(current_user, "cobranca", None)
    raw_valor      = getattr(current_user, "valor", None)
    raw_creditos   = getattr(current_user, "creditos", None)
    raw_mensalidade= getattr(current_user, "mensalidade", None)

    try:
        cobranca = int(str(raw_cobranca)) if raw_cobranca not in (None, "") else 0
    except Exception:
        cobranca = 0

    try:
        valor = float(raw_valor) if raw_valor not in (None, "") else 0.0
    except Exception:
        valor = 0.0

    try:
        creditos = float(raw_creditos) if raw_creditos not in (None, "") else 0.0
    except Exception:
        creditos = 0.0

    mensalidade = _parse_date_any(raw_mensalidade)

    servico = (payload.servico or "padrao").strip()
    entregador = payload.entregador.strip()

    rows: List[Saida] = []

    try:
        # --- COBRANÇA ---
        if cobranca == 0:
            qtd = len(codigos)
            custo = round(valor * qtd, 2)
            if creditos < custo:
                raise HTTPException(
                    status_code=409,
                    detail=f"Créditos insuficientes. Necessário {custo:.2f}, saldo {creditos:.2f}."
                )
            # Debita saldo
            current_user.creditos = round(creditos - custo, 2)
            db.add(current_user)  # marca como dirty

        elif cobranca == 1:
            hoje = date.today()
            if not mensalidade or hoje > mensalidade:
                raise HTTPException(
                    status_code=402,
                    detail="Mensalidade vencida ou não configurada."
                )
        else:
            raise HTTPException(status_code=422, detail="Valor inválido em 'cobranca' (use 0 ou 1).")

        # --- INSERTS EM 'saidas' ---
        for codigo in codigos:
            c = codigo.strip()
            if not c:
                continue
            row = Saida(
                base=base_user,
                username=username,
                entregador=entregador,
                codigo=c,
                servico=servico,
                status="saiu",
            )
            db.add(row)
            rows.append(row)

        # flush para garantir PKs/timestamps antes do commit (se precisarmos retornar a linha)
        db.flush()
        db.commit()

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail="Erro ao registrar saída") from e

    # ---------- RESPOSTA ----------
    if len(rows) == 1:
        db.refresh(rows[0])
        return SaidaOut.model_validate(rows[0])

    saldo_restante = None
    if cobranca == 0:
        saldo_restante = float(getattr(current_user, "creditos", 0.0) or 0.0)

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
