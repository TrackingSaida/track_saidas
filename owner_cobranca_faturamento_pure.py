"""PRD-011 Fatia 3: regras puras de data de faturamento e data de parceria do owner."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Optional

ESTADO_AGENDADO = "agendado"
ESTADO_HOJE = "faturar_hoje"
ESTADO_ATRASADO = "atrasado"
ESTADO_GERADO = "gerado"
ESTADO_SEM_PRAZO = "sem_prazo"

ESTADO_LABELS = {
    ESTADO_AGENDADO: "Agendado",
    ESTADO_HOJE: "Faturar hoje",
    ESTADO_ATRASADO: "Atrasado",
    ESTADO_GERADO: "Gerado",
    ESTADO_SEM_PRAZO: "Prazo não definido",
}


def normalizar_dias_faturamento(valor: Any) -> Optional[int]:
    """Inteiro >= 0. Vazio vira None (prazo não definido)."""
    if valor is None or (isinstance(valor, str) and not valor.strip()):
        return None
    if isinstance(valor, bool):
        raise ValueError("Dias para faturar deve ser um número inteiro maior ou igual a zero.")
    try:
        dias = int(str(valor).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("Dias para faturar deve ser um número inteiro maior ou igual a zero.") from exc
    if dias < 0:
        raise ValueError("Dias para faturar deve ser um número inteiro maior ou igual a zero.")
    return dias


def calcular_data_faturamento(periodo_fim: date, dias: Optional[int]) -> Optional[date]:
    """Fim do período + N dias de calendário. Sem prazo definido, não há data."""
    if dias is None:
        return None
    return periodo_fim + timedelta(days=int(dias))


def estado_faturamento(
    data_faturamento: Optional[date], hoje: date, *, gerada: bool
) -> str:
    """agendado | faturar_hoje | atrasado | gerado | sem_prazo.

    Atrasado: a data passou e a cobrança do período ainda não foi gerada.
    """
    if gerada:
        return ESTADO_GERADO
    if data_faturamento is None:
        return ESTADO_SEM_PRAZO
    if data_faturamento < hoje:
        return ESTADO_ATRASADO
    if data_faturamento == hoje:
        return ESTADO_HOJE
    return ESTADO_AGENDADO


def rotulo_faturar_em(data_faturamento: Optional[date], dias: Optional[int]) -> Optional[str]:
    """Ex.: `30/11 · 15 dias`."""
    if data_faturamento is None:
        return None
    base = data_faturamento.strftime("%d/%m")
    if dias is None:
        return base
    return f"{base} · {int(dias)} {'dia' if int(dias) == 1 else 'dias'}"


def decidir_data_parceria(atual: Optional[date], nova: Optional[date]) -> Optional[date]:
    """Data de parceria é informada uma vez. Retorna a data a gravar ou levanta ValueError.

    - sem valor novo: mantém o atual;
    - atual vazio: grava a nova;
    - atual preenchido e igual: sem mudança;
    - atual preenchido e diferente: recusa.
    """
    if nova is None:
        return atual
    if atual is None:
        return nova
    if atual == nova:
        return atual
    raise ValueError("A data de parceria já foi registrada e não pode ser alterada.")

