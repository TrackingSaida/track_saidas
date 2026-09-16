"""Geração modular de etiquetas PDF/PNG (código existente + envio próprio)."""
from __future__ import annotations

import io
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from etiqueta_identidade_service import (
    fit_logo_image,
    resolver_logo_etiqueta,
    resolver_nome_exibicao,
    resolver_slogan,
)
from models import Owner

logger = logging.getLogger(__name__)


def _gerar_pdf_etiqueta_codigo(
    codigo: str,
    modo_final: str,
    dados_extras: Optional[Dict[str, Any]] = None,
    qr_content: Optional[str] = None,
) -> bytes:
    """Layout legado (POST /etiquetas/gerar) — QR dominante, visual preservado."""
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    import qrcode

    dados = dados_extras or {}
    largura_pag = 100 * mm
    altura_pag = 150 * mm
    margin = 8 * mm
    area_util_w = largura_pag - 2 * margin
    qr_size = 60 * mm

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(largura_pag, altura_pag))
    c.setPageSize((largura_pag, altura_pag))

    def center_x(elem_width: float) -> float:
        return (largura_pag - elem_width) / 2

    y = altura_pag - margin
    titulo = "ROTEVO"
    if modo_final == "shopee":
        titulo = "SHOPEE ENTREGA"
    elif modo_final == "ml":
        titulo = "MERCADO ENVIOS"

    c.setFont("Helvetica", 8)
    tw = c.stringWidth(titulo, "Helvetica", 8)
    c.drawString(center_x(tw), y, titulo)
    y -= 6 * mm

    qr_x = center_x(qr_size)
    qr_y = y - qr_size
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=12,
        border=1,
    )
    qr.add_data(qr_content if qr_content else codigo)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white")
    qr_buf = io.BytesIO()
    qr_img.save(qr_buf, format="PNG")
    qr_buf.seek(0)
    c.drawImage(ImageReader(qr_buf), qr_x, qr_y, width=qr_size, height=qr_size)
    y = qr_y - 4 * mm

    c.setFont("Helvetica-Bold", 14)
    tw = c.stringWidth(codigo, "Helvetica-Bold", 14)
    if tw > area_util_w:
        c.setFont("Helvetica-Bold", 10)
        tw = c.stringWidth(codigo, "Helvetica-Bold", 10)
    c.drawString(center_x(tw), y, codigo)
    y -= 8 * mm

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
        for linha in linhas[:3]:
            if linha:
                c.drawString(margin, y, linha)
                y -= 4 * mm

    rodape = "ROTEVO"
    c.setFont("Helvetica", 6)
    rw = c.stringWidth(rodape, "Helvetica", 6)
    c.drawString(center_x(rw), margin, rodape)
    c.save()
    buf.seek(0)
    return buf.getvalue()


def _gerar_png_etiqueta_codigo(
    codigo: str,
    modo_final: str,
    dados_extras: Optional[Dict[str, Any]] = None,
    qr_content: Optional[str] = None,
) -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    import qrcode

    largura, altura = 1181, 1772
    margem = 94
    qr_size = 708
    img = Image.new("RGB", (largura, altura), "white")
    draw = ImageDraw.Draw(img)

    def _font(size: int, bold: bool = False):
        try:
            if bold:
                return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size=size)
            return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size=size)
        except Exception:
            return ImageFont.load_default()

    def _center_x(text: str, font) -> int:
        left, _, right, _ = draw.textbbox((0, 0), text, font=font)
        return int((largura - (right - left)) / 2)

    titulo = "ROTEVO"
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
    img.paste(qr_img, (int((largura - qr_size) / 2), y))
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

    font_rodape = _font(20)
    draw.text((_center_x("ROTEVO", font_rodape), altura - margem), "ROTEVO", fill="black", font=font_rodape)
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    out.seek(0)
    return out.getvalue()


def _fmt_cep(cep: Optional[str]) -> str:
    digits = "".join(c for c in (cep or "") if c.isdigit())
    if len(digits) == 8:
        return f"{digits[:5]}-{digits[5:]}"
    return (cep or "").strip() or "—"


def _fmt_tel(tel: Optional[str]) -> str:
    digits = "".join(c for c in (tel or "") if c.isdigit())
    if len(digits) == 11:
        return f"({digits[:2]}) {digits[2:7]}-{digits[7:]}"
    if len(digits) == 10:
        return f"({digits[:2]}) {digits[2:6]}-{digits[6:]}"
    return (tel or "").strip() or "—"


def _clip(text: str, max_len: int) -> str:
    t = (text or "").strip()
    if len(t) <= max_len:
        return t
    return t[: max(0, max_len - 1)] + "…"


def _endereco_linha(
    *,
    rua: str,
    numero: str,
    complemento: Optional[str],
    bairro: str,
    cidade: str,
    uf: str,
) -> str:
    parts = [f"{rua}, {numero}".strip(", ")]
    if complemento:
        parts[0] = f"{parts[0]} — {complemento}"
    if bairro:
        parts.append(bairro)
    cidade_uf = f"{cidade} / {uf}".strip(" /")
    if cidade_uf:
        parts.append(cidade_uf)
    return ", ".join(p for p in parts if p)


