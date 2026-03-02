# cep_routes.py — Proxy para consulta CEP (ViaCEP) — evita CORS no frontend
from __future__ import annotations

import re
import requests
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/cep", tags=["CEP"])

VIACEP_URL = "https://viacep.com.br/ws/{cep}/json/"


@router.get("/{cep}")
def get_cep(cep: str):
    """Consulta CEP via ViaCEP (proxy server-side para evitar CORS no browser)."""
    digits = re.sub(r"\D", "", cep or "")
    if len(digits) != 8:
        raise HTTPException(status_code=400, detail="CEP deve ter 8 dígitos")
    try:
        r = requests.get(VIACEP_URL.format(cep=digits), timeout=10)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("erro"):
            raise HTTPException(status_code=404, detail="CEP não encontrado")
        return data
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail="Falha ao consultar CEP") from e
