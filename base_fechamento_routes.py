"""
Rotas de Fechamento de Bases (Coletas)
GET /coletas/fechamentos/calcular — preview
GET /coletas/fechamentos — listar (visão A Receber)
GET /coletas/fechamentos/resumo-por-base — uma linha por base no período
POST /coletas/fechamentos/lote — gerar em lote as bases elegíveis
POST /coletas/fechamentos/marcar-recebido — baixar recebimentos
POST /coletas/fechamentos — criar
GET /coletas/fechamentos/{id_fechamento} — obter (para modal)
PATCH /coletas/fechamentos/{id_fechamento} — reajustar
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from models import (
    BaseFechamento,
    BaseFechamentoItem,
    BasePreco,
    BaseSellerDados,
    Coleta,
    ColetaCalendarioExcecao,
    ColetaExecucao,
    Owner,
    Saida,
    User,
)

from base_fechamento_status_pure import (
    MOTIVO_JA_GERADO,
    fechamento_base_divergente,
    motivo_bloqueio_lote_base,
    status_base_permite_reajuste,
)
from coletas import _decimal, _get_precos_cached, _sub_base_from_token_or_422
from fechamento_valor_pure import adicional_g
from pacote_g_categorias import (
    SERVICOS_G,
    categoria_da_saida,
    g_categorias_com_padrao,
    padrao_id_ou_none,
    regras_por_categoria,
)

router = APIRouter(prefix="/fechamentos", tags=["Fechamentos Bases"])

STATUS_GERADO = "GERADO"
STATUS_REAJUSTADO = "REAJUSTADO"
STATUS_RECEBIDO = "RECEBIDO"
STATUS_ELEGIVEIS_RECEBIMENTO = {STATUS_GERADO, STATUS_REAJUSTADO}


def _exigir_admin_financeiro(current_user: User) -> None:
    try:
        role = int(current_user.role)
    except (TypeError, ValueError):
        role = -1
    if role not in (0, 1):
        raise HTTPException(403, "Apenas admin/root pode acessar o financeiro de bases.")


def _resumo_calendario_fechamento(
    db: Session,
    sub_base: str,
    base_nome: str,
    periodo_inicio: date,
    periodo_fim: date,
) -> dict:
    owner = db.scalar(select(Owner).where(Owner.sub_base == sub_base))
    if owner and bool(owner.ignorar_coleta):
        return {
            "coleta_habilitada": False,
            "agenda_confirmada": False,
            "pronto_para_fechamento": True,
            "dias_esperados": 0,
            "dias_com_lancamento": 0,
            "dias_feriado": 0,
            "dias_justificados": 0,
            "dias_pendentes": [],
        }
    base = db.scalar(
        select(BasePreco).where(
            BasePreco.sub_base == sub_base,
            func.upper(BasePreco.base) == base_nome.strip().upper(),
        )
    )
    if not base:
        raise HTTPException(404, "Base não encontrada nesta sub_base.")
    if not bool(base.agenda_coleta_confirmada):
        return {
            "coleta_habilitada": True,
            "agenda_confirmada": False,
            "pronto_para_fechamento": True,
            "dias_esperados": 0,
            "dias_com_lancamento": 0,
            "dias_feriado": 0,
            "dias_justificados": 0,
            "dias_pendentes": [],
        }
    excecoes = db.scalars(
        select(ColetaCalendarioExcecao).where(
            ColetaCalendarioExcecao.sub_base == sub_base,
            ColetaCalendarioExcecao.data >= periodo_inicio,
            ColetaCalendarioExcecao.data <= periodo_fim,
            (ColetaCalendarioExcecao.base_id == base.id_base)
            | (ColetaCalendarioExcecao.base_id.is_(None)),
        )
    ).all()
    globais = {item.data: item for item in excecoes if item.base_id is None}
    por_base = {item.data: item for item in excecoes if item.base_id == base.id_base}
    execucoes = {
        item.data_operacao: item
        for item in db.scalars(
            select(ColetaExecucao).where(
                ColetaExecucao.sub_base == sub_base,
                ColetaExecucao.base_id == base.id_base,
                ColetaExecucao.data_operacao >= periodo_inicio,
                ColetaExecucao.data_operacao <= periodo_fim,
            )
        ).all()
    }
    # Compatibilidade: coletas anteriores à migração ainda vivem apenas na
    # tabela legada e devem satisfazer a agenda da base.
    lancamentos_legados = {
        item.timestamp.date()
        for item in db.scalars(
            select(Coleta).where(
                Coleta.sub_base == sub_base,
                func.upper(Coleta.base) == base_nome.strip().upper(),
                Coleta.timestamp >= datetime.combine(periodo_inicio, datetime.min.time()),
                Coleta.timestamp <= datetime.combine(periodo_fim, datetime.max.time()),
            )
        ).all()
    }
    dias_configurados = set(base.dias_coleta or [])
    esperado = com_lancamento = feriados = justificados = 0
    pendentes: list[dict] = []
    cursor = periodo_inicio
    while cursor <= periodo_fim:
        excecao = por_base.get(cursor) or globais.get(cursor)
        programado = cursor.isoweekday() in dias_configurados
        coleta_extra = bool(excecao and excecao.tipo == "COLETA_EXTRA")
        if not programado and not coleta_extra:
            cursor += timedelta(days=1)
            continue
        esperado += 1
        if cursor in execucoes or cursor in lancamentos_legados:
            com_lancamento += 1
        elif excecao and excecao.tipo == "FERIADO":
            feriados += 1
        elif excecao and excecao.tipo in ("SEM_COLETA", "JUSTIFICADO"):
            justificados += 1
        else:
            pendentes.append({"data": cursor.isoformat(), "status": "PENDENTE"})
        cursor += timedelta(days=1)
    return {
        "coleta_habilitada": True,
        "agenda_confirmada": True,
        "pronto_para_fechamento": not pendentes,
        "dias_esperados": esperado,
        "dias_com_lancamento": com_lancamento,
        "dias_feriado": feriados,
        "dias_justificados": justificados,
        "dias_pendentes": pendentes,
    }


def _montar_seller_info(db: Session, sub_base: str, base: str) -> Optional[Dict[str, Any]]:
    """
    Retorna informações básicas do seller/base (nome, CNPJ e endereço completo)
    quando existirem em BaseSellerDados. Nunca lança erro; em caso de falta de
    dados retorna None.
    """
    base_norm = (base or "").strip()
    if not base_norm:
        return None

    # 1) Localiza cadastro de preços da base na sub_base
    preco = db.scalar(
        select(BasePreco).where(
            BasePreco.sub_base == sub_base,
            BasePreco.base == base_norm,
        )
    )
    seller: Optional[BaseSellerDados] = None

    # 2) Tenta primeiro pelo vínculo explícito base_id -> BaseSellerDados
    if preco and getattr(preco, "id_base", None) is not None:
        seller = db.scalar(
            select(BaseSellerDados).where(BaseSellerDados.base_id == preco.id_base)
        )

    # 3) Fallback legado: tenta resolver pelo Owner (username/sub_base) -> BaseSellerDados.owner_id
    #    Só é usado quando não há vínculo explícito por base_id (instalações antigas).
    if (
        not seller
        and preco
        and getattr(preco, "username", None)
        and getattr(preco, "id_base", None) is None
    ):
        owner = db.scalar(
            select(Owner).where(
                Owner.username == preco.username,
                Owner.sub_base == sub_base,
            )
        )
        if owner:
            seller = db.scalar(
                select(BaseSellerDados).where(BaseSellerDados.owner_id == owner.id_owner)
            )

    if not seller:
        return None

    # Monta endereço completo de forma resiliente a campos vazios
    partes_endereco: List[str] = []
    if seller.rua:
        rua_num = f"{seller.rua}".strip()
        if seller.numero:
            rua_num = f"{rua_num}, {seller.numero}".strip()
        partes_endereco.append(rua_num)
    if seller.complemento:
        partes_endereco.append(str(seller.complemento).strip())
    bairro_cidade: List[str] = []
    if seller.bairro:
        bairro_cidade.append(str(seller.bairro).strip())
    if seller.cidade:
        bairro_cidade.append(str(seller.cidade).strip())
    if bairro_cidade:
        partes_endereco.append(" - ".join(bairro_cidade))
    uf_cep: List[str] = []
    if seller.estado:
        uf_cep.append(str(seller.estado).strip())
    if seller.cep:
        uf_cep.append(str(seller.cep).strip())
    if uf_cep:
        partes_endereco.append(" ".join(uf_cep))

    endereco_completo = ", ".join([p for p in partes_endereco if p])

    cnpj_raw = (seller.cnpj or "").strip() or None
    cnpj_fmt = cnpj_raw
    if cnpj_raw:
        try:
            digits = "".join(c for c in cnpj_raw if c.isdigit())
            if len(digits) == 14:
                cnpj_fmt = f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}"
        except Exception:
            cnpj_fmt = cnpj_raw

    chave_pix = (getattr(seller, "chave_pix", None) or "").strip() or None

    info: Dict[str, Any] = {
        "nome_base": base_norm,
        "cnpj": cnpj_fmt,
        "endereco_completo": endereco_completo or None,
        "chave_pix": chave_pix,
    }
    return info


def _emitido_por(db: Session, sub_base: str) -> str:
    """
    Retorna o texto para "Emitido por" no relatório: nome_fantasia do Owner ou sub_base.
    Nunca lança erro; em caso de owner ausente ou campos vazios usa sub_base ou "ROTEVO".
    """
    sub = (sub_base or "").strip()
    owner = db.scalar(select(Owner).where(Owner.sub_base == sub)) if sub else None
    if not owner:
        return sub or "ROTEVO"
    nome = (getattr(owner, "nome_fantasia", None) or "").strip()
    if nome:
        return nome
    return (owner.sub_base or "").strip() or "ROTEVO"


def _normalizar_servico_saida(serv: str) -> str:
    """Mapeia Saida.servico para shopee | ml | avulso."""
    s = (serv or "").lower().strip()
    if "shopee" in s:
        return "shopee"
    if "mercado" in s or "ml" in s or "flex" in s:
        return "ml"
    return "avulso"


_REGRA_G_SEM_COBRANCA = {"modo": "valor_fixo", "valor": Decimal("0")}
ORIGENS_COLETA_G_MANUAL = ("manual", "ajuste")


def _contexto_g_base(db: Session, sub_base: str) -> tuple[Dict[int, Dict[str, Any]], Optional[int]]:
    """(regras "Cobrar da base" por categoria, categoria padrão para G legado)."""
    return regras_por_categoria(db, sub_base, "base"), padrao_id_ou_none(db, sub_base)


def _regra_g_base(regras: Dict[int, Dict[str, Any]], categoria_id: Any) -> Dict[str, Any]:
    try:
        return regras.get(int(categoria_id)) or _REGRA_G_SEM_COBRANCA
    except (TypeError, ValueError):
        return _REGRA_G_SEM_COBRANCA


def _valor_g_categorias(
    g_categorias: Optional[Dict[str, Dict[str, int]]],
    precos: Dict[str, Decimal],
    regras: Dict[int, Dict[str, Any]],
) -> Decimal:
    total = Decimal("0.00")
    for cid, qtds in (g_categorias or {}).items():
        regra = _regra_g_base(regras, cid)
        for tipo in SERVICOS_G:
            qtd = int((qtds or {}).get(tipo) or 0)
            if qtd:
                total += adicional_g(precos.get(tipo, Decimal("0")), regra) * qtd
    return total.quantize(Decimal("0.01"))


def _qtd_g_com_cobranca(
    g_categorias: Optional[Dict[str, Dict[str, int]]], regras: Dict[int, Dict[str, Any]]
) -> int:
    """Pacotes G cuja categoria tem valor de cobrança para a base (> 0)."""
    total = 0
    for cid, qtds in (g_categorias or {}).items():
        if Decimal(str(_regra_g_base(regras, cid).get("valor") or 0)) > 0:
            total += sum(int((qtds or {}).get(t) or 0) for t in SERVICOS_G)
    return total


def _g_categorias_item(it: Any, padrao_id: Optional[int]) -> Dict[str, Dict[str, int]]:
    get = it.get if isinstance(it, dict) else (lambda k, d=None: getattr(it, k, d))
    return g_categorias_com_padrao(
        get("g_categorias"), get("g_shopee", 0), get("g_ml", 0), get("g_avulso", 0), padrao_id
    )


def _precos_base(db: Session, sub_base: str, base: str) -> Dict[str, Decimal]:
    try:
        p_s, p_m, p_a = _get_precos_cached(db, sub_base, base)
    except HTTPException:
        p_s = p_m = p_a = Decimal("0.00")
    return {"shopee": p_s, "ml": p_m, "avulso": p_a}


def _valores_itens_informados(
    itens: List[Any],
    precos: Dict[str, Decimal],
    regras: Dict[int, Dict[str, Any]],
    padrao_id: Optional[int],
) -> tuple[Decimal, Decimal, Decimal]:
    """(bruto, cancelados, valor_g) de itens enviados pelo cliente; o G sempre segue as categorias."""
    p_s, p_m, p_a = precos["shopee"], precos["ml"], precos["avulso"]
    bruto = canc = g = Decimal("0.00")
    for it in itens:
        bruto += _decimal(it.shopee) * p_s + _decimal(it.mercado_livre) * p_m + _decimal(it.avulso) * p_a
        canc += _decimal(it.cancelados_shopee) * p_s + _decimal(it.cancelados_ml) * p_m + _decimal(it.cancelados_avulso) * p_a
        g += _valor_g_categorias(_g_categorias_item(it, padrao_id), precos, regras)
    return bruto, canc, g.quantize(Decimal("0.01"))


def _g_por_categoria(
    itens: List[Any],
    regras: Dict[int, Dict[str, Any]],
    padrao_id: Optional[int],
    precos: Optional[Dict[str, Decimal]] = None,
) -> List[GCategoriaResumo]:
    """Totais de G por categoria; com ``precos`` inclui o valor cobrado da base."""
    acum: Dict[str, Dict[str, int]] = {}
    for it in itens:
        for cid, qtds in _g_categorias_item(it, padrao_id).items():
            bucket = acum.setdefault(str(cid), {t: 0 for t in SERVICOS_G})
            for t in SERVICOS_G:
                bucket[t] += int((qtds or {}).get(t) or 0)
    out = []
    for cid, qtds in acum.items():
        qtd = sum(qtds.values())
        if not qtd:
            continue
        regra = regras.get(int(cid)) or {}
        out.append(
            GCategoriaResumo(
                categoria_g_id=int(cid),
                nome=regra.get("nome") or "G",
                qtd=qtd,
                valor=_valor_g_categorias({cid: qtds}, precos, regras) if precos is not None else None,
            )
        )
    out.sort(key=lambda r: r.nome)
    return out


def _build_itens_e_valores(
    db: Session,
    sub_base: str,
    base: str,
    periodo_inicio: date,
    periodo_fim: date,
) -> tuple[List[dict], Decimal, Decimal, Decimal]:
    """
    Retorna (itens, valor_bruto, valor_cancelados, valor_final), com valor_final já somando o G.
    itens: lista de {data, shopee, mercado_livre, avulso, cancelados_*, g_*, g_categorias, valor_g}
    """
    base_norm = base.strip()
    base_key = base_norm.upper()
    from datetime import time as dt_time
    dt_start = datetime.combine(periodo_inicio, dt_time.min)
    dt_end = datetime.combine(periodo_fim, dt_time(23, 59, 59))

    # Coletas: agrupar por (data, base). Inclui ajustes (origem=ajuste) com delta negativo.
    stmt_coletas = select(Coleta).where(
        Coleta.sub_base == sub_base,
        func.upper(Coleta.base) == base_key,
        Coleta.timestamp >= dt_start,
        Coleta.timestamp <= dt_end,
    ).where(
        (Coleta.shopee != 0)
        | (Coleta.mercado_livre != 0)
        | (Coleta.avulso != 0)
        | (Coleta.valor_total != 0)
    )
    rows_coletas = db.scalars(stmt_coletas).all()

    # Cancelados: Saidas com status cancelado, agrupar por (data, base, servico)
    stmt_canc = select(Saida).where(
        Saida.sub_base == sub_base,
        func.lower(Saida.status) == "cancelado",
    )
    if base_norm:
        stmt_canc = stmt_canc.where(func.upper(Saida.base) == base_key)
    stmt_canc = stmt_canc.where(
        Saida.data >= periodo_inicio,
        Saida.data <= periodo_fim,
    )
    rows_canc = db.scalars(stmt_canc).all()

    # Mapa coletas por data (agrupar manuais e codigo)
    mapa_coletas: dict[str, dict] = {}
    for r in rows_coletas:
        dia = r.timestamp.date().isoformat()
        if dia not in mapa_coletas:
            mapa_coletas[dia] = {"shopee": 0, "mercado_livre": 0, "avulso": 0}
        mapa_coletas[dia]["shopee"] += r.shopee or 0
        mapa_coletas[dia]["mercado_livre"] += r.mercado_livre or 0
        mapa_coletas[dia]["avulso"] += r.avulso or 0

    # Mapa cancelados por data e tipo
    mapa_canc: dict[str, dict] = {}
    for c in rows_canc:
        dia = (c.data or c.timestamp.date()).isoformat()
        tipo = _normalizar_servico_saida(c.servico or "")
        if dia not in mapa_canc:
            mapa_canc[dia] = {"shopee": 0, "ml": 0, "avulso": 0}
        mapa_canc[dia][tipo] = mapa_canc[dia].get(tipo, 0) + 1

    regras_g, padrao_g_id = _contexto_g_base(db, sub_base)

    # Pacotes G: Saidas não canceladas com is_grande, por data, categoria e serviço
    stmt_g = select(Saida).where(
        Saida.sub_base == sub_base,
        Saida.is_grande.is_(True),
        func.lower(func.coalesce(Saida.status, "")) != "cancelado",
    )
    if base_norm:
        stmt_g = stmt_g.where(func.upper(Saida.base) == base_key)
    stmt_g = stmt_g.where(
        Saida.data >= periodo_inicio,
        Saida.data <= periodo_fim,
    )
    rows_g = db.scalars(stmt_g).all()
    mapa_g: dict[str, Dict[str, Dict[str, int]]] = {}

    def _somar_g(dia: str, cid: Any, tipo: str, qtd: int) -> None:
        if not qtd or cid is None:
            return
        bucket = mapa_g.setdefault(dia, {}).setdefault(str(cid), {t: 0 for t in SERVICOS_G})
        bucket[tipo] = bucket.get(tipo, 0) + int(qtd)

    for g in rows_g:
        dia = (g.data or g.timestamp.date()).isoformat()
        tipo = _normalizar_servico_saida(g.servico or "")
        _somar_g(dia, categoria_da_saida(g, padrao_g_id), tipo, 1)

    # G informado em coleta manual/ajuste. Coletas por código já contam pelas saídas G acima.
    for r in rows_coletas:
        if (getattr(r, "origem", None) or "codigo") not in ORIGENS_COLETA_G_MANUAL:
            continue
        dia = r.timestamp.date().isoformat()
        g_s = getattr(r, "g_shopee", 0) or 0
        g_m = getattr(r, "g_ml", 0) or 0
        g_a = getattr(r, "g_avulso", 0) or 0
        if g_s == 0 and g_m == 0 and g_a == 0 and not getattr(r, "g_categorias", None):
            g_a = getattr(r, "pacotes_g", 0) or 0
        por_cat = g_categorias_com_padrao(getattr(r, "g_categorias", None), g_s, g_m, g_a, padrao_g_id)
        for cid, qtds in por_cat.items():
            for tipo in SERVICOS_G:
                _somar_g(dia, cid, tipo, qtds.get(tipo, 0))

    # Dias únicos (coletas + cancelados + G)
    dias_set = set(mapa_coletas.keys()) | set(mapa_canc.keys()) | set(mapa_g.keys())
    if not dias_set:
        return [], Decimal("0.00"), Decimal("0.00"), Decimal("0.00")

    precos = _precos_base(db, sub_base, base_norm)
    p_shopee, p_ml, p_avulso = precos["shopee"], precos["ml"], precos["avulso"]

    itens = []
    valor_bruto = Decimal("0.00")
    valor_cancelados = Decimal("0.00")
    valor_g_total = Decimal("0.00")

    for dia in sorted(dias_set):
        c = mapa_coletas.get(dia, {})
        canc = mapa_canc.get(dia, {})
        g_cats = {k: v for k, v in mapa_g.get(dia, {}).items() if any(v.values())}
        shopee = c.get("shopee", 0)
        ml = c.get("mercado_livre", 0)
        avulso = c.get("avulso", 0)
        canc_s = canc.get("shopee", 0)
        canc_ml = canc.get("ml", 0)
        canc_a = canc.get("avulso", 0)
        g_s = sum(v.get("shopee", 0) for v in g_cats.values())
        g_m = sum(v.get("ml", 0) for v in g_cats.values())
        g_a = sum(v.get("avulso", 0) for v in g_cats.values())
        pacotes_g = g_s + g_m + g_a
        v_g = _valor_g_categorias(g_cats, precos, regras_g)
        valor_g_total += v_g

        v_bruto = (_decimal(shopee) * p_shopee + _decimal(ml) * p_ml + _decimal(avulso) * p_avulso).quantize(Decimal("0.01"))
        v_canc = (_decimal(canc_s) * p_shopee + _decimal(canc_ml) * p_ml + _decimal(canc_a) * p_avulso).quantize(Decimal("0.01"))

        valor_bruto += v_bruto
        valor_cancelados += v_canc

        itens.append({
            "data": dia,
            "shopee": shopee,
            "mercado_livre": ml,
            "avulso": avulso,
            "cancelados_shopee": canc_s,
            "cancelados_ml": canc_ml,
            "cancelados_avulso": canc_a,
            "pacotes_g": pacotes_g,
            "g_shopee": g_s,
            "g_ml": g_m,
            "g_avulso": g_a,
            "g_categorias": g_cats or None,
            "valor_g": v_g,
            "pacotes_g_com_cobranca": _qtd_g_com_cobranca(g_cats, regras_g),
        })

    valor_final = (valor_bruto - valor_cancelados + valor_g_total).quantize(Decimal("0.01"))
    return itens, valor_bruto, valor_cancelados, valor_final


# =========================================================
# SCHEMAS
# =========================================================

class FechamentoItemIn(BaseModel):
    data: str
    shopee: int = 0
    mercado_livre: int = 0
    avulso: int = 0
    cancelados_shopee: int = 0
    cancelados_ml: int = 0
    cancelados_avulso: int = 0
    pacotes_g: int = 0
    g_shopee: int = 0
    g_ml: int = 0
    g_avulso: int = 0
    g_categorias: Optional[Dict[str, Dict[str, int]]] = None


class FechamentoItemOut(BaseModel):
    data: str
    shopee: int
    mercado_livre: int
    avulso: int
    cancelados_shopee: int
    cancelados_ml: int
    cancelados_avulso: int
    pacotes_g: int = 0
    g_shopee: int = 0
    g_ml: int = 0
    g_avulso: int = 0
    g_categorias: Optional[Dict[str, Dict[str, int]]] = None
    valor_g: Optional[Decimal] = None


class GCategoriaResumo(BaseModel):
    categoria_g_id: int
    nome: str
    qtd: int = 0
    valor: Optional[Decimal] = None


class FechamentoCreate(BaseModel):
    base: str = Field(min_length=1)
    periodo_inicio: date
    periodo_fim: date
    itens: Optional[List[FechamentoItemIn]] = None
    valor_adicao: Optional[Decimal] = None
    motivo_adicao: Optional[str] = None
    valor_subtracao: Optional[Decimal] = None
    motivo_subtracao: Optional[str] = None
    ajuste_g_valor: Optional[Decimal] = None
    ajuste_g_motivo: Optional[str] = None


class FechamentoUpdate(BaseModel):
    itens: List[FechamentoItemIn]
    valor_adicao: Optional[Decimal] = None
    motivo_adicao: Optional[str] = None
    valor_subtracao: Optional[Decimal] = None
    motivo_subtracao: Optional[str] = None


class CalcularOut(BaseModel):
    base: str
    periodo_inicio: str
    periodo_fim: str
    valor_bruto: Decimal
    valor_cancelados: Decimal
    valor_final: Decimal
    valor_final_com_ajuste_g: Optional[Decimal] = None
    itens: List[FechamentoItemOut]
    precos: dict
    total_g_shopee: int = 0
    total_g_ml: int = 0
    total_g_avulso: int = 0
    total_pacotes_g: int = 0
    valor_g: Decimal = Decimal("0.00")
    g_por_categoria: List[GCategoriaResumo] = []
    ajuste_g_valor: Optional[Decimal] = None
    ajuste_g_motivo: Optional[str] = None
    seller_info: Optional[Dict[str, Any]] = None
    emitido_por: Optional[str] = None
    calendario: dict


class FechamentoOut(BaseModel):
    id_fechamento: int
    sub_base: str
    base: str
    periodo_inicio: date
    periodo_fim: date
    valor_bruto: Decimal
    valor_cancelados: Decimal
    valor_adicao: Decimal = Decimal("0.00")
    motivo_adicao: Optional[str] = None
    valor_subtracao: Decimal = Decimal("0.00")
    motivo_subtracao: Optional[str] = None
    valor_final: Decimal
    status: str
    criado_em: Optional[datetime] = None
    recebido_em: Optional[datetime] = None
    recebido_por: Optional[str] = None
    itens: List[FechamentoItemOut]
    total_g_shopee: int = 0
    total_g_ml: int = 0
    total_g_avulso: int = 0
    total_pacotes_g: int = 0
    valor_g: Decimal = Decimal("0.00")
    g_por_categoria: List[GCategoriaResumo] = []
    ajuste_g_valor: Optional[Decimal] = None
    divergencia_valor: Optional[bool] = None
    valor_bruto_recalculado: Optional[Decimal] = None
    valor_cancelados_recalculado: Optional[Decimal] = None
    valor_final_recalculado: Optional[Decimal] = None
    seller_info: Optional[Dict[str, Any]] = None
    emitido_por: Optional[str] = None


class FechamentoReceberItem(BaseModel):
    id_fechamento: int
    base: str
    periodo_inicio: date
    periodo_fim: date
    valor_final: Decimal
    status: str
    criado_em: Optional[datetime] = None
    recebido_em: Optional[datetime] = None
    recebido_por: Optional[str] = None
    cnpj: Optional[str] = None
    precisa_reajuste: bool = False
    alerta_pos_recebido: bool = False
    valor_final_recalculado: Optional[Decimal] = None


class FechamentoReceberTotais(BaseModel):
    total_a_receber: Decimal = Decimal("0.00")
    total_recebido: Decimal = Decimal("0.00")
    qtd_a_receber: int = 0
    qtd_recebido: int = 0
    qtd_precisa_reajuste: int = 0


class ResumoBaseDia(BaseModel):
    data: str
    shopee: int = 0
    mercado_livre: int = 0
    avulso: int = 0
    cancelados: int = 0
    pacotes_g: int = 0


class ResumoBaseFechamento(BaseModel):
    id_fechamento: int
    status: str
    valor_final: Decimal
    recebido_em: Optional[datetime] = None
    recebido_por: Optional[str] = None


class ResumoBaseLinha(BaseModel):
    base: str
    cnpj: Optional[str] = None
    dias: List[ResumoBaseDia]
    shopee: int = 0
    mercado_livre: int = 0
    avulso: int = 0
    cancelados: int = 0
    pacotes_g: int = 0
    valor_bruto: Decimal = Decimal("0.00")
    valor_cancelados: Decimal = Decimal("0.00")
    valor_g: Decimal = Decimal("0.00")
    valor_atual: Decimal = Decimal("0.00")
    dias_pendentes: List[str] = []
    fechamento: Optional[ResumoBaseFechamento] = None
    situacao: str
    precisa_reajuste: bool = False
    alerta_pos_recebido: bool = False
    valor_final_recalculado: Optional[Decimal] = None
    pode_gerar_lote: bool = False
    motivo_bloqueio: Optional[str] = None


class ResumoBaseSituacao(BaseModel):
    qtd: int = 0
    valor: Decimal = Decimal("0.00")


class ResumoPorBaseOut(BaseModel):
    periodo_inicio: date
    periodo_fim: date
    periodo_aberto: bool
    items: List[ResumoBaseLinha]
    totais: Dict[str, ResumoBaseSituacao]


class FechamentoLoteRequest(BaseModel):
    periodo_inicio: date
    periodo_fim: date
    todos_pendentes: bool = False
    bases: Optional[List[str]] = None


class FechamentoLoteItem(BaseModel):
    base: str
    id_fechamento: Optional[int] = None
    valor_final: Optional[Decimal] = None
    motivo: Optional[str] = None


class FechamentoLoteResponse(BaseModel):
    criados: List[FechamentoLoteItem] = []
    ja_existentes: List[FechamentoLoteItem] = []
    ignorados: List[FechamentoLoteItem] = []
    erros: List[FechamentoLoteItem] = []


class FechamentoReceberLista(BaseModel):
    items: List[FechamentoReceberItem]
    totais: FechamentoReceberTotais


class MarcarRecebidoRequest(BaseModel):
    periodo_inicio: date
    periodo_fim: date
    todos_elegiveis: bool = False
    ids_fechamento: Optional[List[int]] = None


class MarcarRecebidoResponse(BaseModel):
    marcados: int
    ids_fechamento: List[int]


# =========================================================
# GET — Verificar se existe fechamento para base+período
# =========================================================

class VerificarOut(BaseModel):
    existe: bool
    id_fechamento: Optional[int] = None
    status: Optional[str] = None


@router.get("/verificar", response_model=VerificarOut)
def verificar_fechamento_existente(
    base: str = Query(..., min_length=1),
    periodo_inicio: date = Query(...),
    periodo_fim: date = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Retorna se já existe fechamento para base+período (para perguntar reajuste)."""
    _exigir_admin_financeiro(current_user)
    sub_base = _sub_base_from_token_or_422(current_user)
    base_norm = base.strip()
    base_key = base_norm.upper()
    existente = db.scalar(
        select(BaseFechamento).where(
            BaseFechamento.sub_base == sub_base,
            func.upper(BaseFechamento.base) == base_key,
            BaseFechamento.periodo_inicio == periodo_inicio,
            BaseFechamento.periodo_fim == periodo_fim,
        )
    )
    if not existente:
        return VerificarOut(existe=False)
    st = (existente.status or "GERADO").upper()
    if st == "FECHADO":
        st = "GERADO"
    return VerificarOut(existe=True, id_fechamento=existente.id_fechamento, status=st)


