# shopee_int_service.py - Pedidos Shopee (Open Platform v2) → saídas aguardando coleta
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Optional

import requests
from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from models import ShopeeToken

logger = logging.getLogger(__name__)

SHOPEE_ORDER_LIST_PAGE_SIZE = 100
SHOPEE_ORDER_DETAIL_BATCH = 50
SHOPEE_ORDER_DETAIL_CAMPOS = "recipient_address,item_list,package_list,shipping_carrier"
_ERROS_AUTORIZACAO = {"invalid_access_token", "error_auth"}


class ShopeeApiError(Exception):
    def __init__(self, erro: str, mensagem: str = "", status_code: int | None = None):
        self.erro = erro or ""
        self.mensagem = mensagem or ""
        self.status_code = status_code
        super().__init__(f"{self.erro}: {self.mensagem}".strip(": "))

    @property
    def requer_autorizacao(self) -> bool:
        return self.erro in _ERROS_AUTORIZACAO


def _utcnow() -> datetime:
    return datetime.utcnow()


def get_valid_shopee_token(db: Session, token: ShopeeToken) -> str:
    """access_token válido da loja (renova se expirado). LookupError se precisar autorizar de novo."""
    from shopee_routes import refresh_shopee_token, shopee_token_precisa_renovar

    if not (token.refresh_token or "").strip():
        raise LookupError("A loja Shopee precisa ser autorizada novamente.")
    if shopee_token_precisa_renovar(token) and not refresh_shopee_token(db, token):
        if not token.expires_at or token.expires_at <= _utcnow():
            raise LookupError("A loja Shopee precisa ser autorizada novamente.")
    return token.access_token


def _shop_api_get(db: Session, token: ShopeeToken, path: str, params: dict[str, Any]) -> dict[str, Any]:
    """GET assinado de API da loja; renova o token uma vez se a Shopee recusar a autorização."""
    from shopee_routes import _get_shopee_api_config, _sign_api, refresh_shopee_token

    host, partner_id, partner_key = _get_shopee_api_config()
    for tentativa in range(2):
        access_token = get_valid_shopee_token(db, token)
        timestamp = int(time.time())
        sign = _sign_api(partner_id, partner_key, path, timestamp, shop_id=token.shop_id, access_token=access_token)
        query = {
            "partner_id": partner_id,
            "timestamp": timestamp,
            "access_token": access_token,
            "shop_id": token.shop_id,
            "sign": sign,
            **params,
        }
        resp = requests.get(f"{host}{path}", params=query, timeout=20)
        try:
            data = resp.json() if resp.text else {}
        except ValueError:
            data = {}
        erro = data.get("error") or ""
        if not erro and resp.status_code == 200:
            return data.get("response") or {}
        exc = ShopeeApiError(erro or f"http_{resp.status_code}", data.get("message") or "", resp.status_code)
        if exc.requer_autorizacao and tentativa == 0 and refresh_shopee_token(db, token, force=True):
            continue
        if exc.requer_autorizacao:
            raise LookupError("A loja Shopee precisa ser autorizada novamente.") from exc
        raise exc
    raise ShopeeApiError("erro_autorizacao")


def _listar_order_sns(db: Session, token: ShopeeToken, dias: int, max_pedidos: int) -> list[str]:
    from shopee_envios_pure import SHOPEE_STATUS_PENDENTE_COLETA, shopee_janelas

    sns: list[str] = []
    vistos: set[str] = set()
    for status in SHOPEE_STATUS_PENDENTE_COLETA:
        for time_from, time_to in shopee_janelas(dias):
            cursor = ""
            while len(sns) < max_pedidos:
                resp = _shop_api_get(db, token, "/api/v2/order/get_order_list", {
                    "time_range_field": "create_time",
                    "time_from": time_from,
                    "time_to": time_to,
                    "page_size": SHOPEE_ORDER_LIST_PAGE_SIZE,
                    "cursor": cursor,
                    "order_status": status,
                })
                for item in resp.get("order_list") or []:
                    sn = str(item.get("order_sn") or "").strip()
                    if sn and sn not in vistos:
                        vistos.add(sn)
                        sns.append(sn)
                cursor = resp.get("next_cursor") or ""
                if not resp.get("more") or not cursor:
                    break
    return sns[:max_pedidos]


