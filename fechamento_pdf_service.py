"""
Gera PDF de fechamento de motoboy/entregador (reportlab) e grava no B2.

O layout espelha o relatório web (A4 paisagem): resumo, financeiro,
detalhamento diário, ajustes e pacotes grandes.
"""
from __future__ import annotations

import io
import logging
import re
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from etiqueta_identidade_service import (
    fit_logo_image,
    resolver_logo_owner,
    resolver_nome_exibicao,
    resolver_slogan,
)
from models import EntregadorFechamento, EntregadorFechamentoColetaItem, Owner, Saida
from saida_operacional_utils import filtrar_saidas_por_periodo_operacional
from upload_storage_utils import B2_BUCKET_NAME, b2_configured, get_s3_client_optional
from fechamento_resumo_utils import agregar_resumo_fechamento

logger = logging.getLogger(__name__)

COR_FAIXA = colors.Color(27 / 255, 27 / 255, 27 / 255)
COR_INK = colors.Color(27 / 255, 27 / 255, 27 / 255)
COR_MUTED = colors.Color(109 / 255, 109 / 255, 109 / 255)
COR_ZERO = colors.Color(154 / 255, 154 / 255, 154 / 255)
COR_LINE = colors.Color(233 / 255, 233 / 255, 233 / 255)
COR_HEADER = COR_FAIXA
_MESES = (
    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
)
STATUS_LABELS = {
    "PENDENTE": "PENDENTE",
    "GERADO": "GERADO",
    "REAJUSTADO": "REAJUSTADO",
    "PAGO": "PAGO",
    "FECHADO": "GERADO",
}


def _fmt_brl(v) -> str:
    try:
        n = Decimal(str(v or 0))
    except Exception:
        n = Decimal("0")
    s = f"{n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


def _fmt_signed(v) -> str:
    try:
        n = Decimal(str(v or 0))
    except Exception:
        n = Decimal("0")
    if n == 0:
        return _fmt_brl(0)
    sign = "+" if n > 0 else "-"
    return f"{sign}{_fmt_brl(abs(n))}"


def _fmt_date(d) -> str:
    if not d:
        return "—"
    try:
        if hasattr(d, "strftime"):
            return d.strftime("%d/%m/%Y")
        parts = str(d).split("-")
        if len(parts) == 3:
            return f"{parts[2]}/{parts[1]}/{parts[0]}"
        return str(d)
    except Exception:
        return str(d)


def _normalize_status(status: Optional[str]) -> str:
    key = (status or "PENDENTE").upper()
    return STATUS_LABELS.get(key, key)


def _status_amigavel(status: Optional[str]) -> str:
    key = _normalize_status(status)
    return {
        "PAGO": "Pago",
        "GERADO": "Gerado",
        "REAJUSTADO": "Reajustado",
        "PENDENTE": "Pendente",
    }.get(key, key[:1].upper() + key[1:].lower() if key else "Pendente")


def _fmt_pix(chave: Optional[str]) -> str:
    raw = (chave or "").strip()
    if not raw:
        return ""
    digits = "".join(c for c in raw if c.isdigit())
    if digits == raw and len(digits) == 11:
        return f"{digits[:2]} {digits[2:7]}-{digits[7:]}"
    if digits == raw and len(digits) == 10:
        return f"{digits[:2]} {digits[2:6]}-{digits[6:]}"
    return raw


def _fmt_periodo(ini, fim) -> str:
    if ini and fim and getattr(ini, "year", None) and ini.year == fim.year and ini.month == fim.month:
        return f"{ini.day} a {fim.day} de {_MESES[ini.month - 1]} de {ini.year}"
    return f"{_fmt_date(ini)} a {_fmt_date(fim)}"


def _curto(txt: Optional[str], n: int = 46) -> str:
    s = " ".join((txt or "").split())
    if not s:
        return ""
    if len(s) <= n:
        return s
    return s[: n - 3].rstrip() + "..."


def _identidade_sub_base(db: Optional[Session], sub_base: Optional[str]) -> Dict[str, Any]:
    sub = (sub_base or "").strip()
    owner = None
    if db is not None and sub:
        owner = db.scalar(select(Owner).where(Owner.sub_base == sub))
    if owner is None:
        return {"nome": sub or "ROTEVO", "slogan": "", "logo": None}
    return {
        "nome": resolver_nome_exibicao(owner),
        "slogan": resolver_slogan(owner),
        "logo": resolver_logo_owner(owner),
    }


