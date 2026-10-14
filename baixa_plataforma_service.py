# baixa_plataforma_service.py - Baixa automática de pedidos sincronizados pelo status no ML/Shopee
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

import requests
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from baixa_plataforma_pure import (
    ACAO_CANCELAR,
    ACAO_ENTREGAR,
    ACAO_SINALIZAR_CANCELADO,
    EVENTO_CANCELADO_PLATAFORMA,
    PLATAFORMA_ML,
    PLATAFORMA_SHOPEE,
    STATUS_FINAIS,
    acao_baixa,
    ml_status_final,
    payload_historico,
    shopee_status_final,
)
from models import BasePreco, MLConexao, Saida, SaidaHistorico, ShopeeToken

logger = logging.getLogger(__name__)

BAIXA_DIAS = 30
BAIXA_MAX_POR_SELLER = 300


def _utcnow() -> datetime:
    return datetime.utcnow()


def _agora_local() -> datetime:
    """Horário de Brasília sem fuso, padrão das colunas exibidas na web."""
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("America/Sao_Paulo")).replace(tzinfo=None)


def _contagem_vazia() -> dict[str, int]:
    return {"conferidos": 0, "entregues": 0, "cancelados": 0, "sinalizados": 0}


def _base_vinculada(db: Session, sub_base: str, id_base: Optional[int]) -> Optional[str]:
    if id_base is None:
        return None
    row = db.scalar(select(BasePreco).where(BasePreco.id_base == id_base, BasePreco.sub_base == sub_base))
    nome = (row.base or "").strip() if row is not None else ""
    return nome or None


def _saidas_abertas(db: Session, sub_base: str, base_nome: str, coluna_ref, *, dias: int, limite: int) -> list[Saida]:
    """Saídas da sub_base/seller com vínculo na plataforma, ainda não finalizadas nem sinalizadas."""
    desde = _utcnow() - timedelta(days=dias)
    return list(
        db.scalars(
            select(Saida)
            .where(
                Saida.sub_base == sub_base,
                func.upper(func.trim(Saida.base)) == base_nome.upper(),
                coluna_ref.isnot(None),
                func.lower(func.coalesce(Saida.status, "")).notin_(list(STATUS_FINAIS)),
                Saida.plataforma_cancelado_em.is_(None),
                Saida.timestamp >= desde,
            )
            .order_by(Saida.id_saida.desc())
            .limit(limite)
        ).all()
    )


