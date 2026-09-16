"""
Rotas de Etiquetas
POST /etiquetas/gerar — gera etiqueta 100x150mm (QR Code) em PDF/PNG.
Modo genérico (padrão). TODO: futuro - Shopee/ML com autenticação nas APIs.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from models import User, Saida

router = APIRouter(prefix="/etiquetas", tags=["Etiquetas"])
logger = logging.getLogger(__name__)

# TODO: Exportação ZPL
# TODO: Impressão direta Zebra
# TODO: Tabela etiquetas_logs
# TODO: Geração automática ao registrar saída


def _is_ml_servico(s: Optional[str]) -> bool:
    if not s:
        return False
    x = s.strip().lower()
    return "mercado" in x or "ml" in x or "flex" in x


def _is_ml_codigo(codigo: str) -> bool:
    """Código ML: 11 dígitos começando com 4[5-9]."""
    return bool(re.match(r"^4[5-9]\d{9}$", (codigo or "").strip()))


# ============================================================
# SCHEMAS
# ============================================================

class EtiquetaGerarPayload(BaseModel):
    codigo: str = Field(min_length=1, description="Código de rastreio/pedido")
    id_saida: Optional[int] = None  # Busca qr_payload_raw para ML
    servico: Optional[str] = None
    qr_payload: Optional[str] = None  # Payload bruto para QR (ML JSON)
    formato: Optional[str] = Field(default="pdf", description="pdf | png")


# ============================================================
# HELPERS — Resolução de dados externos (fallback em erro)
# ============================================================

# def _normalizar_modo(modo: str) -> str:
#     m = (modo or "").strip().lower()
#     if m in ("shopee", "shp"):
#         return "shopee"
#     if m in ("ml", "mercado livre", "mercadolivre"):
#         return "ml"
#     return "generic"


# TODO: Futuro - autenticação APIs Shopee e Mercado Livre para enriquecer etiquetas
# def _buscar_dados_shopee(db: Session, codigo: str) -> Optional[Dict[str, Any]]:
#     """Tenta obter dados do envio na Shopee. Retorna None em qualquer falha."""
#     try:
#         from shopee_token_service import (
#             get_valid_shopee_access_token,
#             get_latest_shopee_token,
#             _get_shopee_config,
#             _sign_api,
#         )
#         import requests
#         import time
#
#         token = get_latest_shopee_token(db)
#         if not token:
#             return None
#         access_token = get_valid_shopee_access_token(db, shop_id=token.shop_id)
#         host, partner_id, partner_key = _get_shopee_config()
#         path = "/api/v2/order/get_order_list"
#         timestamp = int(time.time())
#         sign = _sign_api(partner_id, partner_key, path, timestamp, token.shop_id, access_token)
#         url = f"{host}{path}"
#         params = {
#             "partner_id": partner_id,
#             "timestamp": timestamp,
#             "sign": sign,
#             "shop_id": token.shop_id,
#         }
#         body = {"order_status": "READY_TO_SHIP", "page_size": 50}
#         resp = requests.post(url, params=params, json=body)
#         if resp.status_code != 200:
#             return None
#         data = resp.json()
#         orders = data.get("response", {}).get("order_list", []) or []
#         for o in orders:
#             tracking = (o.get("tracking_no") or "").strip()
#             if tracking and codigo.upper() in tracking.upper():
#                 addr = o.get("recipient_address", {}) or {}
#                 return {
#                     "destinatario": addr.get("name") or "",
#                     "cidade": addr.get("city") or "",
#                     "cep": addr.get("zipcode") or "",
#                 }
#         return None
#     except Exception as e:
#         logger.warning("Shopee etiqueta: %s", e)
#         return None
#


# ============================================================
# ROTA
# ============================================================

def _resolve_qr_content(
    codigo: str,
    id_saida: Optional[int],
    servico: Optional[str],
    qr_payload: Optional[str],
    sub_base: Optional[str],
    db: Session,
) -> Optional[str]:
    """
    Resolve o conteúdo do QR para etiqueta ML.
    Ordem: 1) qr_payload explícito 2) id_saida com qr_payload_raw 3) experimental fabricado.
    """
    # 1. Payload explícito
    if qr_payload and qr_payload.strip():
        return qr_payload.strip()

    # 2. Buscar por id_saida (mesma sub_base)
    if id_saida and sub_base:
        saida = db.get(Saida, id_saida)
        if saida and saida.sub_base == sub_base and saida.qr_payload_raw:
            return saida.qr_payload_raw

    return None


@router.post("/gerar")
def gerar_etiqueta(
    payload: EtiquetaGerarPayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Gera etiqueta 100x150mm.
    Para ML: usa qr_payload_raw se disponível; senão cai para o código.
    """
    codigo = (payload.codigo or "").strip()
    if not codigo:
        raise HTTPException(400, "Código obrigatório.")

    sub_base = (getattr(current_user, "sub_base", None) or "").strip()
    if not sub_base:
        raise HTTPException(403, "Sub-base não definida.")

    role = int(getattr(current_user, "role", 0) or 0)
    saida_autorizada: Optional[Saida] = None

    if payload.id_saida is not None:
        saida_autorizada = db.get(Saida, payload.id_saida)
        if (
            saida_autorizada is None
            or (saida_autorizada.sub_base or "").strip() != sub_base
        ):
            raise HTTPException(404, "Pedido não encontrado.")
        if role == 4:
            motoboy_id = getattr(current_user, "motoboy_id", None)
            if motoboy_id is None or saida_autorizada.motoboy_id != motoboy_id:
                raise HTTPException(
                    status_code=403,
                    detail="Sem permissão para gerar etiqueta deste pedido.",
                )
        elif role not in (0, 1, 2, 3):
            raise HTTPException(403, "Sem permissão para gerar etiqueta.")
    elif role == 4:
        raise HTTPException(
            status_code=422,
            detail="Informe id_saida para gerar etiqueta no perfil motoboy.",
        )
    elif role not in (0, 1, 2, 3):
        raise HTTPException(403, "Sem permissão para gerar etiqueta.")

    qr_content = _resolve_qr_content(
        codigo=codigo,
        id_saida=payload.id_saida,
        servico=payload.servico,
        qr_payload=payload.qr_payload,
        sub_base=sub_base,
        db=db,
    )

    modo_final = "generic"
    if _is_ml_servico(payload.servico) or (_is_ml_codigo(codigo) and qr_content):
        modo_final = "ml"
    elif codigo.upper().startswith("BR") and len(codigo) >= 14:
        modo_final = "shopee"

    dados_extras: Optional[Dict[str, Any]] = None

    formato = (payload.formato or "pdf").strip().lower()
    if formato not in ("pdf", "png"):
        raise HTTPException(400, "Formato inválido. Use 'pdf' ou 'png'.")

    try:
        from etiqueta_pdf_service import gerar_etiqueta

        if formato == "png":
            content = gerar_etiqueta(
                modo="codigo_existente",
                codigo=codigo,
                formato="png",
                modo_final=modo_final,
                dados_extras=dados_extras,
                qr_content=qr_content,
            )
            media_type = "image/png"
            ext = "png"
        else:
            content = gerar_etiqueta(
                modo="codigo_existente",
                codigo=codigo,
                formato="pdf",
                modo_final=modo_final,
                dados_extras=dados_extras,
                qr_content=qr_content,
            )
            media_type = "application/pdf"
            ext = "pdf"
    except Exception as e:
        logger.exception("Erro ao gerar etiqueta: %s", e)
        raise HTTPException(500, "Falha ao gerar etiqueta.")

    id_part = str(payload.id_saida) if payload.id_saida else "0"
    cod_safe = re.sub(r'[^\w\-.]', '', (codigo or "")[:40]) or "cod"
    srv_safe = re.sub(r'[^\w\-.]', '', (modo_final or "generic")[:20]) or "generic"
    filename = f"etq-tracking-{id_part}-{cod_safe}-{srv_safe}.{ext}"
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
