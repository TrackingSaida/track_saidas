"""
Rotas de Etiquetas
POST /etiquetas/gerar — gera PDF de etiqueta 100x150mm (QR Code + Code128).
Modos: generic (padrão), shopee, ml. Fallback automático para generic em falhas.
"""
from __future__ import annotations

import io
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from models import User

router = APIRouter(prefix="/etiquetas", tags=["Etiquetas"])
logger = logging.getLogger(__name__)

# TODO: Exportação ZPL
# TODO: Impressão direta Zebra
# TODO: Tabela etiquetas_logs
# TODO: Geração automática ao registrar saída


# ============================================================
# SCHEMAS
# ============================================================

class EtiquetaGerarPayload(BaseModel):
    codigo: str = Field(min_length=1, description="Código de rastreio/pedido")
    modo: str = Field(default="generic", description="generic | shopee | ml")


# ============================================================
# HELPERS — Resolução de dados externos (fallback em erro)
# ============================================================

def _normalizar_modo(modo: str) -> str:
    m = (modo or "").strip().lower()
    if m in ("shopee", "shp"):
        return "shopee"
    if m in ("ml", "mercado livre", "mercadolivre"):
        return "ml"
    return "generic"


def _buscar_dados_shopee(db: Session, codigo: str) -> Optional[Dict[str, Any]]:
    """Tenta obter dados do envio na Shopee. Retorna None em qualquer falha."""
    try:
        from shopee_token_service import (
            get_valid_shopee_access_token,
            get_latest_shopee_token,
            _get_shopee_config,
            _sign_api,
        )
        import requests
        import time

        token = get_latest_shopee_token(db)
        if not token:
            return None
        access_token = get_valid_shopee_access_token(db, shop_id=token.shop_id)
        host, partner_id, partner_key = _get_shopee_config()
        path = "/api/v2/order/get_order_list"
        timestamp = int(time.time())
        sign = _sign_api(partner_id, partner_key, path, timestamp, token.shop_id, access_token)
        url = f"{host}{path}"
        params = {
            "partner_id": partner_id,
            "timestamp": timestamp,
            "sign": sign,
            "shop_id": token.shop_id,
        }
        body = {"order_status": "READY_TO_SHIP", "page_size": 50}
        resp = requests.post(url, params=params, json=body)
        if resp.status_code != 200:
            return None
        data = resp.json()
        orders = data.get("response", {}).get("order_list", []) or []
        for o in orders:
            tracking = (o.get("tracking_no") or "").strip()
            if tracking and codigo.upper() in tracking.upper():
                addr = o.get("recipient_address", {}) or {}
                return {
                    "destinatario": addr.get("name") or "",
                    "cidade": addr.get("city") or "",
                    "cep": addr.get("zipcode") or "",
                }
        return None
    except Exception as e:
        logger.warning("Shopee etiqueta: %s", e)
        return None


def _buscar_dados_ml(db: Session, codigo: str) -> Optional[Dict[str, Any]]:
    """Tenta obter dados do shipment no ML. Retorna None em qualquer falha."""
    try:
        from ml_token_service import get_valid_ml_access_token
        import requests
        access_token = get_valid_ml_access_token(db)
        headers = {"Authorization": f"Bearer {access_token}"}
        url = "https://api.mercadolibre.com/shipments/search"
        resp = requests.get(url, headers=headers, params={"tracking_number": codigo})
        if resp.status_code != 200:
            return None
        data = resp.json()
        results = data.get("results") or []
        if not results:
            return None
        shipment = results[0]
        shipment_id = shipment.get("id")
        receiver = shipment.get("receiver_address") or {}
        if shipment_id:
            url2 = f"https://api.mercadolibre.com/marketplace/shipments/{shipment_id}"
            resp2 = requests.get(url2, headers={**headers, "x-format-new": "true"})
            if resp2.status_code == 200:
                d2 = resp2.json()
                dest = d2.get("destination") or {}
                receiver = dest.get("receiver_address") or receiver
        return {
            "destinatario": receiver.get("receiver_name") or receiver.get("name") or "",
            "cidade": receiver.get("city", {}).get("name") if isinstance(receiver.get("city"), dict) else (receiver.get("city") or ""),
            "cep": receiver.get("zip_code") or receiver.get("zipcode") or "",
        }
    except Exception as e:
        logger.warning("ML etiqueta: %s", e)
        return None


