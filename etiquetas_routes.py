"""
Rotas de Etiquetas
POST /etiquetas/gerar — gera etiqueta 100x150mm (QR Code) em PDF/PNG.
Modo genérico (padrão). TODO: futuro - Shopee/ML com autenticação nas APIs.
"""
from __future__ import annotations

import io
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
# GERADOR DE PDF — Layout profissional para impressão térmica
# ============================================================

def _gerar_pdf_etiqueta(
    codigo: str,
    modo_final: str,
    dados_extras: Optional[Dict[str, Any]] = None,
    qr_content: Optional[str] = None,
) -> bytes:
    """
    Gera PDF 100x150mm com layout limpo e profissional.
    Foco total no QR Code. Sem código de barras.
    """
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    import qrcode

    dados = dados_extras or {}

    # Dimensões da página
    largura_pag = 100 * mm
    altura_pag = 150 * mm
    margin = 8 * mm
    area_util_w = largura_pag - 2 * margin
    area_util_h = altura_pag - 2 * margin

    # Tamanho do QR Code (ideal 60x60mm)
    qr_size = 60 * mm

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(largura_pag, altura_pag))
    c.setPageSize((largura_pag, altura_pag))

    # Helper: centralizar elemento horizontalmente
    def center_x(elem_width: float) -> float:
        return (largura_pag - elem_width) / 2

    y = altura_pag - margin

    # ─────────────────────────────────────────────────────────
    # TOPO — Nome do sistema/marketplace (centralizado)
    # ─────────────────────────────────────────────────────────
    titulo = "TRACKING SAÍDAS"
    if modo_final == "shopee":
        titulo = "SHOPEE ENTREGA"
    elif modo_final == "ml":
        titulo = "MERCADO ENVIOS"

    c.setFont("Helvetica", 8)
    tw = c.stringWidth(titulo, "Helvetica", 8)
    c.drawString(center_x(tw), y, titulo)
    y -= 6 * mm

    # ─────────────────────────────────────────────────────────
    # CORPO PRINCIPAL — QR Code (elemento dominante)
    # ─────────────────────────────────────────────────────────
    qr_x = center_x(qr_size)
    qr_y = y - qr_size

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,  # Alta correção
        box_size=12,   # Alta definição para impressão térmica
        border=1,      # Quiet zone mínima
    )
    qr.add_data(qr_content if qr_content else codigo)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white")
    qr_buf = io.BytesIO()
    qr_img.save(qr_buf, format="PNG")
    qr_buf.seek(0)
    c.drawImage(ImageReader(qr_buf), qr_x, qr_y, width=qr_size, height=qr_size)

    y = qr_y - 4 * mm

    # ─────────────────────────────────────────────────────────
    # ABAIXO DO QR — Código de rastreio em texto grande e bold
    # ─────────────────────────────────────────────────────────
    c.setFont("Helvetica-Bold", 14)
    tw = c.stringWidth(codigo, "Helvetica-Bold", 14)
    # Quebra se ultrapassar área útil
    if tw > area_util_w:
        c.setFont("Helvetica-Bold", 10)
        tw = c.stringWidth(codigo, "Helvetica-Bold", 10)
    c.drawString(center_x(tw), y, codigo)
    y -= 8 * mm

    # ─────────────────────────────────────────────────────────
    # BLOCO DE INFORMAÇÕES — Somente se houver dados
    # ─────────────────────────────────────────────────────────
    dest = dados.get("destinatario") or ""
    cidade = dados.get("cidade") or ""
    cep = dados.get("cep") or ""

    if dest or cidade or cep:
        c.setFont("Helvetica", 7)
        linhas = []
        if dest:
            linhas.append(str(dest)[:40])
        if cidade or cep:
            linhas.append(f"{cidade} {cep}".strip()[:40])
        for i, linha in enumerate(linhas[:3]):  # Máx. 3 linhas
            if linha:
                c.drawString(margin, y, linha)
                y -= 4 * mm

    # ─────────────────────────────────────────────────────────
    # RODAPÉ — Discreto e centralizado
    # ─────────────────────────────────────────────────────────
    rodape = "Tracking Saídas"
    c.setFont("Helvetica", 6)
    rw = c.stringWidth(rodape, "Helvetica", 6)
    c.drawString(center_x(rw), margin, rodape)

    c.save()
    buf.seek(0)
    return buf.getvalue()


def _gerar_png_etiqueta(
    codigo: str,
    modo_final: str,
    dados_extras: Optional[Dict[str, Any]] = None,
    qr_content: Optional[str] = None,
) -> bytes:
    """
    Gera PNG com o mesmo conteúdo da etiqueta (foco em visualização e compartilhamento mobile).
    """
    from PIL import Image, ImageDraw, ImageFont
    import qrcode

    # 100x150mm em ~300DPI
    largura, altura = 1181, 1772
    margem = 94
    qr_size = 708

    img = Image.new("RGB", (largura, altura), "white")
    draw = ImageDraw.Draw(img)

    def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        try:
            if bold:
                return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size=size)
            return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size=size)
        except Exception:
            return ImageFont.load_default()

    def _center_x(text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> int:
        left, _, right, _ = draw.textbbox((0, 0), text, font=font)
        w = right - left
        return int((largura - w) / 2)

    titulo = "TRACKING SAÍDAS"
    if modo_final == "shopee":
        titulo = "SHOPEE ENTREGA"
    elif modo_final == "ml":
        titulo = "MERCADO ENVIOS"

    y = margem
    font_titulo = _font(28)
    draw.text((_center_x(titulo, font_titulo), y), titulo, fill="black", font=font_titulo)
    y += 72

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=12,
        border=1,
    )
    qr.add_data(qr_content if qr_content else codigo)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    qr_img = qr_img.resize((qr_size, qr_size))
    qr_x = int((largura - qr_size) / 2)
    img.paste(qr_img, (qr_x, y))
    y += qr_size + 45

    font_codigo = _font(58, bold=True)
    draw.text((_center_x(codigo, font_codigo), y), codigo, fill="black", font=font_codigo)
    y += 84

    dados = dados_extras or {}
    dest = str(dados.get("destinatario") or "").strip()
    cidade = str(dados.get("cidade") or "").strip()
    cep = str(dados.get("cep") or "").strip()
    if dest or cidade or cep:
        font_info = _font(24)
        if dest:
            draw.text((margem, y), dest[:40], fill="black", font=font_info)
            y += 36
        if cidade or cep:
            draw.text((margem, y), f"{cidade} {cep}".strip()[:40], fill="black", font=font_info)

    rodape = "Tracking Saídas"
    font_rodape = _font(20)
    draw.text((_center_x(rodape, font_rodape), altura - margem), rodape, fill="black", font=font_rodape)

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    out.seek(0)
    return out.getvalue()


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

    sub_base = getattr(current_user, "sub_base", None)
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
        if formato == "png":
            content = _gerar_png_etiqueta(
                codigo=codigo,
                modo_final=modo_final,
                dados_extras=dados_extras,
                qr_content=qr_content,
            )
            media_type = "image/png"
            ext = "png"
        else:
            content = _gerar_pdf_etiqueta(
                codigo=codigo,
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