def gerar_pdf_envio_proprio(
    *,
    owner: Optional[Owner],
    codigo: str,
    remetente: Dict[str, Any],
    destinatario: Dict[str, Any],
    peso_kg: Optional[float] = None,
    dimensoes: Optional[str] = None,
    created_at: Optional[datetime] = None,
    nome_exibicao_override: Optional[str] = None,
    slogan_override: Optional[str] = None,
    logo_key_hint: Optional[str] = None,
) -> bytes:
    """
    Layout mockup 100x150mm: header Owner, código+QR, remetente, destinatário, stats, rodapé ROTEVO.
    """
    from reportlab.lib.colors import Color, black, white, HexColor
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    import qrcode

    # Se snapshot guarda key usada, preferir owner atual (resolver_logo já faz fallback)
    _ = logo_key_hint

    largura = 100 * mm
    altura = 150 * mm
    margin = 4.5 * mm
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(largura, altura))

    gray = HexColor("#6B7280")
    dark = HexColor("#111827")
    line = HexColor("#D1D5DB")
    badge_bg = HexColor("#374151")
    footer_bg = HexColor("#1F2937")

    nome = (nome_exibicao_override or resolver_nome_exibicao(owner)).strip()
    slogan = (slogan_override if slogan_override is not None else resolver_slogan(owner)).strip()
    logo_bytes, _origem = resolver_logo_etiqueta(owner)

    # Borda externa
    c.setStrokeColor(line)
    c.setLineWidth(1)
    c.roundRect(2 * mm, 2 * mm, largura - 4 * mm, altura - 4 * mm, 3 * mm, stroke=1, fill=0)

    y = altura - margin - 2 * mm

    # ---- Header ----
    header_h = 18 * mm
    logo_box_w, logo_box_h = 14 * mm, 14 * mm
    if logo_bytes:
        fitted = fit_logo_image(logo_bytes, max_width=280, max_height=280)
        if fitted is not None:
            logo_buf = io.BytesIO()
            fitted.save(logo_buf, format="PNG")
            logo_buf.seek(0)
            aspect = fitted.width / max(1, fitted.height)
            draw_h = logo_box_h
            draw_w = min(logo_box_w, draw_h * aspect)
            c.drawImage(
                ImageReader(logo_buf),
                margin,
                y - draw_h,
                width=draw_w,
                height=draw_h,
                mask="auto",
                preserveAspectRatio=True,
            )
            text_x = margin + draw_w + 2.5 * mm
        else:
            text_x = margin
    else:
        text_x = margin

    c.setFillColor(dark)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(text_x, y - 6 * mm, _clip(nome.upper(), 28))
    if slogan:
        c.setFont("Helvetica", 6)
        c.setFillColor(gray)
        # slogan à direita do header
        sw = c.stringWidth(_clip(slogan, 36), "Helvetica", 6)
        c.drawRightString(largura - margin, y - 6 * mm, _clip(slogan, 36))
        # linha vertical sutil
        c.setStrokeColor(line)
        c.line(largura - margin - sw - 3 * mm, y - 2 * mm, largura - margin - sw - 3 * mm, y - 10 * mm)

    y -= header_h
    c.setStrokeColor(line)
    c.line(margin, y, largura - margin, y)
    y -= 4 * mm

    # ---- Código + QR ----
    qr_size = 28 * mm
    c.setFillColor(gray)
    c.setFont("Helvetica", 7)
    c.drawString(margin, y, "CÓDIGO DO PEDIDO")
    y -= 5 * mm
    c.setFillColor(dark)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(margin, y, _clip(codigo, 22))
    y_codigo = y
    y -= 5 * mm
    c.setFillColor(gray)
    c.setFont("Helvetica", 6)
    c.drawString(margin, y, "ESCANEIE O QR CODE PARA ACOMPANHAR")

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=1,
    )
    qr.add_data(codigo)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white")
    qr_buf = io.BytesIO()
    qr_img.save(qr_buf, format="PNG")
    qr_buf.seek(0)
    qr_x = largura - margin - qr_size
    qr_y = y_codigo - qr_size + 4 * mm
    c.drawImage(ImageReader(qr_buf), qr_x, qr_y, width=qr_size, height=qr_size)

    y = min(y, qr_y) - 3 * mm
    c.setStrokeColor(line)
    c.line(margin, y, largura - margin, y)
    y -= 4 * mm

    def _draw_party_block(title: str, person: Dict[str, Any], y_top: float) -> float:
        c.setFillColor(badge_bg)
        c.roundRect(margin + 8 * mm, y_top - 4 * mm, 28 * mm, 5 * mm, 2 * mm, stroke=0, fill=1)
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 7)
        c.drawCentredString(margin + 8 * mm + 14 * mm, y_top - 2.6 * mm, title)
        yy = y_top - 8 * mm
        c.setFillColor(dark)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(margin, yy, _clip(str(person.get("nome") or "").upper(), 42))
        yy -= 4 * mm
        c.setFont("Helvetica", 7)
        c.setFillColor(dark)
        endereco = _endereco_linha(
            rua=str(person.get("rua") or ""),
            numero=str(person.get("numero") or ""),
            complemento=person.get("complemento"),
            bairro=str(person.get("bairro") or ""),
            cidade=str(person.get("cidade") or ""),
            uf=str(person.get("uf") or ""),
        )
        # wrap simples
        max_w = largura - 2 * margin
        words = endereco.split()
        line_cur = ""
        for w in words:
            trial = f"{line_cur} {w}".strip()
            if c.stringWidth(trial, "Helvetica", 7) > max_w and line_cur:
                c.drawString(margin, yy, line_cur)
                yy -= 3.2 * mm
                line_cur = w
            else:
                line_cur = trial
        if line_cur:
            c.drawString(margin, yy, _clip(line_cur, 70))
            yy -= 3.2 * mm
        c.setFillColor(gray)
        c.drawString(margin, yy, f"CEP {_fmt_cep(person.get('cep'))}")
        yy -= 3.2 * mm
        if person.get("telefone"):
            c.drawString(margin, yy, _fmt_tel(person.get("telefone")))
            yy -= 3.2 * mm
        return yy

    y = _draw_party_block("REMETENTE", remetente, y)
    y -= 2 * mm
    c.setStrokeColor(line)
    c.line(margin, y, largura - margin, y)
    y -= 4 * mm
    y = _draw_party_block("DESTINATÁRIO", destinatario, y)
    y -= 2 * mm
    c.setStrokeColor(line)
    c.line(margin, y, largura - margin, y)
    y -= 3 * mm

    # ---- Stats ----
    col_w = (largura - 2 * margin) / 3
    peso_txt = f"{peso_kg:g} kg".replace(".", ",") if peso_kg is not None else "—"
    dim_txt = (dimensoes or "").strip() or "—"
    data_txt = (created_at or datetime.utcnow()).strftime("%d/%m/%Y")
    stats = [("PESO", peso_txt), ("DIMENSÕES", dim_txt), ("DATA DE ENVIO", data_txt)]
    for i, (label, value) in enumerate(stats):
        x = margin + i * col_w
        c.setFillColor(gray)
        c.setFont("Helvetica", 6)
        c.drawString(x + 1 * mm, y, label)
        c.setFillColor(dark)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(x + 1 * mm, y - 4 * mm, _clip(value, 16))
        if i < 2:
            c.setStrokeColor(line)
            c.line(x + col_w, y + 2 * mm, x + col_w, y - 6 * mm)
    y -= 10 * mm

    # ---- Footer ----
    footer_h = 12 * mm
    c.setFillColor(footer_bg)
    c.rect(2 * mm, 2 * mm, largura - 4 * mm, footer_h, stroke=0, fill=1)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 8)
    c.drawCentredString(largura / 2, 2 * mm + footer_h - 5 * mm, "OBRIGADO PELA SUA CONFIANÇA!")
    c.setFont("Helvetica", 6)
    c.drawCentredString(largura / 2, 2 * mm + 2.5 * mm, "www.rotevo.com.br")

    c.save()
    buf.seek(0)
    return buf.getvalue()