def _logo_reader(logo_bytes: Optional[bytes]) -> Optional[tuple]:
    if not logo_bytes:
        return None
    fitted = fit_logo_image(logo_bytes, max_width=640, max_height=240)
    if fitted is None:
        return None
    buf = io.BytesIO()
    fitted.save(buf, format="PNG")
    buf.seek(0)
    reader = ImageReader(buf)
    reader._saida_buf = buf
    return reader, fitted.width, fitted.height


def build_fechamento_code(fech: EntregadorFechamento) -> str:
    periodo = fech.periodo_fim or fech.periodo_inicio
    key = periodo.strftime("%Y%m") if periodo else "000000"
    raw = (fech.username_entregador or "MOTOBOY").upper()
    raw = re.sub(r"[^A-Z0-9]+", "", raw)[:12] or "MOTOBOY"
    return f"FEC-{key}-{raw}-{int(fech.id_fechamento):06d}"


def _collect_itens_diarios(db: Session, fech: EntregadorFechamento) -> List[Dict[str, Any]]:
    """Monta itens diários no mesmo espírito do GET /entregadores/resumo."""
    from entregador_routes import (
        STATUS_VALOR_BASE_VALIDOS,
        _normalizar_servico,
        _resolve_executor_scope_ids,
        _regras_g_motoboy,
        _toggle_pacote_g_ativo,
        resolver_precos_entregador,
        resolver_precos_motoboy,
    )
    from fechamento_valor_pure import valor_g_adicional
    from pacote_g_categorias import categoria_da_saida

    sub_base = fech.sub_base
    data_inicio = fech.periodo_inicio
    data_fim = fech.periodo_fim
    entregador_id = getattr(fech, "id_entregador", None)
    motoboy_id = getattr(fech, "id_motoboy", None)

    stmt = select(Saida).where(
        Saida.sub_base == sub_base,
        Saida.codigo.isnot(None),
        func.lower(Saida.status).in_(STATUS_VALOR_BASE_VALIDOS),
        or_(
            Saida.entregador_id.isnot(None),
            Saida.entregador.isnot(None),
            Saida.motoboy_id.isnot(None),
        ),
        Saida.timestamp >= datetime.combine(data_inicio, datetime.min.time()),
        Saida.timestamp < datetime.combine(data_fim + timedelta(days=1), datetime.min.time()),
    )

    if motoboy_id is not None:
        # Mesmo recorte de _calcular_valor_base_motoboy_periodo (valor_base gravado).
        stmt = stmt.where(Saida.motoboy_id == motoboy_id)
    else:
        entregador_ids, motoboy_ids = _resolve_executor_scope_ids(
            db=db,
            sub_base_user=sub_base,
            entregador_id=entregador_id,
            motoboy_id=None,
        )
        conds = []
        if entregador_ids:
            conds.append(Saida.entregador_id.in_(sorted(entregador_ids)))
        if motoboy_ids:
            conds.append(Saida.motoboy_id.in_(sorted(motoboy_ids)))
        if not conds:
            return []
        stmt = stmt.where(or_(*conds))

    rows_raw = db.scalars(stmt).all()
    rows, op_ctx_map = filtrar_saidas_por_periodo_operacional(db, rows_raw, data_inicio, data_fim)
    regras_g, padrao_g = _regras_g_motoboy(db, sub_base)

    agrupado: Dict[str, Dict[str, Any]] = {}
    for saida in rows:
        ctx = op_ctx_map.get(saida.id_saida)
        op_ts = (ctx.operacional_ts if ctx and ctx.operacional_ts else None) or saida.timestamp
        dia = op_ts.date().isoformat()
        if dia not in agrupado:
            agrupado[dia] = {
                "data": dia,
                "qtde_shopee": 0,
                "qtde_flex": 0,
                "qtde_avulso": 0,
                "cancel_shopee": 0,
                "cancel_flex": 0,
                "cancel_avulso": 0,
                "total_feitos": 0,
                "total_cancelado": 0,
                "g_total": 0,
                "g_shopee": 0,
                "g_flex": 0,
                "g_avulso": 0,
                "g_feitos": {},
            }
        bucket = agrupado[dia]
        status_norm = (saida.status or "").strip().lower()
        is_cancelado = "cancel" in status_norm
        tipo = _normalizar_servico(saida.servico)
        if is_cancelado:
            bucket["total_cancelado"] += 1
            bucket[f"cancel_{tipo}"] += 1
        else:
            bucket["total_feitos"] += 1
            bucket[f"qtde_{tipo}"] += 1
        if getattr(saida, "is_grande", False):
            bucket["g_total"] += 1
            if not is_cancelado:
                chave_g = (categoria_da_saida(saida, padrao_g), tipo)
                bucket["g_feitos"][chave_g] = bucket["g_feitos"].get(chave_g, 0) + 1
            if tipo == "shopee":
                bucket["g_shopee"] += 1
            elif tipo == "flex":
                bucket["g_flex"] += 1
            else:
                bucket["g_avulso"] += 1

    if motoboy_id is not None:
        precos = resolver_precos_motoboy(db, sub_base, motoboy_id=motoboy_id)
    elif entregador_id is not None and int(entregador_id) > 0:
        precos = resolver_precos_entregador(db, entregador_id, sub_base)
    else:
        precos = resolver_precos_motoboy(db, sub_base)

    toggle_pacote_g = _toggle_pacote_g_ativo(db, sub_base)
    out: List[Dict[str, Any]] = []
    for dia in sorted(agrupado.keys()):
        item = agrupado[dia]
        valor_shopee = Decimal(item["qtde_shopee"]) * precos["shopee_valor"]
        valor_flex = Decimal(item["qtde_flex"]) * precos["ml_valor"]
        valor_avulso = Decimal(item["qtde_avulso"]) * precos["avulso_valor"]
        valor_g = valor_g_adicional(item["g_feitos"], precos, toggle_pacote_g, regras_g)
        valor_feitos = (valor_shopee + valor_flex + valor_avulso + valor_g).quantize(Decimal("0.01"))
        valor_cancel_shopee = (Decimal(item["cancel_shopee"]) * precos["shopee_valor"]).quantize(
            Decimal("0.01")
        )
        valor_cancel_flex = (Decimal(item["cancel_flex"]) * precos["ml_valor"]).quantize(Decimal("0.01"))
        valor_cancel_avulso = (Decimal(item["cancel_avulso"]) * precos["avulso_valor"]).quantize(
            Decimal("0.01")
        )
        valor_cancelados = (valor_cancel_shopee + valor_cancel_flex + valor_cancel_avulso).quantize(
            Decimal("0.01")
        )
        # Cancelados já estão fora de valor_feitos; total = feitos (sem multa).
        total_dia = valor_feitos
        out.append(
            {
                "data": dia,
                "flex": item["qtde_flex"],
                "shopee": item["qtde_shopee"],
                "avulso": item["qtde_avulso"],
                "cancel_shopee": item["cancel_shopee"],
                "cancel_flex": item["cancel_flex"],
                "cancel_avulso": item["cancel_avulso"],
                "g_total": item["g_total"],
                "g_shopee": item["g_shopee"],
                "g_flex": item["g_flex"],
                "g_avulso": item["g_avulso"],
                "total_feitos": item["total_feitos"],
                "total_cancelado": item["total_cancelado"],
                "valor_shopee": valor_shopee.quantize(Decimal("0.01")),
                "valor_flex": valor_flex.quantize(Decimal("0.01")),
                "valor_avulso": valor_avulso.quantize(Decimal("0.01")),
                "valor_cancel_shopee": valor_cancel_shopee,
                "valor_cancel_flex": valor_cancel_flex,
                "valor_cancel_avulso": valor_cancel_avulso,
                "valor_g": valor_g,
                "valor_feitos": valor_feitos,
                "valor_cancelados": valor_cancelados,
                "valor_total": total_dia,
            }
        )
    return out


