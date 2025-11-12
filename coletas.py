# coletas.py
from __future__ import annotations

import datetime  
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List, Literal, Dict, Tuple

from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from models import Coleta, Entregador, BasePreco, User, Saida

router = APIRouter(prefix="/coletas", tags=["Coletas"])

# =========================
# Schemas
# =========================
class ItemLote(BaseModel):
    codigo: str = Field(min_length=1)
    servico: str = Field(min_length=1, description="shopee | ml | mercado_livre | mercado livre | avulso")


class ColetaLoteIn(BaseModel):
    base: str = Field(min_length=1)
    itens: List[ItemLote] = Field(min_length=1)


class ResumoLote(BaseModel):
    inseridos: int
    duplicados: int
    codigos_duplicados: List[str]
    contagem: Dict[str, int]
    precos: Dict[str, str]
    total: str


class ColetaOut(BaseModel):
    id_coleta: int
    timestamp: datetime.datetime  # ✅ usa o módulo completo
    base: str
    sub_base: str
    username_entregador: str
    shopee: int
    mercado_livre: int
    avulso: int
    valor_total: Decimal
    model_config = ConfigDict(from_attributes=True)


# ✅ Corrige erro Pydantic 2.x (TypeAdapter not fully defined)
ColetaOut.model_rebuild()


class LoteResponse(BaseModel):
    coleta: ColetaOut
    resumo: ResumoLote


# =========================
# Helpers
# =========================
def _decimal(v) -> Decimal:
    try:
        return Decimal(str(v or 0))
    except Exception:
        return Decimal("0")


def _fmt_money(d: Decimal) -> str:
    return f"{d.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}"


def _normalize_servico(raw: str) -> Literal["shopee", "mercado_livre", "avulso"]:
    s = (raw or "").strip().lower()
    if s in {"shopee"}:
        return "shopee"
    if s in {"ml", "mercado_livre", "mercado livre"}:
        return "mercado_livre"
    if s in {"avulso"}:
        return "avulso"
    raise HTTPException(status_code=422, detail=f"Serviço inválido: {raw!r}")


def _servico_label_for_saida(s: Literal["shopee", "mercado_livre", "avulso"]) -> str:
    return "Mercado Livre" if s == "mercado_livre" else s


def _find_entregador_for_user(db: Session, user: User) -> Entregador:
    candidates = []
    ue = getattr(user, "username_entregador", None)
    if ue:
        candidates.append(ue)
    un = getattr(user, "username", None)
    if un and un not in candidates:
        candidates.append(un)

    if not candidates:
        raise HTTPException(status_code=404, detail="Usuário sem 'username' compatível para localizar o entregador.")

    ent = db.scalar(select(Entregador).where(Entregador.username_entregador.in_(candidates)))

    if not ent:
        raise HTTPException(status_code=404, detail="Entregador não encontrado para o usuário autenticado.")
    if hasattr(ent, "ativo") and ent.ativo is False:
        raise HTTPException(status_code=403, detail="Entregador inativo.")
    if not getattr(ent, "sub_base", None):
        raise HTTPException(status_code=422, detail="Entregador encontrado, porém sem 'sub_base' definida.")

    return ent


def _resolve_entregador_ou_user_base(db: Session, user: User) -> Tuple[str, str, str]:
    candidates = []
    ue = getattr(user, "username_entregador", None)
    if ue:
        candidates.append(ue)
    un = getattr(user, "username", None)
    if un and un not in candidates:
        candidates.append(un)

    ent = None
    if candidates:
        ent = db.scalar(select(Entregador).where(Entregador.username_entregador.in_(candidates)))

    if ent:
        if hasattr(ent, "ativo") and not ent.ativo:
            raise HTTPException(status_code=403, detail="Entregador inativo.")
        if not getattr(ent, "sub_base", None):
            raise HTTPException(status_code=422, detail="Entregador sem 'sub_base' definida.")
        return ent.sub_base, (ent.nome or ent.username_entregador), ent.username_entregador

    user_id = getattr(user, "id", None)
    u = db.get(User, user_id) if user_id else None
    sub_base = getattr(u, "sub_base", None)
    if not sub_base and getattr(user, "email", None):
        u = db.scalar(select(User).where(User.email == user.email))
        sub_base = getattr(u, "sub_base", None)
    if not sub_base and getattr(user, "username", None):
        u = db.scalar(select(User).where(User.username == user.username))
        sub_base = getattr(u, "sub_base", None)

    if not sub_base:
        raise HTTPException(status_code=422, detail="Usuário sem 'sub_base' definida em 'users'.")

    username_entregador = getattr(user, "username", "sistema")
    nome_exibicao = getattr(user, "username", "Sistema")

    return sub_base, nome_exibicao, username_entregador


