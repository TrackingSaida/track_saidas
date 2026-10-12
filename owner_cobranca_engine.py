"""Motor puro da Cobrança de Owners (sem SQL).

Unidade com código = (sub_base, código normalizado). Vários id_saida do mesmo código
contam uma unidade; os extras aparecem como duplicidade desconsiderada.

Prioridade, só entre as origens incluídas no modelo comercial:
1. coleta (em qualquer data) → 2. entrada (primeiro entrada_base) → 3. saída válida.
A unidade pertence ao período da sua origem prioritária.

Coleta manual é outra unidade: (id_coleta, serviço) com a quantidade vigente.

bruto      = unidades com código elegíveis + saldos manuais
cancelados = unidades do bruto canceladas
faturavel  = bruto − cancelados
valor      = faturavel × preço do período + ajustes
"""
from __future__ import annotations

import calendar
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from dashboard_admin_cobranca import STATUS_CANCELADOS, STATUS_SAIDAS_COBRANCA

MODELOS: Dict[str, frozenset] = {
    "coleta_saida": frozenset({"coleta", "saida"}),
    "entrada_saida": frozenset({"entrada", "saida"}),
    "coleta_entrada_saida": frozenset({"coleta", "entrada", "saida"}),
    "somente_saida": frozenset({"saida"}),
}
MODELO_LABELS = {
    "coleta_saida": "Coleta + saída",
    "entrada_saida": "Entrada + saída",
    "coleta_entrada_saida": "Coleta + entrada + saída",
    "somente_saida": "Somente saída",
}
PERIODICIDADES = ("quinzenal", "mensal")
SERVICOS = ("shopee", "mercado_livre", "avulso")
SERVICO_LABELS = {"shopee": "Shopee", "mercado_livre": "Mercado Livre", "avulso": "Avulso"}

# Composição do demonstrativo, na ordem de exibição.
CLASSIFICACOES_BRUTO = (
    "coleta_identificada",
    "coleta_manual",
    "entrada_complementar",
    "saida_complementar",
    "avulso_adicional",
)
CLASSIFICACAO_LABELS = {
    "coleta_identificada": "Coleta identificada",
    "coleta_manual": "Coleta manual",
    "entrada_complementar": "Entrada complementar",
    "saida_complementar": "Saída complementar",
    "avulso_adicional": "Avulso adicional",
    "cancelado": "Cancelados",
    "duplicidade": "Duplicidade",
    "ajuste": "Ajustes",
}
MOTIVO_LABELS = {
    "cancelado": "Cancelado",
    "duplicidade": "Leitura repetida do mesmo código",
    "outro_periodo": "Já classificado em outro período",
    "ja_faturado": "Já cobrado em outro fechamento",
    "vinculo_administrativo": "Vinculado à coleta manual",
    "removido": "Leitura removida",
    "fora_do_modelo": "Origem fora do modelo comercial",
}
ORIGEM_LABELS = {
    "coleta": "Coleta",
    "entrada": "Entrada",
    "saida": "Saída",
    "manual": "Coleta manual",
}

AJUSTE_TIPOS = ("acrescimo", "desconto", "credito", "correcao")

NIVEL_BLOQUEANTE = "bloqueante"
NIVEL_ATENCAO = "atencao"
NIVEL_INFORMATIVO = "informativo"

DECISAO_TIPOS = ("volumes_distintos", "vinculo_codigos", "ajuste_quantidade")

_STATUS_VALIDOS = frozenset(STATUS_SAIDAS_COBRANCA)
_STATUS_CANCELADOS = frozenset(STATUS_CANCELADOS)
CENT = Decimal("0.01")


# ---------------------------------------------------------------------------
# Entradas do motor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EventoCodigo:
    """Uma linha de `saidas` com as datas das origens já resolvidas."""

    id_saida: int
    codigo: str
    servico: Optional[str] = None
    status: Optional[str] = None
    base: Optional[str] = None
    data_saida: Optional[date] = None
    data_coleta: Optional[date] = None
    id_coleta: Optional[int] = None
    data_entrada: Optional[date] = None
    avulso_lote_id: Optional[int] = None
    is_grande: bool = False


