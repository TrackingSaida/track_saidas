from __future__ import annotations

import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List, Literal, Dict, Tuple

from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from models import Coleta, Entregador, BasePreco, User, Saida
from models import Owner, OwnerCobrancaItem


router = APIRouter(prefix="/coletas", tags=["Coletas"])

# ============================================================
# MODELOS
# ============================================================

class ItemLote(BaseModel):
    codigo: str = Field(min_length=1)
    servico: str = Field(
        min_length=1,
        description="shopee | ml | mercado_livre | mercado livre | avulso"
    )


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
    timestamp: datetime.datetime
    base: str
    sub_base: str
    username_entregador: str
    shopee: int
    mercado_livre: int
    avulso: int
    valor_total: Decimal
    model_config = ConfigDict(from_attributes=True)

ColetaOut.model_rebuild()


class LoteResponse(BaseModel):
    coleta: ColetaOut
    resumo: ResumoLote


# ============================================================
# HELPERS
# ============================================================

def _decimal(v) -> Decimal:
    try:
        return Decimal(str(v or 0))
    except Exception:
        return Decimal("0")


def _fmt_money(d: Decimal) -> str:
    return f"{d.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}"


def _normalize_servico(raw: str) -> Literal["shopee", "mercado_livre", "avulso"]:
    s = (raw or "").strip().lower()
    if s == "shopee":
        return "shopee"
    if s in {"ml", "mercado livre", "mercado_livre"}:
        return "mercado_livre"
    return "avulso"


def _servico_label_for_saida(s: Literal["shopee", "mercado_livre", "avulso"]) -> str:
    return "Mercado Livre" if s == "mercado_livre" else s


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
        ent = db.scalar(
            select(Entregador).where(Entregador.username_entregador.in_(candidates))
        )

    if ent:
        if hasattr(ent, "ativo") and not ent.ativo:
            raise HTTPException(403, "Entregador inativo.")
        if not ent.sub_base:
            raise HTTPException(422, "Entregador sem sub_base definida.")
        return ent.sub_base, (ent.nome or ent.username_entregador), ent.username_entregador

    # fallback via tabela users
    user_id = getattr(user, "id", None)
    u = db.get(User, user_id)
    sub_base = u.sub_base if u else None

    if not sub_base and getattr(user, "email", None):
        u = db.scalar(select(User).where(User.email == user.email))
        sub_base = u.sub_base if u else None

    if not sub_base and getattr(user, "username", None):
        u = db.scalar(select(User).where(User.username == user.username))
        sub_base = u.sub_base if u else None

    if not sub_base:
        raise HTTPException(422, "Usuário sem sub_base definida.")

    return sub_base, getattr(user, "username", "Sistema"), getattr(user, "username", "Sistema")


def _get_precos(db: Session, sub_base: str, base: str):
    precos = db.scalar(
        select(BasePreco).where(BasePreco.sub_base == sub_base, BasePreco.base == base)
    )
    if not precos:
        raise HTTPException(
            404,
            f"Tabela de preços não encontrada para sub_base={sub_base!r} e base={base!r}."
        )
    return _decimal(precos.shopee), _decimal(precos.ml), _decimal(precos.avulso)


# ============================================================
# REPROCESSAMENTO DE COLETA
# ============================================================

def recalcular_coleta(db: Session, id_coleta: int):
    coleta = db.get(Coleta, id_coleta)
    if not coleta:
        raise HTTPException(404, f"Coleta {id_coleta} não encontrada.")

    saidas = db.scalars(select(Saida).where(Saida.id_coleta == id_coleta)).all()
    if not saidas:
        raise HTTPException(400, "Nenhuma saída vinculada à coleta.")

    count = {"shopee": 0, "mercado_livre": 0, "avulso": 0}

    for s in saidas:
        serv = (s.servico or "").lower().replace("_", " ").strip()
        if serv == "shopee":
            count["shopee"] += 1
        elif serv.startswith("mercado"):
            count["mercado_livre"] += 1
        else:
            count["avulso"] += 1

    p_shopee, p_ml, p_avulso = _get_precos(db, coleta.sub_base, coleta.base)

    total = (
        _decimal(count["shopee"]) * p_shopee +
        _decimal(count["mercado_livre"]) * p_ml +
        _decimal(count["avulso"]) * p_avulso
    ).quantize(Decimal("0.01"))

    coleta.shopee = count["shopee"]
    coleta.mercado_livre = count["mercado_livre"]
    coleta.avulso = count["avulso"]
    coleta.valor_total = total

    db.commit()
    db.refresh(coleta)
    return coleta


# ============================================================
# POST /coletas/lote
# ============================================================

