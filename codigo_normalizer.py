"""
Normalização de códigos de leitura (QR/barras).
Replicação da lógica classifyCodigo do tracking-leitura.init.js para uso no backend.
Usado em: POST /mobile/scan e outros fluxos que recebem códigos brutos.
"""
from __future__ import annotations

import json
import re
from typing import Optional


def _to_ascii_digits(s: str) -> str:
    """Converte dígitos unicode (superscript, fullwidth) para ASCII."""
    if not s:
        return ""
    # Superscript digits ⁰¹²³⁴⁵⁶⁷⁸⁹
    sup_map = {"⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4",
               "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9"}
    for k, v in sup_map.items():
        s = s.replace(k, v)
    # Fullwidth digits ０-９ (U+FF10–U+FF19)
    for i in range(10):
        s = s.replace(chr(0xFF10 + i), str(i))
    return s


def _is_codigo_shopee(codigo: str) -> bool:
    """BR + 13 dígitos ou 12 dígitos + letra."""
    if not codigo or not isinstance(codigo, str):
        return False
    c = codigo.upper().strip()
    return bool(re.match(r"^BR(\d{13}|\d{12}[A-Z])$", c))


def normalize_codigo(raw_input: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Normaliza código bruto (QR/barras) para formato de busca em Saida.codigo.
    Retorna (codigo, servico, qr_payload_raw).
    - codigo: normalizado para lookup; None se inválido.
    - servico: Shopee | Mercado Livre | Avulso
    - qr_payload_raw: payload bruto para ML (etiqueta); None se não aplicável.
    """
    raw_input_str = (raw_input or "").strip()
    raw = _to_ascii_digits(raw_input_str).upper().strip()
    all_digits = re.sub(r"\D+", "", raw)

    # PRIORIDADE 0 — Mercado Livre JSON (id, sender_id, hash_code)
    if raw_input_str.strip().startswith("{") and raw_input_str.strip().endswith("}"):
        try:
            obj = json.loads(raw_input_str)
            if isinstance(obj.get("id"), str) and (obj.get("sender_id") is not None or obj.get("hash_code") is not None):
                codigo = str(obj["id"]).strip()
                return codigo, "Mercado Livre", raw_input_str
        except (json.JSONDecodeError, TypeError):
            pass

    # PRIORIDADE 1 — QRCode JSON com external_order_id (usa original para preservar keys)
    if raw_input_str.strip().startswith("{") and raw_input_str.strip().endswith("}"):
        try:
            obj = json.loads(raw_input_str)
            eoid = obj.get("external_order_id") or obj.get("EXTERNAL_ORDER_ID")
            if isinstance(eoid, str):
                codigo = eoid.upper().strip()
                servico = "Shopee" if _is_codigo_shopee(codigo) else "Avulso"
                return codigo, servico, None
        except (json.JSONDecodeError, TypeError):
            pass

    # PRIORIDADE 2 — external_order_id fora de JSON
    ext_match = re.search(r'external_order_id["\']?\s*[:=]\s*["\']?([\w-]+)', raw, re.I)
    if ext_match:
        codigo = ext_match.group(1).upper()
        servico = "Shopee" if _is_codigo_shopee(codigo) else "Avulso"
        return codigo, servico, None

    # PRIORIDADE 3 — MAGALU (external_grouper_code)
    magalu_match = re.search(r'external_grouper_code\^Ç\^(\d{10,})\^', raw, re.I)
    if magalu_match:
        return magalu_match.group(1), "Avulso", None

    # PRIORIDADE 4 — LMxxxx
    if re.match(r"^LM[\w\d-]+$", raw, re.I):
        return raw, "Avulso", None

    # NF-e (44 dígitos) — inválido
    if re.match(r"^\d{44}$", all_digits):
        return None, None, None

    # Shopee
    sh_match = re.search(r"(?:^|[^A-Z0-9])(BR(?:\d{13}|\d{12}[A-Z]))(?=$|[^A-Z0-9])", raw, re.I)
    if sh_match:
        return sh_match.group(1).upper(), "Shopee", None

    # Mercado Livre (45–49 → 11 dígitos)
    ml_run = re.search(r"4[5-9]\d{9,}", all_digits)
    if ml_run:
        codigo = ml_run.group(0)[:11]
        return codigo, "Mercado Livre", raw_input_str

    # AVULSO — CEP (8 dígitos)
    if re.match(r"^\d{8}$", all_digits):
        return all_digits, "Avulso", None

    # AVULSO — EVAS (7 dígitos)
    if re.match(r"^\d{7}$", all_digits):
        return all_digits, "Avulso", None

    # AVULSO — padrões antigos
    if re.match(r"^CP\d{3,}", raw) or re.match(r"^TIME\d{6}$", raw, re.I):
        return raw, "Avulso", None

    # Avulso — telefone
    phone_match = re.search(r"0?(\d{2})[-\s]?(\d{4,5})[-\s]?(\d{4})", raw)
    if phone_match:
        cod = phone_match.group(1) + phone_match.group(2) + phone_match.group(3)
        return cod, "Avulso", None

    # Fallback: se parece código bruto simples (ex: só BR...), tenta usar como está
    if raw and len(raw) >= 5:
        return raw, "Avulso", None

    return None, None, None