@dataclass(frozen=True)
class LancamentoManual:
    id_coleta: int
    servico: str  # shopee | mercado_livre | avulso
    base: str
    data: date
    quantidade: int
    versao_participante: int = 1


@dataclass(frozen=True)
class Decisao:
    tipo: str
    id_coleta: int
    servico: str
    codigos: Tuple[str, ...] = ()
    quantidade_ajustada: Optional[int] = None
    id: Optional[int] = None


@dataclass(frozen=True)
class Remocao:
    id_saida: int
    codigo: str
    servico: Optional[str]
    base: Optional[str]
    data: date


@dataclass(frozen=True)
class Ajuste:
    id: int
    tipo: str
    valor: Decimal
    motivo: str = ""


@dataclass(frozen=True)
class LoteAvulso:
    id: int
    quantidade_declarada: int
    codigos_existentes: int


# ---------------------------------------------------------------------------
# Saídas do motor
# ---------------------------------------------------------------------------


@dataclass
class ItemApuracao:
    tipo: str  # codigo | manual
    origem: str  # coleta | entrada | saida | manual
    classificacao: str
    status_financeiro: str  # cobrado | nao_cobrado | reconciliado
    data_competencia: Optional[date]
    servico: str
    base: Optional[str] = None
    codigo: Optional[str] = None
    id_saida: Optional[int] = None
    id_coleta: Optional[int] = None
    avulso_lote_id: Optional[int] = None
    motivo: Optional[str] = None
    quantidade: int = 1
    is_grande: bool = False
    status_operacional: Optional[str] = None
    no_bruto: bool = False

    @property
    def faturavel(self) -> bool:
        return self.status_financeiro == "cobrado"

    @property
    def cancelado(self) -> bool:
        return self.motivo == "cancelado"


@dataclass(frozen=True)
class Divergencia:
    nivel: str
    codigo: str
    mensagem: str
    referencia: Mapping[str, object] = field(default_factory=dict)


@dataclass
class LinhaComposicao:
    classificacao: str
    quantidade: int
    valor: Decimal


@dataclass
class Apuracao:
    modelo: str
    periodo_inicio: date
    periodo_fim: date
    preco: Decimal
    itens: List[ItemApuracao]
    qtd_bruto: int
    qtd_cancelados: int
    qtd_faturavel: int
    qtd_desconsiderados: int
    composicao: List[LinhaComposicao]
    valor_pacotes: Decimal
    valor_ajustes: Decimal
    valor_total: Decimal
    divergencias: List[Divergencia]
    hash: str
    ajustes: List[Ajuste] = field(default_factory=list)

    @property
    def bloqueantes(self) -> List[Divergencia]:
        return [d for d in self.divergencias if d.nivel == NIVEL_BLOQUEANTE]


# ---------------------------------------------------------------------------
# Regras auxiliares
# ---------------------------------------------------------------------------


def normalizar_codigo(codigo: Optional[str]) -> str:
    return (codigo or "").strip().upper()


def servico_bucket(servico: Optional[str]) -> str:
    s = (servico or "").strip().lower()
    if "shopee" in s:
        return "shopee"
    if "mercado" in s or "flex" in s or s == "ml" or s.startswith("ml ") or s.endswith(" ml"):
        return "mercado_livre"
    return "avulso"


def _norm_base(base: Optional[str]) -> str:
    return (base or "").strip().upper()


def _status(s: Optional[str]) -> str:
    return (s or "").strip().lower()


def sugerir_modelo(*, ignorar_coleta: bool, entrada_habilitada: bool, modo_operacao: str = "codigo") -> str:
    """Sugestão a partir das flags operacionais. Não autoriza fechamento.

    Coleta só manual não tem código para deduplicar com a entrada lida: com entrada
    habilitada, cobrar coleta contaria o mesmo pacote duas vezes.
    """
    if entrada_habilitada and not ignorar_coleta and modo_operacao != "coleta_manual":
        return "coleta_entrada_saida"
    if entrada_habilitada:
        return "entrada_saida"
    if ignorar_coleta:
        return "somente_saida"
    return "coleta_saida"


def valor_ajuste_assinado(tipo: str, valor: Decimal) -> Decimal:
    """Acréscimo soma; desconto e crédito subtraem; correção usa o sinal informado."""
    v = Decimal(str(valor))
    if tipo == "acrescimo":
        return abs(v)
    if tipo in ("desconto", "credito"):
        return -abs(v)
    return v


