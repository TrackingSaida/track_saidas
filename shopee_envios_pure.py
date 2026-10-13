# shopee_envios_pure.py - Mapeamento/filtro de pedidos Shopee (sem I/O, testável)
from __future__ import annotations

import os
import unicodedata
from datetime import datetime
from typing import Any, Iterable, Optional

# Pedido pago aguardando envio (READY_TO_SHIP) ou com envio já organizado (PROCESSED).
SHOPEE_STATUS_PENDENTE_COLETA = ("READY_TO_SHIP", "PROCESSED")
# get_order_list aceita no máximo 15 dias por consulta.
SHOPEE_JANELA_MAX_SEGUNDOS = 15 * 24 * 3600
SHOPEE_CANAIS_PADRAO = ("Entrega Direta",)


def _texto(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _sem_acento(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(c for c in text if not unicodedata.combining(c)).casefold().strip()


def shopee_canais_entrega(raw: Optional[str] = None) -> tuple[str, ...]:
    """Canais logísticos importados (SHOPEE_CANAIS_ENTREGA, separados por vírgula)."""
    raw = os.getenv("SHOPEE_CANAIS_ENTREGA") if raw is None else raw
    canais = tuple(c.strip() for c in (raw or "").split(",") if c.strip())
    return canais or SHOPEE_CANAIS_PADRAO


def shopee_order_canal(order: dict) -> Optional[str]:
    """Nome do canal logístico do pedido (shipping_carrier do pedido ou do pacote)."""
    canal = _texto(order.get("shipping_carrier"))
    if canal:
        return canal
    for pacote in order.get("package_list") or []:
        if isinstance(pacote, dict) and _texto(pacote.get("shipping_carrier")):
            return _texto(pacote.get("shipping_carrier"))
    return None


def shopee_order_no_canal(order: dict, canais: Iterable[str]) -> bool:
    canal = _sem_acento(shopee_order_canal(order))
    if not canal:
        return False
    return any(_sem_acento(c) in canal for c in canais if c)


def shopee_order_pendente_coleta(order: dict, canais: Iterable[str]) -> bool:
    status = str(order.get("order_status") or "").upper()
    return status in SHOPEE_STATUS_PENDENTE_COLETA and shopee_order_no_canal(order, canais)


def shopee_order_destinatario(order: dict) -> dict[str, Any]:
    """Destinatário normalizado (mesmas chaves do ML) a partir de recipient_address."""
    addr = order.get("recipient_address") if isinstance(order.get("recipient_address"), dict) else {}
    return {
        "nome": _texto(addr.get("name")),
        "telefone": _texto(addr.get("phone")),
        "rua": None,
        "numero": None,
        "complemento": None,
        "bairro": _texto(addr.get("district")) or _texto(addr.get("town")),
        "cidade": _texto(addr.get("city")),
        "estado": _texto(addr.get("state")),
        "cep": _texto(addr.get("zipcode")),
        "endereco": _texto(addr.get("full_address")),
        "latitude": None,
        "longitude": None,
        "precisao": None,
    }


def shopee_order_itens(order: dict) -> list[dict[str, Any]]:
    itens = []
    for item in order.get("item_list") or []:
        if not isinstance(item, dict):
            continue
        itens.append({
            "titulo": _texto(item.get("item_name")),
            "quantidade": item.get("model_quantity_purchased") or item.get("quantity") or 1,
        })
    return itens


def shopee_order_package_number(order: dict) -> Optional[str]:
    for pacote in order.get("package_list") or []:
        if isinstance(pacote, dict) and _texto(pacote.get("package_number")):
            return _texto(pacote.get("package_number"))
    return None


def shopee_codigo_rastreio(tracking_number: Any) -> Optional[str]:
    """Rastreio da etiqueta Shopee (BR…) normalizado como o scanner lê; None se ainda não há."""
    from codigo_normalizer import normalize_codigo

    raw = _texto(tracking_number)
    if not raw:
        return None
    codigo, servico, _ = normalize_codigo(raw, strict_qr=False)
    return codigo if codigo and servico == "Shopee" else None


def shopee_janelas(dias: int, now: Optional[datetime] = None) -> list[tuple[int, int]]:
    """Intervalos (time_from, time_to) em unix cobrindo os últimos `dias`, com no máximo 15 dias cada."""
    fim = int((now or datetime.utcnow()).timestamp())
    inicio = fim - max(dias, 1) * 24 * 3600
    janelas = []
    while inicio < fim:
        ate = min(inicio + SHOPEE_JANELA_MAX_SEGUNDOS, fim)
        janelas.append((inicio, ate))
        inicio = ate
    return janelas


def shopee_fora_cobertura_resumo(envio: dict[str, Any]) -> dict[str, Any]:
    """Dados mínimos de um pedido rejeitado por cobertura, para o operador avisar o seller."""
    dest = envio.get("destinatario") or {}
    return {
        "order_sn": envio.get("order_sn"),
        "codigo": envio.get("codigo"),
        "comprador": dest.get("nome"),
        "cep": dest.get("cep"),
        "cidade": dest.get("cidade"),
        "estado": dest.get("estado"),
    }
