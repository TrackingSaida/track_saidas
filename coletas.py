# coletas.py
from __future__ import annotations

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
    timestamp: datetime
    base: str
    sub_base: str
    username_entregador: str
    shopee: int
    mercado_livre: int
    avulso: int
    valor_total: Decimal
    model_config = ConfigDict(from_attributes=True)


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
    # etiqueta que ficará em `saidas.servico`
    return "Mercado Livre" if s == "mercado_livre" else s


def _find_entregador_for_user(db: Session, user: User) -> Entregador:
    """
    Resolve o entregador do usuário autenticado.
    1) tenta por User.username_entregador
    2) cai para User.username
    Exige que o entregador exista e esteja ativo.
    """
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
    """
    Resolve (sub_base, nome_exibicao, username_entregador) do usuário autenticado.
    1) Se tiver entregador ativo vinculado, usa ele.
    2) Caso contrário, usa sub_base do próprio usuário (usuário de sistema / owner).
    """
    # tenta localizar entregador vinculado
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

    # se não tiver entregador, tenta pegar sub_base direto do user (usuário de sistema)
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
# POST /coletas/lote (novo fluxo)
# =========================
@router.post("/lote", status_code=status.HTTP_201_CREATED, response_model=LoteResponse)
def registrar_coleta_em_lote(
    payload: ColetaLoteIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Recebe vários códigos, grava cada um em `saidas` (status='coletado', com a base do payload),
    e grava o consolidado em `coletas` calculando o valor_total a partir da tabela `base`
    (preços por sub_base e base).

    ✅ Agora permite também usuários do sistema (sem entregador vinculado),
    desde que possuam 'sub_base' definida em 'users'.
    """
    # 1) Resolve entregador OU sub_base do usuário (flexível)
    sub_base, entregador_nome, username_entregador = _resolve_entregador_ou_user_base(db, current_user)

    # 2) preços da base
    p_shopee, p_ml, p_avulso = _get_precos(db, sub_base=sub_base, base=payload.base)

    # 3) percorrer itens, inserir em `saidas` e contar por serviço
    count = {"shopee": 0, "mercado_livre": 0, "avulso": 0}
    duplicates: List[str] = []
    created = 0

    try:
        for item in payload.itens:
            serv = _normalize_servico(item.servico)
            codigo = (item.codigo or "").strip()
            if not codigo:
                raise HTTPException(status_code=422, detail="Código vazio no payload.")

            # de-dup por (sub_base, codigo)
            exists = db.scalar(select(Saida).where(Saida.sub_base == sub_base, Saida.codigo == codigo))
            if exists:
                duplicates.append(codigo)
                continue

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

        # 4) consolidado em `coletas`
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

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Falha ao registrar lote: {e}")

    # 5) retorno
    return LoteResponse(
        coleta=ColetaOut.model_validate(coleta),
        resumo=ResumoLote(
            inseridos=created,
            duplicados=len(duplicates),
            codigos_duplicados=duplicates,
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
from datetime import date

@router.get("/", response_model=List[ColetaOut])
def list_coletas(
    base: Optional[str] = Query(None, description="Filtra por base ex.: '3AS'"),
    username_entregador: Optional[str] = Query(None, description="Filtra por username do entregador"),
    data_inicio: Optional[date] = Query(None, description="Filtra coletas a partir desta data (YYYY-MM-DD)"),
    data_fim: Optional[date] = Query(None, description="Filtra coletas até esta data (YYYY-MM-DD)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Lista coletas visíveis para a sub_base do usuário autenticado.
    """
    # sub_base do usuário autenticado
    user_id = getattr(current_user, "id", None)
    sub_base_user: Optional[str] = None
    if user_id is not None:
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

    # ====== Filtros ======
    stmt = select(Coleta).where(Coleta.sub_base == sub_base_user)

    if base:
        stmt = stmt.where(Coleta.base == base.strip())
    if username_entregador:
        stmt = stmt.where(Coleta.username_entregador == username_entregador.strip())
    if data_inicio:
        stmt = stmt.where(Coleta.timestamp >= data_inicio)
    if data_fim:
        stmt = stmt.where(Coleta.timestamp <= data_fim)

    stmt = stmt.order_by(Coleta.timestamp.desc())
    rows = db.scalars(stmt).all()
    return rows