# ---------------------------------------------------------------------------
# Períodos
# ---------------------------------------------------------------------------


def _ultimo_dia(ano: int, mes: int) -> int:
    return calendar.monthrange(ano, mes)[1]


def _mes_anterior(d: date) -> date:
    return (d.replace(day=1) - timedelta(days=1)).replace(day=1)


def quinzena_de(d: date) -> Tuple[date, date]:
    if d.day <= 15:
        return d.replace(day=1), d.replace(day=15)
    return d.replace(day=16), d.replace(day=_ultimo_dia(d.year, d.month))


def mes_de(d: date) -> Tuple[date, date]:
    return d.replace(day=1), d.replace(day=_ultimo_dia(d.year, d.month))


def periodo_de(d: date, periodicidade: str) -> Tuple[date, date]:
    return mes_de(d) if periodicidade == "mensal" else quinzena_de(d)


def periodo_anterior(inicio: date, periodicidade: str) -> Tuple[date, date]:
    return periodo_de(inicio - timedelta(days=1), periodicidade)


def proximo_periodo(fim: date, periodicidade: str) -> Tuple[date, date]:
    return periodo_de(fim + timedelta(days=1), periodicidade)


def presets_periodo(hoje: date) -> List[Dict[str, object]]:
    q_ini, q_fim = quinzena_de(hoje)
    qa_ini, qa_fim = quinzena_de(q_ini - timedelta(days=1))
    m_ini, m_fim = mes_de(hoje)
    ma_ini, ma_fim = mes_de(_mes_anterior(hoje))
    return [
        {"id": "quinzena_atual", "label": "Quinzena atual", "inicio": q_ini, "fim": q_fim, "periodicidade": "quinzenal"},
        {"id": "quinzena_anterior", "label": "Quinzena anterior", "inicio": qa_ini, "fim": qa_fim, "periodicidade": "quinzenal"},
        {"id": "mes_atual", "label": "Mês atual", "inicio": m_ini, "fim": m_fim, "periodicidade": "mensal"},
        {"id": "mes_anterior", "label": "Mês anterior", "inicio": ma_ini, "fim": ma_fim, "periodicidade": "mensal"},
    ]


def periodo_fechavel(inicio: date, fim: date, periodicidade: str) -> bool:
    """Só quinzena (1–15, 16–último) ou mês cheio, conforme a periodicidade do Owner."""
    if fim < inicio:
        return False
    return periodo_de(inicio, periodicidade) == (inicio, fim)


def periodos_sobrepostos(a_ini: date, a_fim: date, b_ini: date, b_fim: date) -> bool:
    return a_ini <= b_fim and b_ini <= a_fim


# ---------------------------------------------------------------------------
# Motor
# ---------------------------------------------------------------------------


def _no_periodo(d: Optional[date], ini: date, fim: date) -> bool:
    return d is not None and ini <= d <= fim


def _evento_saida(ev: EventoCodigo) -> bool:
    st = _status(ev.status)
    return ev.data_saida is not None and (st in _STATUS_VALIDOS or st in _STATUS_CANCELADOS)


def _resolver_origem(
    eventos: Sequence[EventoCodigo], origens: frozenset
) -> Tuple[Optional[str], Optional[date], Optional[EventoCodigo]]:
    if "coleta" in origens:
        com_coleta = [e for e in eventos if e.data_coleta is not None]
        if com_coleta:
            principal = min(com_coleta, key=lambda e: (e.data_coleta, e.id_saida))
            return "coleta", principal.data_coleta, principal
    if "entrada" in origens:
        com_entrada = [e for e in eventos if e.data_entrada is not None]
        if com_entrada:
            principal = min(com_entrada, key=lambda e: (e.data_entrada, e.id_saida))
            return "entrada", principal.data_entrada, principal
    if "saida" in origens:
        com_saida = [e for e in eventos if _evento_saida(e)]
        if com_saida:
            principal = min(com_saida, key=lambda e: (e.data_saida, e.id_saida))
            return "saida", principal.data_saida, principal
    return None, None, None


