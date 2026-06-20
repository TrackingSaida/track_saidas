"""Mapeamento status interno rotas_motoboy → status API consumido pelo app mobile."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from models import RotasMotoboy

API_STATUS_SEM_ROTA = "sem_rota"
API_STATUS_ROTA_PRONTA = "rota_pronta"
API_STATUS_EM_ENTREGA = "em_entrega"


def map_rota_to_api_status(rota: Optional["RotasMotoboy"]) -> str:
    if rota is None:
        return API_STATUS_SEM_ROTA
    status = (getattr(rota, "status", None) or "").strip().lower()
    if status == "ativa" and getattr(rota, "finalizado_em", None) is None:
        return API_STATUS_EM_ENTREGA
    if status == "preparando" and getattr(rota, "finalizado_em", None) is None:
        return API_STATUS_ROTA_PRONTA
    return API_STATUS_SEM_ROTA


def build_rotas_ativa_out(
    rota: Optional["RotasMotoboy"],
    *,
    sub_base: str,
    motoboy_id: int,
    data_iso: str,
    ordem: Optional[List[int]] = None,
) -> Dict[str, Any]:
    api_status = map_rota_to_api_status(rota)
    if rota is None or api_status == API_STATUS_SEM_ROTA:
        return {
            "status": API_STATUS_SEM_ROTA,
            "rota_id": None,
            "ordem": [],
            "parada_atual": 0,
            "data": data_iso,
            "sub_base": sub_base,
            "entregador_id": motoboy_id,
            "sequencia_preservada": True,
            "started_at": None,
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }

    ordem_list = ordem if ordem is not None else []
    updated = getattr(rota, "updated_at", None) or getattr(rota, "iniciado_em", None)
    updated_iso = updated.isoformat() + "Z" if updated else datetime.utcnow().isoformat() + "Z"
    started = getattr(rota, "iniciado_em", None)
    started_iso = started.isoformat() + "Z" if started else None

    return {
        "status": api_status,
        "rota_id": str(rota.id),
        "ordem": ordem_list,
        "parada_atual": int(getattr(rota, "parada_atual", 0) or 0),
        "data": rota.data.isoformat() if getattr(rota, "data", None) else data_iso,
        "sub_base": getattr(rota, "sub_base", None) or sub_base,
        "entregador_id": motoboy_id,
        "sequencia_preservada": True,
        "started_at": started_iso,
        "updated_at": updated_iso,
    }