def montar_resumo_e_datas(db: Session, fech: EntregadorFechamento) -> tuple[Dict[str, Any], List[str]]:
    itens = _collect_itens_diarios(db, fech)
    datas = sorted({str(r.get("data")) for r in itens if r.get("data")})
    resumo = agregar_resumo_fechamento(
        itens,
        valor_adicao=getattr(fech, "valor_adicao", 0),
        valor_subtracao=getattr(fech, "valor_subtracao", 0),
    )
    return resumo, datas


def montar_resumo_fechamento(db: Session, fech: EntregadorFechamento) -> Dict[str, Any]:
    resumo, _ = montar_resumo_e_datas(db, fech)
    return resumo


def _render_fechamento_pdf(**p) -> bytes:
    """Folha A4 paisagem: faixa da sub_base e cartão com o total a pagar."""
    buf = io.BytesIO()
    page = landscape(A4)
    c = canvas.Canvas(buf, pagesize=page)
    page_w, page_h = page
    margem = 12 * mm
    faixa_h = 20 * mm
    rodape_h = 12 * mm
    logo_info = _logo_reader(p.get("logo"))

    def faixa(continuacao: bool = False) -> float:
        if continuacao:
            c.showPage()
        c.setFillColor(COR_FAIXA)
        c.rect(0, page_h - faixa_h, page_w, faixa_h, fill=1, stroke=0)
        x = margem
        if logo_info:
            reader, iw, ih = logo_info
            max_w, max_h = 32 * mm, 11 * mm
            aspect = iw / float(ih or 1)
            if (max_w / max_h) > aspect:
                draw_h = max_h
                draw_w = max_h * aspect
            else:
                draw_w = max_w
                draw_h = max_w / aspect
            pad = 1.1 * mm
            chip_w = draw_w + pad * 2
            chip_h = draw_h + pad * 2
            chip_y = page_h - faixa_h / 2 - chip_h / 2
            c.setFillColor(colors.white)
            c.roundRect(x, chip_y, chip_w, chip_h, 1.3 * mm, fill=1, stroke=0)
            c.drawImage(reader, x + pad, chip_y + pad, draw_w, draw_h, mask="auto")
            x += chip_w + 3 * mm
        nome = (p.get("emitente") or "ROTEVO").strip()
        limite = page_w - margem - 72 * mm - x
        c.setFont("Helvetica-Bold", 9)
        while nome and c.stringWidth(nome, "Helvetica-Bold", 9) > limite and len(nome) > 12:
            nome = nome[:-4].rstrip() + "..."
        c.setFillColor(colors.white)
        c.drawString(x, page_h - 8 * mm, nome)
        slogan = (p.get("slogan") or "").strip()
        if slogan:
            c.setFillColor(colors.Color(0.86, 0.86, 0.86))
            c.setFont("Helvetica", 8)
            c.drawString(x, page_h - 12.6 * mm, _curto(slogan, 70))
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 11)
        c.drawRightString(page_w - margem, page_h - 7.4 * mm, "Fechamento de entregas")
        c.setFont("Helvetica", 8)
        c.drawRightString(page_w - margem, page_h - 11.4 * mm, str(p.get("codigo") or ""))
        chip = (p.get("status_label") or "").upper()
        c.setFont("Helvetica", 7)
        chip_w = c.stringWidth(chip, "Helvetica", 7) + 3.4 * mm
        chip_x = page_w - margem - chip_w
        chip_y = page_h - 16.6 * mm
        c.setStrokeColor(colors.white)
        c.setLineWidth(0.6)
        c.roundRect(chip_x, chip_y, chip_w, 3.5 * mm, 0.9 * mm, fill=0, stroke=1)
        c.setFillColor(colors.white)
        c.drawCentredString(chip_x + chip_w / 2, chip_y + 1.05 * mm, chip)
        return page_h - faixa_h - 6 * mm

    def rodape() -> None:
        c.setFillColor(COR_MUTED)
        c.setFont("Helvetica", 7.5)
        esquerda = f"{p.get('codigo') or ''} · {p.get('status_label') or ''} · Gerado em {p.get('gerado_em') or ''}"
        c.drawString(margem, 6 * mm, esquerda[:120])
        direita = f"Emitido por {p.get('emitente') or 'ROTEVO'} · via ROTEVO"
        c.drawRightString(page_w - margem, 6 * mm, direita[:88])

    y = faixa(False)

    def precisa(altura: float) -> bool:
        nonlocal y
        if y - altura >= rodape_h + 2 * mm:
            return False
        rodape()
        y = faixa(True)
        return True

    card_w = 58 * mm
    card_h = 18 * mm
    card_x = page_w - margem - card_w
    card_top = y
    c.setStrokeColor(colors.Color(0.90, 0.90, 0.90))
    c.setFillColor(colors.white)
    c.setLineWidth(0.7)
    c.roundRect(card_x, card_top - card_h, card_w, card_h, 4.2 * mm, fill=1, stroke=1)
    c.setFillColor(COR_MUTED)
    c.setFont("Helvetica", 7)
    c.drawRightString(card_x + card_w - 4 * mm, card_top - 5.2 * mm, "TOTAL A PAGAR")
    c.setFillColor(COR_INK)
    c.setFont("Helvetica-Bold", 13)
    c.drawRightString(card_x + card_w - 4 * mm, card_top - 12 * mm, _fmt_brl(p.get("valor_final")))

    c.setFillColor(COR_MUTED)
    c.setFont("Helvetica", 7)
    c.drawString(margem, y - 1 * mm, "ENTREGADOR")
    c.setFillColor(COR_INK)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(margem, y - 6.2 * mm, str(p.get("entregador") or "—")[:42])
    c.setFont("Helvetica", 8)
    if p.get("pix"):
        c.drawString(margem, y - 10.4 * mm, f"PIX {p['pix']}")
    c.drawString(margem, y - 14.4 * mm, str(p.get("periodo") or ""))
    y = card_top - card_h - 5 * mm

    kpis = [
        ("FEITOS", str(int(p.get("feitos") or 0))),
        ("CANCELADOS", str(int(p.get("cancelados") or 0))),
        ("PACOTES G", str(int(p.get("pacotes_g") or 0))),
        ("AJUSTES", _fmt_signed(p.get("ajustes") or 0)),
    ]
    col_w = (page_w - 2 * margem) / 4
    for i, (rotulo, valor) in enumerate(kpis):
        x = margem + i * col_w
        if i:
            c.setStrokeColor(COR_LINE)
            c.setLineWidth(0.4)
            c.line(x, y - 8 * mm, x, y + 1.2 * mm)
        recuo = 2.4 * mm if i else 0
        c.setFillColor(COR_MUTED)
        c.setFont("Helvetica", 7)
        c.drawString(x + recuo, y, rotulo)
        c.setFillColor(COR_ZERO if valor == "0" else COR_INK)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x + recuo, y - 5 * mm, valor)
    y -= 12 * mm

    cabecalhos = ["DATA", "FLEX", "SHOPEE", "AVULSO", "G", "FEITOS", "CANC.", "VALOR"]
    pesos = [1.55, 0.8, 0.95, 0.9, 0.6, 0.85, 0.8, 1.25]
    util = page_w - 2 * margem
    larguras = [util * peso / sum(pesos) for peso in pesos]

    def linha_cabecalho() -> None:
        nonlocal y
        precisa(8 * mm)
        c.setFillColor(COR_INK)
        c.setFont("Helvetica-Bold", 7)
        x = margem
        for rotulo, largura in zip(cabecalhos, larguras):
            if rotulo == "DATA":
                c.drawString(x, y, rotulo)
            else:
                c.drawRightString(x + largura - 1 * mm, y, rotulo)
            x += largura
        y -= 1.5 * mm
        c.setStrokeColor(COR_FAIXA)
        c.setLineWidth(1)
        c.line(margem, y, page_w - margem, y)
        y -= 4 * mm

    def linha_dados(celulas: List[str], negrito: bool = False) -> None:
        nonlocal y
        if precisa(6.5 * mm):
            linha_cabecalho()
        c.setFont("Helvetica-Bold" if negrito else "Helvetica", 8)
        x = margem
        for i, (texto, largura) in enumerate(zip(celulas, larguras)):
            bruto = str(texto)
            c.setFillColor(COR_ZERO if bruto == "0" else COR_INK)
            if i == 0:
                c.drawString(x, y, bruto)
            else:
                c.drawRightString(x + largura - 1 * mm, y, bruto)
            x += largura
        y -= 1.3 * mm
        c.setStrokeColor(COR_LINE)
        c.setLineWidth(0.3)
        c.line(margem, y, page_w - margem, y)
        y -= 3.3 * mm

    linhas = list(p.get("linhas") or [])
    linha_cabecalho()
    if not linhas:
        linha_dados(["—", "0", "0", "0", "0", "0", "0", _fmt_brl(0)])
    for item in linhas:
        linha_dados(
            [
                _fmt_date(item["data"]),
                str(item["flex"]),
                str(item["shopee"]),
                str(item["avulso"]),
                str(item["g_total"]),
                str(item["total_feitos"]),
                str(item["total_cancelado"]),
                _fmt_brl(item["valor_feitos"]),
            ]
        )
    linha_dados(
        [
            "Período",
            str(sum(int(item["flex"]) for item in linhas)),
            str(sum(int(item["shopee"]) for item in linhas)),
            str(sum(int(item["avulso"]) for item in linhas)),
            str(int(p.get("pacotes_g") or 0)),
            str(int(p.get("feitos") or 0)),
            str(int(p.get("cancelados") or 0)),
            _fmt_brl(p.get("valor_feitos")),
        ],
        negrito=True,
    )

    coletas = list(p.get("coletas") or [])
    if coletas:
        if precisa(16 * mm):
            pass
        c.setFillColor(COR_INK)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(margem, y, "Diárias de coleta")
        y -= 5 * mm
        for item in coletas:
            if precisa(6 * mm):
                c.setFillColor(COR_INK)
                c.setFont("Helvetica-Bold", 9)
                c.drawString(margem, y, "Diárias de coleta")
                y -= 5 * mm
            c.setFont("Helvetica", 8)
            c.setFillColor(COR_INK)
            c.drawString(margem, y, _fmt_date(item["data"]))
            bases = ", ".join(item.get("bases") or []) or "—"
            c.drawString(margem + 28 * mm, y, _curto(bases, 70))
            c.drawRightString(page_w - margem, y, _fmt_brl(item.get("valor")))
            y -= 4.4 * mm
        y -= 2 * mm

    if precisa(36 * mm):
        pass
    c.setFillColor(COR_INK)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(margem, y, "Composição")
    c.drawString(margem + util * 0.56, y, "Como o valor foi fechado")
    y -= 5 * mm
    comp_top = y
    comp = [("Entregas", _fmt_brl(p.get("valor_feitos")))]
    if Decimal(str(p.get("valor_g") or 0)) > 0:
        comp.append(("inclui adicional de pacotes grandes", _fmt_brl(p.get("valor_g"))))
    comp.append(
        (
            f"Diárias de coleta ({int(p.get('qtd_dias_coleta') or 0)} dias)",
            _fmt_brl(p.get("valor_coletas")),
        )
    )
    if Decimal(str(p.get("adicao") or 0)) > 0:
        motivo = _curto(p.get("motivo_adicao") or "", 36)
        comp.append((f"Acréscimo · {motivo}" if motivo else "Acréscimo", _fmt_signed(p.get("adicao"))))
    if Decimal(str(p.get("subtracao") or 0)) > 0:
        motivo = _curto(p.get("motivo_subtracao") or "", 36)
        comp.append(
            (
                f"Desconto · {motivo}" if motivo else "Desconto",
                _fmt_signed(-abs(Decimal(str(p.get("subtracao") or 0)))),
            )
        )
    comp.append(("Total a pagar", _fmt_brl(p.get("valor_final"))))
    col_valor = margem + util * 0.50
    for i, (rotulo, valor) in enumerate(comp):
        ultimo = i == len(comp) - 1
        if ultimo:
            c.setStrokeColor(COR_INK)
            c.setLineWidth(0.8)
            c.line(margem, y + 3.2 * mm, col_valor, y + 3.2 * mm)
            c.setFont("Helvetica-Bold", 8)
        else:
            c.setFont("Helvetica", 8)
        c.setFillColor(COR_INK)
        c.drawString(margem, y, rotulo[:52])
        c.drawRightString(col_valor, y, valor)
        y -= 4.6 * mm
    if int(p.get("pacotes_g") or 0) > 0:
        c.setFont("Helvetica", 8)
        c.drawString(
            margem,
            y,
            f"Pacotes grandes: Shopee {int(p.get('g_shopee') or 0)}, "
            f"Flex {int(p.get('g_flex') or 0)}, Avulso {int(p.get('g_avulso') or 0)}.",
        )
        y -= 4.4 * mm
    if int(p.get("cancelados") or 0) > 0:
        c.setFont("Helvetica", 8)
        c.setFillColor(COR_INK)
        qtd_canc = int(p.get("cancelados") or 0)
        palavra = "cancelado" if qtd_canc == 1 else "cancelados"
        c.drawString(
            margem,
            y,
            f"{qtd_canc} {palavra} ({_fmt_brl(p.get('valor_cancelados'))}) não entra no pagamento.",
        )
        y -= 4 * mm

    c.setFillColor(COR_INK)
    c.setFont("Helvetica", 8)
    nota = (
        "Entregas do período, mais diárias de coleta, mais acréscimos, menos descontos. "
        "Pacote grande segue a regra da sub_base."
    )
    texto_y = comp_top
    for pedaco in _quebrar(c, nota, "Helvetica", 8, util * 0.40):
        c.drawString(margem + util * 0.56, texto_y, pedaco)
        texto_y -= 3.6 * mm

    rodape()
    c.save()
    return buf.getvalue()


