"""Registrar Entrada na base (sem seller/cobrança de coleta)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth import get_current_user
from codigo_normalizer import canonicalize_servico, is_qr_like_scan_payload, normalize_codigo
from db import get_db
from leitura_manual_auth import ensure_manual_code_entry_allowed
from models import Owner, Saida, SaidaHistorico, User
from saidas_routes import STATUS_NA_BASE, normalizar_status_saida

router = APIRouter(prefix="/entradas", tags=["Entradas"])

MSG_ENTRADA_OBRIGATORIA = "Este pacote ainda não teve entrada na base."


class EntradaLerIn(BaseModel):
    codigo: str = Field(min_length=1)
    origem: Optional[str] = None
    qr_payload_raw: Optional[str] = None


def _owner_entrada_habilitada(db: Session, sub_base: str, user: User) -> bool:
    if bool(getattr(user, "entrada_obrigatoria_habilitada", False)):
        return True
    owner = db.scalar(select(Owner).where(Owner.sub_base == sub_base))
    return bool(owner and getattr(owner, "entrada_obrigatoria_habilitada", False))


def _should_store_qr_payload_raw(servico: str, qr_raw: Optional[str]) -> bool:
    if not qr_raw or not str(qr_raw).strip():
        return False
    return (servico or "").strip().lower() in ("ml", "mercado_livre", "mercado livre")


@router.post("/ler")
def ler_entrada(
    payload: EntradaLerIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    role = int(getattr(current_user, "role", 0) or 0)
    if role not in (0, 1, 2, 3):
        raise HTTPException(status_code=403, detail="Acesso restrito a admin/operador.")

    sub_base = (current_user.sub_base or "").strip()
    if not sub_base:
        raise HTTPException(status_code=403, detail="Sub-base não definida.")

    if not _owner_entrada_habilitada(db, sub_base, current_user):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "ENTRADA_DESABILITADA",
                "message": "Registrar entrada não está habilitado para esta base.",
            },
        )

    origem = ensure_manual_code_entry_allowed(
        db, current_user, origem=getattr(payload, "origem", None) or "camera"
    )
    strict_qr = origem == "camera"
    raw = (payload.codigo or "").strip()
    if strict_qr and not is_qr_like_scan_payload(raw):
        raise HTTPException(
            status_code=422,
            detail="Leitura inválida pela câmera. Use apenas QRCode da etiqueta.",
        )

    codigo, servico, qr_from_norm = normalize_codigo(raw, strict_qr=strict_qr)
    if codigo is None or servico is None:
        raise HTTPException(
            status_code=422,
            detail="Código inválido. Verifique o formato do QR/código de barras.",
        )

    servico_val = canonicalize_servico(servico)
    qr_payload_raw = payload.qr_payload_raw or qr_from_norm
    store_qr = _should_store_qr_payload_raw(servico_val, qr_payload_raw)

    existente = db.scalar(
        select(Saida).where(Saida.sub_base == sub_base, Saida.codigo == codigo)
    )

    if existente is None:
        try:
            row = Saida(
                sub_base=sub_base,
                username=current_user.username,
                codigo=codigo,
                servico=servico_val,
                status=STATUS_NA_BASE,
                qr_payload_raw=qr_payload_raw.strip() if store_qr and qr_payload_raw else None,
            )
            db.add(row)
            db.flush()
            db.add(
                SaidaHistorico(
                    id_saida=row.id_saida,
                    evento="entrada_base",
                    status_novo=STATUS_NA_BASE,
                    user_id=getattr(current_user, "id", None),
                )
            )
            db.commit()
            db.refresh(row)
            return {
                "ok": True,
                "ja_existia": False,
                "id_saida": row.id_saida,
                "codigo": row.codigo,
                "servico": row.servico,
                "status": row.status,
            }
        except Exception as e:
            db.rollback()
            raise HTTPException(500, f"Erro ao registrar entrada: {e}")

    status_norm = normalizar_status_saida(existente.status)

    if status_norm == STATUS_NA_BASE:
        return JSONResponse(
            status_code=409,
            content={
                "code": "JA_NA_BASE",
                "message": "Este pacote já teve entrada na base.",
                "id_saida": existente.id_saida,
                "status": existente.status,
            },
        )

    if status_norm == "coletado":
        status_anterior = existente.status
        existente.status = STATUS_NA_BASE
        db.add(
            SaidaHistorico(
                id_saida=existente.id_saida,
                evento="entrada_base",
                status_anterior=status_anterior,
                status_novo=STATUS_NA_BASE,
                user_id=getattr(current_user, "id", None),
            )
        )
        try:
            db.commit()
            db.refresh(existente)
            return {
                "ok": True,
                "ja_existia": True,
                "promovido_coleta": True,
                "id_saida": existente.id_saida,
                "codigo": existente.codigo,
                "servico": existente.servico,
                "status": existente.status,
            }
        except Exception:
            db.rollback()
            raise HTTPException(500, "Erro ao atualizar entrada.")

    return JSONResponse(
        status_code=422,
        content={
            "code": "STATUS_INVALIDO_ENTRADA",
            "message": "Este pacote não pode receber entrada neste status.",
            "id_saida": existente.id_saida,
            "status": existente.status,
        },
    )