# =========================================================
# GET — Calcular (preview)
# =========================================================

@router.get("/calcular", response_model=CalcularOut)
def calcular_fechamento(
    base: str = Query(..., min_length=1),
    periodo_inicio: date = Query(...),
    periodo_fim: date = Query(...),
    ajuste_g_valor: Optional[Decimal] = Query(None),
    ajuste_g_motivo: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _exigir_admin_financeiro(current_user)
    sub_base = _sub_base_from_token_or_422(current_user)
    if periodo_inicio > periodo_fim:
        raise HTTPException(400, "periodo_inicio deve ser anterior a periodo_fim.")
    if periodo_fim >= date.today():
        raise HTTPException(
            400,
            "Não é permitido calcular fechamento para período ainda em aberto. "
            "Escolha um período cuja data final seja anterior à data de hoje.",
        )

    itens, valor_bruto, valor_cancelados, valor_final = _build_itens_e_valores(
        db, sub_base, base, periodo_inicio, periodo_fim
    )
    precos_dec = _precos_base(db, sub_base, base.strip())
    precos = {k: float(v) for k, v in precos_dec.items()}
    regras_g, padrao_g_id = _contexto_g_base(db, sub_base)
    valor_g = sum((x.get("valor_g") or Decimal("0.00") for x in itens), Decimal("0.00"))

    total_g_shopee = sum(x.get("g_shopee", 0) for x in itens)
    total_g_ml = sum(x.get("g_ml", 0) for x in itens)
    total_g_avulso = sum(x.get("g_avulso", 0) for x in itens)
    total_pacotes_g = sum(x.get("pacotes_g", 0) for x in itens)

    # Ajuste específico de pacotes G (preview apenas)
    ajuste_g_val = _decimal(ajuste_g_valor) if ajuste_g_valor is not None else Decimal("0.00")
    valor_final_com_ajuste_g: Optional[Decimal]
    if ajuste_g_valor is not None:
        valor_final_com_ajuste_g = (valor_final + ajuste_g_val).quantize(Decimal("0.01"))
    else:
        valor_final_com_ajuste_g = None

    seller_info = _montar_seller_info(db, sub_base, base.strip())
    calendario = _resumo_calendario_fechamento(
        db, sub_base, base, periodo_inicio, periodo_fim
    )

    return CalcularOut(
        base=base.strip(),
        periodo_inicio=periodo_inicio.isoformat(),
        periodo_fim=periodo_fim.isoformat(),
        valor_bruto=valor_bruto,
        valor_cancelados=valor_cancelados,
        valor_final=valor_final,
        valor_final_com_ajuste_g=valor_final_com_ajuste_g,
        itens=[FechamentoItemOut(**x) for x in itens],
        precos=precos,
        total_g_shopee=total_g_shopee,
        total_g_ml=total_g_ml,
        total_g_avulso=total_g_avulso,
        total_pacotes_g=total_pacotes_g,
        valor_g=valor_g,
        g_por_categoria=_g_por_categoria(itens, regras_g, padrao_g_id, precos_dec),
        ajuste_g_valor=ajuste_g_valor,
        ajuste_g_motivo=(ajuste_g_motivo or None),
        seller_info=seller_info,
        emitido_por=_emitido_por(db, sub_base),
        calendario=calendario,
    )


# =========================================================
# POST — Criar fechamento
# =========================================================

def _criar_fechamento_base_core(
    db: Session,
    sub_base: str,
    payload: FechamentoCreate,
) -> tuple[BaseFechamento, List[FechamentoItemIn], Optional[Decimal]]:
    """Valida e insere o fechamento (sem commit). Retorna (fechamento, itens, ajuste_g)."""
    if payload.periodo_inicio > payload.periodo_fim:
        raise HTTPException(400, "periodo_inicio deve ser anterior a periodo_fim.")
    if payload.periodo_fim >= date.today():
        raise HTTPException(
            400,
            "Não é permitido criar fechamento para período ainda em aberto. "
            "Escolha um período cuja data final seja anterior à data de hoje.",
        )

    base_norm = payload.base.strip()
    calendario = _resumo_calendario_fechamento(
        db, sub_base, base_norm, payload.periodo_inicio, payload.periodo_fim
    )
    if not calendario["pronto_para_fechamento"]:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Existem dias de coleta pendentes antes do fechamento.",
                "calendario": calendario,
            },
        )

    # Verificar duplicidade
    existente = db.scalar(
        select(BaseFechamento).where(
            BaseFechamento.sub_base == sub_base,
            BaseFechamento.base == base_norm,
            BaseFechamento.periodo_inicio == payload.periodo_inicio,
            BaseFechamento.periodo_fim == payload.periodo_fim,
        )
    )
    if existente:
        raise HTTPException(409, "Já existe fechamento para esta base e período.")

    regras_g, padrao_g_id = _contexto_g_base(db, sub_base)
    if payload.itens:
        itens_data = payload.itens
        valor_bruto, valor_cancelados, valor_g = _valores_itens_informados(
            itens_data, _precos_base(db, sub_base, base_norm), regras_g, padrao_g_id
        )
        valor_final_calc = (valor_bruto - valor_cancelados + valor_g).quantize(Decimal("0.01"))
    else:
        itens_calc, valor_bruto, valor_cancelados, valor_final_calc = _build_itens_e_valores(
            db, sub_base, base_norm, payload.periodo_inicio, payload.periodo_fim
        )
        valor_g = sum((x.get("valor_g") or Decimal("0.00") for x in itens_calc), Decimal("0.00"))
        itens_data = [FechamentoItemIn(**x) for x in itens_calc]

    # Ajustes manuais genéricos
    valor_ad_base = _decimal(payload.valor_adicao).quantize(Decimal("0.01"))
    valor_sub_base = _decimal(payload.valor_subtracao).quantize(Decimal("0.01"))
    motivo_ad_base = (payload.motivo_adicao or "").strip()
    motivo_sub_base = (payload.motivo_subtracao or "").strip()

    ajuste_g_val_raw = payload.ajuste_g_valor
    ajuste_g_motivo_norm = (payload.ajuste_g_motivo or "").strip()
    valor_ad = valor_ad_base
    valor_sub = valor_sub_base
    motivo_ad_final = motivo_ad_base
    motivo_sub_final = motivo_sub_base

    ajuste_g_val_normalizado: Optional[Decimal] = None
    if ajuste_g_val_raw is not None:
        ajuste_g_val = _decimal(ajuste_g_val_raw).quantize(Decimal("0.01"))
        if ajuste_g_val != 0 and not ajuste_g_motivo_norm:
            raise HTTPException(422, "Informe o motivo do ajuste de pacotes G.")
        if ajuste_g_val != 0:
            ajuste_g_val_normalizado = ajuste_g_val
            # Texto padronizado, já incluindo o valor para aparecer claramente no relatório
            ajuste_label = f"[Ajuste G] Motivo: {ajuste_g_motivo_norm}; Valor: R$ {abs(ajuste_g_val):.2f}"
            if ajuste_g_val > 0:
                motivo_ad_final = " | ".join([m for m in [motivo_ad_base, ajuste_label] if m])
            else:
                motivo_sub_final = " | ".join([m for m in [motivo_sub_base, ajuste_label] if m])

    valor_final = (valor_final_calc + valor_ad - valor_sub).quantize(Decimal("0.01"))

    fech = BaseFechamento(
        sub_base=sub_base,
        base=base_norm,
        periodo_inicio=payload.periodo_inicio,
        periodo_fim=payload.periodo_fim,
        valor_bruto=valor_bruto,
        valor_cancelados=valor_cancelados,
        valor_adicao=valor_ad,
        motivo_adicao=(motivo_ad_final or None),
        valor_subtracao=valor_sub,
        motivo_subtracao=(motivo_sub_final or None),
        valor_final=valor_final,
        valor_g=valor_g,
        status=STATUS_GERADO,
    )
    db.add(fech)
    db.flush()

    for it in itens_data:
        dt_val = datetime.strptime(it.data, "%Y-%m-%d").date() if isinstance(it.data, str) else it.data
        item = BaseFechamentoItem(
            id_fechamento=fech.id_fechamento,
            data=dt_val,
            shopee=it.shopee,
            mercado_livre=it.mercado_livre,
            avulso=it.avulso,
            cancelados_shopee=it.cancelados_shopee,
            cancelados_ml=it.cancelados_ml,
            cancelados_avulso=it.cancelados_avulso,
            pacotes_g=getattr(it, "pacotes_g", 0) or 0,
            g_shopee=getattr(it, "g_shopee", 0) or 0,
            g_ml=getattr(it, "g_ml", 0) or 0,
            g_avulso=getattr(it, "g_avulso", 0) or 0,
            g_categorias=_g_categorias_item(it, padrao_g_id) or None,
        )
        db.add(item)
    db.flush()
    return fech, list(itens_data), ajuste_g_val_normalizado