def gerar_etiqueta(
    *,
    modo: str = "codigo_existente",
    codigo: str,
    formato: str = "pdf",
    modo_final: str = "generic",
    dados_extras: Optional[Dict[str, Any]] = None,
    qr_content: Optional[str] = None,
    owner: Optional[Owner] = None,
    remetente: Optional[Dict[str, Any]] = None,
    destinatario: Optional[Dict[str, Any]] = None,
    peso_kg: Optional[float] = None,
    dimensoes: Optional[str] = None,
    created_at: Optional[datetime] = None,
    nome_exibicao_override: Optional[str] = None,
    slogan_override: Optional[str] = None,
    logo_key_hint: Optional[str] = None,
) -> bytes:
    formato = (formato or "pdf").strip().lower()
    if modo == "envio_proprio":
        if formato != "pdf":
            # MVP: envio próprio só PDF
            formato = "pdf"
        return gerar_pdf_envio_proprio(
            owner=owner,
            codigo=codigo,
            remetente=remetente or {},
            destinatario=destinatario or {},
            peso_kg=peso_kg,
            dimensoes=dimensoes,
            created_at=created_at,
            nome_exibicao_override=nome_exibicao_override,
            slogan_override=slogan_override,
            logo_key_hint=logo_key_hint,
        )
    if formato == "png":
        return _gerar_png_etiqueta_codigo(codigo, modo_final, dados_extras, qr_content)
    return _gerar_pdf_etiqueta_codigo(codigo, modo_final, dados_extras, qr_content)