def _quebrar(c: canvas.Canvas, texto: str, fonte: str, tamanho: float, largura: float) -> List[str]:
    palavras = (texto or "").split()
    linhas: List[str] = []
    atual = ""
    for palavra in palavras:
        tentativa = (atual + " " + palavra).strip()
        if c.stringWidth(tentativa, fonte, tamanho) <= largura:
            atual = tentativa
        else:
            if atual:
                linhas.append(atual)
            atual = palavra
    if atual:
        linhas.append(atual)
    return linhas or [""]


def gerar_pdf_bytes(
    fech: EntregadorFechamento,
    chave_pix: Optional[str] = None,
    db: Optional[Session] = None,
) -> bytes:
    """Gera PDF rico (paisagem) quando db é informado; fallback simples sem db."""
    if db is not None:
        try:
            return _gerar_pdf_rico(db, fech, chave_pix=chave_pix)
        except Exception:
            logger.exception(
                "fechamento_pdf_rico_failed id=%s — usando layout simples",
                getattr(fech, "id_fechamento", None),
            )
    return _gerar_pdf_simples(fech, chave_pix=chave_pix)


def _gerar_pdf_simples(fech: EntregadorFechamento, chave_pix: Optional[str] = None) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    y = height - 20 * mm
    left = 18 * mm

    def line(txt: str, size: int = 11, gap: float = 6 * mm, bold: bool = False):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawString(left, y, txt)
        y -= gap

    codigo = build_fechamento_code(fech)
    line("Fechamento de entregas", 16, 8 * mm, bold=True)
    line(f"Código: {codigo}", 12, 6 * mm, bold=True)
    line(f"Status: {_normalize_status(fech.status)}")
    line(f"Executor: {fech.username_entregador or '—'}")
    if chave_pix:
        line(f"PIX: {chave_pix}")
    line(f"Período: {_fmt_date(fech.periodo_inicio)} a {_fmt_date(fech.periodo_fim)}")
    line(f"Gerado em: {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    y -= 4 * mm
    c.setStrokeColor(COR_FAIXA)
    c.setLineWidth(1)
    c.line(left, y, width - left, y)
    y -= 8 * mm

    line("Resumo financeiro", 13, 7 * mm, bold=True)
    line(f"Entregas: {_fmt_brl(getattr(fech, 'valor_entregas', fech.valor_base))}")
    line(
        f"Diárias de coleta ({int(getattr(fech, 'qtd_dias_coleta', 0) or 0)} dias): "
        f"{_fmt_brl(getattr(fech, 'valor_coletas', 0))}"
    )
    line(f"Adições: {_fmt_brl(fech.valor_adicao)}")
    if fech.motivo_adicao:
        line(f"  Motivo adição: {fech.motivo_adicao}", 9, 5 * mm)
    line(f"Subtrações: {_fmt_brl(fech.valor_subtracao)}")
    if fech.motivo_subtracao:
        line(f"  Motivo subtração: {fech.motivo_subtracao}", 9, 5 * mm)
    y -= 2 * mm
    line(f"Total a pagar: {_fmt_brl(fech.valor_final)}", 14, 8 * mm, bold=True)

    c.setFont("Helvetica", 8)
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.drawString(left, 12 * mm, "Documento gerado automaticamente pelo ROTEVO.")
    c.showPage()
    c.save()
    return buf.getvalue()


def _gerar_pdf_rico(
    db: Session,
    fech: EntregadorFechamento,
    *,
    chave_pix: Optional[str] = None,
) -> bytes:
    itens = _collect_itens_diarios(db, fech)
    codigo = build_fechamento_code(fech)
    ent_nome = fech.username_entregador or "Motoboy"

    sum_shopee = sum(int(r["shopee"]) for r in itens)
    sum_flex = sum(int(r["flex"]) for r in itens)
    sum_avulso = sum(int(r["avulso"]) for r in itens)
    total_feitos = sum_shopee + sum_flex + sum_avulso
    total_cancelados = sum(int(r["total_cancelado"]) for r in itens)
    valor_feitos = sum((r["valor_feitos"] for r in itens), Decimal("0"))
    valor_cancelados = sum((r["valor_cancelados"] for r in itens), Decimal("0"))
    total_ajustes = Decimal(str(fech.valor_adicao or 0)) - Decimal(str(fech.valor_subtracao or 0))
    total_g = sum(int(r["g_total"]) for r in itens)
    total_g_shopee = sum(int(r["g_shopee"]) for r in itens)
    total_g_flex = sum(int(r["g_flex"]) for r in itens)
    total_g_avulso = sum(int(r["g_avulso"]) for r in itens)
    total_valor_g = sum((r.get("valor_g") or Decimal("0") for r in itens), Decimal("0"))
    coletas_fechamento = db.scalars(
        select(EntregadorFechamentoColetaItem)
        .where(EntregadorFechamentoColetaItem.id_fechamento == fech.id_fechamento)
        .order_by(EntregadorFechamentoColetaItem.data)
    ).all()
    ident = _identidade_sub_base(db, fech.sub_base)
    status_label = _status_amigavel(fech.status)

    return _render_fechamento_pdf(
        codigo=codigo,
        status_label=status_label,
        entregador=ent_nome,
        pix=_fmt_pix(chave_pix),
        periodo=_fmt_periodo(fech.periodo_inicio, fech.periodo_fim),
        gerado_em=datetime.now().strftime("%d/%m/%Y %H:%M"),
        emitente=ident["nome"],
        slogan=ident["slogan"],
        logo=ident["logo"],
        feitos=total_feitos,
        cancelados=total_cancelados,
        pacotes_g=total_g,
        ajustes=total_ajustes,
        valor_final=Decimal(str(fech.valor_final or 0)),
        valor_feitos=valor_feitos,
        valor_cancelados=valor_cancelados,
        valor_coletas=Decimal(str(getattr(fech, "valor_coletas", 0) or 0)),
        qtd_dias_coleta=int(getattr(fech, "qtd_dias_coleta", 0) or 0),
        valor_g=total_valor_g,
        adicao=Decimal(str(fech.valor_adicao or 0)),
        motivo_adicao=fech.motivo_adicao or "",
        subtracao=Decimal(str(fech.valor_subtracao or 0)),
        motivo_subtracao=fech.motivo_subtracao or "",
        linhas=itens,
        coletas=[
            {"data": item.data, "bases": list(item.bases or []), "valor": item.valor_diaria}
            for item in coletas_fechamento
        ],
        g_shopee=total_g_shopee,
        g_flex=total_g_flex,
        g_avulso=total_g_avulso,
    )



def upload_fechamento_pdf(
    db: Session,
    fech: EntregadorFechamento,
    *,
    chave_pix: Optional[str] = None,
) -> Optional[str]:
    """Gera PDF, sobe ao B2 e atualiza colunas do fechamento. Retorna object_key ou None.

    Usa prefixo `saida/fechamento/...` porque a application key do B2 em produção
    costuma estar restrita ao prefixo `saida/` (fotos). Prefixo `fechamento/` gera AccessDenied.
    """
    pdf_bytes = gerar_pdf_bytes(fech, chave_pix=chave_pix, db=db)
    codigo = build_fechamento_code(fech)
    # Preferir saida/ (entitlement tipico do B2); manter legado fechamento/ como fallback de leitura
    object_key = (
        f"saida/fechamento/{fech.sub_base}/{fech.id_fechamento}/fechamento_{codigo}.pdf"
    )

    client = get_s3_client_optional()
    uploaded = False
    if client and b2_configured():
        try:
            client.put_object(
                Bucket=B2_BUCKET_NAME,
                Key=object_key,
                Body=pdf_bytes,
                ContentType="application/pdf",
            )
            uploaded = True
        except Exception as e:
            err_name = type(e).__name__
            logger.exception(
                "fechamento_pdf_upload_failed id=%s bucket=%s key=%s err=%s — "
                "verifique B2_BUCKET_NAME e permissao write no prefixo saida/",
                fech.id_fechamento,
                B2_BUCKET_NAME,
                object_key,
                err_name,
            )
    else:
        logger.warning("b2_not_configured_fechamento_pdf id=%s", fech.id_fechamento)

    # Só grava key se o upload realmente funcionou (download regenera sob demanda)
    if uploaded:
        fech.pdf_object_key = object_key
        fech.pdf_gerado_em = datetime.utcnow()
        db.flush()
        return object_key

    fech.pdf_gerado_em = datetime.utcnow()
    db.flush()
    return None


def get_fechamento_pdf_bytes(
    db: Session,
    fech: EntregadorFechamento,
    *,
    chave_pix: Optional[str] = None,
) -> bytes:
    """Sempre regenera o PDF rico para garantir paridade web/mobile."""
    return gerar_pdf_bytes(fech, chave_pix=chave_pix, db=db)