def _detalhar_pedidos(db: Session, token: ShopeeToken, sns: list[str]) -> list[dict]:
    pedidos: list[dict] = []
    for i in range(0, len(sns), SHOPEE_ORDER_DETAIL_BATCH):
        lote = sns[i:i + SHOPEE_ORDER_DETAIL_BATCH]
        resp = _shop_api_get(db, token, "/api/v2/order/get_order_detail", {
            "order_sn_list": ",".join(lote),
            "response_optional_fields": SHOPEE_ORDER_DETAIL_CAMPOS,
        })
        pedidos.extend(o for o in resp.get("order_list") or [] if isinstance(o, dict))
    return pedidos


def _buscar_rastreio(db: Session, token: ShopeeToken, order_sn: str, package_number: Optional[str]) -> Optional[str]:
    params: dict[str, Any] = {"order_sn": order_sn}
    if package_number:
        params["package_number"] = package_number
    resp = _shop_api_get(db, token, "/api/v2/logistics/get_tracking_number", params)
    return resp.get("tracking_number")


def listar_pedidos_pendentes(
    db: Session,
    token: ShopeeToken,
    *,
    dias: int = 7,
    max_pedidos: int = 300,
) -> dict[str, Any]:
    """
    Pedidos da loja prontos para coleta (READY_TO_SHIP/PROCESSED) no canal de entrega da base
    (SHOPEE_CANAIS_ENTREGA, padrão "Entrega Direta"), com destinatário e rastreio BR….
    Pedido sem rastreio ainda vem com codigo=None. Lança LookupError (autorização) ou
    ShopeeApiError/requests.RequestException (API).
    """
    from shopee_envios_pure import (
        shopee_canais_entrega,
        shopee_codigo_rastreio,
        shopee_order_canal,
        shopee_order_destinatario,
        shopee_order_itens,
        shopee_order_package_number,
        shopee_order_pendente_coleta,
    )

    canais = shopee_canais_entrega()
    sns = _listar_order_sns(db, token, dias, max_pedidos)
    pedidos = _detalhar_pedidos(db, token, sns)
    resumo = {"pedidos_lidos": len(pedidos), "pendentes": 0, "ignorados": 0, "sem_rastreio": 0}
    canais_encontrados: set[str] = set()
    envios: list[dict[str, Any]] = []
    erros: list[dict[str, Any]] = []
    for order in pedidos:
        canal = shopee_order_canal(order)
        if canal:
            canais_encontrados.add(canal)
        if not shopee_order_pendente_coleta(order, canais):
            resumo["ignorados"] += 1
            continue
        order_sn = str(order.get("order_sn"))
        try:
            rastreio = _buscar_rastreio(db, token, order_sn, shopee_order_package_number(order))
        except (ShopeeApiError, requests.RequestException) as e:
            erros.append({"order_sn": order_sn, "erro": str(e)[:200]})
            continue
        codigo = shopee_codigo_rastreio(rastreio)
        if codigo is None:
            resumo["sem_rastreio"] += 1
        resumo["pendentes"] += 1
        envios.append({
            "order_sn": order_sn,
            "codigo": codigo,
            "tracking_number": rastreio,
            "status": order.get("order_status"),
            "canal": canal,
            "data_venda": order.get("create_time"),
            "itens": shopee_order_itens(order),
            "destinatario": shopee_order_destinatario(order),
        })
    return {
        "shop_id": token.shop_id,
        "dias": dias,
        "canais_importados": list(canais),
        "canais_encontrados": sorted(canais_encontrados),
        "resumo": resumo,
        "envios": envios,
        "erros": erros,
    }


def _lock_sync_sub_base(db: Session, sub_base: str) -> None:
    """Serializa syncs concorrentes (cron x botão) da mesma sub_base até o commit."""
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"shopee_int_sync:{sub_base}"})


