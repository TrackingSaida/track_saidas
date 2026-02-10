from __future__ import annotations

import datetime
import time
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List, Literal, Dict, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from models import Coleta, Entregador, BasePreco, User, Saida, Owner
from models import OwnerCobrancaItem


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
    entregador_id: Optional[int] = None


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


def _sub_base_from_token_or_422(user: User) -> str:
    """
    Novo contrato: sub_base vem no JWT (auth stateless).
    """
    sb = getattr(user, "sub_base", None)
    if not sb:
        raise HTTPException(422, "Usuário sem sub_base definida.")
    return sb


def _resolve_entregador_info(db: Session, user: User) -> Tuple[str, str, str, Optional[int]]:
    """
    Objetivo: reduzir consultas.
    - Primeiro usa o que já vem do JWT (sub_base + username).
    - Opcionalmente consulta Entregador 1x para validar ativo e pegar 'nome'.
    - Não faz fallback para tabela User (isso era custo antigo do auth).
    """
    sub_base = _sub_base_from_token_or_422(user)
    username = getattr(user, "username", None) or "Sistema"

    # Se existir Entregador, valida ativo e melhora o nome exibido
    ent = db.scalar(
        select(Entregador).where(Entregador.username_entregador == username)
    )
    if ent:
        if hasattr(ent, "ativo") and not ent.ativo:
            raise HTTPException(403, "Entregador inativo.")
        if getattr(ent, "sub_base", None):
            sub_base = ent.sub_base  # mantém compatibilidade se a verdade estiver no Entregador
        entregador_nome = (getattr(ent, "nome", None) or ent.username_entregador)
        return sub_base, entregador_nome, ent.username_entregador, ent.id_entregador

    # Sem Entregador cadastrado: usa JWT
    return sub_base, username, username, None


# ============================================================
# CACHE BasePreco (TTL curto, por-processo)
# ============================================================
_BASE_PRECO_CACHE_TTL_S = 120.0
_base_preco_cache: Dict[Tuple[str, str], Tuple[float, Decimal, Decimal, Decimal]] = {}