@router.post("/lote", response_model=LoteResponse, status_code=201)
def registrar_coleta_em_lote(payload: ColetaLoteIn, db: Session = Depends(get_db),
                             current_user: User = Depends(get_current_user)):

    sub_base, entregador_nome, username_entregador = _resolve_entregador_ou_user_base(db, current_user)

    owner = db.scalar(select(Owner).where(Owner.sub_base == sub_base))
    if not owner:
        raise HTTPException(404, "Owner não encontrado.")
    valor_unit = Decimal(str(owner.valor or 0))

    p_shopee, p_ml, p_avulso = _get_precos(db, sub_base, payload.base)

    created = 0
    saidas_ids = []
    count = {"shopee": 0, "mercado_livre": 0, "avulso": 0}

    try:
        coleta = Coleta(
            sub_base=sub_base,
            base=payload.base,
            username_entregador=username_entregador,
            shopee=0,
            mercado_livre=0,
            avulso=0,
            valor_total=Decimal("0.00"),
        )
        db.add(coleta)
        db.flush()

        for item in payload.itens:
            serv_key = _normalize_servico(item.servico)
            codigo = item.codigo.strip()

            exists = db.scalar(
                select(Saida).where(Saida.sub_base == sub_base, Saida.codigo == codigo)
            )
            if exists:
                raise HTTPException(409, f"Código '{codigo}' já coletado.")

            saida = Saida(
                sub_base=sub_base,
                base=payload.base,
                username=current_user.username,
                entregador=entregador_nome,
                codigo=codigo,
                servico=_servico_label_for_saida(serv_key),
                status="coletado",
                id_coleta=coleta.id_coleta,
            )
            db.add(saida)
            db.flush()
            saidas_ids.append(saida.id_saida)

            count[serv_key] += 1
            created += 1

        coleta.shopee = count["shopee"]
        coleta.mercado_livre = count["mercado_livre"]
        coleta.avulso = count["avulso"]
        coleta.valor_total = (
            count["shopee"] * p_shopee +
            count["mercado_livre"] * p_ml +
            count["avulso"] * p_avulso
        ).quantize(Decimal("0.01"))

        db.flush()

        for id_saida in saidas_ids:
            item = OwnerCobrancaItem(
                sub_base=sub_base,
                id_coleta=coleta.id_coleta,
                id_saida=id_saida,
                valor=valor_unit
            )
            db.add(item)

        db.commit()
        db.refresh(coleta)

    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"Falha ao registrar lote: {e}")

    return LoteResponse(
        coleta=ColetaOut.model_validate(coleta),
        resumo=ResumoLote(
            inseridos=created,
            duplicados=0,
            codigos_duplicados=[],
            contagem=count,
            precos={
                "shopee": _fmt_money(p_shopee),
                "ml": _fmt_money(p_ml),
                "avulso": _fmt_money(p_avulso),
            },
            total=_fmt_money(coleta.valor_total),
        ),
    )


# ============================================================
# GET /coletas        (ORIGINAL — mantido sem alterações)
# ============================================================

@router.get("/", response_model=List[ColetaOut])
def list_coletas(
    base: Optional[str] = Query(None),
    username_entregador: Optional[str] = Query(None),
    data_inicio: Optional[datetime.date] = Query(None),
    data_fim: Optional[datetime.date] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):

    # Resolve sub_base do usuário
    user_id = getattr(current_user, "id", None)
    sub_base_user = None

    if user_id:
        u = db.get(User, user_id)
        sub_base_user = getattr(u, "sub_base", None)

    if not sub_base_user and current_user.email:
        u = db.scalar(select(User).where(User.email == current_user.email))
        sub_base_user = u.sub_base if u else None

    if not sub_base_user and current_user.username:
        u = db.scalar(select(User).where(User.username == current_user.username))
        sub_base_user = u.sub_base if u else None

    if not sub_base_user:
        raise HTTPException(400, "sub_base não definida.")

    stmt = select(Coleta).where(Coleta.sub_base == sub_base_user)

    if base:
        stmt = stmt.where(Coleta.base == base.strip())

    if username_entregador:
        stmt = stmt.where(Coleta.username_entregador == username_entregador.strip())

    if data_inicio:
        stmt = stmt.where(Coleta.timestamp >= data_inicio)

    if data_fim:
        stmt = stmt.where(Coleta.timestamp <= data_fim)

    stmt = stmt.where(
        (Coleta.shopee > 0) |
        (Coleta.mercado_livre > 0) |
        (Coleta.avulso > 0) |
        (Coleta.valor_total > 0)
    )

    stmt = stmt.order_by(Coleta.timestamp.desc())
    rows = db.scalars(stmt).all()
    return rows


# ============================================================
# POST /coletas/recalcular/{id_coleta}
# ============================================================

@router.post("/recalcular/{id_coleta}", response_model=ColetaOut)
def api_recalcular_coleta(id_coleta: int, db: Session = Depends(get_db)):
    coleta = recalcular_coleta(db, id_coleta)
    return ColetaOut.model_validate(coleta)


# ============================================================
# NOVA ROTA — /coletas/resumo
# Agrupamento POR DIA + BASE com paginação real
# ============================================================

class ResumoItem(BaseModel):
    data: str
    base: str
    shopee: int
    mercado_livre: int
    avulso: int
    valor_total: Decimal
    cancelados: int
    entregadores: str