@router.post("", response_model=FechamentoOut, status_code=201)
def criar_fechamento(
    payload: FechamentoCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _exigir_admin_financeiro(current_user)
    sub_base = _sub_base_from_token_or_422(current_user)
    base_norm = payload.base.strip()
    fech, itens_data, ajuste_g_val_normalizado = _criar_fechamento_base_core(db, sub_base, payload)
    db.commit()
    db.refresh(fech)

    total_g_shopee = sum(getattr(it, "g_shopee", 0) or 0 for it in itens_data)
    total_g_ml = sum(getattr(it, "g_ml", 0) or 0 for it in itens_data)
    total_g_avulso = sum(getattr(it, "g_avulso", 0) or 0 for it in itens_data)
    total_pacotes_g = sum(getattr(it, "pacotes_g", 0) or 0 for it in itens_data)
    regras_g, padrao_g_id = _contexto_g_base(db, sub_base)

    itens_out = [
        FechamentoItemOut(
            data=it.data if isinstance(it.data, str) else it.data.isoformat(),
            shopee=it.shopee,
            mercado_livre=it.mercado_livre,
            avulso=it.avulso,
            cancelados_shopee=it.cancelados_shopee,
            cancelados_ml=it.cancelados_ml,
            cancelados_avulso=it.cancelados_avulso,
            pacotes_g=getattr(it, "pacotes_g", 0) or 0,
            g_shopee=getattr(it, "g_shopee", 0) or 0,
            g_ml=getattr(it, "g_ml", 0) or 0,
            g_avulso=getattr(it, "g_avulso", 0) or 0,
            g_categorias=getattr(it, "g_categorias", None),
        )
        for it in itens_data
    ]

    seller_info = _montar_seller_info(db, sub_base, base_norm)

    return FechamentoOut(
        id_fechamento=fech.id_fechamento,
        sub_base=fech.sub_base,
        base=fech.base,
        periodo_inicio=fech.periodo_inicio,
        periodo_fim=fech.periodo_fim,
        valor_bruto=fech.valor_bruto,
        valor_cancelados=fech.valor_cancelados,
        valor_adicao=fech.valor_adicao,
        motivo_adicao=fech.motivo_adicao,
        valor_subtracao=fech.valor_subtracao,
        motivo_subtracao=fech.motivo_subtracao,
        valor_final=fech.valor_final,
        status=fech.status,
        criado_em=fech.criado_em,
        recebido_em=getattr(fech, "recebido_em", None),
        recebido_por=getattr(fech, "recebido_por", None),
        itens=itens_out,
        total_g_shopee=total_g_shopee,
        total_g_ml=total_g_ml,
        total_g_avulso=total_g_avulso,
        total_pacotes_g=total_pacotes_g,
        valor_g=fech.valor_g or Decimal("0.00"),
        g_por_categoria=_g_por_categoria(itens_data, regras_g, padrao_g_id),
        ajuste_g_valor=ajuste_g_val_normalizado,
        seller_info=seller_info,
        emitido_por=_emitido_por(db, sub_base),
    )


# =========================================================
# GET/POST — A Receber (fechamentos consolidados das bases)
# =========================================================

def _status_receber(fech: BaseFechamento) -> str:
    status_atual = (fech.status or "").strip().upper()
    return STATUS_GERADO if status_atual == "FECHADO" else status_atual


def _divergencia_fechamento(
    db: Session,
    sub_base: str,
    fech: BaseFechamento,
    itens_gravados: Optional[List[BaseFechamentoItem]] = None,
    recalculo: Optional[tuple] = None,
) -> Dict[str, Any]:
    """Recalcula o período e compara com o fechamento gravado (coletas alteradas depois de gerar)."""
    if itens_gravados is None:
        itens_gravados = db.scalars(
            select(BaseFechamentoItem).where(BaseFechamentoItem.id_fechamento == fech.id_fechamento)
        ).all()
    g_gravado = (
        sum(getattr(i, "g_shopee", 0) or 0 for i in itens_gravados),
        sum(getattr(i, "g_ml", 0) or 0 for i in itens_gravados),
        sum(getattr(i, "g_avulso", 0) or 0 for i in itens_gravados),
    )
    itens_rec, bruto_rec, canc_rec, _ = recalculo or _build_itens_e_valores(
        db, sub_base, fech.base, fech.periodo_inicio, fech.periodo_fim
    )
    valor_g_rec = sum((x.get("valor_g") or Decimal("0.00") for x in itens_rec), Decimal("0.00"))
    g_rec = (
        sum(x.get("g_shopee", 0) for x in itens_rec),
        sum(x.get("g_ml", 0) for x in itens_rec),
        sum(x.get("g_avulso", 0) for x in itens_rec),
    )
    divergente = fechamento_base_divergente(
        bruto_gravado=fech.valor_bruto,
        cancelados_gravado=fech.valor_cancelados,
        g_gravado=g_gravado,
        bruto_atual=bruto_rec,
        cancelados_atual=canc_rec,
        g_atual=g_rec,
        valor_g_gravado=getattr(fech, "valor_g", None) or Decimal("0.00"),
        valor_g_atual=valor_g_rec,
    )
    valor_final_rec = None
    if divergente:
        valor_final_rec = (
            bruto_rec - canc_rec + valor_g_rec
            + (fech.valor_adicao or Decimal("0"))
            - (fech.valor_subtracao or Decimal("0"))
        ).quantize(Decimal("0.01"))
    return {
        "divergente": divergente,
        "valor_bruto": bruto_rec,
        "valor_cancelados": canc_rec,
        "valor_final": valor_final_rec,
    }


@router.get("", response_model=FechamentoReceberLista)
def listar_fechamentos_a_receber(
    periodo_inicio: date = Query(...),
    periodo_fim: date = Query(...),
    status: Optional[str] = Query(None, description="GERADO|REAJUSTADO|RECEBIDO"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _exigir_admin_financeiro(current_user)
    sub_base = _sub_base_from_token_or_422(current_user)
    if periodo_inicio > periodo_fim:
        raise HTTPException(400, "periodo_inicio deve ser anterior a periodo_fim.")

    rows = list(
        db.scalars(
            select(BaseFechamento).where(
                BaseFechamento.sub_base == sub_base,
                BaseFechamento.periodo_inicio == periodo_inicio,
                BaseFechamento.periodo_fim == periodo_fim,
            )
        ).all()
    )
    status_filtro = (status or "").strip().upper() or None
    items: List[FechamentoReceberItem] = []
    total_a_receber = Decimal("0.00")
    total_recebido = Decimal("0.00")
    qtd_a_receber = qtd_recebido = qtd_precisa_reajuste = 0
    cnpj_por_base: Dict[str, Optional[str]] = {}

    for fech in rows:
        status_atual = _status_receber(fech)
        valor = Decimal(str(fech.valor_final or 0))
        div = _divergencia_fechamento(db, sub_base, fech)
        precisa_reajuste = bool(div["divergente"] and status_atual in STATUS_ELEGIVEIS_RECEBIMENTO)
        alerta_pos_recebido = bool(div["divergente"] and status_atual == STATUS_RECEBIDO)
        if status_atual in STATUS_ELEGIVEIS_RECEBIMENTO:
            total_a_receber += valor
            qtd_a_receber += 1
        elif status_atual == STATUS_RECEBIDO:
            total_recebido += valor
            qtd_recebido += 1
        if precisa_reajuste:
            qtd_precisa_reajuste += 1
        if status_filtro and status_atual != status_filtro:
            continue
        if fech.base not in cnpj_por_base:
            info = _montar_seller_info(db, sub_base, fech.base) or {}
            cnpj_por_base[fech.base] = info.get("cnpj")
        items.append(
            FechamentoReceberItem(
                id_fechamento=int(fech.id_fechamento),
                base=fech.base,
                periodo_inicio=fech.periodo_inicio,
                periodo_fim=fech.periodo_fim,
                valor_final=fech.valor_final,
                status=status_atual,
                criado_em=fech.criado_em,
                recebido_em=getattr(fech, "recebido_em", None),
                recebido_por=getattr(fech, "recebido_por", None),
                cnpj=cnpj_por_base.get(fech.base),
                precisa_reajuste=precisa_reajuste,
                alerta_pos_recebido=alerta_pos_recebido,
                valor_final_recalculado=div["valor_final"],
            )
        )

    items.sort(key=lambda item: ((item.base or "").casefold(), item.id_fechamento))
    return FechamentoReceberLista(
        items=items,
        totais=FechamentoReceberTotais(
            total_a_receber=total_a_receber.quantize(Decimal("0.01")),
            total_recebido=total_recebido.quantize(Decimal("0.01")),
            qtd_a_receber=qtd_a_receber,
            qtd_recebido=qtd_recebido,
            qtd_precisa_reajuste=qtd_precisa_reajuste,
        ),
    )


@router.post("/marcar-recebido", response_model=MarcarRecebidoResponse)
def marcar_fechamentos_recebidos(
    payload: MarcarRecebidoRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _exigir_admin_financeiro(current_user)
    sub_base = _sub_base_from_token_or_422(current_user)
    if payload.periodo_inicio > payload.periodo_fim:
        raise HTTPException(400, "periodo_inicio deve ser anterior a periodo_fim.")
    rows = list(
        db.scalars(
            select(BaseFechamento).where(
                BaseFechamento.sub_base == sub_base,
                BaseFechamento.periodo_inicio == payload.periodo_inicio,
                BaseFechamento.periodo_fim == payload.periodo_fim,
            )
        ).all()
    )
    if payload.todos_elegiveis:
        alvos = [item for item in rows if _status_receber(item) in STATUS_ELEGIVEIS_RECEBIMENTO]
    else:
        ids = {int(item) for item in (payload.ids_fechamento or [])}
        if not ids:
            raise HTTPException(422, "Selecione ao menos um fechamento.")
        por_id = {int(item.id_fechamento): item for item in rows}
        alvos = []
        for id_fechamento in ids:
            fech = por_id.get(id_fechamento)
            if not fech:
                raise HTTPException(404, f"Fechamento {id_fechamento} não encontrado no período/sub_base.")
            if _status_receber(fech) not in STATUS_ELEGIVEIS_RECEBIMENTO:
                raise HTTPException(400, f"Fechamento {id_fechamento} não está elegível para recebimento.")
            alvos.append(fech)
    if not alvos:
        raise HTTPException(400, "Nenhum fechamento elegível para marcar como recebido.")

    agora = datetime.utcnow()
    recebido_por = (getattr(current_user, "username", None) or "").strip() or str(current_user.id)
    ids_marcados = []
    for fech in alvos:
        fech.status = STATUS_RECEBIDO
        fech.recebido_em = agora
        fech.recebido_por = recebido_por
        ids_marcados.append(int(fech.id_fechamento))
    db.commit()
    return MarcarRecebidoResponse(marcados=len(ids_marcados), ids_fechamento=ids_marcados)


# =========================================================
# GET — Resumo por base / POST — Lote
# =========================================================

def _linhas_resumo_por_base(
    db: Session,
    sub_base: str,
    periodo_inicio: date,
    periodo_fim: date,
) -> List[ResumoBaseLinha]:
    """Uma linha por base com movimento, agenda confirmada ou fechamento no período."""
    from datetime import time as dt_time

    dt_start = datetime.combine(periodo_inicio, dt_time.min)
    dt_end = datetime.combine(periodo_fim, dt_time(23, 59, 59))
    nomes: Dict[str, str] = {}

    def _registrar(nome: Optional[str], prioritario: bool = False) -> None:
        nome_norm = (nome or "").strip()
        if not nome_norm:
            return
        chave = nome_norm.upper()
        if prioritario or chave not in nomes:
            nomes[chave] = nome_norm

    for nome in db.scalars(
        select(Coleta.base).where(
            Coleta.sub_base == sub_base,
            Coleta.timestamp >= dt_start,
            Coleta.timestamp <= dt_end,
        ).distinct()
    ).all():
        _registrar(nome)
    for nome in db.scalars(
        select(Saida.base).where(
            Saida.sub_base == sub_base,
            Saida.data >= periodo_inicio,
            Saida.data <= periodo_fim,
            (func.lower(Saida.status) == "cancelado") | (Saida.is_grande.is_(True)),
        ).distinct()
    ).all():
        _registrar(nome)

    precos_por_chave: Dict[str, BasePreco] = {}
    for preco in db.scalars(select(BasePreco).where(BasePreco.sub_base == sub_base)).all():
        chave = (preco.base or "").strip().upper()
        if not chave:
            continue
        precos_por_chave[chave] = preco
        if chave in nomes:
            _registrar(preco.base, prioritario=True)
        elif bool(preco.ativo) and bool(preco.agenda_coleta_confirmada):
            _registrar(preco.base)

    fechamentos: Dict[str, BaseFechamento] = {}
    for fech in db.scalars(
        select(BaseFechamento).where(
            BaseFechamento.sub_base == sub_base,
            BaseFechamento.periodo_inicio == periodo_inicio,
            BaseFechamento.periodo_fim == periodo_fim,
        )
    ).all():
        chave = (fech.base or "").strip().upper()
        fechamentos[chave] = fech
        _registrar(fech.base, prioritario=chave not in precos_por_chave)

    linhas: List[ResumoBaseLinha] = []
    for chave, nome in nomes.items():
        recalculo = _build_itens_e_valores(db, sub_base, nome, periodo_inicio, periodo_fim)
        itens, bruto, canc, valor_atual = recalculo
        pendentes: List[str] = []
        if chave in precos_por_chave:
            try:
                cal = _resumo_calendario_fechamento(db, sub_base, nome, periodo_inicio, periodo_fim)
                pendentes = [d["data"] for d in cal.get("dias_pendentes") or []]
            except HTTPException:
                pendentes = []
        fech = fechamentos.get(chave)
        if not itens and not fech and not pendentes:
            continue

        dias = [
            ResumoBaseDia(
                data=x["data"],
                shopee=x["shopee"],
                mercado_livre=x["mercado_livre"],
                avulso=x["avulso"],
                cancelados=x["cancelados_shopee"] + x["cancelados_ml"] + x["cancelados_avulso"],
                pacotes_g=x["pacotes_g"],
            )
            for x in itens
        ]
        total_g = sum(d.pacotes_g for d in dias)
        situacao = "PENDENTE"
        fech_out = None
        precisa_reajuste = alerta_pos_recebido = False
        valor_final_rec = None
        if fech:
            situacao = _status_receber(fech)
            fech_out = ResumoBaseFechamento(
                id_fechamento=int(fech.id_fechamento),
                status=situacao,
                valor_final=fech.valor_final,
                recebido_em=getattr(fech, "recebido_em", None),
                recebido_por=getattr(fech, "recebido_por", None),
            )
            div = _divergencia_fechamento(db, sub_base, fech, recalculo=recalculo)
            precisa_reajuste = bool(div["divergente"] and situacao in STATUS_ELEGIVEIS_RECEBIMENTO)
            alerta_pos_recebido = bool(div["divergente"] and situacao == STATUS_RECEBIDO)
            valor_final_rec = div["valor_final"]

        motivo = motivo_bloqueio_lote_base(
            status_fechamento=fech.status if fech else None,
            qtd_dias=len(dias),
            qtd_dias_pendentes=len(pendentes),
            total_pacotes_g=total_g,
            pacotes_g_com_regra=sum(x.get("pacotes_g_com_cobranca", 0) for x in itens),
        )
        linhas.append(
            ResumoBaseLinha(
                base=nome,
                cnpj=(_montar_seller_info(db, sub_base, nome) or {}).get("cnpj"),
                dias=dias,
                shopee=sum(d.shopee for d in dias),
                mercado_livre=sum(d.mercado_livre for d in dias),
                avulso=sum(d.avulso for d in dias),
                cancelados=sum(d.cancelados for d in dias),
                pacotes_g=total_g,
                valor_bruto=bruto,
                valor_cancelados=canc,
                valor_g=sum((x.get("valor_g") or Decimal("0.00") for x in itens), Decimal("0.00")),
                valor_atual=valor_atual,
                dias_pendentes=pendentes,
                fechamento=fech_out,
                situacao=situacao,
                precisa_reajuste=precisa_reajuste,
                alerta_pos_recebido=alerta_pos_recebido,
                valor_final_recalculado=valor_final_rec,
                pode_gerar_lote=motivo is None,
                motivo_bloqueio=motivo,
            )
        )
    linhas.sort(key=lambda linha: linha.base.casefold())
    return linhas


@router.get("/resumo-por-base", response_model=ResumoPorBaseOut)
def resumo_por_base(
    periodo_inicio: date = Query(...),
    periodo_fim: date = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _exigir_admin_financeiro(current_user)
    sub_base = _sub_base_from_token_or_422(current_user)
    if periodo_inicio > periodo_fim:
        raise HTTPException(400, "periodo_inicio deve ser anterior a periodo_fim.")

    linhas = _linhas_resumo_por_base(db, sub_base, periodo_inicio, periodo_fim)
    totais = {chave: ResumoBaseSituacao() for chave in ("PENDENTE", "A_RECEBER", "RECEBIDO", "REVISAO")}

    def _somar(chave: str, valor: Decimal) -> None:
        totais[chave].qtd += 1
        totais[chave].valor = (totais[chave].valor + Decimal(str(valor or 0))).quantize(Decimal("0.01"))

    for linha in linhas:
        if linha.situacao == "PENDENTE":
            _somar("PENDENTE", linha.valor_atual)
        elif linha.situacao in STATUS_ELEGIVEIS_RECEBIMENTO and linha.fechamento:
            _somar("A_RECEBER", linha.fechamento.valor_final)
        elif linha.situacao == STATUS_RECEBIDO and linha.fechamento:
            _somar("RECEBIDO", linha.fechamento.valor_final)
        if linha.precisa_reajuste and linha.fechamento:
            _somar("REVISAO", linha.fechamento.valor_final)

    return ResumoPorBaseOut(
        periodo_inicio=periodo_inicio,
        periodo_fim=periodo_fim,
        periodo_aberto=periodo_fim >= date.today(),
        items=linhas,
        totais=totais,
    )


@router.post("/lote", response_model=FechamentoLoteResponse)
def criar_fechamentos_lote(
    payload: FechamentoLoteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _exigir_admin_financeiro(current_user)
    sub_base = _sub_base_from_token_or_422(current_user)
    if payload.periodo_inicio > payload.periodo_fim:
        raise HTTPException(400, "periodo_inicio deve ser anterior a periodo_fim.")
    if payload.periodo_fim >= date.today():
        raise HTTPException(400, "O período ainda está em aberto. Escolha um período encerrado.")

    linhas = {
        linha.base.upper(): linha
        for linha in _linhas_resumo_por_base(db, sub_base, payload.periodo_inicio, payload.periodo_fim)
    }
    if payload.todos_pendentes:
        alvos = [linha.base for linha in linhas.values() if linha.situacao == "PENDENTE"]
    else:
        alvos = [b.strip() for b in (payload.bases or []) if b and b.strip()]
        if not alvos:
            raise HTTPException(422, "Selecione ao menos uma base.")

    resultado = FechamentoLoteResponse()
    for nome in dict.fromkeys(alvos):
        linha = linhas.get(nome.upper())
        if not linha:
            resultado.ignorados.append(FechamentoLoteItem(base=nome, motivo="SEM_COLETAS"))
            continue
        if linha.motivo_bloqueio == MOTIVO_JA_GERADO:
            resultado.ja_existentes.append(
                FechamentoLoteItem(
                    base=linha.base,
                    id_fechamento=linha.fechamento.id_fechamento if linha.fechamento else None,
                    valor_final=linha.fechamento.valor_final if linha.fechamento else None,
                )
            )
            continue
        if not linha.pode_gerar_lote:
            resultado.ignorados.append(FechamentoLoteItem(base=linha.base, motivo=linha.motivo_bloqueio))
            continue
        try:
            with db.begin_nested():
                fech, _, _ = _criar_fechamento_base_core(
                    db,
                    sub_base,
                    FechamentoCreate(
                        base=linha.base,
                        periodo_inicio=payload.periodo_inicio,
                        periodo_fim=payload.periodo_fim,
                    ),
                )
            resultado.criados.append(
                FechamentoLoteItem(base=linha.base, id_fechamento=int(fech.id_fechamento), valor_final=fech.valor_final)
            )
        except HTTPException as exc:
            detalhe = exc.detail if isinstance(exc.detail, str) else (exc.detail or {}).get("message")
            if exc.status_code == 409 and isinstance(exc.detail, str):
                resultado.ja_existentes.append(FechamentoLoteItem(base=linha.base, motivo=detalhe))
            else:
                resultado.erros.append(FechamentoLoteItem(base=linha.base, motivo=detalhe or "Erro ao gerar."))
        except Exception:
            resultado.erros.append(FechamentoLoteItem(base=linha.base, motivo="Erro ao gerar."))
    db.commit()
    return resultado


# =========================================================
# GET — Obter fechamento (para modal)
# =========================================================

@router.get("/{id_fechamento}", response_model=FechamentoOut)
def obter_fechamento(
    id_fechamento: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _exigir_admin_financeiro(current_user)
    sub_base = _sub_base_from_token_or_422(current_user)
    fech = db.get(BaseFechamento, id_fechamento)
    if not fech or fech.sub_base != sub_base:
        raise HTTPException(404, "Fechamento não encontrado.")

    itens = db.scalars(
        select(BaseFechamentoItem)
        .where(BaseFechamentoItem.id_fechamento == id_fechamento)
        .order_by(BaseFechamentoItem.data)
    ).all()

    itens_out = [
        FechamentoItemOut(
            data=i.data.isoformat(),
            shopee=i.shopee,
            mercado_livre=i.mercado_livre,
            avulso=i.avulso,
            cancelados_shopee=i.cancelados_shopee,
            cancelados_ml=i.cancelados_ml,
            cancelados_avulso=i.cancelados_avulso,
            pacotes_g=getattr(i, "pacotes_g", 0) or 0,
            g_shopee=getattr(i, "g_shopee", 0) or 0,
            g_ml=getattr(i, "g_ml", 0) or 0,
            g_avulso=getattr(i, "g_avulso", 0) or 0,
            g_categorias=getattr(i, "g_categorias", None),
        )
        for i in itens
    ]

    total_g_shopee = sum(getattr(i, "g_shopee", 0) or 0 for i in itens)
    total_g_ml = sum(getattr(i, "g_ml", 0) or 0 for i in itens)
    total_g_avulso = sum(getattr(i, "g_avulso", 0) or 0 for i in itens)
    total_pacotes_g = sum(getattr(i, "pacotes_g", 0) or 0 for i in itens)

    div = _divergencia_fechamento(db, sub_base, fech, itens)
    divergencia = div["divergente"]
    regras_g, padrao_g_id = _contexto_g_base(db, sub_base)
    valor_bruto_rec = div["valor_bruto"]
    valor_cancelados_rec = div["valor_cancelados"]
    valor_final_rec = div["valor_final"]

    seller_info = _montar_seller_info(db, sub_base, fech.base)

    return FechamentoOut(
        id_fechamento=fech.id_fechamento,
        sub_base=fech.sub_base,
        base=fech.base,
        periodo_inicio=fech.periodo_inicio,
        periodo_fim=fech.periodo_fim,
        valor_bruto=fech.valor_bruto,
        valor_cancelados=fech.valor_cancelados,
        valor_adicao=getattr(fech, "valor_adicao", None) or Decimal("0.00"),
        motivo_adicao=getattr(fech, "motivo_adicao", None),
        valor_subtracao=getattr(fech, "valor_subtracao", None) or Decimal("0.00"),
        motivo_subtracao=getattr(fech, "motivo_subtracao", None),
        valor_final=fech.valor_final,
        status=fech.status,
        criado_em=fech.criado_em,
        recebido_em=getattr(fech, "recebido_em", None),
        recebido_por=getattr(fech, "recebido_por", None),
        itens=itens_out,
        total_g_shopee=total_g_shopee,
        total_g_ml=total_g_ml,
        total_g_avulso=total_g_avulso,
        total_pacotes_g=total_pacotes_g,
        valor_g=getattr(fech, "valor_g", None) or Decimal("0.00"),
        g_por_categoria=_g_por_categoria(itens, regras_g, padrao_g_id, _precos_base(db, sub_base, fech.base)),
        ajuste_g_valor=None,
        divergencia_valor=divergencia if divergencia else None,
        valor_bruto_recalculado=valor_bruto_rec if divergencia else None,
        valor_cancelados_recalculado=valor_cancelados_rec if divergencia else None,
        valor_final_recalculado=valor_final_rec if divergencia else None,
        seller_info=seller_info,
        emitido_por=_emitido_por(db, sub_base),
    )


# =========================================================
# PATCH — Reajustar fechamento
# =========================================================

@router.patch("/{id_fechamento}", response_model=FechamentoOut)
def atualizar_fechamento(
    id_fechamento: int,
    payload: FechamentoUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _exigir_admin_financeiro(current_user)
    sub_base = _sub_base_from_token_or_422(current_user)
    fech = db.get(BaseFechamento, id_fechamento)
    if not fech or fech.sub_base != sub_base:
        raise HTTPException(404, "Fechamento não encontrado.")
    if not status_base_permite_reajuste(fech.status):
        if (fech.status or "").strip().upper() == STATUS_RECEBIDO:
            raise HTTPException(409, "Fechamentos já recebidos não podem ser reajustados.")
        raise HTTPException(400, "Apenas fechamentos com status GERADO ou REAJUSTADO podem ser reajustados.")

    regras_g, padrao_g_id = _contexto_g_base(db, sub_base)
    valor_bruto, valor_cancelados, valor_g = _valores_itens_informados(
        payload.itens, _precos_base(db, sub_base, fech.base), regras_g, padrao_g_id
    )

    if payload.valor_adicao is not None:
        fech.valor_adicao = Decimal(str(payload.valor_adicao)).quantize(Decimal("0.01"))
    if payload.motivo_adicao is not None:
        fech.motivo_adicao = (payload.motivo_adicao or "").strip() or None
    if payload.valor_subtracao is not None:
        fech.valor_subtracao = Decimal(str(payload.valor_subtracao)).quantize(Decimal("0.01"))
    if payload.motivo_subtracao is not None:
        fech.motivo_subtracao = (payload.motivo_subtracao or "").strip() or None

    valor_ad = getattr(fech, "valor_adicao", None) or Decimal("0.00")
    valor_sub = getattr(fech, "valor_subtracao", None) or Decimal("0.00")
    valor_final = (valor_bruto - valor_cancelados + valor_g + valor_ad - valor_sub).quantize(Decimal("0.01"))

    fech.valor_bruto = valor_bruto
    fech.valor_cancelados = valor_cancelados
    fech.valor_g = valor_g
    fech.valor_final = valor_final
    fech.status = STATUS_REAJUSTADO

    # Remover itens antigos e inserir novos
    for i in db.scalars(select(BaseFechamentoItem).where(BaseFechamentoItem.id_fechamento == id_fechamento)).all():
        db.delete(i)
    db.flush()

    for it in payload.itens:
        dt_val = datetime.strptime(it.data, "%Y-%m-%d").date() if isinstance(it.data, str) else it.data
        item = BaseFechamentoItem(
            id_fechamento=id_fechamento,
            data=dt_val,
            g_categorias=_g_categorias_item(it, padrao_g_id) or None,
            shopee=it.shopee,
            mercado_livre=it.mercado_livre,
            avulso=it.avulso,
            cancelados_shopee=it.cancelados_shopee,
            cancelados_ml=it.cancelados_ml,
            cancelados_avulso=it.cancelados_avulso,
            pacotes_g=getattr(it, "pacotes_g", 0) or 0,
            g_shopee=getattr(it, "g_shopee", 0) or 0,
            g_ml=getattr(it, "g_ml", 0) or 0,
            g_avulso=getattr(it, "g_avulso", 0) or 0,
        )
        db.add(item)

    db.commit()
    db.refresh(fech)

    itens = db.scalars(
        select(BaseFechamentoItem).where(BaseFechamentoItem.id_fechamento == id_fechamento).order_by(BaseFechamentoItem.data)
    ).all()
    itens_out = [
        FechamentoItemOut(
            data=i.data.isoformat(),
            shopee=i.shopee, mercado_livre=i.mercado_livre, avulso=i.avulso,
            cancelados_shopee=i.cancelados_shopee, cancelados_ml=i.cancelados_ml, cancelados_avulso=i.cancelados_avulso,
            pacotes_g=getattr(i, "pacotes_g", 0) or 0,
            g_shopee=getattr(i, "g_shopee", 0) or 0,
            g_ml=getattr(i, "g_ml", 0) or 0,
            g_avulso=getattr(i, "g_avulso", 0) or 0,
            g_categorias=getattr(i, "g_categorias", None),
        )
        for i in itens
    ]
    total_g_shopee = sum(getattr(i, "g_shopee", 0) or 0 for i in itens)
    total_g_ml = sum(getattr(i, "g_ml", 0) or 0 for i in itens)
    total_g_avulso = sum(getattr(i, "g_avulso", 0) or 0 for i in itens)
    total_pacotes_g = sum(getattr(i, "pacotes_g", 0) or 0 for i in itens)

    return FechamentoOut(
        id_fechamento=fech.id_fechamento,
        sub_base=fech.sub_base,
        base=fech.base,
        periodo_inicio=fech.periodo_inicio,
        periodo_fim=fech.periodo_fim,
        valor_bruto=fech.valor_bruto,
        valor_cancelados=fech.valor_cancelados,
        valor_adicao=fech.valor_adicao,
        motivo_adicao=fech.motivo_adicao,
        valor_subtracao=fech.valor_subtracao,
        motivo_subtracao=fech.motivo_subtracao,
        valor_final=fech.valor_final,
        status=fech.status,
        criado_em=fech.criado_em,
        recebido_em=getattr(fech, "recebido_em", None),
        recebido_por=getattr(fech, "recebido_por", None),
        itens=itens_out,
        total_g_shopee=total_g_shopee,
        total_g_ml=total_g_ml,
        total_g_avulso=total_g_avulso,
        total_pacotes_g=total_pacotes_g,
        valor_g=getattr(fech, "valor_g", None) or Decimal("0.00"),
        g_por_categoria=_g_por_categoria(itens, regras_g, padrao_g_id, _precos_base(db, sub_base, fech.base)),
        ajuste_g_valor=None,
        emitido_por=_emitido_por(db, fech.sub_base),
    )
