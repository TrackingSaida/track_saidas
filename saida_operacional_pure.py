"""Tipos e regras operacionais puras (sem SQLAlchemy) — usados em listagem e testes."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

EVENTOS_ATRIBUICAO_VALIDOS = {
    "lido",
    "scan",
    "assumir",
    "assumido",
    "reatribuicao",
    "reatribuido",
    "nova_saida_mesmo_entregador",
    "lancar_avulso",
}

EVENTOS_REATRIBUICAO = {
    "assumir",
    "assumido",
    "reatribuicao",
    "reatribuido",
}

EVENTOS_INVALIDANTES = {
    "removido_sem_inicio",
    "desatribuido",
}

EVENTOS_UI_ULTIMA_ACAO = {
    "em_rota",
    "entregue",
    "ausente",
    "cancelado",
    "status_saiu_manual",
    "status_coletado_manual",
    "status_nao_coletado_manual",
}

ROTULOS_ACAO = {
    "lido": "Leu pedido",
    "scan": "Escaneou pedido",
    "assumir": "Reatribuiu pedido",
    "assumido": "Reatribuiu pedido",
    "reatribuicao": "Reatribuiu pedido",
    "reatribuido": "Reatribuiu pedido",
    "nova_saida_mesmo_entregador": "Nova saída confirmada com mesmo motoboy",
    "lancar_avulso": "Lançou avulso",
    "reatribuido_em_rota": "Reatribuído -> Iniciou rota",
    "removido_sem_inicio": "Removeu sem iniciar rota",
    "em_rota": "Iniciou rota",
    "entregue": "Finalizou entrega",
    "ausente": "Registrou ausência",
    "cancelado": "Registrou cancelamento",
    "desatribuido": "Desatribuiu pedido",
    "status_saiu_manual": "Atualizou status para Saiu para entrega",
    "status_coletado_manual": "Atualizou status para Coletado",
    "status_nao_coletado_manual": "Atualizou status para Não Coletado",
}


@dataclass
class SaidaOperacionalContext:
    id_saida: int
    ultimo_evento: Optional[str]
    ultimo_evento_ts: Optional[datetime]
    acao_label: Optional[str]
    executado_por: Optional[str]
    ultimo_ator_username: Optional[str]
    ultimo_ator_user_id: Optional[int]
    operacional_evento: Optional[str]
    operacional_ts: Optional[datetime]
    leitura_valida: bool
    removido_sem_inicio_ativo: bool


def _normalizar_evento(evento: Optional[str]) -> str:
    return (evento or "").strip().lower().replace(" ", "_")


def resolver_chave_acao(evento: Optional[str], houve_reatribuicao: bool = False) -> Optional[str]:
    if not evento:
        return None
    key = _normalizar_evento(evento)
    if key in EVENTOS_REATRIBUICAO:
        return "reatribuido"
    if key == "em_rota" and houve_reatribuicao:
        return "reatribuido_em_rota"
    return key


def rotulo_acao_evento(evento: Optional[str], houve_reatribuicao: bool = False) -> Optional[str]:
    key = resolver_chave_acao(evento, houve_reatribuicao=houve_reatribuicao)
    if not key:
        return None
    return ROTULOS_ACAO.get(key, (evento or "").replace("_", " ").strip().capitalize())


def deve_excluir_saida_operacional(ctx: Optional[SaidaOperacionalContext]) -> bool:
    return bool(ctx and ctx.removido_sem_inicio_ativo)


def timestamp_operacional_saida(
    ctx: Optional[SaidaOperacionalContext],
    saida_ts: Optional[datetime],
) -> Optional[datetime]:
    if ctx and ctx.removido_sem_inicio_ativo:
        return None
    if ctx:
        return ctx.operacional_ts or ctx.ultimo_evento_ts or saida_ts
    return saida_ts
