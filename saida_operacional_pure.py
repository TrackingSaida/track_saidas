"""Tipos e regras operacionais puras (sem SQLAlchemy) — usados em listagem e testes."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional, Sequence, Tuple

EVENTOS_ATRIBUICAO_VALIDOS = {
    "lido",
    "scan",
    "assumir",
    "assumido",
    "reatribuicao",
    "reatribuido",
    "nova_saida_mesmo_entregador",
    "lancar_avulso",
    "leitura_saida",
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
    "devolucao",
    "encerrado_sistema",
    "status_saiu_manual",
    "status_coletado_manual",
    "status_nao_coletado_manual",
    # Precisam entrar no contexto da listagem para "Última ação" acompanhar o histórico.
    "entrada_base",
    "saida_conferida",
    "saida_reconferida",
    "criado_coleta",
    "coleta",
    "cancelado_plataforma",
    "leitura_saida",
    "sessao_leitura_finalizada",
    "saida_confirmada",
    "saida_removida_antes_confirmacao",
    "rota_iniciada",
    "rota_preparada",
}

ROTULOS_ACAO = {
    "lido": "Leu pedido",
    "scan": "Escaneou pedido",
    "assumir": "Reatribuiu pedido",
    "assumido": "Reatribuiu pedido",
    "reatribuicao": "Reatribuiu pedido",
    "reatribuido": "Reatribuiu pedido",
    "lancar_avulso": "Lançou avulso",
    "reatribuido_em_rota": "Reatribuído -> Iniciou rota",
    "removido_sem_inicio": "Removeu sem iniciar rota",
    "em_rota": "Iniciou rota",
    "rota_iniciada": "Rota iniciada",
    "rota_preparada": "Rota preparada",
    "entregue": "Finalizou entrega",
    "ausente": "Registrou ausência",
    "ausente_lote": "Registrou ausência em lote",
    "entregue_lote": "Finalizou entrega em lote",
    "nova_tentativa": "Liberou nova tentativa",
    "liberacao_ausencias": "Nova tentativa liberada pela operação",
    "cancelado": "Registrou cancelamento",
    "devolucao": "Devolvido à base",
    "encerrado_sistema": "Encerrado pelo sistema",
    "rota_cancelada": "Rota cancelada",
    "desatribuido": "Desatribuiu pedido",
    "status_saiu_manual": "Atualizou status para Saiu para entrega",
    "status_coletado_manual": "Atualizou status para Coletado",
    "status_nao_coletado_manual": "Atualizou status para Não Coletado",
    "entrada_base": "Entrada na base",
    "saida_conferida": "Saída conferida",
    "saida_reconferida": "Saída reconferida",
    "criado_coleta": "Pacote coletado",
    "coleta": "Pacote coletado",
    "ml_int_sync": "Importado do Mercado Livre",
    "shopee_int_sync": "Importado da Shopee",
    "cancelado_plataforma": "Cancelado na plataforma: verificar devolução",
    "base_transferida": "Transferiu base da coleta",
    "etiqueta_gerada": "Emitiu etiqueta",
    "etiqueta_cancelada": "Cancelou etiqueta",
    "etiqueta_expirada": "Etiqueta expirada",
    "leitura_saida": "Leu pacote para saída",
    "sessao_leitura_finalizada": "Finalizou leitura de saída",
    "saida_confirmada": "Confirmou início da entrega",
    "saida_removida_antes_confirmacao": "Removeu antes de começar entrega",
    "nova_saida_mesmo_entregador": "Pacote incluído em novo lote de saída",
}

FALLBACK_ACAO_HISTORICO_ANTERIOR = "Histórico anterior à atualização"

MENSAGEM_ACAO_PEDIDO_OBSOLETA = (
    "Este pedido foi removido da sua carga e não precisa mais ser sincronizado."
)


def classificar_acao_obsoleta(
    *,
    status: Optional[str],
    motoboy_id: Optional[int],
    ator_motoboy_id: Optional[int],
) -> Optional[str]:
    """Motivo estável da outbox, sem identificar o motoboy novo."""
    bruto = (status or "").strip().upper()
    if bruto in {"CANCELADO", "CANCELADA"}:
        return "CANCELADO"
    if motoboy_id is None:
        return "DESATRIBUIDO"
    try:
        if ator_motoboy_id is None or int(motoboy_id) != int(ator_motoboy_id):
            return "REATRIBUIDO"
    except (TypeError, ValueError):
        return "DESATRIBUIDO"
    return None

ROTULOS_SITUACAO = {
    "AGUARDANDO_CONFIRMACAO_SAIDA": "Aguardando confirmação de saída",
    "SAIU_PARA_ENTREGA": "Saiu para entrega",
    "SAIU": "Saiu para entrega",
    "EM_ROTA": "Em rota",
    "ENTREGUE": "Entregue",
    "AUSENTE": "Ausente",
    "CANCELADO": "Cancelado",
    "ENCERRADO_SISTEMA": "Encerrado",
    "ENCERRADO": "Encerrado",
    "NA_BASE": "Na base",
    "ETIQUETADO": "Etiqueta gerada",
    "COLETADO": "Coletado",
    "AGUARDANDO_COLETA": "Aguardando coleta",
}

_STATUS_CONTA_COMO_SAIU = {
    "saiu",
    "saiu para entrega",
    "saiu pra entrega",
    "saiu_para_entrega",
    "saiu_pra_entrega",
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


@dataclass(frozen=True)
class MarcosOperacionaisFechamento:
    """Marcos usados no fechamento: data operacional e exclusão por remoção."""

    operacional_ts: Optional[datetime]
    ultimo_evento_ts: Optional[datetime]
    removido_sem_inicio_ativo: bool


EVENTOS_FECHAMENTO_MARCOS = frozenset(
    EVENTOS_ATRIBUICAO_VALIDOS | EVENTOS_INVALIDANTES | EVENTOS_UI_ULTIMA_ACAO
)


def resolver_marcos_operacionais_fechamento(
    eventos: Sequence[Tuple[str, datetime]],
) -> MarcosOperacionaisFechamento:
    """Mesma máquina de estados do contexto operacional, só com timestamps.

    `eventos` deve estar ordenado por (timestamp, id), como o histórico no banco.
    """
    ultimo_ts: Optional[datetime] = None
    op_ts: Optional[datetime] = None
    removido = False
    for evento_raw, ts in eventos:
        evento = (evento_raw or "").strip().lower()
        if evento not in EVENTOS_FECHAMENTO_MARCOS:
            continue
        ultimo_ts = ts
        if evento in EVENTOS_INVALIDANTES:
            op_ts = None
            removido = True
            continue
        if evento in EVENTOS_ATRIBUICAO_VALIDOS:
            op_ts = ts
            removido = False
    return MarcosOperacionaisFechamento(
        operacional_ts=op_ts,
        ultimo_evento_ts=ultimo_ts,
        removido_sem_inicio_ativo=removido,
    )


def timestamp_operacional_marcos(
    marcos: Optional[MarcosOperacionaisFechamento],
    saida_ts: Optional[datetime],
) -> Optional[datetime]:
    if marcos and marcos.removido_sem_inicio_ativo:
        return None
    if marcos:
        return marcos.operacional_ts or marcos.ultimo_evento_ts or saida_ts
    return saida_ts


def marcos_de_ultimos_eventos(
    *,
    ultimo_atrib_ou_inv_evento: Optional[str],
    ultimo_atrib_ou_inv_ts: Optional[datetime],
    ultimo_marco_ts: Optional[datetime],
) -> MarcosOperacionaisFechamento:
    """Equivalente ao resolver completo usando só o último atrib/inv e o último marco.

    O estado final da máquina só depende desses dois marcos (não da sequência inteira).
    """
    evento = (ultimo_atrib_ou_inv_evento or "").strip().lower()
    if evento in EVENTOS_INVALIDANTES:
        return MarcosOperacionaisFechamento(
            operacional_ts=None,
            ultimo_evento_ts=ultimo_marco_ts,
            removido_sem_inicio_ativo=True,
        )
    if evento in EVENTOS_ATRIBUICAO_VALIDOS:
        return MarcosOperacionaisFechamento(
            operacional_ts=ultimo_atrib_ou_inv_ts,
            ultimo_evento_ts=ultimo_marco_ts,
            removido_sem_inicio_ativo=False,
        )
    return MarcosOperacionaisFechamento(
        operacional_ts=None,
        ultimo_evento_ts=ultimo_marco_ts,
        removido_sem_inicio_ativo=False,
    )


def _normalizar_evento(evento: Optional[str]) -> str:
    return (evento or "").strip().lower().replace(" ", "_")


def eh_reatribuicao_real(
    evento: Optional[str],
    motoboy_id_anterior: Optional[int] = None,
) -> bool:
    """
    Reatribuição real exige evento de reatribuição E titular anterior.
    Primeira atribuição (ex.: saída após entrada NA_BASE) não é reatribuição.
    """
    key = _normalizar_evento(evento)
    if key not in EVENTOS_REATRIBUICAO:
        return False
    return motoboy_id_anterior is not None


def normalizar_evento_atribuicao(
    evento: Optional[str],
    motoboy_id_anterior: Optional[int] = None,
) -> Optional[str]:
    """
    Converte 'assumir/reatribuicao' sem titular anterior em 'scan'
    (mesma última ação da 1ª saída sem entrada obrigatória).
    """
    if not evento:
        return None
    key = _normalizar_evento(evento)
    if key in EVENTOS_REATRIBUICAO and motoboy_id_anterior is None:
        return "scan"
    return key


def resolver_chave_acao(
    evento: Optional[str],
    houve_reatribuicao: bool = False,
    motoboy_id_anterior: Optional[int] = None,
) -> Optional[str]:
    if not evento:
        return None
    key = normalizar_evento_atribuicao(evento, motoboy_id_anterior=motoboy_id_anterior)
    if not key:
        return None
    if key in EVENTOS_REATRIBUICAO:
        return "reatribuido"
    if key == "em_rota" and houve_reatribuicao:
        return "reatribuido_em_rota"
    return key


def evento_tecnico_nao_substitui_acao(evento: Optional[str]) -> bool:
    """Retry e reconciliação não viram a Ação operacional exibida."""
    key = _normalizar_evento(evento)
    if not key:
        return False
    if key in {
        "retry",
        "sync_retry",
        "outbox_retry",
        "reconciliacao",
        "reconciliacao_sync",
        "retry_reconciliacao",
    }:
        return True
    if "reconcil" in key:
        return True
    if key == "retry" or key.startswith("retry_") or key.endswith("_retry"):
        return True
    return False


def _chave_status(status: Optional[str]) -> str:
    return (status or "").strip().lower().replace(" ", "_")


def status_aguardando_confirmacao_saida(status: Optional[str]) -> bool:
    return _chave_status(status) in {
        "aguardando_confirmacao_saida",
        "aguardando_confirmação_de_saída",
    }


def conta_como_saiu_para_entrega(status: Optional[str]) -> bool:
    """Indicador de saída confirmada. Aguardando confirmação não entra."""
    if status_aguardando_confirmacao_saida(status):
        return False
    bruto = (status or "").strip().lower()
    return bruto in _STATUS_CONTA_COMO_SAIU or _chave_status(status) in {
        "saiu",
        "saiu_para_entrega",
        "saiu_pra_entrega",
    }


def rotulo_situacao(status: Optional[str]) -> str:
    raw = (status or "").strip()
    if not raw:
        return "—"
    if status_aguardando_confirmacao_saida(raw):
        return ROTULOS_SITUACAO["AGUARDANDO_CONFIRMACAO_SAIDA"]
    if conta_como_saiu_para_entrega(raw):
        return "Saiu para entrega"
    chave = raw.upper().replace(" ", "_")
    if chave in ROTULOS_SITUACAO:
        return ROTULOS_SITUACAO[chave]
    lower = raw.lower()
    if lower in {"não coletado", "nao coletado", "não coletada", "nao coletada"}:
        return "Não coletado"
    return raw[:1].upper() + raw[1:]


def apresentar_situacao_acao(
    *,
    status: Optional[str],
    acao_label: Optional[str],
    acao_codigo: Optional[str] = None,
) -> dict:
    """Rótulos de listagem. Legado sem Ação conhecida usa o fallback amigável."""
    acao = (acao_label or "").strip()
    codigo = acao_codigo
    status_up = (status or "").strip().upper()
    if acao in {"", "—", "-"} and status_up == "ETIQUETADO":
        acao = "Emitiu etiqueta"
        codigo = codigo or "etiqueta_gerada"
    if acao in {"", "—", "-", "Sem ação"}:
        acao = FALLBACK_ACAO_HISTORICO_ANTERIOR
        codigo = None
    return {
        "acao": acao,
        "acao_codigo": codigo,
        "acao_label": acao,
        "situacao": status,
        "situacao_label": rotulo_situacao(status),
    }


def rotulo_acao_evento(
    evento: Optional[str],
    houve_reatribuicao: bool = False,
    motoboy_id_anterior: Optional[int] = None,
) -> Optional[str]:
    if evento_tecnico_nao_substitui_acao(evento):
        return None
    key = resolver_chave_acao(
        evento,
        houve_reatribuicao=houve_reatribuicao,
        motoboy_id_anterior=motoboy_id_anterior,
    )
    if not key:
        return None
    return ROTULOS_ACAO.get(key)


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


def janela_timestamp_periodo(inicio: date, fim: date) -> Tuple[datetime, datetime]:
    """Intervalo [inicio 00:00, fim+1 00:00) para filtrar timestamp sem func.date()."""
    ts_ini = datetime.combine(inicio, time.min)
    ts_fim_excl = datetime.combine(fim + timedelta(days=1), time.min)
    return ts_ini, ts_fim_excl
