from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from models import User, LogLeitura

# ============================================================
# ROTAS DE LOGS DE LEITURA
# ============================================================

router = APIRouter(prefix="/logs", tags=["Logs"])


# ============================================================
# SCHEMA
# ============================================================

class LogLeituraCreate(BaseModel):
    # origem da leitura
    origem: str = Field(..., examples=["camera", "teclado"])

    # tipo de operação
    tipo: str = Field(..., examples=["saida", "coleta"])

    # dados do código
    codigo: Optional[str] = None

    # resultado final
    resultado: str = Field(
        ...,
        examples=[
            "ok",
            "duplicado",
            "ja_saiu",
            "erro_http",
            "erro_patch",
            "erro_excecao",
            "nao_coletado_registrado",
        ],
    )

    # ─────────────────────────────
    # MÉTRICAS ANTIGAS (mantidas)
    delta_from_last_read_ms: Optional[float] = None
    delta_read_to_send_ms: Optional[float] = None
    delta_send_to_response_ms: Optional[float] = None

    # timestamp da leitura (performance.now)
    ts_read: Optional[float] = None

    # ─────────────────────────────
    # MÉTRICA BACKEND (header X-Backend-Process-Time)
    backend_processing_ms: Optional[float] = None

    # ─────────────────────────────
    # CONTEXTO DE DEVICE / REDE
    network_status: Optional[str] = None
    device_type: Optional[str] = None
    os: Optional[str] = None


# ============================================================
# POST — REGISTRAR LOG DE LEITURA
# ============================================================

@router.post(
    "/leituras",
    status_code=status.HTTP_204_NO_CONTENT,
)
def registrar_log_leitura(
    payload: LogLeituraCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Registra logs de leitura (coletas e saídas).

    ✔ Fire-and-forget
    ✔ Nunca interfere no fluxo
    ✔ Nunca quebra UX
    ✔ Falhas são silenciosas
    """

    sub_base = getattr(current_user, "sub_base", None)
    username = getattr(current_user, "username", None)

    # 🔕 Se faltar contexto mínimo, ignora silenciosamente
    if not sub_base or not username:
        return

    try:
        log = LogLeitura(
            sub_base=sub_base,
            username=username,

            origem=payload.origem,
            tipo=payload.tipo,
            codigo=payload.codigo,

            resultado=payload.resultado,

            # métricas antigas
            delta_from_last_read_ms=payload.delta_from_last_read_ms,
            delta_read_to_send_ms=payload.delta_read_to_send_ms,
            delta_send_to_response_ms=payload.delta_send_to_response_ms,

            ts_read=payload.ts_read,

            backend_processing_ms=payload.backend_processing_ms,

            network_status=payload.network_status,
            device_type=payload.device_type,
            os=payload.os,
        )

        db.add(log)
        db.commit()

    except Exception as e:
        db.rollback()
        # 🔇 nunca propaga erro
        print("[LOG_LEITURA_ERROR]", str(e))

    return