# ============================================================
# GERADOR DE PDF
# ============================================================

def _gerar_pdf_etiqueta(
    codigo: str,
    modo_final: str,
    dados_extras: Optional[Dict[str, Any]] = None,
) -> bytes:
    """Gera PDF 100x150mm com QR Code e Code128."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    import qrcode
    import barcode
    from barcode.writer import ImageWriter

    dados = dados_extras or {}
    largura = 100 * mm
    altura = 150 * mm

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(largura, altura))
    c.setPageSize((largura, altura))

    # Margens
    margin = 5 * mm
    x = margin
    y = altura - margin

    # --- TOPO: Logo / Título ---
    titulo = "Tracking Saídas"
    if modo_final == "shopee":
        titulo = "Shopee"
    elif modo_final == "ml":
        titulo = "Mercado Livre"
    c.setFont("Helvetica-Bold", 10)
    c.drawString(x, y, titulo)
    y -= 8 * mm

    # --- QR Code (aprox 25x25 mm) ---
    qr = qrcode.QRCode(version=1, box_size=4, border=1)
    qr.add_data(codigo)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white")
    qr_buf = io.BytesIO()
    qr_img.save(qr_buf, format="PNG")
    qr_buf.seek(0)
    from reportlab.lib.utils import ImageReader
    c.drawImage(ImageReader(qr_buf), x, y - 25 * mm, width=25 * mm, height=25 * mm)
    y -= 28 * mm

    c.setFont("Helvetica", 9)
    c.drawString(x, y, codigo)
    y -= 6 * mm

    # --- Code128 ---
    try:
        import tempfile
        import os
        code128 = barcode.get_barcode_class("code128")
        bc = code128(codigo, writer=ImageWriter())
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            bc.write(tmp_path)
            with open(tmp_path, "rb") as f:
                bc_bytes = f.read()
            c.drawImage(ImageReader(io.BytesIO(bc_bytes)), x, y - 12 * mm, width=90 * mm, height=12 * mm)
            y -= 15 * mm
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except Exception:
        c.setFont("Helvetica", 8)
        c.drawString(x, y, codigo)
        y -= 6 * mm

    # --- Destinatário (se disponível) ---
    dest = dados.get("destinatario") or ""
    cidade = dados.get("cidade") or ""
    cep = dados.get("cep") or ""
    if dest or cidade or cep:
        c.setFont("Helvetica", 8)
        if dest:
            c.drawString(x, y, str(dest)[:40])
            y -= 5 * mm
        if cidade or cep:
            c.drawString(x, y, f"{cidade} {cep}".strip()[:40])
            y -= 5 * mm

    # --- Rodapé ---
    y = margin + 8 * mm
    c.setFont("Helvetica", 7)
    c.drawString(x, y, "Etiqueta de Envio - Tracking Saídas")

    c.save()
    buf.seek(0)
    return buf.getvalue()


# ============================================================
# ROTA
# ============================================================

@router.post("/gerar")
def gerar_etiqueta(
    payload: EtiquetaGerarPayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Gera etiqueta PDF 100x150mm.
    Modo generic: sempre funciona.
    Modo shopee/ml: tenta enriquecer com dados da API; em falha usa generic.
    """
    codigo = (payload.codigo or "").strip()
    if not codigo:
        raise HTTPException(400, "Código obrigatório.")

    modo = _normalizar_modo(payload.modo)
    modo_final = modo
    dados_extras: Optional[Dict[str, Any]] = None

    if modo == "shopee":
        dados_extras = _buscar_dados_shopee(db, codigo)
        if dados_extras is None:
            modo_final = "generic"
            dados_extras = None

    elif modo == "ml":
        dados_extras = _buscar_dados_ml(db, codigo)
        if dados_extras is None:
            modo_final = "generic"
            dados_extras = None

    try:
        pdf_bytes = _gerar_pdf_etiqueta(codigo, modo_final, dados_extras)
    except Exception as e:
        logger.exception("Erro ao gerar PDF etiqueta: %s", e)
        raise HTTPException(500, "Falha ao gerar PDF.")

    filename = f"etiqueta_{codigo[:30]}_{modo_final}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
