"""
Normalização de códigos de leitura (QR/barras).
Replicação da lógica classifyCodigo do tracking-leitura.init.js para uso no backend.
Usado em: POST /mobile/scan e outros fluxos que recebem códigos brutos.
"""
from __future__ import annotations

import json
import re
from typing import Optional

# DDDs válidos no Brasil (sem 23, 25, 26, 29, 36, 39, 40, 50, 52, 70, 90)
_DDD_VALIDOS = frozenset(
    {
        11, 12, 13, 14, 15, 16, 17, 18, 19,
        21, 22, 24, 27, 28,
        31, 32, 33, 34, 35, 37, 38,
        41, 42, 43, 44, 45, 46, 47, 48, 49,
        51, 53, 54, 55,
        61, 62, 63, 64, 65, 66, 67, 68, 69,
        71, 73, 74, 75, 77, 79,
        81, 82, 83, 84, 85, 86, 87, 88, 89,
        91, 92, 93, 94, 95, 96, 97, 98, 99,
    }
)

_AVULSO_CODIGO_RE = re.compile(r"^AVULSO(-[A-Z0-9-]+)?$")


def _to_ascii_digits(s: str) -> str:
    """Converte dígitos unicode (superscript, fullwidth) para ASCII."""
    if not s:
        return ""
    sup_map = {
        "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4",
        "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9",
    }
    for k, v in sup_map.items():
        s = s.replace(k, v)
    for i in range(10):
        s = s.replace(chr(0xFF10 + i), str(i))
    return s


def _is_codigo_shopee(codigo: str) -> bool:
    """BR + 13 dígitos ou 12 dígitos + letra."""
    if not codigo or not isinstance(codigo, str):
        return False
    c = codigo.upper().strip()
    return bool(re.match(r"^BR(\d{13}|\d{12}[A-Z])$", c))