class ResumoResponse(BaseModel):
    page: int
    pageSize: int
    totalPages: int
    totalItems: int
    items: List[ResumoItem]


@router.get("/resumo", response_model=ResumoResponse)
def resumo_coletas(
    base: Optional[str] = Query(None),
    data_inicio: Optional[datetime.date] = Query(None),
    data_fim: Optional[datetime.date] = Query(None),
    page: int = Query(1, ge=1),
    pageSize: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):


    # ----------------------------------------------------------
    # Resolver sub_base
    # ----------------------------------------------------------
    user_id = getattr(current_user, "id", None)
    sub_base_user = None

    if user_id:
        u = db.get(User, user_id)
        sub_base_user = u.sub_base if u else None

    if not sub_base_user and current_user.email:
        u = db.scalar(select(User).where(User.email == current_user.email))
        sub_base_user = u.sub_base if u else None

    if not sub_base_user and current_user.username:
        u = db.scalar(select(User).where(User.username == current_user.username))
        sub_base_user = u.sub_base if u else None

    if not sub_base_user:
        raise HTTPException(400, "sub_base não definida.")

    # Normaliza base (se houver)
    base_norm = base.strip().lower() if base else None

    # ----------------------------------------------------------
    # Filtro principal — tabela COLETAS
    # ----------------------------------------------------------
    stmt = select(Coleta).where(Coleta.sub_base == sub_base_user)

    # Base case-insensitive
    if base_norm:
        stmt = stmt.where(func.lower(Coleta.base) == base_norm)

    # Data início
    if data_inicio:
        stmt = stmt.where(Coleta.timestamp >= data_inicio)

    # Data fim (incluindo o dia inteiro)
    if data_fim:
        dt_end = datetime.datetime.combine(data_fim, datetime.time(23, 59, 59))
        stmt = stmt.where(Coleta.timestamp <= dt_end)

    stmt = stmt.order_by(Coleta.timestamp.asc())

    rows = db.scalars(stmt).all()

    # ----------------------------------------------------------
    # Buscar cancelados — tabela SAIDAS
    # ----------------------------------------------------------
    cancelados_stmt = select(Saida).where(
        Saida.sub_base == sub_base_user,
        func.lower(Saida.status) == "cancelado"
    )

    if base_norm:
        cancelados_stmt = cancelados_stmt.where(func.lower(Saida.base) == base_norm)

    if data_inicio:
        cancelados_stmt = cancelados_stmt.where(Saida.timestamp >= data_inicio)

    if data_fim:
        cancelados_stmt = cancelados_stmt.where(Saida.timestamp <= dt_end)

    cancelados_rows = db.scalars(cancelados_stmt).all()

    # ----------------------------------------------------------
    # Montar mapa de cancelados
    # ----------------------------------------------------------
    mapa_cancelados = {}

    for c in cancelados_rows:
        dia = c.timestamp.date().isoformat()
        baseKey = (c.base or "").strip().upper()
        key = f"{dia}_{baseKey}"
        mapa_cancelados[key] = mapa_cancelados.get(key, 0) + 1

    # ----------------------------------------------------------
    # Agrupar coletas por DIA + BASE
    # ----------------------------------------------------------
    agrupado = {}

    for r in rows:
        dia = r.timestamp.date().isoformat()
        baseKey = (r.base or "").strip().upper()

        key = f"{dia}_{baseKey}"

        if key not in agrupado:
            agrupado[key] = {
                "data": dia,
                "base": baseKey,
                "shopee": 0,
                "mercado_livre": 0,
                "avulso": 0,
                "valor_total": Decimal("0.00"),
                "entregadores": set(),
            }

        agrupado[key]["shopee"] += r.shopee
        agrupado[key]["mercado_livre"] += r.mercado_livre
        agrupado[key]["avulso"] += r.avulso
        agrupado[key]["valor_total"] += r.valor_total
        agrupado[key]["entregadores"].add(r.username_entregador or "-")

    # ----------------------------------------------------------
    # Lista final
    # ----------------------------------------------------------
    lista = []

    for key, item in agrupado.items():
        canc = mapa_cancelados.get(key, 0)

        lista.append(
            ResumoItem(
                data=item["data"],
                base=item["base"],
                shopee=item["shopee"],
                mercado_livre=item["mercado_livre"],
                avulso=item["avulso"],
                valor_total=item["valor_total"],
                cancelados=canc,
                entregadores=" | ".join(item["entregadores"])
            )
        )

    # Ordenação por data ASC
    lista.sort(key=lambda x: x.data)

    # ----------------------------------------------------------
    # Paginação REAL (igual front usa)
    # ----------------------------------------------------------
    totalItems = len(lista)
    totalPages = (totalItems + pageSize - 1) // pageSize

    start = (page - 1) * pageSize
    end = start + pageSize
    items = lista[start:end]

    return ResumoResponse(
        page=page,
        pageSize=pageSize,
        totalPages=totalPages,
        totalItems=totalItems,
        items=items
    )