def saidas_existentes_por_pedido(db: Session, sub_base: str, envios: list[dict]) -> dict[str, Any]:
    """order_sn → Saida já existente na sub_base (por shopee_order_sn ou rastreio lido no scanner)."""
    from models import Saida

    if not envios:
        return {}
    por_codigo = {e["codigo"]: e["order_sn"] for e in envios if e.get("codigo")}
    sns = [e["order_sn"] for e in envios]
    filtros = [Saida.shopee_order_sn.in_(sns)]
    if por_codigo:
        filtros.append(Saida.codigo.in_(list(por_codigo)))
    rows = db.scalars(select(Saida).where(Saida.sub_base == sub_base, or_(*filtros))).all()
    existentes: dict[str, Any] = {}
    for row in rows:
        if row.shopee_order_sn in sns:
            existentes.setdefault(row.shopee_order_sn, row)
        elif row.codigo in por_codigo:
            existentes.setdefault(por_codigo[row.codigo], row)
    return existentes


def _completar_saida_existente(db: Session, saida: Any, envio: dict, detail_fields: dict) -> bool:
    """Preenche nº do pedido Shopee e destinatário que faltam numa saída já registrada (não sobrescreve)."""
    from models import SaidaDetail
    from ml_int_envios_pure import campos_faltantes

    alterou = False
    if not saida.shopee_order_sn:
        saida.shopee_order_sn = envio["order_sn"]
        alterou = True
    detail = db.scalar(
        select(SaidaDetail)
        .where(SaidaDetail.id_saida == saida.id_saida)
        .order_by(SaidaDetail.id_detail.desc())
        .limit(1)
    )
    if detail is not None:
        atual = {k: getattr(detail, k, None) for k in detail_fields}
        for k, v in campos_faltantes(atual, detail_fields).items():
            setattr(detail, k, v)
            alterou = True
    return alterou


