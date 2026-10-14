"""Consultas e ciclo de vida da Cobrança de Owners.

A regra fica em `owner_cobranca_engine`. Aqui: buscar a operação de UM Owner por
`sub_base` do registro (nunca do cliente), montar a prévia, conferir, fechar,
ajustar, reabrir e anular. Fechado é snapshot: nada aqui relê a operação para ele.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from fastapi import HTTPException
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pedido_sincronizado import SQL_SINCRONIZADO_NAO_COLETADO
from owner_cobranca_engine import (
    AJUSTE_PERIODO_TIPOS,
    AJUSTE_TIPOS,
    CLASSIFICACAO_LABELS,
    DECISAO_TIPOS,
    ESCOPO_CICLO,
    ESCOPO_PERIODO,
    MODELOS,
    NIVEL_ATENCAO,
    NIVEL_BLOQUEANTE,
    NIVEL_INFORMATIVO,
    PERIODICIDADES,
    Ajuste,
    Apuracao,
    Decisao,
    Divergencia,
    EventoCodigo,
    LancamentoManual,
    LoteAvulso,
    Remocao,
    apurar,
    calcular_ajustes,
    normalizar_codigo,
    periodo_fechavel,
    periodos_sobrepostos,
    situacao_pacotes,
    sugerir_modelo,
    valor_ajuste_assinado,
)
from models import (
    BaseSellerDados,
    Owner,
    OwnerApuracao,
    OwnerApuracaoDecisao,
    OwnerConfiguracaoCobranca,
    OwnerFechamento,
    OwnerFechamentoAjuste,
    OwnerFechamentoComposicao,
    OwnerFechamentoHistorico,
    OwnerFechamentoItem,
    OwnerPrecoVigencia,
    OwnerUnidadeFaturada,
    User,
)

STATUS_VIGENTES = ("fechado", "informado")
STATUS_LISTA = ("em_apuracao", "conferido", "fechado", "informado")


# ---------------------------------------------------------------------------
# Owner, configuração e preço
# ---------------------------------------------------------------------------


def carregar_owner(db: Session, owner_id: int) -> Owner:
    owner = db.get(Owner, int(owner_id))
    if owner is None or not (owner.sub_base or "").strip():
        raise HTTPException(404, "Owner não encontrado.")
    return owner


def listar_owners(db: Session, *, incluir_teste: bool = False, owner_id: Optional[int] = None) -> List[Owner]:
    stmt = select(Owner).where(Owner.ativo.is_(True), Owner.sub_base.isnot(None))
    if not incluir_teste:
        stmt = stmt.where(Owner.teste.is_(False))
    if owner_id:
        stmt = stmt.where(Owner.id_owner == int(owner_id))
    return list(db.scalars(stmt.order_by(func.lower(func.coalesce(Owner.nome_fantasia, Owner.username)))).all())


def nome_owner(owner: Owner) -> str:
    return (owner.nome_fantasia or "").strip() or (owner.username or "").strip() or (owner.sub_base or "")


def obter_config(db: Session, owner: Owner) -> OwnerConfiguracaoCobranca:
    """Configuração gravada; sem ela, devolve a sugestão (não persistida, pendente)."""
    cfg = db.scalar(
        select(OwnerConfiguracaoCobranca).where(OwnerConfiguracaoCobranca.owner_id == owner.id_owner)
    )
    if cfg is not None:
        return cfg
    sugerido = sugerir_modelo(
        ignorar_coleta=bool(owner.ignorar_coleta),
        entrada_habilitada=bool(getattr(owner, "entrada_obrigatoria_habilitada", False)),
        modo_operacao=owner.modo_operacao or "codigo",
    )
    return OwnerConfiguracaoCobranca(
        owner_id=owner.id_owner,
        sub_base=owner.sub_base,
        modelo=sugerido,
        modelo_sugerido=sugerido,
        periodicidade="quinzenal",
        status="pendente_confirmacao",
    )


def listar_precos(db: Session, owner_id: int) -> List[OwnerPrecoVigencia]:
    return list(
        db.scalars(
            select(OwnerPrecoVigencia)
            .where(OwnerPrecoVigencia.owner_id == owner_id)
            .order_by(OwnerPrecoVigencia.inicio.desc())
        ).all()
    )


def preco_vigente(precos: Sequence[OwnerPrecoVigencia], em: date) -> Optional[OwnerPrecoVigencia]:
    candidatos = [p for p in precos if p.inicio <= em and (p.fim is None or p.fim >= em)]
    return max(candidatos, key=lambda p: p.inicio) if candidatos else None


def salvar_config(db: Session, owner: Owner, user: User, dados: Dict[str, Any]) -> OwnerConfiguracaoCobranca:
    modelo = dados.get("modelo")
    periodicidade = dados.get("periodicidade")
    if modelo is not None and modelo not in MODELOS:
        raise HTTPException(422, "Modelo comercial inválido.")
    if periodicidade is not None and periodicidade not in PERIODICIDADES:
        raise HTTPException(422, "Periodicidade inválida.")

    cfg = db.scalar(
        select(OwnerConfiguracaoCobranca).where(OwnerConfiguracaoCobranca.owner_id == owner.id_owner)
    )
    if cfg is None:
        cfg = obter_config(db, owner)
        db.add(cfg)

    if periodicidade and periodicidade != cfg.periodicidade:
        hoje = date.today()
        vigentes = db.scalars(
            select(OwnerFechamento).where(
                OwnerFechamento.owner_id == owner.id_owner,
                OwnerFechamento.status.in_(STATUS_VIGENTES),
                OwnerFechamento.periodo_fim >= hoje.replace(day=1),
            )
        ).all()
        from owner_cobranca_engine import periodo_de

        novo_ini, novo_fim = periodo_de(hoje, periodicidade)
        for f in vigentes:
            if periodos_sobrepostos(novo_ini, novo_fim, f.periodo_inicio, f.periodo_fim) and (
                (f.periodo_inicio, f.periodo_fim) != (novo_ini, novo_fim)
            ):
                raise HTTPException(
                    409,
                    "A nova periodicidade cruza um período já fechado. Altere depois que o período atual terminar.",
                )
        cfg.periodicidade = periodicidade

    if modelo:
        cfg.modelo = modelo
    for campo in ("razao_social", "documento", "telefone", "endereco", "contato_financeiro", "observacao"):
        if campo in dados:
            valor = dados.get(campo)
            setattr(cfg, campo, (str(valor).strip() or None) if valor is not None else None)
    if dados.get("confirmar"):
        cfg.status = "confirmado"
        cfg.confirmado_por = user.id
        cfg.confirmado_em = datetime.now()
    cfg.sub_base = owner.sub_base
    cfg.atualizado_por = user.id
    cfg.atualizado_em = datetime.now()
    db.flush()
    return cfg


def adicionar_preco(
    db: Session, owner: Owner, user: User, *, valor: Decimal, inicio: date, motivo: Optional[str]
) -> OwnerPrecoVigencia:
    if valor is None or Decimal(str(valor)) < 0:
        raise HTTPException(422, "Informe um preço maior ou igual a zero.")
    fechado_depois = db.scalar(
        select(OwnerFechamento.id).where(
            OwnerFechamento.owner_id == owner.id_owner,
            OwnerFechamento.status.in_(STATUS_VIGENTES),
            OwnerFechamento.periodo_inicio >= inicio,
        ).limit(1)
    )
    if fechado_depois:
        raise HTTPException(
            409, "Já existe período fechado a partir desta data. O preço novo deve começar depois dele."
        )
    precos = listar_precos(db, owner.id_owner)
    if any(p.inicio == inicio for p in precos):
        raise HTTPException(409, "Já existe um preço com início nesta data.")
    anterior = preco_vigente(precos, inicio - timedelta(days=1))
    if anterior is not None and (anterior.fim is None or anterior.fim >= inicio):
        anterior.fim = inicio - timedelta(days=1)
    posterior = min((p for p in precos if p.inicio > inicio), key=lambda p: p.inicio, default=None)
    novo = OwnerPrecoVigencia(
        owner_id=owner.id_owner,
        valor=Decimal(str(valor)),
        inicio=inicio,
        fim=(posterior.inicio - timedelta(days=1)) if posterior else None,
        motivo=(motivo or "").strip() or None,
        criado_por=user.id,
    )
    db.add(novo)
    db.flush()
    return novo


# ---------------------------------------------------------------------------
# Leitura da operação (um Owner, um período)
# ---------------------------------------------------------------------------


def _bounds(ini: date, fim: date) -> Tuple[datetime, datetime]:
    """[início, fim+1) em horário de Brasília, como o resto da operação (timestamp naive)."""
    return datetime.combine(ini, time.min), datetime.combine(fim + timedelta(days=1), time.min)


_SQL_EVENTOS = text(
    """
    WITH codigos AS (
        SELECT s.codigo
          FROM saidas s
         WHERE s.sub_base = :sb AND s.codigo IS NOT NULL
           AND s.data BETWEEN :ini AND :fim
        UNION
        SELECT s.codigo
          FROM coletas c
          JOIN saidas s ON s.id_coleta = c.id_coleta
         WHERE c.sub_base = :sb AND s.sub_base = :sb AND s.codigo IS NOT NULL
           AND c.timestamp >= :t0 AND c.timestamp < :t1
           AND COALESCE(c.origem, 'codigo') <> 'manual'
        UNION
        SELECT s.codigo
          FROM saida_historico h
          JOIN saidas s ON s.id_saida = h.id_saida
         WHERE h.evento = 'entrada_base'
           AND h.timestamp >= :t0 AND h.timestamp < :t1
           AND s.sub_base = :sb AND s.codigo IS NOT NULL
    )
    SELECT s.id_saida, s.codigo, s.servico, s.status, s.base, s.data,
           s.id_coleta, s.avulso_lote_id, s.is_grande,
           CASE WHEN c.id_coleta IS NOT NULL AND COALESCE(c.origem, 'codigo') <> 'manual'
                THEN c.timestamp END AS coleta_ts,
           (SELECT MIN(h.timestamp) FROM saida_historico h
             WHERE h.id_saida = s.id_saida AND h.evento = 'entrada_base') AS entrada_ts
      FROM saidas s
      LEFT JOIN coletas c ON c.id_coleta = s.id_coleta
     WHERE s.sub_base = :sb
       AND s.codigo IN (SELECT codigo FROM codigos)
    """
    # Pedido sincronizado (ML/Shopee) nunca coletado não é cobrado do owner.
    + "   AND NOT " + SQL_SINCRONIZADO_NAO_COLETADO + "\n"
)

_SQL_MANUAIS = text(
    """
    SELECT c.id_coleta, c.base, c.timestamp, c.shopee, c.mercado_livre, c.avulso,
           COALESCE(p.versao, 1) AS versao
      FROM coletas c
      LEFT JOIN coleta_execucao_participantes p ON p.id_participante = c.participante_id
     WHERE c.sub_base = :sb AND c.origem = 'manual'
       AND c.timestamp >= :t0 AND c.timestamp < :t1
    """
)

_SQL_REMOCOES = text(
    """
    SELECT id_saida, codigo, servico, base, data_operacao
      FROM coleta_leitura_remocoes
     WHERE sub_base = :sb AND data_operacao BETWEEN :ini AND :fim
    """
)

_SQL_LOTES = text(
    """
    SELECT l.id, l.quantidade,
           (SELECT COUNT(*) FROM saidas s WHERE s.avulso_lote_id = l.id AND s.sub_base = :sb) AS codigos
      FROM avulso_lote l
     WHERE l.sub_base = :sb AND l.id = ANY(:ids)
    """
)

_SQL_LEDGER = text(
    """
    SELECT COUNT(*) FROM owner_cobranca_itens
     WHERE sub_base = :sb AND NOT cancelado
       AND timestamp >= :t0 AND timestamp < :t1
    """
)


def _to_date(v: Any) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    return v


def carregar_eventos(db: Session, sub_base: str, ini: date, fim: date) -> List[EventoCodigo]:
    t0, t1 = _bounds(ini, fim)
    rows = db.execute(_SQL_EVENTOS, {"sb": sub_base, "ini": ini, "fim": fim, "t0": t0, "t1": t1}).all()
    return [
        EventoCodigo(
            id_saida=int(r.id_saida),
            codigo=r.codigo or "",
            servico=r.servico,
            status=r.status,
            base=r.base,
            data_saida=_to_date(r.data),
            data_coleta=_to_date(r.coleta_ts),
            id_coleta=int(r.id_coleta) if r.id_coleta is not None and r.coleta_ts is not None else None,
            data_entrada=_to_date(r.entrada_ts),
            avulso_lote_id=int(r.avulso_lote_id) if r.avulso_lote_id else None,
            is_grande=bool(r.is_grande),
        )
        for r in rows
    ]


def carregar_manuais(db: Session, sub_base: str, ini: date, fim: date) -> List[LancamentoManual]:
    t0, t1 = _bounds(ini, fim)
    out: List[LancamentoManual] = []
    for r in db.execute(_SQL_MANUAIS, {"sb": sub_base, "t0": t0, "t1": t1}).all():
        for servico, qtd in (("shopee", r.shopee), ("mercado_livre", r.mercado_livre), ("avulso", r.avulso)):
            if int(qtd or 0) > 0:
                out.append(
                    LancamentoManual(
                        id_coleta=int(r.id_coleta),
                        servico=servico,
                        base=r.base or "",
                        data=_to_date(r.timestamp),
                        quantidade=int(qtd),
                        versao_participante=int(r.versao or 1),
                    )
                )
    return out


def carregar_remocoes(db: Session, sub_base: str, ini: date, fim: date) -> List[Remocao]:
    return [
        Remocao(
            id_saida=int(r.id_saida),
            codigo=r.codigo or "",
            servico=r.servico,
            base=r.base,
            data=r.data_operacao,
        )
        for r in db.execute(_SQL_REMOCOES, {"sb": sub_base, "ini": ini, "fim": fim}).all()
    ]


def carregar_lotes(db: Session, sub_base: str, ids: Iterable[int]) -> Dict[int, LoteAvulso]:
    lista = sorted({int(i) for i in ids if i})
    if not lista:
        return {}
    return {
        int(r.id): LoteAvulso(id=int(r.id), quantidade_declarada=int(r.quantidade or 0), codigos_existentes=int(r.codigos or 0))
        for r in db.execute(_SQL_LOTES, {"sb": sub_base, "ids": lista}).all()
    }


def codigos_ja_faturados(db: Session, sub_base: str, codigos: Iterable[str]) -> List[str]:
    lista = sorted({normalizar_codigo(c) for c in codigos if c})
    if not lista:
        return []
    return list(
        db.scalars(
            select(OwnerUnidadeFaturada.codigo).where(
                OwnerUnidadeFaturada.sub_base == sub_base,
                OwnerUnidadeFaturada.ativo.is_(True),
                OwnerUnidadeFaturada.codigo.in_(lista),
            )
        ).all()
    )


def decisoes_ativas(db: Session, owner_id: int, ini: date, fim: date) -> List[OwnerApuracaoDecisao]:
    return list(
        db.scalars(
            select(OwnerApuracaoDecisao)
            .where(
                OwnerApuracaoDecisao.owner_id == owner_id,
                OwnerApuracaoDecisao.periodo_inicio == ini,
                OwnerApuracaoDecisao.periodo_fim == fim,
                OwnerApuracaoDecisao.ativo.is_(True),
            )
            .order_by(OwnerApuracaoDecisao.id)
        ).all()
    )


def ajustes_pendentes(db: Session, owner_id: int, ini: date, fim: date) -> List[OwnerFechamentoAjuste]:
    """Ajustes ainda não aplicados: os nascidos de fechamentos anteriores a este período
    e os lançados para este mesmo período."""
    rows = db.execute(
        select(OwnerFechamentoAjuste, OwnerFechamento.periodo_fim)
        .outerjoin(OwnerFechamento, OwnerFechamento.id == OwnerFechamentoAjuste.fechamento_origem_id)
        .where(
            OwnerFechamentoAjuste.owner_id == owner_id,
            OwnerFechamentoAjuste.fechamento_aplicado_id.is_(None),
            OwnerFechamentoAjuste.removido_em.is_(None),
        )
        .order_by(OwnerFechamentoAjuste.id)
    ).all()
    saida: List[OwnerFechamentoAjuste] = []
    for a, origem_fim in rows:
        if a.periodo_inicio is not None:
            if a.periodo_inicio == ini and a.periodo_fim == fim:
                saida.append(a)
        elif origem_fim is None or origem_fim < ini:
            saida.append(a)
    return saida


def ajuste_para_engine(a: OwnerFechamentoAjuste) -> Ajuste:
    return Ajuste(
        id=a.id,
        tipo=a.tipo,
        valor=Decimal(a.valor),
        motivo=a.motivo,
        escopo=ESCOPO_PERIODO if a.periodo_inicio is not None else ESCOPO_CICLO,
        percentual=Decimal(a.percentual) if a.percentual is not None else None,
    )


def ajustes_do_periodo(db: Session, owner_id: int, ini: date, fim: date) -> List[OwnerFechamentoAjuste]:
    """Ajustes lançados para este período (ativos), aplicados ou não."""
    return list(
        db.scalars(
            select(OwnerFechamentoAjuste)
            .where(
                OwnerFechamentoAjuste.owner_id == owner_id,
                OwnerFechamentoAjuste.periodo_inicio == ini,
                OwnerFechamentoAjuste.periodo_fim == fim,
                OwnerFechamentoAjuste.removido_em.is_(None),
            )
            .order_by(OwnerFechamentoAjuste.id)
        ).all()
    )


def fechamento_vigente(db: Session, owner_id: int, ini: date, fim: date) -> Optional[OwnerFechamento]:
    return db.scalar(
        select(OwnerFechamento).where(
            OwnerFechamento.owner_id == owner_id,
            OwnerFechamento.periodo_inicio == ini,
            OwnerFechamento.periodo_fim == fim,
            OwnerFechamento.status.in_(STATUS_VIGENTES),
        )
    )


def fechamentos_sobrepostos(db: Session, owner_id: int, ini: date, fim: date) -> List[OwnerFechamento]:
    return [
        f
        for f in db.scalars(
            select(OwnerFechamento).where(
                OwnerFechamento.owner_id == owner_id,
                OwnerFechamento.status.in_(STATUS_VIGENTES),
                OwnerFechamento.periodo_inicio <= fim,
                OwnerFechamento.periodo_fim >= ini,
            )
        ).all()
        if (f.periodo_inicio, f.periodo_fim) != (ini, fim)
    ]


def versoes(db: Session, owner_id: int, ini: date, fim: date) -> List[OwnerFechamento]:
    return list(
        db.scalars(
            select(OwnerFechamento)
            .where(
                OwnerFechamento.owner_id == owner_id,
                OwnerFechamento.periodo_inicio == ini,
                OwnerFechamento.periodo_fim == fim,
            )
            .order_by(OwnerFechamento.versao.desc())
        ).all()
    )


# ---------------------------------------------------------------------------
# Prévia
# ---------------------------------------------------------------------------


@dataclass
class ApuracaoOwner:
    owner: Owner
    config: OwnerConfiguracaoCobranca
    periodo_inicio: date
    periodo_fim: date
    preco: Optional[OwnerPrecoVigencia]
    status: str
    apuracao: Optional[Apuracao] = None
    fechamento: Optional[OwnerFechamento] = None
    conferencia: Optional[OwnerApuracao] = None
    divergencias: List[Divergencia] = field(default_factory=list)
    fechavel: bool = False
    motivo_bloqueio_fechar: Optional[str] = None

    @property
    def bloqueantes(self) -> List[Divergencia]:
        return [d for d in self.divergencias if d.nivel == NIVEL_BLOQUEANTE]

    @property
    def pode_conferir(self) -> bool:
        return self.status in ("em_apuracao", "conferido") and not self.bloqueantes

    @property
    def pode_fechar(self) -> bool:
        return self.status == "conferido" and not self.bloqueantes and self.motivo_bloqueio_fechar is None

    @property
    def totais(self) -> Dict[str, Any]:
        if self.fechamento is not None:
            f = self.fechamento
            return {
                "bruto": int(f.qtd_bruto),
                "cancelados": int(f.qtd_cancelados),
                "faturavel": int(f.qtd_faturavel),
                "desconsiderados": int(f.qtd_desconsiderados),
                "valor_pacotes": Decimal(f.valor_pacotes),
                "valor_ajustes": Decimal(f.valor_ajustes),
                "valor_total": Decimal(f.valor_total),
                "preco_unitario": Decimal(f.preco_unitario),
            }
        a = self.apuracao
        return {
            "bruto": a.qtd_bruto,
            "cancelados": a.qtd_cancelados,
            "faturavel": a.qtd_faturavel,
            "desconsiderados": a.qtd_desconsiderados,
            "valor_pacotes": a.valor_pacotes,
            "valor_ajustes": a.valor_ajustes,
            "valor_total": a.valor_total,
            "preco_unitario": a.preco if self.preco is not None else None,
        }


def _ambos_modos(owner: Owner) -> bool:
    return (owner.modo_operacao or "codigo") == "ambos"


def apurar_owner(
    db: Session,
    owner: Owner,
    ini: date,
    fim: date,
    *,
    hoje: Optional[date] = None,
    usar_snapshot: bool = True,
) -> ApuracaoOwner:
    """Snapshot se o período estiver fechado; senão, prévia ao vivo."""
    hoje = hoje or date.today()
    cfg = obter_config(db, owner)
    precos = listar_precos(db, owner.id_owner)
    preco = preco_vigente(precos, ini)

    if usar_snapshot:
        fech = fechamento_vigente(db, owner.id_owner, ini, fim)
        if fech is not None:
            return ApuracaoOwner(
                owner=owner,
                config=cfg,
                periodo_inicio=ini,
                periodo_fim=fim,
                preco=preco,
                status=fech.status,
                fechamento=fech,
                fechavel=True,
            )

    sub_base = owner.sub_base
    eventos = carregar_eventos(db, sub_base, ini, fim)
    manuais = carregar_manuais(db, sub_base, ini, fim)
    remocoes = carregar_remocoes(db, sub_base, ini, fim)
    lotes = carregar_lotes(db, sub_base, (e.avulso_lote_id for e in eventos))
    faturados = codigos_ja_faturados(db, sub_base, (e.codigo for e in eventos))
    decisoes = decisoes_ativas(db, owner.id_owner, ini, fim)
    ajustes = ajustes_pendentes(db, owner.id_owner, ini, fim)

    ap = apurar(
        modelo=cfg.modelo,
        periodo_inicio=ini,
        periodo_fim=fim,
        preco=Decimal(preco.valor) if preco is not None else None,
        eventos=eventos,
        manuais=manuais,
        decisoes=[
            Decisao(
                tipo=d.tipo,
                id_coleta=int(d.id_coleta),
                servico=d.servico,
                codigos=tuple(d.codigos or ()),
                quantidade_ajustada=d.quantidade_ajustada,
                id=d.id,
            )
            for d in decisoes
        ],
        remocoes=remocoes,
        codigos_ja_faturados=faturados,
        ajustes=[ajuste_para_engine(a) for a in ajustes],
        lotes_avulso=lotes,
    )

    divergencias: List[Divergencia] = []
    if cfg.status != "confirmado":
        divergencias.append(
            Divergencia(
                NIVEL_BLOQUEANTE,
                "sem_modelo_confirmado",
                "Modelo comercial ainda não confirmado. Confirme em Configuração comercial.",
            )
        )
    if preco is None:
        divergencias.append(
            Divergencia(NIVEL_BLOQUEANTE, "sem_preco", "Sem preço vigente no início do período.")
        )
    for f in fechamentos_sobrepostos(db, owner.id_owner, ini, fim):
        divergencias.append(
            Divergencia(
                NIVEL_BLOQUEANTE,
                "periodo_sobreposto",
                f"Este período cruza o fechamento de {f.periodo_inicio.strftime('%d/%m/%Y')} a "
                f"{f.periodo_fim.strftime('%d/%m/%Y')}.",
                {"fechamento_id": f.id},
            )
        )
    divergencias.extend(ap.divergencias)

    t0, t1 = _bounds(ini, fim)
    ledger = int(db.execute(_SQL_LEDGER, {"sb": sub_base, "t0": t0, "t1": t1}).scalar() or 0)
    coleta_cod = sum(1 for it in ap.itens if it.no_bruto and it.tipo == "codigo" and it.origem == "coleta")
    if ledger and ledger != coleta_cod:
        divergencias.append(
            Divergencia(
                NIVEL_INFORMATIVO,
                "ledger_diferente",
                f"O registro antigo de cobrança tem {ledger} itens no período; a apuração tem "
                f"{coleta_cod} coletas identificadas. A apuração é a fonte oficial.",
                {"ledger": ledger, "apuracao": coleta_cod},
            )
        )

    conferencia = db.scalar(
        select(OwnerApuracao).where(
            OwnerApuracao.owner_id == owner.id_owner,
            OwnerApuracao.periodo_inicio == ini,
            OwnerApuracao.periodo_fim == fim,
        )
    )
    status = "em_apuracao"
    if conferencia is not None:
        if conferencia.hash == ap.hash:
            status = "conferido"
        else:
            divergencias.append(
                Divergencia(
                    NIVEL_ATENCAO,
                    "hash_alterado",
                    "A operação mudou depois da conferência. Confira de novo antes de fechar.",
                )
            )

    fechavel = periodo_fechavel(ini, fim, cfg.periodicidade)
    motivo_fechar: Optional[str] = None
    if not fechavel:
        rotulo = "a quinzena" if cfg.periodicidade == "quinzenal" else "o mês"
        motivo_fechar = f"Período personalizado só consulta. Para fechar, escolha {rotulo} do Owner."
    elif fim >= hoje:
        motivo_fechar = "O período ainda não terminou. Feche a partir do dia seguinte ao fim."

    return ApuracaoOwner(
        owner=owner,
        config=cfg,
        periodo_inicio=ini,
        periodo_fim=fim,
        preco=preco,
        status=status,
        apuracao=ap,
        conferencia=conferencia,
        divergencias=divergencias,
        fechavel=fechavel,
        motivo_bloqueio_fechar=motivo_fechar,
    )


# ---------------------------------------------------------------------------
# Decisões
# ---------------------------------------------------------------------------


def registrar_decisao(
    db: Session,
    owner: Owner,
    user: User,
    *,
    ini: date,
    fim: date,
    tipo: str,
    id_coleta: Optional[int],
    servico: Optional[str],
    motivo: str,
    codigos: Sequence[str] = (),
    quantidade_ajustada: Optional[int] = None,
    aplicar_todos_ambiguos: bool = False,
) -> List[OwnerApuracaoDecisao]:
    if tipo not in DECISAO_TIPOS:
        raise HTTPException(422, "Tipo de decisão inválido.")
    motivo = (motivo or "").strip()
    if len(motivo) < 5:
        raise HTTPException(422, "Explique o motivo da decisão (mínimo 5 caracteres).")
    if fechamento_vigente(db, owner.id_owner, ini, fim) is not None:
        raise HTTPException(409, "Período já fechado. Use ajuste no próximo ciclo.")

    atual = apurar_owner(db, owner, ini, fim, usar_snapshot=False)
    ambiguos = [d.referencia for d in atual.divergencias if d.codigo == "ambiguidade_manual"]

    alvos: List[Tuple[int, str]] = []
    if aplicar_todos_ambiguos:
        if tipo != "volumes_distintos":
            raise HTTPException(422, "Só a confirmação de volumes distintos pode cobrir vários dias.")
        alvos = [(int(r["id_coleta"]), str(r["servico"])) for r in ambiguos]
        if not alvos:
            raise HTTPException(409, "Não há dias ambíguos neste período.")
    else:
        if not id_coleta or not servico:
            raise HTTPException(422, "Informe o lançamento manual (id_coleta e serviço).")
        manuais = {(m.id_coleta, m.servico): m for m in carregar_manuais(db, owner.sub_base, ini, fim)}
        manual = manuais.get((int(id_coleta), servico))
        if manual is None:
            raise HTTPException(404, "Lançamento manual não encontrado neste Owner e período.")
        alvos = [(int(id_coleta), servico)]
        if tipo == "vinculo_codigos":
            cods = sorted({normalizar_codigo(c) for c in codigos if normalizar_codigo(c)})
            if not cods:
                raise HTTPException(422, "Informe os códigos a vincular.")
            if len(cods) > manual.quantidade:
                raise HTTPException(422, "Há mais códigos do que a quantidade manual do lançamento.")
            no_periodo = {
                it.codigo
                for it in atual.apuracao.itens
                if it.tipo == "codigo" and (it.no_bruto or it.status_financeiro == "reconciliado")
            }
            fora = [c for c in cods if c not in no_periodo]
            if fora:
                raise HTTPException(422, f"Códigos fora da apuração deste período: {', '.join(fora[:5])}.")
            codigos = cods
        if tipo == "ajuste_quantidade":
            if quantidade_ajustada is None or int(quantidade_ajustada) < 0:
                raise HTTPException(422, "Informe a quantidade ajustada.")

    criadas: List[OwnerApuracaoDecisao] = []
    for alvo_coleta, alvo_servico in alvos:
        dec = OwnerApuracaoDecisao(
            owner_id=owner.id_owner,
            sub_base=owner.sub_base,
            periodo_inicio=ini,
            periodo_fim=fim,
            tipo=tipo,
            id_coleta=alvo_coleta,
            servico=alvo_servico,
            codigos=list(codigos) if tipo == "vinculo_codigos" else None,
            quantidade_ajustada=int(quantidade_ajustada) if tipo == "ajuste_quantidade" else None,
            motivo=motivo,
            criado_por=user.id,
            criado_por_username=user.username,
        )
        db.add(dec)
        criadas.append(dec)
    db.flush()
    registrar_historico(
        db,
        owner_id=owner.id_owner,
        ini=ini,
        fim=fim,
        acao="decisao",
        user=user,
        motivo=motivo,
        detalhes={"tipo": tipo, "alvos": [{"id_coleta": c, "servico": s} for c, s in alvos]},
    )
    return criadas


def revogar_decisao(db: Session, owner: Owner, user: User, decisao_id: int) -> OwnerApuracaoDecisao:
    dec = db.get(OwnerApuracaoDecisao, int(decisao_id))
    if dec is None or dec.owner_id != owner.id_owner or not dec.ativo:
        raise HTTPException(404, "Decisão não encontrada.")
    if fechamento_vigente(db, owner.id_owner, dec.periodo_inicio, dec.periodo_fim) is not None:
        raise HTTPException(409, "Período já fechado.")
    dec.ativo = False
    dec.revogado_por = user.id
    dec.revogado_em = datetime.now()
    registrar_historico(
        db,
        owner_id=owner.id_owner,
        ini=dec.periodo_inicio,
        fim=dec.periodo_fim,
        acao="decisao_revogada",
        user=user,
        detalhes={"decisao_id": dec.id},
    )
    db.flush()
    return dec


# ---------------------------------------------------------------------------
# Histórico
# ---------------------------------------------------------------------------


def registrar_historico(
    db: Session,
    *,
    owner_id: int,
    ini: date,
    fim: date,
    acao: str,
    user: User,
    fechamento_id: Optional[int] = None,
    status_anterior: Optional[str] = None,
    status_novo: Optional[str] = None,
    motivo: Optional[str] = None,
    detalhes: Optional[Dict[str, Any]] = None,
) -> None:
    db.add(
        OwnerFechamentoHistorico(
            owner_id=owner_id,
            periodo_inicio=ini,
            periodo_fim=fim,
            fechamento_id=fechamento_id,
            acao=acao,
            status_anterior=status_anterior,
            status_novo=status_novo,
            motivo=motivo,
            detalhes=detalhes,
            user_id=user.id,
            username=user.username,
        )
    )


def historico(db: Session, owner_id: int, ini: date, fim: date) -> List[OwnerFechamentoHistorico]:
    return list(
        db.scalars(
            select(OwnerFechamentoHistorico)
            .where(
                OwnerFechamentoHistorico.owner_id == owner_id,
                OwnerFechamentoHistorico.periodo_inicio == ini,
                OwnerFechamentoHistorico.periodo_fim == fim,
            )
            .order_by(OwnerFechamentoHistorico.criado_em.desc(), OwnerFechamentoHistorico.id.desc())
        ).all()
    )


# ---------------------------------------------------------------------------
# Conferir e fechar
# ---------------------------------------------------------------------------


def _travar_periodo(db: Session, owner_id: int, ini: date, fim: date) -> None:
    """Trava transacional por Owner e período: segundo fechar concorrente espera e recebe conflito."""
    chave = f"owner_cobranca:{int(owner_id)}:{ini.isoformat()}:{fim.isoformat()}"
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": chave})


def conferir(db: Session, owner: Owner, user: User, ini: date, fim: date) -> ApuracaoOwner:
    _travar_periodo(db, owner.id_owner, ini, fim)
    atual = apurar_owner(db, owner, ini, fim)
    if atual.fechamento is not None:
        raise HTTPException(409, "Período já fechado.")
    if atual.bloqueantes:
        raise HTTPException(409, atual.bloqueantes[0].mensagem)
    conf = atual.conferencia
    if conf is None:
        conf = OwnerApuracao(
            owner_id=owner.id_owner,
            sub_base=owner.sub_base,
            periodo_inicio=ini,
            periodo_fim=fim,
            hash=atual.apuracao.hash,
        )
        db.add(conf)
    conf.hash = atual.apuracao.hash
    conf.conferido_por = user.id
    conf.conferido_por_username = user.username
    conf.conferido_em = datetime.now()
    registrar_historico(
        db,
        owner_id=owner.id_owner,
        ini=ini,
        fim=fim,
        acao="conferir",
        user=user,
        status_anterior=atual.status,
        status_novo="conferido",
        detalhes={"hash": atual.apuracao.hash, "valor_total": str(atual.apuracao.valor_total)},
    )
    db.flush()
    atual.status = "conferido"
    atual.conferencia = conf
    atual.divergencias = [d for d in atual.divergencias if d.codigo != "hash_alterado"]
    return atual


def _seller_dados(db: Session, owner_id: int) -> Optional[BaseSellerDados]:
    """Cadastro de CNPJ/endereço do Owner; prefere o registro geral (sem base vinculada)."""
    return db.scalar(
        select(BaseSellerDados)
        .where(BaseSellerDados.owner_id == owner_id)
        .order_by(BaseSellerDados.base_id.is_not(None), BaseSellerDados.id_seller)
        .limit(1)
    )


def _txt(v: Any) -> Optional[str]:
    s = str(v or "").strip()
    return s or None


def dados_cadastro(db: Session, owner: Owner, cfg: OwnerConfiguracaoCobranca) -> Dict[str, Any]:
    """Dados do Owner para o demonstrativo, lidos do cadastro. Campo sem cadastro fica vazio."""
    from base import _format_endereco_seller

    seller = _seller_dados(db, owner.id_owner)
    endereco = _txt(_format_endereco_seller(seller)) if seller is not None else None
    return {
        "id_owner": owner.id_owner,
        "nome": nome_owner(owner),
        "username": owner.username,
        "sub_base": owner.sub_base,
        "email": _txt(owner.email),
        "contato": _txt(owner.contato),
        "razao_social": _txt(cfg.razao_social),
        "documento": _txt(seller.cnpj if seller is not None else None) or _txt(cfg.documento),
        "telefone": _txt(owner.contato) or _txt(cfg.telefone),
        "endereco": endereco or _txt(cfg.endereco),
        "cidade": _txt(seller.cidade) if seller is not None else None,
        "estado": _txt(seller.estado) if seller is not None else None,
        "contato_financeiro": _txt(cfg.contato_financeiro),
    }


def _owner_snapshot(db: Session, owner: Owner, cfg: OwnerConfiguracaoCobranca) -> Dict[str, Any]:
    return dados_cadastro(db, owner, cfg)


def fechar(
    db: Session,
    owner: Owner,
    user: User,
    ini: date,
    fim: date,
    *,
    hash_confirmado: str,
    observacao: Optional[str] = None,
) -> OwnerFechamento:
    _travar_periodo(db, owner.id_owner, ini, fim)
    if fechamento_vigente(db, owner.id_owner, ini, fim) is not None:
        raise HTTPException(409, "Este período já foi fechado.")
    atual = apurar_owner(db, owner, ini, fim, usar_snapshot=False)
    ap = atual.apuracao
    if atual.bloqueantes:
        raise HTTPException(409, atual.bloqueantes[0].mensagem)
    if atual.motivo_bloqueio_fechar:
        raise HTTPException(409, atual.motivo_bloqueio_fechar)
    if atual.status != "conferido":
        raise HTTPException(409, "Confira o período antes de fechar.")
    if not hash_confirmado or hash_confirmado != ap.hash:
        raise HTTPException(409, "Os números mudaram desde a conferência. Revise e confira de novo.")

    cfg = atual.config
    versao_atual = db.scalar(
        select(func.max(OwnerFechamento.versao)).where(
            OwnerFechamento.owner_id == owner.id_owner,
            OwnerFechamento.periodo_inicio == ini,
            OwnerFechamento.periodo_fim == fim,
        )
    )
    fech = OwnerFechamento(
        owner_id=owner.id_owner,
        sub_base=owner.sub_base,
        periodo_inicio=ini,
        periodo_fim=fim,
        versao=int(versao_atual or 0) + 1,
        status="fechado",
        modelo=cfg.modelo,
        periodicidade=cfg.periodicidade,
        preco_unitario=ap.preco,
        qtd_bruto=ap.qtd_bruto,
        qtd_cancelados=ap.qtd_cancelados,
        qtd_faturavel=ap.qtd_faturavel,
        qtd_desconsiderados=ap.qtd_desconsiderados,
        valor_pacotes=ap.valor_pacotes,
        valor_ajustes=ap.valor_ajustes,
        valor_total=ap.valor_total,
        hash=ap.hash,
        owner_snapshot=_owner_snapshot(db, owner, cfg),
        config_snapshot={
            "modelo": cfg.modelo,
            "periodicidade": cfg.periodicidade,
            "status": cfg.status,
            "preco_vigencia_id": atual.preco.id if atual.preco else None,
            "decisoes": [
                {"id": d.id, "tipo": d.tipo, "id_coleta": d.id_coleta, "servico": d.servico, "motivo": d.motivo}
                for d in decisoes_ativas(db, owner.id_owner, ini, fim)
            ],
            "divergencias": [
                {"nivel": d.nivel, "codigo": d.codigo, "mensagem": d.mensagem} for d in atual.divergencias
            ],
        },
        observacao=(observacao or "").strip() or None,
        fechado_por=user.id,
        fechado_por_username=user.username,
        fechado_em=datetime.now(),
    )
    db.add(fech)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Este período já foi fechado por outra pessoa.")

    if ap.composicao:
        db.execute(
            insert(OwnerFechamentoComposicao),
            [
                {
                    "fechamento_id": fech.id,
                    "classificacao": c.classificacao,
                    "quantidade": c.quantidade,
                    "valor": c.valor,
                    "ordem": i,
                }
                for i, c in enumerate(ap.composicao)
            ],
        )
    itens_payload = [
        {
            "fechamento_id": fech.id,
            "tipo": it.tipo,
            "codigo": it.codigo,
            "id_saida": it.id_saida,
            "id_coleta": it.id_coleta,
            "avulso_lote_id": it.avulso_lote_id,
            "servico": it.servico,
            "base": it.base,
            "data_competencia": it.data_competencia,
            "origem": it.origem,
            "status_financeiro": it.status_financeiro,
            "motivo": it.motivo,
            "quantidade": it.quantidade,
            "is_grande": it.is_grande,
            "status_operacional": it.status_operacional,
        }
        for it in ap.itens
    ]
    for i in range(0, len(itens_payload), 5000):
        db.execute(insert(OwnerFechamentoItem), itens_payload[i : i + 5000])

    unidades = [
        {"fechamento_id": fech.id, "owner_id": owner.id_owner, "sub_base": owner.sub_base, "codigo": it.codigo}
        for it in ap.itens
        if it.tipo == "codigo" and it.no_bruto and it.codigo
    ]
    try:
        for i in range(0, len(unidades), 5000):
            db.execute(insert(OwnerUnidadeFaturada), unidades[i : i + 5000])
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Há código já cobrado em outro fechamento. Recarregue a prévia.")

    if ap.ajustes:
        db.execute(
            update(OwnerFechamentoAjuste)
            .where(
                OwnerFechamentoAjuste.id.in_([a.id for a in ap.ajustes]),
                OwnerFechamentoAjuste.fechamento_aplicado_id.is_(None),
            )
            .values(fechamento_aplicado_id=fech.id)
        )
        for a in ap.ajustes:
            if a.escopo == ESCOPO_PERIODO:
                db.execute(
                    update(OwnerFechamentoAjuste)
                    .where(OwnerFechamentoAjuste.id == a.id)
                    .values(valor=ap.valores_ajustes.get(a.id, a.valor))
                )
    db.query(OwnerApuracao).filter(
        OwnerApuracao.owner_id == owner.id_owner,
        OwnerApuracao.periodo_inicio == ini,
        OwnerApuracao.periodo_fim == fim,
    ).delete(synchronize_session=False)
    registrar_historico(
        db,
        owner_id=owner.id_owner,
        ini=ini,
        fim=fim,
        acao="fechar",
        user=user,
        fechamento_id=fech.id,
        status_anterior="conferido",
        status_novo="fechado",
        detalhes={"versao": fech.versao, "valor_total": str(fech.valor_total), "hash": fech.hash},
    )
    db.flush()
    return fech


# ---------------------------------------------------------------------------
# Depois do fechamento
# ---------------------------------------------------------------------------


def carregar_fechamento(db: Session, fechamento_id: int) -> Tuple[OwnerFechamento, Owner]:
    fech = db.get(OwnerFechamento, int(fechamento_id))
    if fech is None:
        raise HTTPException(404, "Fechamento não encontrado.")
    owner = carregar_owner(db, fech.owner_id)
    return fech, owner


def informar(db: Session, fech: OwnerFechamento, user: User) -> OwnerFechamento:
    if fech.status != "fechado":
        raise HTTPException(409, "Só um período fechado pode ser marcado como informado.")
    fech.status = "informado"
    fech.informado_por = user.id
    fech.informado_em = datetime.now()
    registrar_historico(
        db,
        owner_id=fech.owner_id,
        ini=fech.periodo_inicio,
        fim=fech.periodo_fim,
        acao="informar",
        user=user,
        fechamento_id=fech.id,
        status_anterior="fechado",
        status_novo="informado",
    )
    db.flush()
    return fech


def _desfazer_vigencia(db: Session, fech: OwnerFechamento) -> None:
    db.execute(
        update(OwnerUnidadeFaturada)
        .where(OwnerUnidadeFaturada.fechamento_id == fech.id)
        .values(ativo=False)
    )
    db.execute(
        update(OwnerFechamentoAjuste)
        .where(OwnerFechamentoAjuste.fechamento_aplicado_id == fech.id)
        .values(fechamento_aplicado_id=None)
    )


def reabrir(db: Session, fech: OwnerFechamento, user: User, motivo: str) -> OwnerFechamento:
    motivo = (motivo or "").strip()
    if len(motivo) < 5:
        raise HTTPException(422, "Explique o motivo da reabertura (mínimo 5 caracteres).")
    _travar_periodo(db, fech.owner_id, fech.periodo_inicio, fech.periodo_fim)
    if fech.status not in STATUS_VIGENTES:
        raise HTTPException(409, "Só um período fechado ou informado pode ser reaberto.")
    anterior = fech.status
    fech.status = "reaberto"
    fech.reaberto_por = user.id
    fech.reaberto_em = datetime.now()
    fech.reaberto_motivo = motivo
    _desfazer_vigencia(db, fech)
    registrar_historico(
        db,
        owner_id=fech.owner_id,
        ini=fech.periodo_inicio,
        fim=fech.periodo_fim,
        acao="reabrir",
        user=user,
        fechamento_id=fech.id,
        status_anterior=anterior,
        status_novo="reaberto",
        motivo=motivo,
    )
    db.flush()
    return fech


def anular(db: Session, fech: OwnerFechamento, user: User, motivo: str) -> OwnerFechamento:
    motivo = (motivo or "").strip()
    if len(motivo) < 5:
        raise HTTPException(422, "Explique o motivo da anulação (mínimo 5 caracteres).")
    _travar_periodo(db, fech.owner_id, fech.periodo_inicio, fech.periodo_fim)
    if fech.status not in STATUS_VIGENTES:
        raise HTTPException(409, "Só um período fechado ou informado pode ser anulado.")
    anterior = fech.status
    fech.status = "anulado"
    fech.anulado_por = user.id
    fech.anulado_em = datetime.now()
    fech.anulado_motivo = motivo
    _desfazer_vigencia(db, fech)
    registrar_historico(
        db,
        owner_id=fech.owner_id,
        ini=fech.periodo_inicio,
        fim=fech.periodo_fim,
        acao="anular",
        user=user,
        fechamento_id=fech.id,
        status_anterior=anterior,
        status_novo="anulado",
        motivo=motivo,
    )
    db.flush()
    return fech


def lancar_ajuste(
    db: Session,
    fech: OwnerFechamento,
    user: User,
    *,
    tipo: str,
    valor: Decimal,
    motivo: str,
    chave_idempotencia: str,
) -> Tuple[OwnerFechamentoAjuste, bool]:
    """Ajuste em dinheiro que entra no próximo período fechado. Retorna (ajuste, criado)."""
    if tipo not in AJUSTE_TIPOS:
        raise HTTPException(422, "Tipo de ajuste inválido.")
    motivo = (motivo or "").strip()
    if len(motivo) < 5:
        raise HTTPException(422, "Explique o motivo do ajuste (mínimo 5 caracteres).")
    chave = (chave_idempotencia or "").strip()
    if not chave or len(chave) > 120:
        raise HTTPException(422, "Chave de idempotência inválida.")
    try:
        valor_dec = Decimal(str(valor)).quantize(Decimal("0.01"))
    except Exception:
        raise HTTPException(422, "Valor inválido.")
    if valor_dec == 0:
        raise HTTPException(422, "O ajuste precisa de valor diferente de zero.")

    existente = db.scalar(
        select(OwnerFechamentoAjuste).where(OwnerFechamentoAjuste.chave_idempotencia == chave)
    )
    if existente is not None:
        if existente.fechamento_origem_id != fech.id:
            raise HTTPException(409, "Chave de ajuste já usada em outro fechamento.")
        return existente, False
    if fech.status not in STATUS_VIGENTES:
        raise HTTPException(409, "Ajuste só pode partir de um período fechado ou informado.")

    aj = OwnerFechamentoAjuste(
        owner_id=fech.owner_id,
        sub_base=fech.sub_base,
        fechamento_origem_id=fech.id,
        tipo=tipo,
        valor=valor_ajuste_assinado(tipo, valor_dec),
        motivo=motivo,
        chave_idempotencia=chave,
        criado_por=user.id,
        criado_por_username=user.username,
    )
    db.add(aj)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existente = db.scalar(
            select(OwnerFechamentoAjuste).where(OwnerFechamentoAjuste.chave_idempotencia == chave)
        )
        if existente is None:
            raise HTTPException(409, "Não foi possível registrar o ajuste.")
        return existente, False
    registrar_historico(
        db,
        owner_id=fech.owner_id,
        ini=fech.periodo_inicio,
        fim=fech.periodo_fim,
        acao="ajuste",
        user=user,
        fechamento_id=fech.id,
        motivo=motivo,
        detalhes={"tipo": tipo, "valor": str(aj.valor), "ajuste_id": aj.id},
    )
    db.flush()
    return aj, True


def lancar_ajuste_periodo(
    db: Session,
    owner: Owner,
    user: User,
    *,
    ini: date,
    fim: date,
    tipo: str,
    valor: Optional[Decimal],
    percentual: Optional[Decimal],
    motivo: str,
    chave_idempotencia: str,
) -> Tuple[OwnerFechamentoAjuste, bool]:
    """Desconto (R$ ou %), acréscimo ou isenção do próprio período, antes do fechamento."""
    if tipo not in AJUSTE_PERIODO_TIPOS:
        raise HTTPException(422, "Tipo de ajuste inválido.")
    motivo = (motivo or "").strip()
    if len(motivo) < 5:
        raise HTTPException(422, "Explique o motivo do ajuste (mínimo 5 caracteres).")
    chave = (chave_idempotencia or "").strip()
    if not chave or len(chave) > 120:
        raise HTTPException(422, "Chave de idempotência inválida.")

    existente = db.scalar(
        select(OwnerFechamentoAjuste).where(OwnerFechamentoAjuste.chave_idempotencia == chave)
    )
    if existente is not None:
        if (
            existente.owner_id != owner.id_owner
            or existente.periodo_inicio != ini
            or existente.periodo_fim != fim
        ):
            raise HTTPException(409, "Chave de ajuste já usada em outro período.")
        return existente, False

    _travar_periodo(db, owner.id_owner, ini, fim)
    if fechamento_vigente(db, owner.id_owner, ini, fim) is not None:
        raise HTTPException(409, "Período já fechado. Use ajuste no próximo ciclo ou reabra o período.")

    pct: Optional[Decimal] = None
    valor_dec = Decimal("0.00")
    if tipo == "isencao":
        if any(a.tipo == "isencao" for a in ajustes_do_periodo(db, owner.id_owner, ini, fim)):
            raise HTTPException(409, "Este período já está isento.")
    elif tipo == "desconto" and percentual is not None:
        try:
            pct = Decimal(str(percentual)).quantize(Decimal("0.01"))
        except Exception:
            raise HTTPException(422, "Percentual inválido.")
        if pct <= 0 or pct > 100:
            raise HTTPException(422, "O percentual precisa estar entre 0,01 e 100.")
    else:
        try:
            valor_dec = Decimal(str(valor)).quantize(Decimal("0.01"))
        except Exception:
            raise HTTPException(422, "Valor inválido.")
        if valor_dec <= 0:
            raise HTTPException(422, "Informe um valor maior que zero.")

    atual = apurar_owner(db, owner, ini, fim, usar_snapshot=False)
    novo = Ajuste(
        id=-1,
        tipo=tipo,
        valor=valor_dec,
        escopo=ESCOPO_PERIODO,
        percentual=pct,
    )
    simulado = calcular_ajustes(atual.apuracao.valor_pacotes, list(atual.apuracao.ajustes) + [novo])
    total = atual.apuracao.valor_pacotes + sum(simulado.values(), start=Decimal("0"))
    if total < 0:
        raise HTTPException(422, "Esse desconto deixaria o total negativo.")

    aj = OwnerFechamentoAjuste(
        owner_id=owner.id_owner,
        sub_base=owner.sub_base,
        periodo_inicio=ini,
        periodo_fim=fim,
        tipo=tipo,
        valor=valor_ajuste_assinado(tipo, valor_dec) if tipo != "isencao" else Decimal("0.00"),
        percentual=pct,
        motivo=motivo,
        chave_idempotencia=chave,
        criado_por=user.id,
        criado_por_username=user.username,
    )
    db.add(aj)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existente = db.scalar(
            select(OwnerFechamentoAjuste).where(OwnerFechamentoAjuste.chave_idempotencia == chave)
        )
        if existente is None:
            raise HTTPException(409, "Não foi possível registrar o ajuste.")
        return existente, False
    registrar_historico(
        db,
        owner_id=owner.id_owner,
        ini=ini,
        fim=fim,
        acao="ajuste_periodo",
        user=user,
        motivo=motivo,
        detalhes={
            "tipo": tipo,
            "valor": str(aj.valor),
            "percentual": str(pct) if pct is not None else None,
            "ajuste_id": aj.id,
        },
    )
    db.flush()
    return aj, True


def remover_ajuste_periodo(db: Session, owner: Owner, user: User, ajuste_id: int) -> OwnerFechamentoAjuste:
    aj = db.get(OwnerFechamentoAjuste, int(ajuste_id))
    if (
        aj is None
        or aj.owner_id != owner.id_owner
        or aj.periodo_inicio is None
        or aj.removido_em is not None
    ):
        raise HTTPException(404, "Ajuste não encontrado.")
    _travar_periodo(db, owner.id_owner, aj.periodo_inicio, aj.periodo_fim)
    if aj.fechamento_aplicado_id is not None or fechamento_vigente(
        db, owner.id_owner, aj.periodo_inicio, aj.periodo_fim
    ) is not None:
        raise HTTPException(409, "Período já fechado. Reabra o período para remover o ajuste.")
    aj.removido_em = datetime.now()
    aj.removido_por_username = user.username
    registrar_historico(
        db,
        owner_id=owner.id_owner,
        ini=aj.periodo_inicio,
        fim=aj.periodo_fim,
        acao="ajuste_removido",
        user=user,
        detalhes={"ajuste_id": aj.id, "tipo": aj.tipo},
    )
    db.flush()
    return aj


def _normalizar_status():
    from saidas_routes import normalizar_status_saida

    return normalizar_status_saida


def situacao_preview(ap: Apuracao) -> List[Dict[str, Any]]:
    return situacao_pacotes(
        ((it.tipo, it.status_operacional, it.quantidade) for it in ap.itens if it.faturavel),
        _normalizar_status(),
    )


def situacao_fechamento(db: Session, fech: OwnerFechamento) -> List[Dict[str, Any]]:
    rows = db.execute(
        select(
            OwnerFechamentoItem.tipo,
            OwnerFechamentoItem.status_operacional,
            func.coalesce(func.sum(OwnerFechamentoItem.quantidade), 0),
        )
        .where(
            OwnerFechamentoItem.fechamento_id == fech.id,
            OwnerFechamentoItem.status_financeiro == "cobrado",
        )
        .group_by(OwnerFechamentoItem.tipo, OwnerFechamentoItem.status_operacional)
    ).all()
    return situacao_pacotes(((t, s, int(q)) for t, s, q in rows), _normalizar_status())


def resumo_por_dia(db: Session, fech: OwnerFechamento) -> List[Tuple[date, int]]:
    rows = db.execute(
        select(
            OwnerFechamentoItem.data_competencia,
            func.coalesce(func.sum(OwnerFechamentoItem.quantidade), 0),
        )
        .where(
            OwnerFechamentoItem.fechamento_id == fech.id,
            OwnerFechamentoItem.status_financeiro == "cobrado",
        )
        .group_by(OwnerFechamentoItem.data_competencia)
        .order_by(OwnerFechamentoItem.data_competencia)
    ).all()
    return [(d, int(q)) for d, q in rows if d is not None]


def ajustes_do_fechamento(db: Session, fech: OwnerFechamento) -> Dict[str, List[OwnerFechamentoAjuste]]:
    aplicados = list(
        db.scalars(
            select(OwnerFechamentoAjuste)
            .where(OwnerFechamentoAjuste.fechamento_aplicado_id == fech.id)
            .order_by(OwnerFechamentoAjuste.id)
        ).all()
    )
    lancados = list(
        db.scalars(
            select(OwnerFechamentoAjuste)
            .where(OwnerFechamentoAjuste.fechamento_origem_id == fech.id)
            .order_by(OwnerFechamentoAjuste.id)
        ).all()
    )
    return {"aplicados": aplicados, "lancados": lancados}


def composicao_fechamento(db: Session, fech: OwnerFechamento) -> List[OwnerFechamentoComposicao]:
    return list(
        db.scalars(
            select(OwnerFechamentoComposicao)
            .where(OwnerFechamentoComposicao.fechamento_id == fech.id)
            .order_by(OwnerFechamentoComposicao.ordem)
        ).all()
    )


def itens_fechamento_query(
    fechamento_id: int,
    *,
    codigo: Optional[str] = None,
    data: Optional[date] = None,
    servico: Optional[str] = None,
    origem: Optional[str] = None,
    filtro: Optional[str] = None,
):
    stmt = select(OwnerFechamentoItem).where(OwnerFechamentoItem.fechamento_id == fechamento_id)
    if codigo:
        stmt = stmt.where(OwnerFechamentoItem.codigo.contains(normalizar_codigo(codigo)))
    if data:
        stmt = stmt.where(OwnerFechamentoItem.data_competencia == data)
    if servico:
        stmt = stmt.where(OwnerFechamentoItem.servico == servico)
    if origem:
        stmt = stmt.where(OwnerFechamentoItem.origem == origem)
    if filtro == "faturavel":
        stmt = stmt.where(OwnerFechamentoItem.status_financeiro == "cobrado")
    elif filtro == "cancelado":
        stmt = stmt.where(OwnerFechamentoItem.motivo == "cancelado")
    elif filtro == "duplicado":
        stmt = stmt.where(OwnerFechamentoItem.motivo == "duplicidade")
    elif filtro == "manual":
        stmt = stmt.where(OwnerFechamentoItem.tipo == "manual")
    elif filtro == "reconciliado":
        stmt = stmt.where(OwnerFechamentoItem.status_financeiro == "reconciliado")
    elif filtro == "desconsiderado":
        stmt = stmt.where(
            OwnerFechamentoItem.status_financeiro != "cobrado",
            func.coalesce(OwnerFechamentoItem.motivo, "") != "cancelado",
        )
    return stmt


def label_classificacao(c: str) -> str:
    return CLASSIFICACAO_LABELS.get(c, c)
