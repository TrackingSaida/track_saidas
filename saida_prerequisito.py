"""Pré-requisito de saída: Coleta OU Entrada (não AND)."""
from __future__ import annotations

from typing import Any, Dict, Optional

STATUS_NA_BASE = "NA_BASE"
STATUS_COLETADO = "coletado"


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
    """
    if ja_em_rota_ou_saida:
        return None

    coleta_on = bool(coleta_habilitada)
    entrada_on = bool(entrada_habilitada)
    if not coleta_on and not entrada_on:
        return None

    st = _norm(status_norm)
    ok_coleta = saida_existe and st == STATUS_COLETADO
    # Existir após coleta (já na base) também “veio da coleta”, mas para OR
    # NA_BASE já cobre o lado entrada; coletado cobre o lado coleta.
    ok_entrada = saida_existe and st == STATUS_NA_BASE

    if not saida_existe:
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

    # Pacote existe
    if coleta_on and entrada_on:
        if ok_coleta or ok_entrada:
            return None
        # Existe mas ainda não está apto (ex. status estranho) — exige um dos dois
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

    # só coleta: existência do registro basta (fluxo legado)
    return None