def _get_precos_cached(db: Session, sub_base: str, base: str) -> Tuple[Decimal, Decimal, Decimal]:
    """
    Evita SELECT repetido de BasePreco quando o usuário faz vários lotes na mesma base.
    TTL curto para tolerar atualizações.
    """
    key = (sub_base, base)
    now = time.time()
    hit = _base_preco_cache.get(key)
    if hit and hit[0] > now:
        _, p_shopee, p_ml, p_avulso = hit
        return p_shopee, p_ml, p_avulso

    precos = db.scalar(
        select(BasePreco).where(BasePreco.sub_base == sub_base, BasePreco.base == base)
    )
    if not precos:
        raise HTTPException(
            404,
            f"Tabela de preços não encontrada para sub_base={sub_base!r} e base={base!r}."
        )

    p_shopee = _decimal(precos.shopee)
    p_ml = _decimal(precos.ml)
    p_avulso = _decimal(precos.avulso)

    _base_preco_cache[key] = (now + _BASE_PRECO_CACHE_TTL_S, p_shopee, p_ml, p_avulso)
    return p_shopee, p_ml, p_avulso


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

    p_shopee, p_ml, p_avulso = _get_precos_cached(db, coleta.sub_base, coleta.base)

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
def registrar_coleta_em_lote(
    payload: ColetaLoteIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 1) Resolve sub_base + entregador: prioriza payload.entregador_id, senão JWT
    sub_base = _sub_base_from_token_or_422(current_user)
    if payload.entregador_id is not None:
        ent = db.get(Entregador, payload.entregador_id)
        if not ent or ent.sub_base != sub_base:
            raise HTTPException(422, "Entregador não encontrado ou não pertence à sua base.")
        if hasattr(ent, "ativo") and not ent.ativo:
            raise HTTPException(403, "Entregador inativo.")
        entregador_nome = (getattr(ent, "nome", None) or ent.username_entregador) or ""
        username_entregador = ent.username_entregador or ""
        entregador_id = ent.id_entregador
    else:
        sub_base, entregador_nome, username_entregador, entregador_id = _resolve_entregador_info(db, current_user)

    # 2) preços BasePreco para valores de entradas da coleta (valor_total e resumo)
    p_shopee, p_ml, p_avulso = _get_precos_cached(db, sub_base, payload.base)

    # 3) valor de cobrança do admin ao owner: somente Owner.valor (nunca BasePreco como fallback)
    owner = db.scalar(select(Owner).where(Owner.sub_base == sub_base))
    valor_cobranca_owner = _decimal(getattr(owner, "valor", 0)) if owner else Decimal("0")

    # 4) Normaliza itens e detecta duplicados no próprio payload (zero DB)
    #    (você vai tratar duplicidade no front, mas aqui evita lixo óbvio e reduz queries)
    norm_codes: List[str] = []
    seen = set()
    for it in payload.itens:
        c = (it.codigo or "").strip()
        if not c:
            raise HTTPException(422, "Código inválido.")
        if c in seen:
            # Mantém comportamento de falhar, mas agora sem DB
            raise HTTPException(409, f"Código '{c}' duplicado no lote.")
        seen.add(c)
        norm_codes.append(c)

    # 5) Checagem de duplicidade no banco em 1 consulta (IN)
    #    Mesmo com front ajustado, isso protege integridade e evita N SELECTs.
    existing_codes = set(
        db.scalars(
            select(Saida.codigo).where(
                Saida.sub_base == sub_base,
                Saida.codigo.in_(norm_codes)
            )
        ).all()
    )
    if existing_codes:
        # Para manter compatibilidade com o comportamento antigo (falha no primeiro),
        # escolhemos um determinístico.
        dup = sorted(existing_codes)[0]
        raise HTTPException(409, f"Código '{dup}' já coletado.")

    created = 0
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

        # 6) Inserção em loop, sem SELECTs dentro
        for item in payload.itens:
            serv_key = _normalize_servico(item.servico)
            codigo = item.codigo.strip()

            saida = Saida(
                sub_base=sub_base,
                base=payload.base,
                username=getattr(current_user, "username", None),
                entregador=entregador_nome,
                entregador_id=entregador_id,
                codigo=codigo,
                servico=_servico_label_for_saida(serv_key),
                status="coletado",
                id_coleta=coleta.id_coleta,
            )
            db.add(saida)
            db.flush()

            # Cobrança do admin ao owner: somente Owner.valor por pacote (nunca BasePreco)
            db.add(
                OwnerCobrancaItem(
                    sub_base=sub_base,
                    id_coleta=coleta.id_coleta,
                    id_saida=saida.id_saida,
                    valor=valor_cobranca_owner,
                )
            )

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

        db.commit()
        db.refresh(coleta)

    except HTTPException:
        db.rollback()
        raise
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
# GET /coletas
# (REFATORADO: sub_base vem do JWT; remove 2-3 SELECTs por request)
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
    sub_base_user = _sub_base_from_token_or_422(current_user)

    stmt = select(Coleta).where(Coleta.sub_base == sub_base_user)

    if base:
        stmt = stmt.where(Coleta.base == base.strip())

    if username_entregador:
        stmt = stmt.where(Coleta.username_entregador == username_entregador.strip())

    if data_inicio:
        stmt = stmt.where(Coleta.timestamp >= data_inicio)

    if data_fim:
        # inclui dia inteiro
        dt_end = datetime.datetime.combine(data_fim, datetime.time(23, 59, 59))
        stmt = stmt.where(Coleta.timestamp <= dt_end)

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
# (REFATORADO: sub_base vem do JWT; remove 2-3 SELECTs por request)
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
    sumShopee: int
    sumMercado: int
    sumAvulso: int
    sumValor: Decimal
    sumCancelados: int
    sumTotalColetas: int


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
    sub_base_user = _sub_base_from_token_or_422(current_user)

    base_norm = base.strip().lower() if base else None

    # intervalo fim (incluindo dia inteiro)
    dt_end = None
    if data_fim:
        dt_end = datetime.datetime.combine(data_fim, datetime.time(23, 59, 59))

    # ----------------------------------------------------------
    # Filtro principal — tabela COLETAS
    # ----------------------------------------------------------
    stmt = select(Coleta).where(Coleta.sub_base == sub_base_user)

    if base_norm:
        stmt = stmt.where(func.lower(Coleta.base) == base_norm)

    if data_inicio:
        stmt = stmt.where(Coleta.timestamp >= data_inicio)

    if dt_end:
        stmt = stmt.where(Coleta.timestamp <= dt_end)

    stmt = stmt.order_by(Coleta.timestamp.asc())
    rows = db.scalars(stmt).all()

    # ----------------------------------------------------------
    # Buscar cancelados — tabela SAIDAS (mantido)
    # ----------------------------------------------------------
    cancelados_stmt = select(Saida).where(
        Saida.sub_base == sub_base_user,
        func.lower(Saida.status) == "cancelado"
    )

    if base_norm:
        cancelados_stmt = cancelados_stmt.where(func.lower(Saida.base) == base_norm)

    if data_inicio:
        cancelados_stmt = cancelados_stmt.where(Saida.timestamp >= data_inicio)

    if dt_end:
        cancelados_stmt = cancelados_stmt.where(Saida.timestamp <= dt_end)

    cancelados_rows = db.scalars(cancelados_stmt).all()

    mapa_cancelados: Dict[str, int] = {}
    for c in cancelados_rows:
        dia = c.timestamp.date().isoformat()
        baseKey = (c.base or "").strip().upper()
        key = f"{dia}_{baseKey}"
        mapa_cancelados[key] = mapa_cancelados.get(key, 0) + 1

    # ----------------------------------------------------------
    # Agrupar coletas por DIA + BASE
    # ----------------------------------------------------------
    agrupado: Dict[str, Dict] = {}
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

    lista: List[ResumoItem] = []
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
                entregadores=" | ".join(item["entregadores"]),
            )
        )

    lista.sort(key=lambda x: x.data)

    sumShopee = sum(i.shopee for i in lista)
    sumMercado = sum(i.mercado_livre for i in lista)
    sumAvulso = sum(i.avulso for i in lista)
    sumValor = sum(i.valor_total for i in lista)
    sumCancelados = sum(i.cancelados for i in lista)
    sumTotalColetas = sumShopee + sumMercado + sumAvulso

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
        items=items,
        sumShopee=sumShopee,
        sumMercado=sumMercado,
        sumAvulso=sumAvulso,
        sumValor=sumValor,
        sumCancelados=sumCancelados,
        sumTotalColetas=sumTotalColetas,
    )
