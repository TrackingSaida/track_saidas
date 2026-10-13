# ml_int_envios_pure.py - Mapeamento/filtro de envios ML (sem I/O, testável)
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

# Flex: o próprio seller/transportadora parceira entrega ao comprador.
ML_LOGISTIC_FLEX = "self_service"
# Envio etiquetado e aguardando sair do seller.
ML_STATUS_PENDENTE_COLETA = "ready_to_ship"


def _nome(value: Any) -> Optional[str]:
    """Campos de localidade vêm como {id, name} no formato novo e às vezes como texto."""
    if isinstance(value, dict):
        value = value.get("name")
    text = str(value).strip() if value is not None else ""
    return text or None


def _texto(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coord(value: Any) -> Optional[float]:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if num != 0 else None


# geolocation_type do ML → coord_precision usado pelo app (rooftop | street | approx).
_ML_GEO_PRECISAO = {
    "ROOFTOP": "rooftop",
    "RANGE_INTERPOLATED": "street",
    "GEOMETRIC_CENTER": "street",
    "APPROXIMATE": "approx",
}


def ml_geolocation_precisao(geolocation_type: Any) -> Optional[str]:
    return _ML_GEO_PRECISAO.get(str(geolocation_type or "").strip().upper())


def ml_shipment_logistic_type(ship: dict) -> Optional[str]:
    """Tipo logístico: `logistic.type` (x-format-new) ou `logistic_type` (formato antigo)."""
    logistic = ship.get("logistic")
    if isinstance(logistic, dict) and logistic.get("type"):
        return str(logistic["type"]).lower()
    if ship.get("logistic_type"):
        return str(ship["logistic_type"]).lower()
    return None


def ml_shipment_pendente_coleta(ship: dict, logistic_types: Iterable[str] = (ML_LOGISTIC_FLEX,)) -> bool:
    status = (ship.get("status") or "").lower()
    if status != ML_STATUS_PENDENTE_COLETA:
        return False
    tipos = {t.lower() for t in logistic_types}
    return ml_shipment_logistic_type(ship) in tipos


def ml_shipment_destinatario(ship: dict) -> dict[str, Any]:
    """
    Destinatário normalizado a partir do shipment ML.
    x-format-new: destination{receiver_*, shipping_address{...}}.
    Formato antigo: receiver_address{..., receiver_name, receiver_phone}.
    """
    dest = ship.get("destination") if isinstance(ship.get("destination"), dict) else {}
    addr = dest.get("shipping_address") if isinstance(dest.get("shipping_address"), dict) else None
    if addr is None:
        addr = ship.get("receiver_address") if isinstance(ship.get("receiver_address"), dict) else dest

    return {
        "nome": _texto(dest.get("receiver_name") or addr.get("receiver_name")),
        "telefone": _texto(dest.get("receiver_phone") or addr.get("receiver_phone")),
        "rua": _texto(addr.get("street_name")),
        "numero": _texto(addr.get("street_number")),
        "complemento": _texto(addr.get("comment") or dest.get("comments")),
        "bairro": _nome(addr.get("neighborhood")) or _nome(addr.get("municipality")),
        "cidade": _nome(addr.get("city")),
        "estado": _nome(addr.get("state")),
        "cep": _texto(addr.get("zip_code")),
        "endereco": _texto(addr.get("address_line")),
        "latitude": _coord(addr.get("latitude")),
        "longitude": _coord(addr.get("longitude")),
        "precisao": ml_geolocation_precisao(addr.get("geolocation_type")),
    }


def destinatario_to_saida_detail_fields(dest: dict[str, Any]) -> dict[str, Any]:
    """Mapeia destinatário normalizado para colunas dest_* do SaidaDetail (omite vazios)."""
    fields = {
        "dest_nome": dest.get("nome"),
        "dest_rua": dest.get("rua"),
        "dest_numero": dest.get("numero"),
        "dest_complemento": dest.get("complemento"),
        "dest_bairro": dest.get("bairro"),
        "dest_cidade": dest.get("cidade"),
        "dest_estado": dest.get("estado"),
        "dest_cep": dest.get("cep"),
        "dest_contato": dest.get("telefone"),
        "endereco_formatado": dest.get("endereco"),
        "latitude": dest.get("latitude"),
        "longitude": dest.get("longitude"),
    }
    if fields["latitude"] is not None and fields["longitude"] is not None:
        fields["endereco_origem"] = "mercado_livre"
        fields["geocode_source"] = "mercado_livre"
        fields["coord_precision"] = dest.get("precisao")
    else:
        fields.pop("latitude")
        fields.pop("longitude")
    return {k: v for k, v in fields.items() if v is not None}


def campos_faltantes(atual: dict[str, Any], novos: dict[str, Any]) -> dict[str, Any]:
    """Somente os campos novos cujo valor atual está vazio (não sobrescreve dado existente)."""
    return {k: v for k, v in novos.items() if atual.get(k) in (None, "")}


def ml_order_shipment_id(order: Any) -> Optional[int]:
    """Extrai shipping.id de um pedido do /orders/search (results são objetos de pedido)."""
    if not isinstance(order, dict):
        return None
    shipping = order.get("shipping") or {}
    sid = shipping.get("id") if isinstance(shipping, dict) else None
    try:
        return int(sid) if sid else None
    except (TypeError, ValueError):
        return None


def ml_date_from(dias: int, now: Optional[datetime] = None) -> str:
    """Data inicial no formato aceito por order.date_created.from."""
    base = (now or datetime.utcnow()) - timedelta(days=max(dias, 0))
    return base.strftime("%Y-%m-%dT00:00:00.000-00:00")
