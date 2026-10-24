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
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from models import User, Saida, SaidaHistorico

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

AVISO_QR_ML_INCOMPLETO = "qr_ml_incompleto"


def _saida_ml_por_codigo(db: Session, sub_base: str, codigo: str) -> Optional[Saida]:
    """Saída ML da sub_base pelo código digitado (tela Etiquetas não envia id_saida)."""
    if not _is_ml_codigo(codigo):
        return None
    return db.scalar(
        select(Saida)
        .where(
            Saida.sub_base == sub_base,
            or_(Saida.codigo == codigo, Saida.ml_shipment_id == int(codigo)),
        )
        .order_by(Saida.id_saida.desc())
        .limit(1)
    )


def _sincronizado_ml(db: Session, saida: Saida) -> bool:
    return bool(saida.ml_shipment_id) and db.scalar(
        select(SaidaHistorico.id)
        .where(SaidaHistorico.id_saida == saida.id_saida, SaidaHistorico.evento == "ml_int_sync")
        .limit(1)
    ) is not None


def _completar_qr_ml_sincronizado(db: Session, saida: Saida) -> bool:
    """
    Saída importada do ML sem o JSON completo do QR: busca o QR na etiqueta oficial do seller
    e grava em qr_payload_raw (uma vez). Retorna True se a saída ficou com o QR completo.
    """
    from ml_int_service import obter_qr_etiqueta_ml

    qr = obter_qr_etiqueta_ml(db, saida)
    if not qr:
        return False
    saida.qr_payload_raw = qr
    db.commit()
    return True


def _resolve_qr_content(
    qr_payload: Optional[str],
    saida: Optional[Saida],
    db: Session,
) -> tuple[Optional[str], Optional[str]]:
    """
    Resolve o conteúdo do QR para etiqueta ML e um aviso opcional.
    Ordem: 1) qr_payload explícito 2) qr_payload_raw da saída, completado pela etiqueta
    oficial do ML quando a saída foi importada do ML e ainda não tem o JSON completo.
    """
    if qr_payload and qr_payload.strip():
        return qr_payload.strip(), None
    if saida is None:
        return None, None
    from qr_payload_utils import is_ml_qr_completo

    aviso = None
    if (
        not is_ml_qr_completo(saida.qr_payload_raw)
        and _sincronizado_ml(db, saida)
        and not _completar_qr_ml_sincronizado(db, saida)
    ):
        aviso = AVISO_QR_ML_INCOMPLETO
    return (saida.qr_payload_raw or None), aviso


def _pdf_oficial_ml(db: Session, saida: Optional[Saida], formato: str) -> Optional[bytes]:
    """PDF oficial do Mercado Livre, ou None para cair no gerador próprio."""
    if formato != "pdf" or saida is None or not getattr(saida, "ml_shipment_id", None):
        return None
    from ml_int_service import obter_pdf_etiqueta_ml

    try:
        return obter_pdf_etiqueta_ml(db, saida)
    except Exception as e:
        logger.info("PDF oficial ML indisponível para saída %s: %s", getattr(saida, "id_saida", None), e)
        return None


def _gravar_qr_ml_se_faltar(db: Session, saida: Saida) -> None:
    """Completa qr_payload_raw depois do PDF oficial. Falha aqui não troca o arquivo entregue."""
    from qr_payload_utils import is_ml_qr_completo

    if is_ml_qr_completo(getattr(saida, "qr_payload_raw", None)):
        return
    try:
        _completar_qr_ml_sincronizado(db, saida)
    except Exception as e:
        logger.info("QR oficial não gravado na saída %s: %s", getattr(saida, "id_saida", None), e)


@router.post("/gerar")
def gerar_etiqueta(
    payload: EtiquetaGerarPayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Gera etiqueta 100x150mm.
    Pedido ML com conta da sub_base devolve o PDF oficial; senão usa o gerador próprio.
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

    saida_ref = saida_autorizada or _saida_ml_por_codigo(db, sub_base, codigo)

    formato = (payload.formato or "pdf").strip().lower()
    if formato not in ("pdf", "png"):
        raise HTTPException(400, "Formato inválido. Use 'pdf' ou 'png'.")

    pdf_oficial = _pdf_oficial_ml(db, saida_ref, formato)
    if pdf_oficial and saida_ref is not None:
        _gravar_qr_ml_se_faltar(db, saida_ref)
        id_part = str(payload.id_saida) if payload.id_saida else "0"
        cod_safe = re.sub(r'[^\w\-.]', '', (codigo or "")[:40]) or "cod"
        filename = f"etq-tracking-{id_part}-{cod_safe}-ml.pdf"
        return Response(
            content=pdf_oficial,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    qr_content, aviso = _resolve_qr_content(payload.qr_payload, saida_ref, db)

    modo_final = "generic"
    if _is_ml_servico(payload.servico) or (_is_ml_codigo(codigo) and qr_content):
        modo_final = "ml"
    elif codigo.upper().startswith("BR") and len(codigo) >= 14:
        modo_final = "shopee"

    dados_extras: Optional[Dict[str, Any]] = None

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
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if aviso:
        headers["X-Etiqueta-Aviso"] = aviso
    return Response(
        content=content,
        media_type=media_type,
        headers=headers,
    )
