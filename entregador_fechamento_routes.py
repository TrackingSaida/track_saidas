"""
Rotas de Fechamento de Entregador
POST /entregadores/fechamentos — criar
PATCH /entregadores/fechamentos/{id_fechamento} — editar/reabrir
GET /entregadores/fechamentos/{id_fechamento} — obter um (para modal)
GET/PUT /entregadores/fechamentos/config — critério por sub_base
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from models import (
    Entregador,
    EntregadorFechamento,
    EntregadorFechamentoItem,
    Motoboy,
    MotoboySubBase,
    Saida,
    User,
)
from saida_operacional_utils import filtrar_saidas_por_periodo_operacional

from entregador_routes import (
    _resolve_user_base,
    resolver_precos_entregador,
    resolver_precos_motoboy,
    _calcular_valor_base_motoboy_periodo,
    _calcular_valor_base_periodo,
    _normalizar_servico,
    _toggle_pacote_g_ativo,
    STATUS_VALOR_BASE_VALIDOS,
)
from fechamento_criterio_pure import (
    MODO_CONFIRMACAO_ENTREGA,
    MODO_OPERACIONAL,
    MODOS_VALIDOS,
    normalizar_modo,
)
from fechamento_criterio_service import (
    calcular_itens_fechamento,
    get_modo_fechamento,
    persistir_itens_fechamento,
    saidas_ja_fechadas,
    upsert_modo_fechamento,
)

router = APIRouter(prefix="", tags=["Fechamentos"])

# Status aceitos
STATUS_GERADO = "GERADO"
STATUS_REAJUSTADO = "REAJUSTADO"

# Status válidos para saidas no cálculo (fonte única compartilhada)
STATUS_SAIDAS_VALIDOS = STATUS_VALOR_BASE_VALIDOS


def _contar_g_por_servico_entregador(
    db: Session,
    sub_base: str,
    id_entregador: int,
    periodo_inicio: date,
    periodo_fim: date,
) -> dict:
    """Conta saídas com is_grande no período por serviço (shopee, ml, avulso)."""
    stmt = select(Saida).where(
        Saida.sub_base == sub_base,
        Saida.entregador_id == id_entregador,
        Saida.is_grande.is_(True),
    )
    stmt = stmt.where(func.lower(Saida.status).in_(STATUS_SAIDAS_VALIDOS))
    rows_raw = db.scalars(stmt).all()
    rows, _ = filtrar_saidas_por_periodo_operacional(db, rows_raw, periodo_inicio, periodo_fim)
    g_shopee = g_ml = g_avulso = 0
    for s in rows:
        t = _normalizar_servico(s.servico)
        if t == "shopee":
            g_shopee += 1
        elif t == "flex":
            g_ml += 1
        else:
            g_avulso += 1
    return {"shopee": g_shopee, "ml": g_ml, "avulso": g_avulso, "total": g_shopee + g_ml + g_avulso}


def _contar_g_de_itens(itens) -> dict:
    g_shopee = g_ml = g_avulso = 0
    for it in itens:
        if not it.is_grande:
            continue
        t = _normalizar_servico(it.servico)
        if t == "shopee":
            g_shopee += 1
        elif t == "flex":
            g_ml += 1
        else:
            g_avulso += 1
    return {"shopee": g_shopee, "ml": g_ml, "avulso": g_avulso, "total": g_shopee + g_ml + g_avulso}


def _contar_g_por_servico_motoboy(
    db: Session,
    sub_base: str,
    motoboy_id: int,
    periodo_inicio: date,
    periodo_fim: date,
    modo: Optional[str] = None,
) -> dict:
    """Conta saídas com is_grande no período por serviço (shopee, ml, avulso)."""
    modo_norm = normalizar_modo(modo or get_modo_fechamento(db, sub_base))
    precos = resolver_precos_motoboy(db, sub_base, motoboy_id=motoboy_id)
    itens, _, _ = calcular_itens_fechamento(
        db,
        sub_base=sub_base,
        periodo_inicio=periodo_inicio,
        periodo_fim=periodo_fim,
        precos=precos,
        modo=modo_norm,
        motoboy_id=motoboy_id,
        toggle_pacote_g=False,
    )
    return _contar_g_de_itens(itens)


def _resolve_motoboy_subbase(db: Session, sub_base: str, motoboy_id: int) -> Motoboy:
    """Retorna o Motoboy se existir e estiver vinculado à sub_base."""
    motoboy = db.get(Motoboy, motoboy_id)
    if not motoboy:
        raise HTTPException(404, "Motoboy não encontrado.")
    vinc = db.scalar(
        select(MotoboySubBase).where(
            MotoboySubBase.motoboy_id == motoboy_id,
            MotoboySubBase.sub_base == sub_base,
            MotoboySubBase.ativo.is_(True),
        )
    )
    if not vinc:
        raise HTTPException(422, "Motoboy não vinculado a esta sub_base.")
    return motoboy


def _get_motoboy_username(db: Session, motoboy: Motoboy) -> str:
    """Username ou nome do User do motoboy para username_entregador."""
    if not motoboy or not motoboy.user_id:
        return f"Motoboy {motoboy.id_motoboy}"
    u = db.get(User, motoboy.user_id)
    if not u:
        return f"Motoboy {motoboy.id_motoboy}"
    return (u.username or f"{u.nome or ''} {u.sobrenome or ''}".strip() or f"Motoboy {motoboy.id_motoboy}").strip()


def _get_motoboy_chave_pix(db: Session, motoboy_id: int) -> Optional[str]:
    """Busca a chave PIX atual do motoboy, quando existir."""
    motoboy = db.get(Motoboy, motoboy_id)
    if not motoboy:
        return None
    return (getattr(motoboy, "chave_pix", None) or "").strip() or None


def _assert_admin_root(current_user: User) -> None:
    role = int(getattr(current_user, "role", 0) or 0)
    if role not in (0, 1):
        raise HTTPException(status_code=403, detail="Acesso restrito a admin/root.")


def _modo_do_fechamento(fech: EntregadorFechamento) -> str:
    return normalizar_modo(getattr(fech, "modo_criterio", None) or MODO_OPERACIONAL)


def _itens_out(db: Session, id_fechamento: int) -> List[Dict[str, Any]]:
    rows = db.scalars(
        select(EntregadorFechamentoItem)
        .where(EntregadorFechamentoItem.id_fechamento == id_fechamento)
        .order_by(
            EntregadorFechamentoItem.data_confirmacao.nullslast(),
            EntregadorFechamentoItem.data_operacional.nullslast(),
            EntregadorFechamentoItem.id_item,
        )
    ).all()
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "id_item": r.id_item,
                "id_saida": r.id_saida,
                "codigo": r.codigo,
                "id_motoboy": r.id_motoboy,
                "servico": r.servico,
                "status_evento": r.status_evento,
                "valor": r.valor,
                "is_grande": bool(r.is_grande),
                "data_operacional": r.data_operacional.isoformat() if r.data_operacional else None,
                "data_confirmacao": r.data_confirmacao.isoformat() if r.data_confirmacao else None,
            }
        )
    return out


# =========================================================
# SCHEMAS
# =========================================================

class FechamentoCreate(BaseModel):
    id_entregador: Optional[int] = Field(None, gt=0)
    id_motoboy: Optional[int] = Field(None, gt=0)
    periodo_inicio: date
    periodo_fim: date
    valor_adicao: Optional[Decimal] = Decimal("0.00")
    motivo_adicao: Optional[str] = None
    valor_subtracao: Optional[Decimal] = Decimal("0.00")
    motivo_subtracao: Optional[str] = None

    @model_validator(mode="after")
    def check_actor(self):
        if (self.id_entregador is None) == (self.id_motoboy is None):
            raise ValueError("Informe exatamente um de id_entregador ou id_motoboy.")
        return self


class FechamentoUpdate(BaseModel):
    valor_adicao: Optional[Decimal] = None
    motivo_adicao: Optional[str] = None
    valor_subtracao: Optional[Decimal] = None
    motivo_subtracao: Optional[str] = None
    atualizar_valor_base: Optional[bool] = None  # True = usar valor_base recalculado


class FechamentoOut(BaseModel):
    id_fechamento: int
    sub_base: str
    id_entregador: Optional[int] = None
    id_motoboy: Optional[int] = None
    username_entregador: Optional[str] = None
    chave_pix: Optional[str] = None
    periodo_inicio: date
    periodo_fim: date
    valor_base: Decimal
    valor_adicao: Decimal
    motivo_adicao: Optional[str] = None
    valor_subtracao: Decimal
    motivo_subtracao: Optional[str] = None
    valor_final: Decimal
    status: str
    criado_em: Optional[datetime] = None
    divergencia_valor_base: Optional[bool] = None
    valor_base_recalculado: Optional[Decimal] = None
    modo_criterio: str = MODO_OPERACIONAL
    criterio_data_label: str = "Data da operação"
    itens: Optional[List[Dict[str, Any]]] = None
    previa: Optional[Dict[str, Any]] = None


class FechamentoConfigOut(BaseModel):
    sub_base: str
    modo: str
    criterio_data_label: str
    modos_disponiveis: List[Dict[str, str]]
    updated_at: Optional[datetime] = None


class FechamentoConfigUpdate(BaseModel):
    modo: str = Field(min_length=1)


def _label_criterio(modo: str) -> str:
    return "Data da entrega" if normalizar_modo(modo) == MODO_CONFIRMACAO_ENTREGA else "Data da operação"


def _modos_disponiveis() -> List[Dict[str, str]]:
    return [
        {
            "valor": MODO_OPERACIONAL,
            "label": "Por saída operacional",
            "descricao": "Considera bipagem/atribuição no período (comportamento atual).",
        },
        {
            "valor": MODO_CONFIRMACAO_ENTREGA,
            "label": "Por confirmação de entrega",
            "descricao": "Considera somente entregas confirmadas no período. Ausentes não entram.",
        },
    ]


# =========================================================
# GET/PUT — Configuração do critério por sub_base
# =========================================================

@router.get("/fechamentos/config", response_model=FechamentoConfigOut)
def obter_config_fechamento(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    sub_base = _resolve_user_base(db, current_user)
    modo = get_modo_fechamento(db, sub_base)
    from models import SubBaseFechamentoConfig

    row = db.scalars(
        select(SubBaseFechamentoConfig).where(SubBaseFechamentoConfig.sub_base == sub_base)
    ).first()
    return FechamentoConfigOut(
        sub_base=sub_base,
        modo=modo,
        criterio_data_label=_label_criterio(modo),
        modos_disponiveis=_modos_disponiveis(),
        updated_at=row.updated_at if row else None,
    )


@router.put("/fechamentos/config", response_model=FechamentoConfigOut)
def atualizar_config_fechamento(
    payload: FechamentoConfigUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _assert_admin_root(current_user)
    sub_base = _resolve_user_base(db, current_user)
    modo = normalizar_modo(payload.modo)
    if modo not in MODOS_VALIDOS:
        raise HTTPException(400, "modo inválido. Use operacional ou confirmacao_entrega.")
    row = upsert_modo_fechamento(
        db,
        sub_base=sub_base,
        modo=modo,
        updated_by=getattr(current_user, "id", None),
    )
    return FechamentoConfigOut(
        sub_base=sub_base,
        modo=row.modo,
        criterio_data_label=_label_criterio(row.modo),
        modos_disponiveis=_modos_disponiveis(),
        updated_at=row.updated_at,
    )


# =========================================================
# GET — Calcular valor_base (preview para modal)
# =========================================================

@router.get("/fechamentos/calcular")
def calcular_valor_base_preview(
    entregador_id: Optional[int] = Query(None),
    motoboy_id: Optional[int] = Query(None),
    periodo_inicio: date = Query(...),
    periodo_fim: date = Query(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Retorna valor_base calculado para o período (sem criar fechamento). Informe entregador_id ou motoboy_id."""
    sub_base = _resolve_user_base(db, current_user)
    modo = get_modo_fechamento(db, sub_base)

    if periodo_inicio > periodo_fim:
        raise HTTPException(400, "periodo_inicio deve ser anterior a periodo_fim.")
    if periodo_fim >= date.today():
        raise HTTPException(
            400,
            "Não é permitido calcular fechamento para período ainda em aberto. "
            "Escolha um período cuja data final seja anterior à data de hoje.",
        )
    if (entregador_id is None) == (motoboy_id is None):
        raise HTTPException(400, "Informe exatamente um de entregador_id ou motoboy_id.")

    if motoboy_id is not None:
        motoboy = _resolve_motoboy_subbase(db, sub_base, motoboy_id)
        precos = resolver_precos_motoboy(db, sub_base, motoboy_id=motoboy_id)
        itens, valor_base, previa = calcular_itens_fechamento(
            db,
            sub_base=sub_base,
            periodo_inicio=periodo_inicio,
            periodo_fim=periodo_fim,
            precos=precos,
            modo=modo,
            motoboy_id=motoboy_id,
            toggle_pacote_g=_toggle_pacote_g_ativo(db, sub_base),
            com_preview=True,
        )
        executor_nome = _get_motoboy_username(db, motoboy)
        g = _contar_g_de_itens(itens)
        ja = saidas_ja_fechadas(db, [i.id_saida for i in itens])
        return {
            "valor_base": valor_base,
            "entregador_id": None,
            "motoboy_id": motoboy_id,
            "entregador_nome": executor_nome,
            "periodo_inicio": periodo_inicio.isoformat(),
            "periodo_fim": periodo_fim.isoformat(),
            "g_por_servico": {"shopee": g["shopee"], "ml": g["ml"], "avulso": g["avulso"]},
            "g_total": g["total"],
            "modo_criterio": modo,
            "criterio_data_label": _label_criterio(modo),
            "qtde_itens": len(itens),
            "itens_ja_fechados": [
                {"id_saida": x.id_saida, "codigo": x.codigo, "id_fechamento": x.id_fechamento}
                for x in ja
            ],
            "previa": previa,
            "itens": [
                {
                    "id_saida": i.id_saida,
                    "codigo": i.codigo,
                    "servico": i.servico,
                    "status_evento": i.status_evento,
                    "valor": i.valor,
                    "is_grande": i.is_grande,
                    "data_operacional": i.data_operacional.isoformat() if i.data_operacional else None,
                    "data_confirmacao": i.data_confirmacao.isoformat() if i.data_confirmacao else None,
                }
                for i in itens
            ],
        }

    ent = db.get(Entregador, entregador_id)
    if not ent or ent.sub_base != sub_base:
        raise HTTPException(404, "Entregador não encontrado.")

    valor_base = _calcular_valor_base_periodo(
        db, sub_base, entregador_id, periodo_inicio, periodo_fim, modo=modo
    )
    g = _contar_g_por_servico_entregador(db, sub_base, entregador_id, periodo_inicio, periodo_fim)

    return {
        "valor_base": valor_base,
        "entregador_id": entregador_id,
        "motoboy_id": None,
        "entregador_nome": ent.nome or "",
        "periodo_inicio": periodo_inicio.isoformat(),
        "periodo_fim": periodo_fim.isoformat(),
        "g_por_servico": {"shopee": g["shopee"], "ml": g["ml"], "avulso": g["avulso"]},
        "g_total": g["total"],
        "modo_criterio": modo,
        "criterio_data_label": _label_criterio(modo),
        "previa": None,
        "itens": [],
    }


