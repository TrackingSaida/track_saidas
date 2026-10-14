# baixa_plataforma_pure.py - Regras da baixa automática de pedidos sincronizados (ML/Shopee)
"""
Status final informado pela plataforma → ação no sistema:
- pedido ainda aguardando coleta (entregue ou cancelado lá) → cancelado aqui (nunca coletado, sem cobrança);
- pedido já coletado/em rota entregue lá → entregue aqui;
- pedido já coletado/em rota cancelado lá → não muda o status; só sinaliza para o operador
  (pode haver devolução física e o cancelado desconta do fechamento do seller).
Pedido já finalizado no sistema nunca é alterado.
"""
from __future__ import annotations

import json
from typing import Any, Optional

STATUS_FINAIS = frozenset({"entregue", "cancelado", "cancelada", "devolvido", "devolvida"})

FINAL_ENTREGUE = "entregue"
FINAL_CANCELADO = "cancelado"

ACAO_ENTREGAR = "entregar"
ACAO_CANCELAR = "cancelar"
ACAO_SINALIZAR_CANCELADO = "sinalizar_cancelado"

EVENTO_CANCELADO_PLATAFORMA = "cancelado_plataforma"

PLATAFORMA_ML = "Mercado Livre"
PLATAFORMA_SHOPEE = "Shopee"

_SHOPEE_ENTREGUE = ("COMPLETED", "TO_CONFIRM_RECEIVE")
_SHOPEE_CANCELADO = ("CANCELLED",)


def status_finalizado(status: Optional[str]) -> bool:
    return (status or "").strip().lower() in STATUS_FINAIS


def ml_status_final(shipment: dict[str, Any]) -> Optional[str]:
    st = str((shipment or {}).get("status") or "").strip().lower()
    if st == "delivered":
        return FINAL_ENTREGUE
    if st == "cancelled":
        return FINAL_CANCELADO
    return None


def shopee_status_final(order: dict[str, Any]) -> Optional[str]:
    st = str((order or {}).get("order_status") or "").strip().upper()
    if st in _SHOPEE_ENTREGUE:
        return FINAL_ENTREGUE
    if st in _SHOPEE_CANCELADO:
        return FINAL_CANCELADO
    return None


def acao_baixa(status_sistema: Optional[str], final: Optional[str], *, ja_sinalizado: bool = False) -> Optional[str]:
    if final not in (FINAL_ENTREGUE, FINAL_CANCELADO) or status_finalizado(status_sistema):
        return None
    if (status_sistema or "").strip().lower() == "aguardando_coleta":
        return ACAO_CANCELAR
    if final == FINAL_ENTREGUE:
        return ACAO_ENTREGAR
    return None if ja_sinalizado else ACAO_SINALIZAR_CANCELADO


def payload_historico(plataforma: str, status_plataforma: Optional[str] = None) -> str:
    data: dict[str, str] = {"plataforma": plataforma}
    if status_plataforma:
        data["status_plataforma"] = status_plataforma
    return json.dumps(data, ensure_ascii=False)


def rotulo_evento_plataforma(
    evento: Optional[str],
    plataforma: Optional[str],
    status_plataforma: Optional[str] = None,
) -> Optional[str]:
    """Rótulo amigável para eventos gerados pela baixa automática; None se não for o caso."""
    nome = (plataforma or "").strip()
    if not nome:
        return None
    feminino = nome == PLATAFORMA_SHOPEE
    pelo, no = ("pela", "na") if feminino else ("pelo", "no")
    ev = (evento or "").strip().lower()
    st = (status_plataforma or "").strip().lower()
    if ev == "entregue":
        return f"Entrega confirmada {pelo} {nome}"
    if ev == "cancelado":
        if st == FINAL_ENTREGUE:
            return f"Entregue {pelo} {nome} sem coleta: cancelado aqui"
        if st == FINAL_CANCELADO:
            return f"Cancelado {no} {nome} antes da coleta"
        return f"Cancelado {pelo} {nome} (não coletado)"
    if ev == EVENTO_CANCELADO_PLATAFORMA:
        return f"Cancelado {no} {nome}: verificar devolução"
    return None


def rotulo_evento_payload(evento: Optional[str], data: dict[str, Any]) -> Optional[str]:
    return rotulo_evento_plataforma(
        evento, str(data.get("plataforma") or ""), str(data.get("status_plataforma") or ""),
    )
