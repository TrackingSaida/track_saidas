from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from models import User, LogLeitura
from log_leitura_service import RESULTADOS_CRITICOS, registrar_log_leitura_critico

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

    # Mantém compatibilidade do endpoint legado, mas restringe aos críticos acordados.
    if payload.resultado not in RESULTADOS_CRITICOS:
        return

    try:
        registrar_log_leitura_critico(
            sub_base=sub_base,
            username=username,
            origem=payload.origem,
            tipo=payload.tipo,
            codigo=payload.codigo,
            resultado=payload.resultado,
            role=getattr(current_user, "role", None),
            motoboy_id=getattr(current_user, "motoboy_id", None),
            id_saida=None,
            origem_app="web",
            endpoint="/logs/leituras",
        )

    except Exception as e:
        # 🔇 nunca propaga erro
        print("[LOG_LEITURA_ERROR]", str(e))

    return
