from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from db import SessionLocal
from models import LogLeitura

# Apenas eventos críticos acordados com o negócio.
RESULTADOS_CRITICOS = {
    "duplicado",
    "atribuido_a_outro",
    "assumiu_de_outro",
    "leitura_dia_anterior_aguardando_confirmacao",
    "nova_saida_mesmo_entregador_confirmada",
    "bloqueio_status_finalizado",
}

# Evita gravações repetidas do mesmo evento vindo de mais de um endpoint.
DEDUP_WINDOW_SECONDS = 5


def registrar_log_leitura_critico(
    *,
    sub_base: Optional[str],
    username: Optional[str],
    origem: Optional[str],
    tipo: str,
    codigo: Optional[str],
    resultado: str,
    role: Optional[int] = None,
    motoboy_id: Optional[int] = None,
    id_saida: Optional[int] = None,
    origem_app: Optional[str] = None,
    endpoint: Optional[str] = None,
    db: Optional[Session] = None,
) -> None:
    """
    Grava log crítico de leitura.
    Se `db` for passado, reutiliza a sessão do request (evita 2ª conexão do pool).
    Caso contrário, usa sessão isolada. Falhas são silenciosas por design.
    """
    if not sub_base or not username:
        return
    if resultado not in RESULTADOS_CRITICOS:
        return

    owns_session = db is None
    if owns_session:
        db = SessionLocal()
    assert db is not None
    try:
        cutoff = datetime.utcnow() - timedelta(seconds=DEDUP_WINDOW_SECONDS)
        dup = db.scalar(
            select(LogLeitura.id).where(
                LogLeitura.sub_base == sub_base,
                LogLeitura.username == username,
                LogLeitura.tipo == tipo,
                LogLeitura.resultado == resultado,
                LogLeitura.codigo == codigo,
                LogLeitura.id_saida == id_saida,
                LogLeitura.motoboy_id == motoboy_id,
                LogLeitura.created_at >= cutoff,
            ).limit(1)
        )
        if dup:
            return

        db.add(
            LogLeitura(
                sub_base=sub_base,
                username=username,
                origem=origem or "desconhecida",
                tipo=tipo,
                codigo=(codigo or "").strip() or None,
                resultado=resultado,
                role=role,
                motoboy_id=motoboy_id,
                id_saida=id_saida,
                origem_app=origem_app,
                endpoint=endpoint,
            )
        )
        db.commit()
    except Exception:
        db.rollback()
    finally:
        if owns_session:
            db.close()