def _tem_evento_no_periodo(eventos: Sequence[EventoCodigo], ini: date, fim: date) -> bool:
    for e in eventos:
        if _no_periodo(e.data_coleta, ini, fim) or _no_periodo(e.data_entrada, ini, fim):
            return True
        if _evento_saida(e) and _no_periodo(e.data_saida, ini, fim):
            return True
    return False


def _classificacao(origem: str, avulso: bool) -> str:
    if avulso:
        return "avulso_adicional"
    return {
        "coleta": "coleta_identificada",
        "entrada": "entrada_complementar",
        "saida": "saida_complementar",
        "manual": "coleta_manual",
    }[origem]


def _chave_manual(id_coleta: int, servico: str) -> Tuple[int, str]:
    return int(id_coleta), servico


def apurar(
    *,
    modelo: str,
    periodo_inicio: date,
    periodo_fim: date,
    preco: Optional[Decimal],
    eventos: Iterable[EventoCodigo],
    manuais: Iterable[LancamentoManual] = (),
    decisoes: Iterable[Decisao] = (),
    remocoes: Iterable[Remocao] = (),
    codigos_ja_faturados: Iterable[str] = (),
    ajustes: Iterable[Ajuste] = (),
    lotes_avulso: Mapping[int, LoteAvulso] | None = None,
) -> Apuracao:
    if modelo not in MODELOS:
        raise ValueError(f"Modelo comercial inválido: {modelo}")
    origens = MODELOS[modelo]
    ini, fim = periodo_inicio, periodo_fim
    preco_val = Decimal(str(preco)) if preco is not None else Decimal("0")
    ja_faturados: Set[str] = {normalizar_codigo(c) for c in codigos_ja_faturados}
    lotes_avulso = lotes_avulso or {}

    decisoes_por_manual: Dict[Tuple[int, str], List[Decisao]] = {}
    for d in decisoes:
        decisoes_por_manual.setdefault(_chave_manual(d.id_coleta, d.servico), []).append(d)
    vinculados: Dict[str, Tuple[int, str]] = {}
    for chave, lista in decisoes_por_manual.items():
        for d in lista:
            if d.tipo == "vinculo_codigos":
                for c in d.codigos:
                    vinculados[normalizar_codigo(c)] = chave

    grupos: Dict[str, List[EventoCodigo]] = {}
    for ev in eventos:
        chave = normalizar_codigo(ev.codigo)
        if not chave:
            continue
        grupos.setdefault(chave, []).append(ev)

    itens: List[ItemApuracao] = []
    divergencias: List[Divergencia] = []
    n_outro_periodo = 0
    n_servico_vazio = 0
    lotes_no_periodo: Set[int] = set()

    for chave in sorted(grupos):
        evs = sorted(grupos[chave], key=lambda e: e.id_saida)
        origem, data_comp, principal = _resolver_origem(evs, origens)
        if origem is None or principal is None:
            if _tem_evento_no_periodo(evs, ini, fim):
                e0 = evs[0]
                itens.append(
                    ItemApuracao(
                        tipo="codigo",
                        origem="coleta" if e0.data_coleta else ("entrada" if e0.data_entrada else "saida"),
                        classificacao="desconsiderado",
                        status_financeiro="nao_cobrado",
                        data_competencia=e0.data_coleta or e0.data_entrada or e0.data_saida,
                        servico=servico_bucket(e0.servico),
                        base=e0.base,
                        codigo=chave,
                        id_saida=e0.id_saida,
                        id_coleta=e0.id_coleta,
                        avulso_lote_id=e0.avulso_lote_id,
                        motivo="fora_do_modelo",
                        is_grande=bool(e0.is_grande),
                        status_operacional=e0.status,
                    )
                )
            continue

        if not _no_periodo(data_comp, ini, fim):
            if _tem_evento_no_periodo(evs, ini, fim):
                n_outro_periodo += 1
                itens.append(
                    ItemApuracao(
                        tipo="codigo",
                        origem=origem,
                        classificacao="desconsiderado",
                        status_financeiro="nao_cobrado",
                        data_competencia=data_comp,
                        servico=servico_bucket(principal.servico),
                        base=principal.base,
                        codigo=chave,
                        id_saida=principal.id_saida,
                        id_coleta=principal.id_coleta,
                        avulso_lote_id=principal.avulso_lote_id,
                        motivo="outro_periodo",
                        is_grande=bool(principal.is_grande),
                        status_operacional=principal.status,
                    )
                )
            continue

        avulso_lote = next((e.avulso_lote_id for e in evs if e.avulso_lote_id), None)
        if avulso_lote:
            lotes_no_periodo.add(int(avulso_lote))
        cancelado = all(_status(e.status) in _STATUS_CANCELADOS for e in evs)
        if not (principal.servico or "").strip():
            n_servico_vazio += 1

        base_item = dict(
            tipo="codigo",
            origem=origem,
            data_competencia=data_comp,
            servico=servico_bucket(principal.servico),
            base=principal.base,
            codigo=chave,
            id_saida=principal.id_saida,
            id_coleta=principal.id_coleta,
            avulso_lote_id=avulso_lote,
            is_grande=any(bool(e.is_grande) for e in evs),
            status_operacional=principal.status,
        )
        if chave in ja_faturados:
            itens.append(
                ItemApuracao(
                    classificacao="desconsiderado",
                    status_financeiro="nao_cobrado",
                    motivo="ja_faturado",
                    **base_item,
                )
            )
        elif chave in vinculados:
            itens.append(
                ItemApuracao(
                    classificacao="desconsiderado",
                    status_financeiro="reconciliado",
                    motivo="vinculo_administrativo",
                    **base_item,
                )
            )
        else:
            itens.append(
                ItemApuracao(
                    classificacao=_classificacao(origem, bool(avulso_lote)),
                    status_financeiro="nao_cobrado" if cancelado else "cobrado",
                    motivo="cancelado" if cancelado else None,
                    no_bruto=True,
                    **base_item,
                )
            )

        for extra in evs:
            if extra.id_saida == principal.id_saida:
                continue
            itens.append(
                ItemApuracao(
                    tipo="codigo",
                    origem=origem,
                    classificacao="duplicidade",
                    status_financeiro="nao_cobrado",
                    data_competencia=extra.data_saida or extra.data_coleta or extra.data_entrada,
                    servico=servico_bucket(extra.servico),
                    base=extra.base,
                    codigo=chave,
                    id_saida=extra.id_saida,
                    id_coleta=extra.id_coleta,
                    avulso_lote_id=extra.avulso_lote_id,
                    motivo="duplicidade",
                    is_grande=bool(extra.is_grande),
                    status_operacional=extra.status,
                )
            )

    # Coleta manual: unidade por (id_coleta, serviço), sem código.
    manuais_periodo = [m for m in manuais if _no_periodo(m.data, ini, fim) and int(m.quantidade or 0) > 0]
    if manuais_periodo and "coleta" not in origens:
        total = sum(int(m.quantidade) for m in manuais_periodo)
        divergencias.append(
            Divergencia(
                NIVEL_INFORMATIVO,
                "manual_fora_do_modelo",
                f"{total} pacotes de coleta manual não entram: o modelo comercial não cobra coleta.",
                {"quantidade": total},
            )
        )
        manuais_periodo = []

    codificados_dia: Dict[Tuple[str, str, date], List[ItemApuracao]] = {}
    for it in itens:
        if it.tipo != "codigo" or it.data_competencia is None:
            continue
        if not (it.no_bruto or it.status_financeiro == "reconciliado"):
            continue
        codificados_dia.setdefault(
            (_norm_base(it.base), it.servico, it.data_competencia), []
        ).append(it)

    manuais_ambiguos = 0
    for m in sorted(manuais_periodo, key=lambda x: (x.data, x.id_coleta, x.servico)):
        chave_m = _chave_manual(m.id_coleta, m.servico)
        decs = decisoes_por_manual.get(chave_m, [])
        qtd = int(m.quantidade)
        ajuste_qtd = [d for d in decs if d.tipo == "ajuste_quantidade" and d.quantidade_ajustada is not None]
        if ajuste_qtd:
            qtd = int(ajuste_qtd[-1].quantidade_ajustada)
        candidatos = codificados_dia.get((_norm_base(m.base), m.servico, m.data), [])
        ref = {
            "id_coleta": m.id_coleta,
            "servico": m.servico,
            "base": m.base,
            "data": m.data.isoformat(),
            "quantidade_manual": int(m.quantidade),
            "codigos_no_dia": len(candidatos),
        }
        if candidatos and not decs:
            manuais_ambiguos += 1
            divergencias.append(
                Divergencia(
                    NIVEL_BLOQUEANTE,
                    "ambiguidade_manual",
                    f"{m.data.strftime('%d/%m')} · {m.base} · {SERVICO_LABELS.get(m.servico, m.servico)}: "
                    f"{m.quantidade} manuais e {len(candidatos)} códigos no mesmo dia. "
                    "Confirme se são volumes distintos ou vincule os códigos.",
                    ref,
                )
            )
        elif not candidatos:
            divergencias.append(
                Divergencia(
                    NIVEL_INFORMATIVO,
                    "manual_isolado",
                    f"{m.data.strftime('%d/%m')} · {m.base} · {SERVICO_LABELS.get(m.servico, m.servico)}: "
                    f"{m.quantidade} pacotes manuais sem código, cobrados como saldo.",
                    ref,
                )
            )
        if int(m.versao_participante or 1) > 1:
            divergencias.append(
                Divergencia(
                    NIVEL_ATENCAO,
                    "manual_editado",
                    f"{m.data.strftime('%d/%m')} · {m.base}: o lançamento manual foi editado depois de criado.",
                    ref,
                )
            )
        if qtd <= 0:
            continue
        itens.append(
            ItemApuracao(
                tipo="manual",
                origem="manual",
                classificacao="coleta_manual",
                status_financeiro="cobrado",
                data_competencia=m.data,
                servico=m.servico,
                base=m.base,
                id_coleta=m.id_coleta,
                quantidade=qtd,
                no_bruto=True,
            )
        )

    codigos_presentes = set(grupos)
    for r in remocoes:
        if not _no_periodo(r.data, ini, fim):
            continue
        if normalizar_codigo(r.codigo) in codigos_presentes:
            continue
        itens.append(
            ItemApuracao(
                tipo="codigo",
                origem="coleta",
                classificacao="desconsiderado",
                status_financeiro="nao_cobrado",
                data_competencia=r.data,
                servico=servico_bucket(r.servico),
                base=r.base,
                codigo=normalizar_codigo(r.codigo),
                id_saida=r.id_saida,
                motivo="removido",
            )
        )

    # Totais
    qtd_bruto = sum(it.quantidade for it in itens if it.no_bruto)
    qtd_cancelados = sum(it.quantidade for it in itens if it.no_bruto and it.cancelado)
    qtd_faturavel = qtd_bruto - qtd_cancelados
    qtd_desconsiderados = sum(it.quantidade for it in itens if not it.no_bruto)

    composicao: List[LinhaComposicao] = []
    for cls in CLASSIFICACOES_BRUTO:
        q = sum(it.quantidade for it in itens if it.no_bruto and it.classificacao == cls)
        if q:
            composicao.append(LinhaComposicao(cls, q, (preco_val * q).quantize(CENT)))
    if qtd_cancelados:
        composicao.append(
            LinhaComposicao("cancelado", -qtd_cancelados, (-preco_val * qtd_cancelados).quantize(CENT))
        )
    n_dup = sum(1 for it in itens if it.motivo == "duplicidade")
    if n_dup:
        composicao.append(LinhaComposicao("duplicidade", n_dup, Decimal("0.00")))

    lista_ajustes = list(ajustes)
    valor_pacotes = (preco_val * qtd_faturavel).quantize(CENT)
    valor_ajustes = sum(
        (valor_ajuste_assinado(a.tipo, a.valor) for a in lista_ajustes), start=Decimal("0")
    ).quantize(CENT)
    if lista_ajustes:
        composicao.append(LinhaComposicao("ajuste", len(lista_ajustes), valor_ajustes))
    valor_total = (valor_pacotes + valor_ajustes).quantize(CENT)

    # Divergências de atenção / informativas
    for lote_id in sorted(lotes_no_periodo):
        lote = lotes_avulso.get(lote_id)
        if lote and lote.codigos_existentes < lote.quantidade_declarada:
            divergencias.append(
                Divergencia(
                    NIVEL_ATENCAO,
                    "lote_incompleto",
                    f"Lote avulso {lote_id}: {lote.quantidade_declarada} declarados e "
                    f"{lote.codigos_existentes} códigos gerados. Cobramos só os códigos.",
                    {"avulso_lote_id": lote_id},
                )
            )
    if n_servico_vazio:
        divergencias.append(
            Divergencia(
                NIVEL_ATENCAO,
                "servico_vazio",
                f"{n_servico_vazio} pacotes sem serviço informado; entram como Avulso.",
                {"quantidade": n_servico_vazio},
            )
        )
    if n_outro_periodo:
        divergencias.append(
            Divergencia(
                NIVEL_ATENCAO,
                "outro_periodo",
                f"{n_outro_periodo} pacotes tiveram movimento neste período, mas a origem cobrada é de outro período.",
                {"quantidade": n_outro_periodo},
            )
        )
    n_g = sum(1 for it in itens if it.faturavel and it.is_grande)
    if n_g:
        divergencias.append(
            Divergencia(
                NIVEL_INFORMATIVO,
                "pacote_g",
                f"{n_g} pacotes G. O preço unitário não muda.",
                {"quantidade": n_g},
            )
        )

    hash_ = calcular_hash(
        modelo=modelo,
        periodo_inicio=ini,
        periodo_fim=fim,
        preco=preco_val,
        itens=itens,
        ajustes=lista_ajustes,
    )
    return Apuracao(
        modelo=modelo,
        periodo_inicio=ini,
        periodo_fim=fim,
        preco=preco_val,
        itens=itens,
        qtd_bruto=qtd_bruto,
        qtd_cancelados=qtd_cancelados,
        qtd_faturavel=qtd_faturavel,
        qtd_desconsiderados=qtd_desconsiderados,
        composicao=composicao,
        valor_pacotes=valor_pacotes,
        valor_ajustes=valor_ajustes,
        valor_total=valor_total,
        divergencias=divergencias,
        hash=hash_,
        ajustes=lista_ajustes,
    )


