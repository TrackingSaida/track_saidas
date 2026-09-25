"""Lógica pura: volume Coletados OU Entrada (denominador do Acompanhamento).

Alinhado ao PRD-003: mesmas fontes dos cards Indicadores (Coletas + Entradas),
sem usar estoque residual (ainda_na_base).
"""
from __future__ import annotations

from typing import Iterable, Set


def volume_coletados_ou_entrada(
    *,
    total_coletas: int,
    ids_entrada: Iterable[int],
    ids_coleta_pacotes: Iterable[int],
) -> int:
    """
    Volume do dia que entrou por coleta e/ou entrada na base (OU).

    - ids_entrada: pacotes com evento entrada_base no período (Indicadores.Entradas).
    - ids_coleta_pacotes: pacotes vinculados a Coleta do período (leituras por código).
    - total_coletas: soma Shopee+ML+Avulso das linhas Coleta (Indicadores.Coletas),
      pode incluir volume manual sem id_saida.

    União de pacotes + excesso agregado de coleta que não tem linha Saida.
    """
    entrada: Set[int] = {int(i) for i in ids_entrada}
    coleta: Set[int] = {int(i) for i in ids_coleta_pacotes}
    pacotes = entrada | coleta
    excesso_manual = max(0, int(total_coletas or 0) - len(coleta))
    return len(pacotes) + excesso_manual
