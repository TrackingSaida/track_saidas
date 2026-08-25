"""Serviço de Conferência de Saída (motoboy + dia operacional)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from conferencia_saida_pure import filtrar_status_conferencia, status_conta_na_conferencia
from models import ConferenciaSaida, Motoboy, Owner, Saida, SaidaHistorico, User
from saida_operacional_utils import (
    carregar_contexto_operacional,
    carregar_saidas_candidatas_periodo,
    deve_excluir_saida_operacional,
    filtrar_saidas_por_periodo_operacional,
    timestamp_operacional_saida,
)

STATUS_PENDENTE = "pendente"
STATUS_RECONFERIR = "reconferir"
STATUS_CONFERIDA = "conferida"

LABEL_FECHAMENTO = {
    STATUS_CONFERIDA: "Conferido",
    STATUS_PENDENTE: "Não conferido",
    STATUS_RECONFERIR: "Não conferido",
}


def owner_conferencia_habilitada(db: Session, sub_base: str, user: Optional[User] = None) -> bool:
    if user is not None and bool(getattr(user, "conferencia_saida_habilitada", False)):
        return True
    owner = db.scalar(select(Owner).where(Owner.sub_base == sub_base))
    return bool(owner and getattr(owner, "conferencia_saida_habilitada", False))


def _owner_id_for_sub_base(db: Session, sub_base: str) -> Optional[int]:
    owner = db.scalar(select(Owner).where(Owner.sub_base == sub_base))
    return int(owner.id_owner) if owner else None


def upsert_conferencia_dia(
    db: Session,
    *,
    sub_base: str,
    motoboy_id: int,
    data_ref: date,
    qtd: int,
) -> Tuple[Optional[ConferenciaSaida], bool]:
    """
    Cria/atualiza conferência do dia (staff Confirmar Leitura ou motoboy ao iniciar rota).
    Retorna (row, virou_reconferir) — virou_reconferir só True na transição conferida→reconferir.
    """
    if qtd <= 0:
        return None, False
    row = db.scalar(
        select(ConferenciaSaida).where(
            ConferenciaSaida.sub_base == sub_base,
            ConferenciaSaida.motoboy_id == motoboy_id,
            ConferenciaSaida.data_ref == data_ref,
        )
    )
    now = datetime.utcnow()
    if row is None:
        row = ConferenciaSaida(
            sub_base=sub_base,
            owner_id=_owner_id_for_sub_base(db, sub_base),
            motoboy_id=motoboy_id,
            data_ref=data_ref,
            status=STATUS_PENDENTE,
            ultima_abertura_em=now,
            qtd_no_momento=qtd,
        )
        db.add(row)
        return row, False

    row.qtd_no_momento = qtd
    row.ultima_abertura_em = now
    virou_reconferir = False
    if row.status == STATUS_CONFERIDA:
        row.status = STATUS_RECONFERIR
        row.conferido_por = None
        row.conferido_em = None
        virou_reconferir = True
    return row, virou_reconferir


def upsert_conferencia_apos_iniciar_rota(
    db: Session,
    *,
    sub_base: str,
    motoboy_id: int,
    data_ref: date,
    qtd: int,
) -> Tuple[Optional[ConferenciaSaida], bool]:
    """Alias histórico — mesma regra de upsert_conferencia_dia."""
    return upsert_conferencia_dia(
        db,
        sub_base=sub_base,
        motoboy_id=motoboy_id,
        data_ref=data_ref,
        qtd=qtd,
    )


def _servico_bucket(servico: Optional[str]) -> str:
    s = (servico or "").strip().lower()
    if s in ("shopee",):
        return "shopee"
    if s in ("ml", "mercado_livre", "mercado livre", "mercadolivre"):
        return "ml"
    return "avulso"


def somar_servicos_saidas(rows: List[Saida]) -> Tuple[int, int, int]:
    shopee = ml = avulso = 0
    for s in rows:
        b = _servico_bucket(s.servico)
        if b == "shopee":
            shopee += 1
        elif b == "ml":
            ml += 1
        else:
            avulso += 1
    return shopee, ml, avulso


def servico_label_amigavel(servico: Optional[str]) -> str:
    b = _servico_bucket(servico)
    if b == "shopee":
        return "Shopee"
    if b == "ml":
        return "ML"
    return "Avulso"


def listar_saidas_motoboy_dia(
    db: Session,
    *,
    sub_base: str,
    motoboy_id: int,
    data_ref: date,
) -> List[Saida]:
    """Pacotes do motoboy com data operacional == data_ref (reatribuição conta no dia D).

    Exclui cancelados/encerrados. Entregue permanece: ainda é saída válida do dia.
    """
    rows_all = carregar_saidas_candidatas_periodo(
        db,
        sub_base=sub_base,
        motoboy_ids=[motoboy_id],
        inicio=data_ref,
        fim=data_ref,
    )
    filtradas, _ = filtrar_saidas_por_periodo_operacional(db, rows_all, data_ref, data_ref)
    return filtrar_status_conferencia(list(filtradas))


EVENTOS_CONFERENCIA_PACOTE = ("saida_conferida", "saida_reconferida")


def _ids_ja_conferidos(db: Session, saida_ids: List[int]) -> set[int]:
    if not saida_ids:
        return set()
    rows = db.scalars(
        select(SaidaHistorico.id_saida)
        .where(
            SaidaHistorico.id_saida.in_(saida_ids),
            SaidaHistorico.evento.in_(EVENTOS_CONFERENCIA_PACOTE),
        )
        .distinct()
    ).all()
    return {int(x) for x in rows if x is not None}


def listar_saidas_novas_apos_conferencia(
    db: Session,
    *,
    sub_base: str,
    motoboy_id: int,
    data_ref: date,
) -> List[Saida]:
    """Pacotes do motoboy/dia sem evento saida_conferida/saida_reconferida.

    Após conferir/reconferir, os pacotes do dia ganham o evento; os que
    entram depois (novo Começar Entrega) ficam sem ele até a próxima ação.
    """
    saidas = listar_saidas_motoboy_dia(
        db, sub_base=sub_base, motoboy_id=motoboy_id, data_ref=data_ref
    )
    if not saidas:
        return []
    ja = _ids_ja_conferidos(db, [int(s.id_saida) for s in saidas])
    novos = [s for s in saidas if int(s.id_saida) not in ja]
    novos.sort(key=lambda s: ((s.codigo or "").upper(), int(s.id_saida)))
    return novos


def _saidas_relevantes_por_chave(
    db: Session,
    *,
    sub_base: str,
    chaves: List[Tuple[int, date]],
) -> Tuple[Dict[Tuple[int, date], int], List[Saida], Dict]:
    """Carrega saídas válidas da conferência por (motoboy_id, data operacional).

    Retorna (totais, lista_relevante, ctx_map).
    """
    chave_set = {(int(m), d) for m, d in chaves}
    if not chave_set:
        return {}, [], {}
    motoboy_ids = {m for m, _ in chave_set}
    datas = [d for _, d in chave_set]
    saidas = carregar_saidas_candidatas_periodo(
        db,
        sub_base=sub_base,
        motoboy_ids=list(motoboy_ids),
        inicio=min(datas),
        fim=max(datas),
    )
    totais: Dict[Tuple[int, date], int] = {k: 0 for k in chave_set}
    if not saidas:
        return totais, [], {}
    ctx_map = carregar_contexto_operacional(db, [int(s.id_saida) for s in saidas])
    relevantes: List[Saida] = []
    for s in saidas:
        if s.motoboy_id is None:
            continue
        ctx = ctx_map.get(int(s.id_saida))
        if deve_excluir_saida_operacional(ctx):
            continue
        if not status_conta_na_conferencia(getattr(s, "status", None)):
            continue
        ts = timestamp_operacional_saida(ctx, getattr(s, "timestamp", None))
        if ts is None:
            continue
        key = (int(s.motoboy_id), ts.date())
        if key not in chave_set:
            continue
        relevantes.append(s)
        totais[key] = totais.get(key, 0) + 1
    return totais, relevantes, ctx_map


def contar_saidas_por_motoboy_dia(
    db: Session,
    *,
    sub_base: str,
    chaves: List[Tuple[int, date]],
) -> Dict[Tuple[int, date], int]:
    """Conta pacotes válidos (lidos no dia, sem cancelado/encerrado) por motoboy/dia."""
    totais, _, _ = _saidas_relevantes_por_chave(db, sub_base=sub_base, chaves=chaves)
    return totais


def contar_novos_por_motoboy_dia(
    db: Session,
    *,
    sub_base: str,
    chaves: List[Tuple[int, date]],
) -> Dict[Tuple[int, date], int]:
    """Conta pacotes novos (sem conferência/reconferência) por (motoboy_id, data_ref operacional)."""
    _totais, novos = contar_totais_e_novos_por_motoboy_dia(
        db, sub_base=sub_base, chaves=chaves
    )
    return novos


def contar_totais_e_novos_por_motoboy_dia(
    db: Session,
    *,
    sub_base: str,
    chaves: List[Tuple[int, date]],
) -> Tuple[Dict[Tuple[int, date], int], Dict[Tuple[int, date], int]]:
    """Retorna (totais válidos, pacotes novos) por (motoboy_id, data_ref)."""
    if not chaves:
        return {}, {}
    chave_set = {(int(m), d) for m, d in chaves}
    totais, relevantes, ctx_map = _saidas_relevantes_por_chave(
        db, sub_base=sub_base, chaves=chaves
    )
    novos: Dict[Tuple[int, date], int] = {k: 0 for k in chave_set}
    if not relevantes:
        return totais, novos
    ja = _ids_ja_conferidos(db, [int(s.id_saida) for s in relevantes])
    for s in relevantes:
        if int(s.id_saida) in ja:
            continue
        ctx = ctx_map.get(int(s.id_saida))
        ts = timestamp_operacional_saida(ctx, getattr(s, "timestamp", None))
        if ts is None:
            continue
        key = (int(s.motoboy_id), ts.date())
        novos[key] = novos.get(key, 0) + 1
    return totais, novos


def resumo_novos_pacotes(saidas_novas: List[Saida]) -> dict:
    shopee, ml, avulso = somar_servicos_saidas(saidas_novas)
    pacotes = [
        {
            "codigo": (s.codigo or "").strip(),
            "servico": servico_label_amigavel(s.servico),
        }
        for s in saidas_novas
        if (s.codigo or "").strip()
    ]
    return {
        "novos_qtd": len(pacotes),
        "novos_shopee": shopee,
        "novos_mercado": ml,
        "novos_avulso": avulso,
        "novos_pacotes": pacotes,
    }


def carregar_nomes_motoboy(db: Session, motoboy_ids: List[int]) -> Dict[int, str]:
    from motoboy_nome_utils import carregar_nomes_motoboy_ids
    return carregar_nomes_motoboy_ids(db, motoboy_ids)


def label_fechamento(status: str) -> str:
    return LABEL_FECHAMENTO.get(status, "Não conferido")


def conferencia_por_dia_periodo(
    db: Session,
    *,
    sub_base: str,
    motoboy_id: int,
    periodo_inicio: date,
    periodo_fim: date,
) -> List[dict]:
    rows = list(
        db.scalars(
            select(ConferenciaSaida).where(
                ConferenciaSaida.sub_base == sub_base,
                ConferenciaSaida.motoboy_id == motoboy_id,
                ConferenciaSaida.data_ref >= periodo_inicio,
                ConferenciaSaida.data_ref <= periodo_fim,
            ).order_by(ConferenciaSaida.data_ref.asc())
        ).all()
    )
    return [
        {
            "data": r.data_ref.isoformat(),
            "status": r.status,
            "label": label_fechamento(r.status),
        }
        for r in rows
    ]


def conferir_saida(
    db: Session,
    *,
    sub_base: str,
    motoboy_id: int,
    data_ref: date,
    user_id: Optional[int],
) -> ConferenciaSaida:
    row = db.scalar(
        select(ConferenciaSaida).where(
            ConferenciaSaida.sub_base == sub_base,
            ConferenciaSaida.motoboy_id == motoboy_id,
            ConferenciaSaida.data_ref == data_ref,
        )
    )
    if row is None:
        raise ValueError("CONFERENCIA_NAO_ENCONTRADA")

    saidas = listar_saidas_motoboy_dia(
        db, sub_base=sub_base, motoboy_id=motoboy_id, data_ref=data_ref
    )
    # Reconferência usa evento distinto para histórico / última ação.
    evento_hist = (
        "saida_reconferida" if row.status == STATUS_RECONFERIR else "saida_conferida"
    )
    now = datetime.utcnow()
    row.status = STATUS_CONFERIDA
    row.conferido_por = user_id
    row.conferido_em = now
    row.qtd_no_momento = len(saidas)

    for s in saidas:
        db.add(
            SaidaHistorico(
                id_saida=s.id_saida,
                evento=evento_hist,
                status_anterior=s.status,
                status_novo=s.status,
                user_id=user_id,
            )
        )
    return row