def calcular_hash(
    *,
    modelo: str,
    periodo_inicio: date,
    periodo_fim: date,
    preco: Decimal,
    itens: Sequence[ItemApuracao],
    ajustes: Sequence[Ajuste] = (),
) -> str:
    """Hash das entradas da apuração. Muda se a operação, o preço ou os ajustes mudarem."""
    linhas = sorted(
        (
            it.tipo,
            it.codigo or "",
            it.id_coleta or 0,
            it.servico,
            it.origem,
            it.status_financeiro,
            it.motivo or "",
            it.quantidade,
            it.data_competencia.isoformat() if it.data_competencia else "",
            it.id_saida or 0,
        )
        for it in itens
    )
    payload = {
        "modelo": modelo,
        "inicio": periodo_inicio.isoformat(),
        "fim": periodo_fim.isoformat(),
        "preco": str(Decimal(str(preco)).normalize()),
        "itens": linhas,
        "ajustes": sorted((a.id, a.tipo, str(a.valor)) for a in ajustes),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def filtrar_itens(
    itens: Sequence[ItemApuracao],
    *,
    codigo: Optional[str] = None,
    data: Optional[date] = None,
    servico: Optional[str] = None,
    origem: Optional[str] = None,
    filtro: Optional[str] = None,
) -> List[ItemApuracao]:
    """filtro: faturavel | cancelado | duplicado | manual | reconciliado | desconsiderado."""
    out: List[ItemApuracao] = []
    cod = normalizar_codigo(codigo) if codigo else ""
    for it in itens:
        if cod and cod not in (it.codigo or ""):
            continue
        if data and it.data_competencia != data:
            continue
        if servico and it.servico != servico:
            continue
        if origem and it.origem != origem:
            continue
        if filtro == "faturavel" and not it.faturavel:
            continue
        if filtro == "cancelado" and not (it.no_bruto and it.cancelado):
            continue
        if filtro == "duplicado" and it.motivo != "duplicidade":
            continue
        if filtro == "manual" and it.tipo != "manual":
            continue
        if filtro == "reconciliado" and it.status_financeiro != "reconciliado":
            continue
        if filtro == "desconsiderado" and it.no_bruto:
            continue
        out.append(it)
    out.sort(
        key=lambda it: (
            it.data_competencia or date.min,
            0 if it.tipo == "manual" else 1,
            it.codigo or "",
            it.id_saida or 0,
        )
    )
    return out
