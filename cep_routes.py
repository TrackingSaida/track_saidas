# cep_routes.py — Proxy para consulta CEP (ViaCEP + fallback BrasilAPI) — evita CORS no frontend
from __future__ import annotations

import re
import requests
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/cep", tags=["CEP"])

VIACEP_URL = "https://viacep.com.br/ws/{cep}/json/"
BRASILAPI_URL = "https://brasilapi.com.br/api/cep/v1/{cep}"
# User-Agent evita bloqueio em alguns ambientes (ex.: Render / CDNs)
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TrackSaidas/1.0; +https://tracking-saidas.com.br)"}


def _fetch_viacep(cep_digits: str) -> dict | None:
    """Consulta ViaCEP. Retorna dict no formato ViaCEP ou None em falha."""
    try:
        r = requests.get(
            VIACEP_URL.format(cep=cep_digits),
            headers=HEADERS,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and not data.get("erro"):
            return data
    except requests.RequestException:
        pass
    return None


def _fetch_brasilapi(cep_digits: str) -> dict | None:
    """Consulta BrasilAPI e retorna dict no formato ViaCEP (logradouro, bairro, localidade, uf, cep) ou None."""
    try:
        r = requests.get(
            BRASILAPI_URL.format(cep=cep_digits),
            headers=HEADERS,
            timeout=15,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, dict):
            return None
        # Mapear para formato ViaCEP esperado pelo frontend
        cep_val = (data.get("cep") or "").replace("-", "").replace(".", "").strip()
        if len(cep_val) != 8:
            cep_val = cep_digits
        return {
            "cep": cep_val,
            "logradouro": data.get("street") or "",
            "bairro": data.get("neighborhood") or "",
            "localidade": data.get("city") or "",
            "uf": data.get("state") or "",
        }
    except requests.RequestException:
        pass
    except (ValueError, KeyError):
        pass
    return None


@router.get("/{cep}")
def get_cep(cep: str):
    """Consulta CEP via ViaCEP com fallback para BrasilAPI (formato ViaCEP na resposta)."""
    digits = re.sub(r"\D", "", cep or "")
    if len(digits) != 8:
        raise HTTPException(status_code=400, detail="CEP deve ter 8 dígitos")

    data = _fetch_viacep(digits)
    if data is not None:
        return data

    data = _fetch_brasilapi(digits)
    if data is not None:
        return data

    raise HTTPException(status_code=404, detail="CEP não encontrado")