def _get_precos(db: Session, sub_base: str, base: str) -> Tuple[Decimal, Decimal, Decimal]:
    precos = db.scalar(select(BasePreco).where(BasePreco.sub_base == sub_base, BasePreco.base == base))
    if not precos:
        raise HTTPException(
            status_code=404,
            detail=f"Tabela de preços não encontrada para sub_base={sub_base!r} e base={base!r}.",
        )
    return _decimal(precos.shopee), _decimal(precos.ml), _decimal(precos.avulso)


# =========================
# POST /coletas/lote
# =========================

@router.post("/lote", status_code=status.HTTP_201_CREATED, response_model=LoteResponse)
def registrar_coleta_em_lote(
    payload: ColetaLoteIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base, entregador_nome, username_entregador = _resolve_entregador_ou_user_base(db, current_user)
    p_shopee, p_ml, p_avulso = _get_precos(db, sub_base=sub_base, base=payload.base)

    count = {"shopee": 0, "mercado_livre": 0, "avulso": 0}
    created = 0

    try:
        for item in payload.itens:
            serv = _normalize_servico(item.servico)
            codigo = (item.codigo or "").strip()
            if not codigo:
                raise HTTPException(status_code=422, detail="Código vazio no payload.")

            exists = db.scalar(
                select(Saida).where(Saida.sub_base == sub_base, Saida.codigo == codigo)
            )
            if exists:
                # 🚫 Interrompe imediatamente e cancela o lote
                raise HTTPException(
                    status_code=409,
                    detail=f"O código '{codigo}' já foi coletado anteriormente."
                )

            saida = Saida(
                sub_base=sub_base,
                base=payload.base,
                username=getattr(current_user, "username", None),
                entregador=entregador_nome,
                codigo=codigo,
                servico=_servico_label_for_saida(serv),
                status="coletado",
            )
            db.add(saida)
            created += 1
            count[serv] += 1

        total = (
            _decimal(count["shopee"]) * p_shopee
            + _decimal(count["mercado_livre"]) * p_ml
            + _decimal(count["avulso"]) * p_avulso
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        coleta = Coleta(
            sub_base=sub_base,
            base=payload.base,
            username_entregador=username_entregador,
            shopee=count["shopee"],
            mercado_livre=count["mercado_livre"],
            avulso=count["avulso"],
            valor_total=total,
        )
        db.add(coleta)
        db.commit()
        db.refresh(coleta)

    except HTTPException as e:
        db.rollback()
        raise e
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Falha ao registrar lote: {e}")

    return LoteResponse(
        coleta=ColetaOut.model_validate(coleta),
        resumo=ResumoLote(
            inseridos=created,
            duplicados=0,
            codigos_duplicados=[],
            contagem=dict(count),
            precos={
                "shopee": _fmt_money(p_shopee),
                "ml": _fmt_money(p_ml),
                "avulso": _fmt_money(p_avulso),
            },
            total=_fmt_money(coleta.valor_total),
        ),
    )

# =========================
# GET /coletas
# =========================
@router.get("/", response_model=List[ColetaOut])
def list_coletas(
    base: Optional[str] = Query(None),
    username_entregador: Optional[str] = Query(None),
    data_inicio: Optional[datetime.date] = Query(None),
    data_fim: Optional[datetime.date] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    user_id = getattr(current_user, "id", None)
    sub_base_user: Optional[str] = None

    if user_id:
        u = db.get(User, user_id)
        sub_base_user = getattr(u, "sub_base", None)
    if not sub_base_user and getattr(current_user, "email", None):
        u = db.scalar(select(User).where(User.email == current_user.email))
        sub_base_user = getattr(u, "sub_base", None)
    if not sub_base_user and getattr(current_user, "username", None):
        u = db.scalar(select(User).where(User.username == current_user.username))
        sub_base_user = getattr(u, "sub_base", None)
    if not sub_base_user:
        raise HTTPException(status_code=400, detail="sub_base não definida no usuário.")

    # 🔹 Aqui estava o erro: o stmt precisa estar FORA do if acima
    stmt = select(Coleta).where(Coleta.sub_base == sub_base_user)

    if base:
        stmt = stmt.where(Coleta.base == base.strip())
    if username_entregador:
        stmt = stmt.where(Coleta.username_entregador == username_entregador.strip())
    if data_inicio:
        stmt = stmt.where(Coleta.timestamp >= data_inicio)
    if data_fim:
        stmt = stmt.where(Coleta.timestamp <= data_fim)

    # 🔹 filtro para excluir coletas com tudo zero
    stmt = stmt.where(
        (Coleta.shopee > 0)
        | (Coleta.mercado_livre > 0)
        | (Coleta.avulso > 0)
        | (Coleta.valor_total > 0)
    )

    stmt = stmt.order_by(Coleta.timestamp.desc())
    rows = db.scalars(stmt).all()
    return rows