# =========================================================
# POST — Criar fechamento
# =========================================================

@router.post("/fechamentos", response_model=FechamentoOut, status_code=201)
def criar_fechamento(
    payload: FechamentoCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    sub_base = _resolve_user_base(db, current_user)
    modo = get_modo_fechamento(db, sub_base)

    if payload.periodo_inicio > payload.periodo_fim:
        raise HTTPException(400, "periodo_inicio deve ser anterior a periodo_fim.")
    if payload.periodo_fim >= date.today():
        raise HTTPException(
            400,
            "Não é permitido criar fechamento para período ainda em aberto. "
            "Escolha um período cuja data final seja anterior à data de hoje.",
        )

    chave_pix: Optional[str] = None
    itens_calc = []
    if payload.id_motoboy is not None:
        motoboy = _resolve_motoboy_subbase(db, sub_base, payload.id_motoboy)
        username_ent = _get_motoboy_username(db, motoboy)
        chave_pix = (getattr(motoboy, "chave_pix", None) or "").strip() or None
        id_entregador_val = None
        id_motoboy_val = payload.id_motoboy
        existente = db.scalar(
            select(EntregadorFechamento).where(
                EntregadorFechamento.sub_base == sub_base,
                EntregadorFechamento.id_motoboy == payload.id_motoboy,
                EntregadorFechamento.periodo_inicio == payload.periodo_inicio,
                EntregadorFechamento.periodo_fim == payload.periodo_fim,
            )
        )
        precos = resolver_precos_motoboy(db, sub_base, motoboy_id=payload.id_motoboy)
        itens_calc, valor_base, _ = calcular_itens_fechamento(
            db,
            sub_base=sub_base,
            periodo_inicio=payload.periodo_inicio,
            periodo_fim=payload.periodo_fim,
            precos=precos,
            modo=modo,
            motoboy_id=payload.id_motoboy,
            toggle_pacote_g=_toggle_pacote_g_ativo(db, sub_base),
        )
    else:
        ent = db.get(Entregador, payload.id_entregador)
        if not ent or ent.sub_base != sub_base:
            raise HTTPException(404, "Entregador não encontrado.")
        username_ent = ent.username_entregador or ent.nome or ""
        id_entregador_val = payload.id_entregador
        id_motoboy_val = None
        existente = db.scalar(
            select(EntregadorFechamento).where(
                EntregadorFechamento.sub_base == sub_base,
                EntregadorFechamento.id_entregador == payload.id_entregador,
                EntregadorFechamento.periodo_inicio == payload.periodo_inicio,
                EntregadorFechamento.periodo_fim == payload.periodo_fim,
            )
        )
        valor_base = _calcular_valor_base_periodo(
            db, sub_base, payload.id_entregador,
            payload.periodo_inicio, payload.periodo_fim,
            modo=modo,
        )

    if existente:
        raise HTTPException(
            409,
            "Já existe fechamento para este executor e período."
        )

    ja = saidas_ja_fechadas(db, [i.id_saida for i in itens_calc])
    if ja:
        codigos = ", ".join(sorted({(x.codigo or str(x.id_saida)) for x in ja})[:8])
        raise HTTPException(
            409,
            detail={
                "code": "ITENS_JA_FECHADOS",
                "message": (
                    "Há pedidos já incluídos em outro fechamento. "
                    f"Exemplos: {codigos}"
                ),
                "itens": [
                    {
                        "id_saida": x.id_saida,
                        "codigo": x.codigo,
                        "id_fechamento": x.id_fechamento,
                    }
                    for x in ja
                ],
            },
        )

    valor_ad = Decimal(str(payload.valor_adicao or 0)).quantize(Decimal("0.01"))
    valor_sub = Decimal(str(payload.valor_subtracao or 0)).quantize(Decimal("0.01"))
    valor_final = (valor_base + valor_ad - valor_sub).quantize(Decimal("0.01"))

    fech = EntregadorFechamento(
        sub_base=sub_base,
        id_entregador=id_entregador_val,
        id_motoboy=id_motoboy_val,
        username_entregador=username_ent,
        periodo_inicio=payload.periodo_inicio,
        periodo_fim=payload.periodo_fim,
        valor_base=valor_base,
        valor_adicao=valor_ad,
        motivo_adicao=(payload.motivo_adicao or "").strip() or None,
        valor_subtracao=valor_sub,
        motivo_subtracao=(payload.motivo_subtracao or "").strip() or None,
        valor_final=valor_final,
        status=STATUS_GERADO,
        modo_criterio=modo,
    )
    db.add(fech)
    db.flush()
    if itens_calc:
        persistir_itens_fechamento(db, fech.id_fechamento, itens_calc)
    db.commit()
    db.refresh(fech)

    return FechamentoOut(
        id_fechamento=fech.id_fechamento,
        sub_base=fech.sub_base,
        id_entregador=fech.id_entregador,
        id_motoboy=fech.id_motoboy,
        username_entregador=fech.username_entregador,
        chave_pix=chave_pix,
        periodo_inicio=fech.periodo_inicio,
        periodo_fim=fech.periodo_fim,
        valor_base=fech.valor_base,
        valor_adicao=fech.valor_adicao,
        motivo_adicao=fech.motivo_adicao,
        valor_subtracao=fech.valor_subtracao,
        motivo_subtracao=fech.motivo_subtracao,
        valor_final=fech.valor_final,
        status=fech.status,
        criado_em=fech.criado_em,
        modo_criterio=_modo_do_fechamento(fech),
        criterio_data_label=_label_criterio(_modo_do_fechamento(fech)),
        itens=_itens_out(db, fech.id_fechamento),
    )


# =========================================================
# GET — Obter fechamento (para modal de edição)
# =========================================================

@router.get("/fechamentos/{id_fechamento}", response_model=FechamentoOut)
def obter_fechamento(
    id_fechamento: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    sub_base = _resolve_user_base(db, current_user)

    fech = db.get(EntregadorFechamento, id_fechamento)
    if not fech or fech.sub_base != sub_base:
        raise HTTPException(404, "Fechamento não encontrado.")

    modo = _modo_do_fechamento(fech)
    chave_pix: Optional[str] = None
    if getattr(fech, "id_motoboy", None) is not None:
        chave_pix = _get_motoboy_chave_pix(db, fech.id_motoboy)
        valor_base_recalc = _calcular_valor_base_motoboy_periodo(
            db, sub_base, fech.id_motoboy,
            fech.periodo_inicio, fech.periodo_fim,
            modo=modo,
        )
    else:
        valor_base_recalc = _calcular_valor_base_periodo(
            db, sub_base, fech.id_entregador,
            fech.periodo_inicio, fech.periodo_fim,
            modo=modo,
        )
    divergencia = valor_base_recalc != fech.valor_base

    return FechamentoOut(
        id_fechamento=fech.id_fechamento,
        sub_base=fech.sub_base,
        id_entregador=fech.id_entregador,
        id_motoboy=getattr(fech, "id_motoboy", None),
        username_entregador=fech.username_entregador,
        chave_pix=chave_pix,
        periodo_inicio=fech.periodo_inicio,
        periodo_fim=fech.periodo_fim,
        valor_base=fech.valor_base,
        valor_adicao=fech.valor_adicao,
        motivo_adicao=fech.motivo_adicao,
        valor_subtracao=fech.valor_subtracao,
        motivo_subtracao=fech.motivo_subtracao,
        valor_final=fech.valor_final,
        status=fech.status,
        criado_em=fech.criado_em,
        divergencia_valor_base=divergencia if divergencia else None,
        valor_base_recalculado=valor_base_recalc if divergencia else None,
        modo_criterio=modo,
        criterio_data_label=_label_criterio(modo),
        itens=_itens_out(db, fech.id_fechamento),
    )


# =========================================================
# PATCH — Editar / Reabrir fechamento
# =========================================================

@router.patch("/fechamentos/{id_fechamento}", response_model=FechamentoOut)
def atualizar_fechamento(
    id_fechamento: int,
    payload: FechamentoUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    sub_base = _resolve_user_base(db, current_user)

    fech = db.get(EntregadorFechamento, id_fechamento)
    if not fech or fech.sub_base != sub_base:
        raise HTTPException(404, "Fechamento não encontrado.")
    if (fech.status or "").upper() != STATUS_GERADO:
        raise HTTPException(
            400,
            "Apenas fechamentos com status GERADO podem ser reajustados.",
        )

    modo = _modo_do_fechamento(fech)
    chave_pix: Optional[str] = None
    if getattr(fech, "id_motoboy", None) is not None:
        chave_pix = _get_motoboy_chave_pix(db, fech.id_motoboy)
        valor_base_recalc = _calcular_valor_base_motoboy_periodo(
            db, sub_base, fech.id_motoboy,
            fech.periodo_inicio, fech.periodo_fim,
            modo=modo,
        )
    else:
        valor_base_recalc = _calcular_valor_base_periodo(
            db, sub_base, fech.id_entregador,
            fech.periodo_inicio, fech.periodo_fim,
            modo=modo,
        )

    if payload.atualizar_valor_base is True:
        fech.valor_base = valor_base_recalc

    # Atualizar adição/subtração
    if payload.valor_adicao is not None:
        fech.valor_adicao = Decimal(str(payload.valor_adicao)).quantize(Decimal("0.01"))
    if payload.motivo_adicao is not None:
        fech.motivo_adicao = (payload.motivo_adicao or "").strip() or None
    if payload.valor_subtracao is not None:
        fech.valor_subtracao = Decimal(str(payload.valor_subtracao)).quantize(Decimal("0.01"))
    if payload.motivo_subtracao is not None:
        fech.motivo_subtracao = (payload.motivo_subtracao or "").strip() or None

    # Recalcular valor_final
    fech.valor_final = (
        fech.valor_base + fech.valor_adicao - fech.valor_subtracao
    ).quantize(Decimal("0.01"))

    fech.status = STATUS_REAJUSTADO

    db.commit()
    db.refresh(fech)

    return FechamentoOut(
        id_fechamento=fech.id_fechamento,
        sub_base=fech.sub_base,
        id_entregador=fech.id_entregador,
        id_motoboy=getattr(fech, "id_motoboy", None),
        username_entregador=fech.username_entregador,
        chave_pix=chave_pix,
        periodo_inicio=fech.periodo_inicio,
        periodo_fim=fech.periodo_fim,
        valor_base=fech.valor_base,
        valor_adicao=fech.valor_adicao,
        motivo_adicao=fech.motivo_adicao,
        valor_subtracao=fech.valor_subtracao,
        motivo_subtracao=fech.motivo_subtracao,
        valor_final=fech.valor_final,
        status=fech.status,
        criado_em=fech.criado_em,
        modo_criterio=modo,
        criterio_data_label=_label_criterio(modo),
        itens=_itens_out(db, fech.id_fechamento),
    )