def sync_envios_sub_base(
    db: Session,
    sub_base: str,
    *,
    shop_id: Optional[int] = None,
    user_id: Optional[int] = None,
    dias: int = 7,
    max_pedidos: int = 300,
) -> dict[str, Any]:
    """
    Importa pedidos Shopee prontos para coleta das lojas vinculadas a seller da sub_base:
    cria Saida (aguardando_coleta, código = rastreio BR…) + SaidaDetail (destinatário) e
    completa dados faltantes de saídas já registradas. Mesmas regras do ML: loja sem vínculo
    é pulada; com cobertura de CEP configurada, pedido novo fora da área é rejeitado e
    listado em "fora_cobertura". Pedido ainda sem rastreio não é importado (volta no próximo sync).
    """
    from cobertura_cep_service import cep_coberto, list_prefixos_ativos
    from models import BasePreco, Saida, SaidaDetail, SaidaHistorico
    from ml_int_envios_pure import destinatario_to_saida_detail_fields
    from shopee_envios_pure import shopee_fora_cobertura_resumo

    prefixos_cobertura = list_prefixos_ativos(db, sub_base)

    q = db.query(ShopeeToken).filter(ShopeeToken.sub_base == sub_base)
    if shop_id is not None:
        q = q.filter(ShopeeToken.shop_id == shop_id)
    tokens = q.order_by(ShopeeToken.criado_em.desc(), ShopeeToken.id.desc()).all()

    vistos: set[int] = set()
    sellers: list[dict[str, Any]] = []
    total_criados = 0
    for token in tokens:
        if token.shop_id in vistos:
            continue
        vistos.add(token.shop_id)
        seller: dict[str, Any] = {
            "shop_id": token.shop_id,
            "nickname": token.shop_name,
            "id_base": token.id_base,
            "base_nome": None,
            "motivo": None,
            "pendentes": 0,
            "criados": 0,
            "ja_registrados": 0,
            "atualizados": 0,
            "sem_rastreio": 0,
            "fora_cobertura": [],
            "erros": [],
        }
        sellers.append(seller)

        base_row = None
        if token.id_base is not None:
            base_row = db.scalar(
                select(BasePreco).where(BasePreco.id_base == token.id_base, BasePreco.sub_base == sub_base)
            )
        if base_row is None or not (base_row.base or "").strip():
            seller["motivo"] = "sem_vinculo"
            continue
        base_nome = base_row.base.strip()
        seller["base_nome"] = base_nome

        try:
            data = listar_pedidos_pendentes(db, token, dias=dias, max_pedidos=max_pedidos)
        except (LookupError, RuntimeError) as e:
            seller["motivo"] = "requer_reautorizacao" if "autoriza" in str(e) else "erro_conexao"
            seller["erros"].append({"erro": str(e)})
            continue
        except (ShopeeApiError, requests.RequestException) as e:
            seller["motivo"] = "erro_api_shopee"
            seller["erros"].append({"erro": str(e)[:200]})
            continue

        seller["erros"].extend(data["erros"])
        envios = data["envios"]
        seller["pendentes"] = len(envios)
        if not envios:
            continue

        try:
            _lock_sync_sub_base(db, sub_base)
            existentes = saidas_existentes_por_pedido(db, sub_base, envios)
            for envio in envios:
                sn = envio["order_sn"]
                detail_fields = destinatario_to_saida_detail_fields(envio["destinatario"])
                existente = existentes.get(sn)
                if existente is not None:
                    seller["ja_registrados"] += 1
                    try:
                        with db.begin_nested():
                            if _completar_saida_existente(db, existente, envio, detail_fields):
                                seller["atualizados"] += 1
                    except Exception as e:
                        seller["erros"].append({"order_sn": sn, "erro": str(e)[:200]})
                    continue
                if not envio.get("codigo"):
                    seller["sem_rastreio"] += 1
                    continue
                if not cep_coberto((envio.get("destinatario") or {}).get("cep"), prefixos_cobertura):
                    seller["fora_cobertura"].append(shopee_fora_cobertura_resumo(envio))
                    continue
                try:
                    with db.begin_nested():
                        nova = Saida(
                            sub_base=sub_base,
                            codigo=envio["codigo"],
                            servico="Shopee",
                            status="aguardando_coleta",
                            shopee_order_sn=sn,
                            base=base_nome,
                        )
                        db.add(nova)
                        db.flush()
                        db.add(
                            SaidaDetail(
                                id_saida=nova.id_saida,
                                id_entregador=None,
                                status="Aguardando coleta",
                                **detail_fields,
                            )
                        )
                        db.add(
                            SaidaHistorico(
                                id_saida=nova.id_saida,
                                evento="shopee_int_sync",
                                status_novo="aguardando_coleta",
                                user_id=user_id,
                            )
                        )
                    seller["criados"] += 1
                except Exception as e:
                    seller["erros"].append({"order_sn": sn, "erro": str(e)[:200]})
            db.commit()
        except Exception as e:
            db.rollback()
            seller["criados"] = 0
            seller["atualizados"] = 0
            seller["erros"].append({"erro": f"Falha ao gravar: {str(e)[:200]}"})
            continue
        total_criados += seller["criados"]
        if seller["fora_cobertura"]:
            logger.info(
                "shopee_int_sync: sub_base=%s shop_id=%s rejeitados_fora_cobertura=%s pedidos=%s",
                sub_base,
                token.shop_id,
                len(seller["fora_cobertura"]),
                [f["order_sn"] for f in seller["fora_cobertura"]],
            )

    return {"sub_base": sub_base, "criados": total_criados, "dias": dias, "sellers": sellers}


def sync_envios_todas_sub_bases(
    db: Session,
    *,
    dias: int = 7,
    max_runtime_seconds: int = 240,
) -> dict[str, Any]:
    """Cron: sincroniza todas as sub_bases com loja Shopee vinculada a seller, com orçamento de tempo."""
    inicio = time.monotonic()
    sub_bases = [
        sb
        for (sb,) in db.query(ShopeeToken.sub_base)
        .filter(ShopeeToken.id_base.isnot(None))
        .distinct()
        .order_by(ShopeeToken.sub_base)
        .all()
        if (sb or "").strip()
    ]
    resultados: list[dict[str, Any]] = []
    pendentes_tempo: list[str] = []
    for sb in sub_bases:
        if time.monotonic() - inicio > max_runtime_seconds:
            pendentes_tempo.append(sb)
            continue
        try:
            resultados.append(sync_envios_sub_base(db, sb, dias=dias))
        except Exception as e:
            db.rollback()
            logger.warning("shopee sync_envios_todas_sub_bases: falha sub_base=%s: %s", sb, e)
            resultados.append({"sub_base": sb, "criados": 0, "erro": str(e)[:200]})
    return {
        "sub_bases": len(sub_bases),
        "criados": sum(r.get("criados", 0) for r in resultados),
        "partial": bool(pendentes_tempo),
        "nao_processadas": pendentes_tempo,
        "resultados": resultados,
    }