def _normalize_shopee_codigo(raw: str, all_digits: str) -> Optional[str]:
    """Extrai ou normaliza código Shopee (BR + 13 dígitos ou 12 + letra)."""
    text = _to_ascii_digits(str(raw or "")).upper().strip()
    if _is_codigo_shopee(text):
        return text

    sh_match = re.search(r"(?:^|[^A-Z0-9])(BR(?:\d{13}|\d{12}[A-Z]))(?=$|[^A-Z0-9])", text, re.I)
    if sh_match:
        return sh_match.group(1).upper()

    digits = re.sub(r"\D+", "", all_digits or text)
    if len(digits) in (12, 13) and digits.isdigit():
        candidate = f"BR{digits}"
        if _is_codigo_shopee(candidate):
            return candidate

    if len(text) == 13 and text.endswith(tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ")):
        candidate = f"BR{text[:-1]}{text[-1]}"
        if _is_codigo_shopee(candidate):
            return candidate

    return None


def _is_codigo_avulso_gerado(raw: str) -> bool:
    """Código avulso gerado pelo sistema (ex.: AVULSO-9JULHO-000019 ou AVULSO-000019)."""
    return bool(_AVULSO_CODIGO_RE.match(_to_ascii_digits(str(raw or "")).upper().strip()))


def _extract_ml_codigo(value: str) -> Optional[str]:
    """Extrai código Mercado Livre (45-49) normalizado para 11 dígitos."""
    if not value:
        return None
    digits = re.sub(r"\D+", "", _to_ascii_digits(str(value)))
    ml_run = re.search(r"4[5-9]\d{9,}", digits)
    if not ml_run:
        return None
    return ml_run.group(0)[:11]


def _is_telefone_brasil(raw: str, all_digits: Optional[str] = None) -> Optional[str]:
    """
    Valida telefone BR (DDD + número). Retorna só dígitos (10 ou 11) ou None.
    Não aceita 12+ dígitos (prioridade Shopee).
    """
    digits = re.sub(r"\D+", "", all_digits if all_digits is not None else _to_ascii_digits(str(raw or "")))
    if not digits:
        return None
    if len(digits) in (12, 13) and digits.startswith("55"):
        digits = digits[2:]
    if len(digits) not in (10, 11):
        return None

    ddd = int(digits[:2])
    if ddd not in _DDD_VALIDOS:
        return None

    if len(digits) == 11:
        if digits[2] != "9":
            return None
    elif len(digits) == 10:
        if digits[2] == "9":
            return None

    return digits


def canonicalize_servico(servico: Optional[str]) -> str:
    """Normaliza rótulo de serviço para Shopee | Mercado Livre | Avulso."""
    s = (servico or "").strip().lower()
    if "shopee" in s:
        return "Shopee"
    if "mercado" in s or "flex" in s or re.search(r"\bml\b", s):
        return "Mercado Livre"
    return "Avulso"


def is_qr_like_scan_payload(raw_input: str) -> bool:
    """Sinaliza se o payload parece uma leitura de QR válida para o fluxo mobile (câmera)."""
    raw_input_str = (raw_input or "").strip()
    if not raw_input_str:
        return False
    raw = _to_ascii_digits(raw_input_str).upper().strip()
    all_digits = re.sub(r"\D+", "", raw)

    if raw_input_str.startswith("{") and raw_input_str.endswith("}"):
        try:
            obj = json.loads(raw_input_str)
            if isinstance(obj, dict):
                if obj.get("external_order_id") or obj.get("EXTERNAL_ORDER_ID"):
                    return True
                if obj.get("id") is not None and (
                    obj.get("sender_id") is not None
                    or obj.get("SENDER_ID") is not None
                    or obj.get("hash_code") is not None
                    or obj.get("HASH_CODE") is not None
                ):
                    return True
        except (json.JSONDecodeError, TypeError):
            return False

    if re.search(r'external_order_id["\']?\s*[:=]\s*["\']?([\w-]+)', raw, re.I):
        return True
    if _normalize_shopee_codigo(raw, all_digits):
        return True
    if _extract_ml_codigo(all_digits):
        return True
    if _is_codigo_avulso_gerado(raw_input_str):
        return True
    return False


def _classify_codigo_text(codigo_raw: str, strict_qr: bool = False) -> tuple[Optional[str], Optional[str]]:
    """Classifica um código textual em serviço canônico."""
    raw = _to_ascii_digits(str(codigo_raw or "")).upper().strip()
    all_digits = re.sub(r"\D+", "", raw)

    shopee = _normalize_shopee_codigo(raw, all_digits)
    if shopee:
        return shopee, "Shopee"

    ml_codigo = _extract_ml_codigo(raw)
    if ml_codigo:
        return ml_codigo, "Mercado Livre"

    if _is_codigo_avulso_gerado(raw):
        return raw.strip().upper(), "Avulso"

    if strict_qr:
        return None, None

    phone = _is_telefone_brasil(raw, all_digits)
    if phone:
        return phone, "Avulso"

    return None, None


def normalize_codigo(raw_input: str, strict_qr: bool = False) -> tuple[Optional[str], Optional[str], Optional[str]]:
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
    json_obj = None
    is_json_payload = raw_input_str.startswith("{") and raw_input_str.endswith("}")

    if is_json_payload:
        try:
            json_obj = json.loads(raw_input_str)
        except (json.JSONDecodeError, TypeError):
            json_obj = None

    # PRIORIDADE 0 — Mercado Livre JSON (id e/ou marcadores sender/hash)
    if isinstance(json_obj, dict):
        raw_id = json_obj.get("id")
        if raw_id is not None:
            id_str = str(raw_id).strip()
            ml_id = _extract_ml_codigo(id_str)
            has_ml_markers = any(
                json_obj.get(k) is not None
                for k in ("sender_id", "SENDER_ID", "hash_code", "HASH_CODE")
            )
            if id_str and (has_ml_markers or ml_id):
                return (ml_id or id_str), "Mercado Livre", raw_input_str

    # PRIORIDADE 1 — QRCode JSON com external_order_id
    if isinstance(json_obj, dict):
        eoid = json_obj.get("external_order_id") or json_obj.get("EXTERNAL_ORDER_ID")
        if isinstance(eoid, str):
            codigo, servico = _classify_codigo_text(eoid, strict_qr=strict_qr)
            if codigo is None:
                return None, None, None
            return codigo, servico, None

    # PRIORIDADE 2 — external_order_id fora de JSON
    ext_match = re.search(r'external_order_id["\']?\s*[:=]\s*["\']?([\w-]+)', raw, re.I)
    if ext_match:
        codigo, servico = _classify_codigo_text(ext_match.group(1), strict_qr=strict_qr)
        if codigo is None:
            return None, None, None
        return codigo, servico, None

    # NF-e (44 dígitos) — inválido
    if re.match(r"^\d{44}$", all_digits):
        return None, None, None

    # Shopee (BR embutido ou 12–13 dígitos)
    shopee = _normalize_shopee_codigo(raw, all_digits)
    if shopee:
        return shopee, "Shopee", None

    # Mercado Livre (45–49 → 11 dígitos)
    ml_codigo = _extract_ml_codigo(all_digits)
    if ml_codigo:
        return ml_codigo, "Mercado Livre", raw_input_str

    # AVULSO-* gerado pelo sistema
    if _is_codigo_avulso_gerado(raw):
        return raw.strip().upper(), "Avulso", None

    if strict_qr:
        return None, None, None

    # Telefone BR válido (único avulso aceito no scan manual)
    phone = _is_telefone_brasil(raw, all_digits)
    if phone:
        return phone, "Avulso", None

    return None, None, None
