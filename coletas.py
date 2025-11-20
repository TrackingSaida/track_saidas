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

    # ====== Ajuste: contabilizar cancelamentos ======
    # Se houver registros de saídas com status "cancelado" dentro do período/base
    # estes devem ser descontados do resumo de coletas (por data/base/serviço)
    # A lógica a seguir consolida cancelamentos por data e base, e subtrai das
    # contagens de coletas correspondentes antes de retornar ao front.
    try:
        # agrupa cancelamentos por (data, base)
        cancelamentos: Dict[Tuple[datetime.date, Optional[str]], Dict[str, Decimal]] = {}

        # Constrói filtro para Saida cancelada na mesma sub_base
        cancel_stmt = select(Saida).where(
            Saida.sub_base == sub_base_user,
            # status pode ser 'Cancelado' (case-insensitive)
            Saida.status.ilike("%cancelado%"),
        )
        # Aplica mesmo filtro de base, se fornecido
        if base:
            cancel_stmt = cancel_stmt.where(Saida.base == base.strip())
        # Aplica filtros de data caso definidos (mesma lógica do list_coletas)
        if data_inicio:
            # início do dia para timestamp
            dt_ini = datetime.datetime.combine(data_inicio, datetime.time.min)
            cancel_stmt = cancel_stmt.where(Saida.timestamp >= dt_ini)
        if data_fim:
            # final do dia para timestamp
            dt_fim = datetime.datetime.combine(data_fim, datetime.time.max)
            cancel_stmt = cancel_stmt.where(Saida.timestamp <= dt_fim)

        cancel_rows = db.scalars(cancel_stmt).all()

        # Define função local para classificar o serviço do cancelamento
        def _class_servico(raw: Optional[str]) -> Optional[str]:
            s = (raw or "").strip().lower()
            if not s:
                return None
            # As saídas gravadas via lote usam rótulos 'shopee', 'Mercado Livre', 'avulso'
            # normalizamos para as mesmas chaves usadas em Coleta.shopee/mercado_livre/avulso
            if s == "shopee":
                return "shopee"
            # lidar com "mercado livre" ou variantes
            if s.replace("_", " ").replace("  ", " ") in {"mercado livre", "mercado_livre", "mercado livre"}:
                return "mercado_livre"
            # 'ml' também representa Mercado Livre
            if s == "ml":
                return "mercado_livre"
            if s == "mercadolivre":
                return "mercado_livre"
            if s == "avulso":
                return "avulso"
            # fallback: tenta com espaços normalizados
            if s.replace(" ", "").startswith("mercadolivre"):
                return "mercado_livre"
            return None

        # Agrupa cancelamentos por data/base e serviço
        for c in cancel_rows:
            key = (c.timestamp.date(), getattr(c, "base", None))
            # ignora cancelamento sem base associada
            if not key[1]:
                continue
            # classifica serviço
            sv = _class_servico(getattr(c, "servico", None))
            if not sv:
                continue
            if key not in cancelamentos:
                cancelamentos[key] = {
                    "shopee": Decimal(0),
                    "mercado_livre": Decimal(0),
                    "avulso": Decimal(0),
                    "valor_total": Decimal(0),
                }
            cancelamentos[key][sv] += Decimal(1)

        # calcula valor_total a ser descontado por base utilizando preços por serviço
        # (utiliza _get_precos para buscar preço da base somente uma vez)
        precos_cache: Dict[Optional[str], Tuple[Decimal, Decimal, Decimal]] = {}
        for (d, b), vals in list(cancelamentos.items()):
            # obtém preços
            if b not in precos_cache:
                try:
                    precos_cache[b] = _get_precos(db, sub_base=sub_base_user, base=b)
                except Exception:
                    # se não encontrar preços, define zero para evitar erro
                    precos_cache[b] = (Decimal(0), Decimal(0), Decimal(0))
            p_sh, p_ml, p_av = precos_cache[b]
            # calcula total por cancelamento
            total_cancel = (
                (vals.get("shopee", 0) * p_sh)
                + (vals.get("mercado_livre", 0) * p_ml)
                + (vals.get("avulso", 0) * p_av)
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            cancelamentos[(d, b)]["valor_total"] = total_cancel

        # Realiza o desconto nas contagens/valores dos objetos retornados
        if cancelamentos:
            # percorre rows na ordem original (já ordenados por timestamp desc)
            for r in rows:
                key = (r.timestamp.date(), getattr(r, "base", None))
                if key in cancelamentos:
                    canc = cancelamentos[key]
                    # obtém preços para este base
                    p_sh, p_ml, p_av = precos_cache.get(key[1], (Decimal(0), Decimal(0), Decimal(0)))
                    # desconta shopee
                    sub_sh = 0
                    if getattr(r, "shopee", 0) > 0 and canc.get("shopee", 0) > 0:
                        sub_sh = min(int(r.shopee), int(canc["shopee"]))
                        r.shopee -= sub_sh
                        canc["shopee"] -= Decimal(sub_sh)
                    # desconta mercado_livre
                    sub_ml = 0
                    if getattr(r, "mercado_livre", 0) > 0 and canc.get("mercado_livre", 0) > 0:
                        sub_ml = min(int(r.mercado_livre), int(canc["mercado_livre"]))
                        r.mercado_livre -= sub_ml
                        canc["mercado_livre"] -= Decimal(sub_ml)
                    # desconta avulso
                    sub_av = 0
                    if getattr(r, "avulso", 0) > 0 and canc.get("avulso", 0) > 0:
                        sub_av = min(int(r.avulso), int(canc["avulso"]))
                        r.avulso -= sub_av
                        canc["avulso"] -= Decimal(sub_av)
                    # calcula valor total a descontar baseado nas quantidades removidas
                    sub_val = (
                        (Decimal(sub_sh) * p_sh)
                        + (Decimal(sub_ml) * p_ml)
                        + (Decimal(sub_av) * p_av)
                    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    if sub_val > 0 and getattr(r, "valor_total", None) is not None:
                        # valor_total em Coleta é um Decimal
                        r.valor_total = (Decimal(r.valor_total) - sub_val).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                        canc["valor_total"] -= sub_val
                    # se todos cancelamentos deste dia/base foram aplicados, remove da lista
                    if (
                        canc.get("shopee", 0) <= 0
                        and canc.get("mercado_livre", 0) <= 0
                        and canc.get("avulso", 0) <= 0
                        and canc.get("valor_total", 0) <= 0
                    ):
                        del cancelamentos[key]

        # após ajuste, remove linhas que ficaram zeradas
        adjusted_rows = []
        for r in rows:
            try:
                has_counts = (r.shopee or 0) > 0 or (r.mercado_livre or 0) > 0 or (r.avulso or 0) > 0
                has_valor = (r.valor_total or Decimal(0)) > Decimal(0)
            except Exception:
                has_counts = True
                has_valor = True
            if has_counts or has_valor:
                adjusted_rows.append(r)
        rows = adjusted_rows
    except Exception:
        # em caso de erro ao aplicar descontos, apenas retorna os dados originais
        pass

    return rows