def aplicar_baixa(db: Session, saida: Saida, final: Optional[str], plataforma: str) -> Optional[str]:
    """Aplica a ação da baixa (com lock da linha); retorna a ação aplicada ou None."""
    atual = db.scalar(
        select(Saida)
        .where(Saida.id_saida == saida.id_saida)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if atual is None:
        return None
    acao = acao_baixa(atual.status, final, ja_sinalizado=atual.plataforma_cancelado_em is not None)
    if acao is None:
        return None
    agora = _agora_local()
    anterior = atual.status
    if acao == ACAO_ENTREGAR:
        atual.status = "entregue"
        atual.data_hora_entrega = _utcnow()
        evento = "entregue"
    elif acao == ACAO_CANCELAR:
        atual.status = "cancelado"
        evento = "cancelado"
    else:
        atual.plataforma_cancelado_em = agora
        evento = EVENTO_CANCELADO_PLATAFORMA
    db.add(
        SaidaHistorico(
            id_saida=atual.id_saida,
            evento=evento,
            status_anterior=anterior,
            status_novo=atual.status,
            payload=payload_historico(plataforma),
        )
    )
    return acao


def _contabilizar(contagem: dict[str, int], acao: Optional[str]) -> None:
    if acao == ACAO_ENTREGAR:
        contagem["entregues"] += 1
    elif acao == ACAO_CANCELAR:
        contagem["cancelados"] += 1
    elif acao == ACAO_SINALIZAR_CANCELADO:
        contagem["sinalizados"] += 1


def _aplicar_lote(
    db: Session,
    pares: list[tuple[Saida, Optional[str]]],
    plataforma: str,
    seller: dict[str, Any],
) -> None:
    for saida, final in pares:
        if final is None:
            continue
        try:
            with db.begin_nested():
                _contabilizar(seller, aplicar_baixa(db, saida, final, plataforma))
        except Exception as e:
            seller["erros"].append({"id_saida": saida.id_saida, "erro": str(e)[:200]})
    db.commit()


def conferir_ml_sub_base(
    db: Session,
    sub_base: str,
    *,
    dias: int = BAIXA_DIAS,
    limite: int = BAIXA_MAX_POR_SELLER,
    deadline: Optional[float] = None,
) -> dict[str, Any]:
    from ml_int_service import fetch_shipment, get_valid_access_token

    sellers: list[dict[str, Any]] = []
    vistos: set[int] = set()
    conexoes = (
        db.query(MLConexao)
        .filter(MLConexao.sub_base == sub_base, MLConexao.id_base.isnot(None))
        .order_by(MLConexao.criado_em.desc(), MLConexao.id.desc())
        .all()
    )
    for conexao in conexoes:
        if conexao.user_id_ml in vistos:
            continue
        vistos.add(conexao.user_id_ml)
        base_nome = _base_vinculada(db, sub_base, conexao.id_base)
        if not base_nome:
            continue
        seller: dict[str, Any] = {"user_id_ml": conexao.user_id_ml, "base_nome": base_nome, **_contagem_vazia(), "erros": []}
        sellers.append(seller)
        saidas = _saidas_abertas(db, sub_base, base_nome, Saida.ml_shipment_id, dias=dias, limite=limite)
        if not saidas:
            continue
        try:
            token = get_valid_access_token(db, conexao.user_id_ml, sub_base)
        except (LookupError, RuntimeError) as e:
            seller["erros"].append({"erro": str(e)[:200]})
            continue
        pares: list[tuple[Saida, Optional[str]]] = []
        for saida in saidas:
            if deadline is not None and time.monotonic() > deadline:
                seller["parcial"] = True
                break
            seller["conferidos"] += 1
            try:
                ship = fetch_shipment(
                    token, int(saida.ml_shipment_id), db=db, user_id_ml=conexao.user_id_ml, sub_base=sub_base,
                )
            except requests.HTTPError as e:
                seller["erros"].append({"shipment_id": saida.ml_shipment_id, "erro": f"HTTP {e.response.status_code}"})
                continue
            except requests.RequestException as e:
                seller["erros"].append({"shipment_id": saida.ml_shipment_id, "erro": str(e)[:200]})
                continue
            pares.append((saida, ml_status_final(ship)))
        _aplicar_lote(db, pares, PLATAFORMA_ML, seller)
    return {"sub_base": sub_base, "sellers": sellers}


def conferir_shopee_sub_base(
    db: Session,
    sub_base: str,
    *,
    dias: int = BAIXA_DIAS,
    limite: int = BAIXA_MAX_POR_SELLER,
    deadline: Optional[float] = None,
) -> dict[str, Any]:
    from shopee_int_service import SHOPEE_ORDER_DETAIL_BATCH, ShopeeApiError, _shop_api_get

    sellers: list[dict[str, Any]] = []
    vistos: set[int] = set()
    tokens = (
        db.query(ShopeeToken)
        .filter(ShopeeToken.sub_base == sub_base, ShopeeToken.id_base.isnot(None))
        .order_by(ShopeeToken.criado_em.desc(), ShopeeToken.id.desc())
        .all()
    )
    for token in tokens:
        if token.shop_id in vistos:
            continue
        vistos.add(token.shop_id)
        base_nome = _base_vinculada(db, sub_base, token.id_base)
        if not base_nome:
            continue
        seller: dict[str, Any] = {"shop_id": token.shop_id, "base_nome": base_nome, **_contagem_vazia(), "erros": []}
        sellers.append(seller)
        saidas = _saidas_abertas(db, sub_base, base_nome, Saida.shopee_order_sn, dias=dias, limite=limite)
        pares: list[tuple[Saida, Optional[str]]] = []
        for i in range(0, len(saidas), SHOPEE_ORDER_DETAIL_BATCH):
            if deadline is not None and time.monotonic() > deadline:
                seller["parcial"] = True
                break
            lote = saidas[i:i + SHOPEE_ORDER_DETAIL_BATCH]
            try:
                resp = _shop_api_get(db, token, "/api/v2/order/get_order_detail", {
                    "order_sn_list": ",".join(s.shopee_order_sn for s in lote),
                })
            except LookupError as e:
                seller["erros"].append({"erro": str(e)[:200]})
                break
            except (ShopeeApiError, requests.RequestException) as e:
                seller["erros"].append({"erro": str(e)[:200]})
                continue
            por_sn = {str(o.get("order_sn")): o for o in resp.get("order_list") or [] if isinstance(o, dict)}
            seller["conferidos"] += len(lote)
            pares.extend((s, shopee_status_final(por_sn.get(s.shopee_order_sn) or {})) for s in lote)
        _aplicar_lote(db, pares, PLATAFORMA_SHOPEE, seller)
    return {"sub_base": sub_base, "sellers": sellers}


def _somar(resultados: list[dict[str, Any]]) -> dict[str, int]:
    total = _contagem_vazia()
    for r in resultados:
        for s in r.get("sellers") or []:
            for k in total:
                total[k] += int(s.get(k) or 0)
    return total


def _conferir_todas(
    db: Session,
    modelo,
    conferir: Callable[..., dict[str, Any]],
    deadline: float,
) -> dict[str, Any]:
    sub_bases = [
        sb
        for (sb,) in db.query(modelo.sub_base).filter(modelo.id_base.isnot(None)).distinct().order_by(modelo.sub_base).all()
        if (sb or "").strip()
    ]
    resultados: list[dict[str, Any]] = []
    nao_processadas: list[str] = []
    for sb in sub_bases:
        if time.monotonic() > deadline:
            nao_processadas.append(sb)
            continue
        try:
            resultados.append(conferir(db, sb, deadline=deadline))
        except Exception as e:
            db.rollback()
            logger.warning("baixa_plataforma: falha sub_base=%s: %s", sb, e)
            resultados.append({"sub_base": sb, "erro": str(e)[:200]})
    return {**_somar(resultados), "nao_processadas": nao_processadas, "resultados": resultados}


def conferir_todas_sub_bases(db: Session, *, max_runtime_seconds: int) -> dict[str, Any]:
    """Cron: baixa automática ML e Shopee de todas as sub_bases com conta vinculada, no orçamento de tempo."""
    inicio = time.monotonic()
    deadline_ml = inicio + max_runtime_seconds / 2
    ml = _conferir_todas(db, MLConexao, conferir_ml_sub_base, deadline_ml)
    shopee = _conferir_todas(db, ShopeeToken, conferir_shopee_sub_base, inicio + max_runtime_seconds)
    return {"mercado_livre": ml, "shopee": shopee}
