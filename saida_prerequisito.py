"""Pré-requisito de saída: Coleta OU Entrada (não AND)."""
from __future__ import annotations

from typing import Any, Dict, Optional

STATUS_NA_BASE = "NA_BASE"
STATUS_COLETADO = "coletado"
STATUS_ETIQUETADO = "ETIQUETADO"


def _norm(status: Optional[str]) -> str:
    return (status or "").strip()


def avaliar_prerequisito_saida(
    *,
    coleta_habilitada: bool,
    entrada_habilitada: bool,
    saida_existe: bool,
    status_norm: Optional[str] = None,
    ja_em_rota_ou_saida: bool = False,
    permitir_registrar_nao_coletado: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Retorna None se a saída pode seguir, ou dict {code, message} para 422.

    Matriz:
    - coleta on, entrada off → exige pacote existente (coleta), salvo registrar_nao_coletado
    - coleta off, entrada on → exige Entrada (NA_BASE) ou já em rota/saída
    - ambas on → Coleta OU Entrada (coletado OU NA_BASE)
    - ambas off → sem pré-requisito deste tipo

    ETIQUETADO (envio próprio só com etiqueta) NÃO libera saída.
    """
    if ja_em_rota_ou_saida:
        return None

    coleta_on = bool(coleta_habilitada)
    entrada_on = bool(entrada_habilitada)
    if not coleta_on and not entrada_on:
        return None

    st = _norm(status_norm)
    # Etiqueta gerada ainda não passou por coleta/entrada operacional
    if saida_existe and st.upper() == STATUS_ETIQUETADO:
        saida_existe_operacional = False
    else:
        saida_existe_operacional = saida_existe

    ok_coleta = saida_existe_operacional and st == STATUS_COLETADO
    ok_entrada = saida_existe_operacional and st == STATUS_NA_BASE

    if not saida_existe_operacional:
        if permitir_registrar_nao_coletado and coleta_on and not entrada_on:
            return None
        if coleta_on and entrada_on:
            return {
                "code": "PRE_REQUISITO_SAIDA",
                "message": "Pacote precisa de Coleta ou Entrada antes da saída.",
            }
        if entrada_on:
            return {
                "code": "ENTRADA_OBRIGATORIA",
                "message": "Este pacote ainda não teve entrada na base.",
            }
        if permitir_registrar_nao_coletado:
            return None
        return {
            "code": "NAO_COLETADO",
            "message": "Código não coletado.",
        }

    # Pacote existe operacionalmente
    if coleta_on and entrada_on:
        if ok_coleta or ok_entrada:
            return None
        return {
            "code": "PRE_REQUISITO_SAIDA",
            "message": "Pacote precisa de Coleta ou Entrada antes da saída.",
        }

    if entrada_on and not coleta_on:
        if ok_entrada:
            return None
        return {
            "code": "ENTRADA_OBRIGATORIA",
            "message": "Este pacote ainda não teve entrada na base.",
        }

    # só coleta: existência do registro basta (fluxo legado) — ETIQUETADO já tratado acima
    return None
