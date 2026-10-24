"""Ciclo de fechamento da base e resolução da chave PIX a receber.

Nenhum (nulo) não valida o intervalo. Semanal, quinzenal e mensal exigem
exatamente um ciclo civil fechado.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Optional

PERIODOS_FECHAMENTO = ("semanal", "quinzenal", "mensal")
MOTIVO_PERIODO_FECHAMENTO = "PERIODO_FECHAMENTO"

_ROTULO_CICLO = {
    "semanal": "semana (segunda a domingo)",
    "quinzenal": "quinzena (dia 1 ao 15, ou dia 16 ao fim do mês)",
    "mensal": "mês (dia 1 ao último dia)",
}


def normalizar_periodo_fechamento(valor: Optional[str]) -> Optional[str]:
    """Nulo, vazio e 'nenhum' viram ausência de ciclo. Valor fora da lista é inválido."""
    if valor is None:
        return None
    texto = str(valor).strip().lower()
    if texto in ("", "nenhum"):
        return None
    if texto not in PERIODOS_FECHAMENTO:
        raise ValueError("periodo_fechamento deve ser semanal, quinzenal, mensal ou vazio.")
    return texto


def resolver_chave_pix(chave_base: Optional[str], chave_global: Optional[str]) -> Optional[str]:
    """A chave da base prevalece. Sem as duas, não há PIX no relatório."""
    base = (chave_base or "").strip()
    if base:
        return base
    global_pix = (chave_global or "").strip()
    return global_pix or None


def ciclo_corresponde(periodo: Optional[str], inicio: date, fim: date) -> bool:
    """Nenhum corresponde a qualquer intervalo. Os demais exigem um ciclo exato."""
    codigo = normalizar_periodo_fechamento(periodo)
    if codigo is None:
        return True
    if fim < inicio:
        return False
    if codigo == "semanal":
        return inicio.weekday() == 0 and fim == inicio + timedelta(days=6)
    if inicio.year != fim.year or inicio.month != fim.month:
        return False
    ultimo = calendar.monthrange(inicio.year, inicio.month)[1]
    if codigo == "quinzenal":
        return (inicio.day == 1 and fim.day == 15) or (inicio.day == 16 and fim.day == ultimo)
    return inicio.day == 1 and fim.day == ultimo


def aviso_ciclo_fechamento(
    nome_base: str,
    periodo: Optional[str],
    inicio: date,
    fim: date,
) -> Optional[str]:
    """Texto para o operador quando o intervalo não é o ciclo da base. Nulo se pode gerar."""
    if ciclo_corresponde(periodo, inicio, fim):
        return None
    codigo = normalizar_periodo_fechamento(periodo)
    rotulo = _ROTULO_CICLO.get(codigo or "", "período configurado")
    nome = (nome_base or "Esta base").strip() or "Esta base"
    return (
        f"{nome} fecha por {rotulo}. "
        f"O período {inicio:%d/%m/%Y} a {fim:%d/%m/%Y} não corresponde. "
        "Ajuste o período para gerar o fechamento."
    )


def renome_fechamento_bloqueado(periodos_antigos: list, periodos_novos: list, mesma_chave: bool) -> bool:
    """Dois fechamentos no mesmo período não podem ficar com o mesmo nome."""
    vistos = set()
    for periodo in periodos_antigos:
        if periodo in vistos:
            return True
        vistos.add(periodo)
    if mesma_chave:
        return False
    return bool(vistos & set(periodos_novos))
