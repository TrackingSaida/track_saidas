"""Financeiro → Cobrança de Owners. Acesso exclusivo do root (role 0).

Role 1 (admin da operação), operador, coletador e motoboy recebem 403 em qualquer
rota, mesmo informando owner_id. A sub_base usada é sempre a do Owner carregado.
"""
from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from auth import _coerce_role_int, get_current_user
from db import get_db
from models import OwnerFechamento, OwnerFechamentoItem, User
import owner_cobranca_service as svc
from owner_cobranca_engine import (
    MODELO_LABELS,
    MOTIVO_LABELS,
    NIVEL_ATENCAO,
    NIVEL_BLOQUEANTE,
    NIVEL_INFORMATIVO,
    ORIGEM_LABELS,
    SERVICO_LABELS,
    filtrar_itens,
    periodo_de,
    presets_periodo,
    quinzena_de,
)


def require_root(current_user: User = Depends(get_current_user)) -> User:
    if _coerce_role_int(getattr(current_user, "role", None)) != 0:
        raise HTTPException(403, "Acesso restrito ao root.")
    return current_user


router = APIRouter(
    prefix="/admin/cobranca-owners",
    tags=["Cobrança de Owners"],
    dependencies=[Depends(require_root)],
)

STATUS_LABELS = {
    "em_apuracao": "Em apuração",
    "conferido": "Conferido",
    "fechado": "Fechado",
    "informado": "Informado",
    "reaberto": "Reaberto",
    "anulado": "Anulado",
}
STATUS_OPERACIONAL_LABELS = {
    "saiu": "Saiu para entrega",
    "saiu pra entrega": "Saiu para entrega",
    "saiu_pra_entrega": "Saiu para entrega",
    "saiu_para_entrega": "Saiu para entrega",
    "em_rota": "Em rota",
    "entregue": "Entregue",
    "ausente": "Ausente",
    "cancelado": "Cancelado",
    "cancelada": "Cancelado",
    "coletado": "Coletado",
    "na_base": "Na base",
}
AJUSTE_LABELS = {"acrescimo": "Acréscimo", "desconto": "Desconto", "credito": "Crédito", "correcao": "Correção"}
DECISAO_LABELS = {
    "volumes_distintos": "Volumes distintos",
    "vinculo_codigos": "Códigos vinculados",
    "ajuste_quantidade": "Quantidade ajustada",
}
ACAO_LABELS = {
    "conferir": "Conferido",
    "fechar": "Fechado",
    "informar": "Marcado como informado",
    "reabrir": "Reaberto",
    "anular": "Anulado",
    "ajuste": "Ajuste lançado",
    "decisao": "Decisão registrada",
    "decisao_revogada": "Decisão revogada",
}


# ---------------------------------------------------------------------------
# Serialização
# ---------------------------------------------------------------------------


def _money(v: Any) -> Optional[str]:
    if v is None:
        return None
    return str(Decimal(str(v)).quantize(Decimal("0.01")))


def _preco(v: Any) -> Optional[str]:
    if v is None:
        return None
    d = Decimal(str(v)).normalize()
    if d.as_tuple().exponent > -2:
        d = d.quantize(Decimal("0.01"))
    return format(d, "f")


def _iso(v: Any) -> Optional[str]:
    return v.isoformat() if v is not None else None


def _status_operacional(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    k = s.strip().lower()
    return STATUS_OPERACIONAL_LABELS.get(k, k.replace("_", " ").capitalize())


def _item_dict(it: Any) -> Dict[str, Any]:
    status_fin = it.status_financeiro
    motivo = it.motivo
    return {
        "tipo": it.tipo,
        "codigo": it.codigo,
        "id_saida": it.id_saida,
        "id_coleta": it.id_coleta,
        "avulso_lote_id": it.avulso_lote_id,
        "servico": it.servico,
        "servico_label": SERVICO_LABELS.get(it.servico or "", it.servico or "—"),
        "base": it.base,
        "data": _iso(it.data_competencia),
        "origem": it.origem,
        "origem_label": ORIGEM_LABELS.get(it.origem, it.origem),
        "status_financeiro": status_fin,
        "status_financeiro_label": {
            "cobrado": "Cobrado",
            "nao_cobrado": "Não cobrado",
            "reconciliado": "Reconciliado",
        }.get(status_fin, status_fin),
        "motivo": motivo,
        "motivo_label": MOTIVO_LABELS.get(motivo, motivo) if motivo else None,
        "quantidade": int(it.quantidade or 0),
        "is_grande": bool(it.is_grande),
        "status_operacional": _status_operacional(it.status_operacional),
    }


def _divergencias_dict(divs) -> List[Dict[str, Any]]:
    ordem = {NIVEL_BLOQUEANTE: 0, NIVEL_ATENCAO: 1, NIVEL_INFORMATIVO: 2}
    return [
        {"nivel": d.nivel, "codigo": d.codigo, "mensagem": d.mensagem, "referencia": dict(d.referencia or {})}
        for d in sorted(divs, key=lambda d: ordem.get(d.nivel, 9))
    ]


def _contagem_divergencias(divs) -> Dict[str, int]:
    out = {NIVEL_BLOQUEANTE: 0, NIVEL_ATENCAO: 0, NIVEL_INFORMATIVO: 0}
    for d in divs:
        out[d.nivel] = out.get(d.nivel, 0) + 1
    return out


def _config_dict(cfg) -> Dict[str, Any]:
    return {
        "persistida": cfg.id is not None,
        "modelo": cfg.modelo,
        "modelo_label": MODELO_LABELS.get(cfg.modelo, cfg.modelo),
        "modelo_sugerido": cfg.modelo_sugerido,
        "periodicidade": cfg.periodicidade,
        "status": cfg.status,
        "confirmado": cfg.status == "confirmado",
        "confirmado_em": _iso(cfg.confirmado_em),
        "razao_social": cfg.razao_social,
        "documento": cfg.documento,
        "telefone": cfg.telefone,
        "endereco": cfg.endereco,
        "contato_financeiro": cfg.contato_financeiro,
        "observacao": cfg.observacao,
    }


def _owner_dict(owner) -> Dict[str, Any]:
    return {
        "id_owner": owner.id_owner,
        "nome": svc.nome_owner(owner),
        "username": owner.username,
        "sub_base": owner.sub_base,
        "teste": bool(owner.teste),
    }


def _fechamento_resumo(f: OwnerFechamento) -> Dict[str, Any]:
    return {
        "id": f.id,
        "owner_id": f.owner_id,
        "periodo_inicio": _iso(f.periodo_inicio),
        "periodo_fim": _iso(f.periodo_fim),
        "versao": f.versao,
        "status": f.status,
        "status_label": STATUS_LABELS.get(f.status, f.status),
        "modelo": f.modelo,
        "modelo_label": MODELO_LABELS.get(f.modelo, f.modelo),
        "preco_unitario": _preco(f.preco_unitario),
        "qtd_faturavel": f.qtd_faturavel,
        "valor_total": _money(f.valor_total),
        "fechado_em": _iso(f.fechado_em),
        "fechado_por": f.fechado_por_username,
        "informado_em": _iso(f.informado_em),
        "reaberto_em": _iso(f.reaberto_em),
        "reaberto_motivo": f.reaberto_motivo,
        "anulado_em": _iso(f.anulado_em),
        "anulado_motivo": f.anulado_motivo,
    }


def _ajuste_dict(a) -> Dict[str, Any]:
    return {
        "id": a.id,
        "tipo": a.tipo,
        "tipo_label": AJUSTE_LABELS.get(a.tipo, a.tipo),
        "valor": _money(a.valor),
        "motivo": a.motivo,
        "fechamento_origem_id": a.fechamento_origem_id,
        "fechamento_aplicado_id": a.fechamento_aplicado_id,
        "criado_em": _iso(a.criado_em),
        "criado_por": a.criado_por_username,
    }


def _composicao_dict(classificacao: str, quantidade: int, valor: Any) -> Dict[str, Any]:
    return {
        "classificacao": classificacao,
        "label": svc.label_classificacao(classificacao),
        "quantidade": int(quantidade),
        "valor": _money(valor),
    }


# ---------------------------------------------------------------------------
# Período
# ---------------------------------------------------------------------------


def _periodo(data_inicio: Optional[date], data_fim: Optional[date]) -> tuple[date, date]:
    if data_inicio is None and data_fim is None:
        return quinzena_de(date.today())
    if data_inicio is None or data_fim is None:
        raise HTTPException(422, "Informe início e fim do período.")
    if data_fim < data_inicio:
        raise HTTPException(422, "O fim do período é anterior ao início.")
    if (data_fim - data_inicio).days > 92:
        raise HTTPException(422, "Período máximo de consulta: 92 dias.")
    return data_inicio, data_fim


def _presets() -> List[Dict[str, Any]]:
    return [
        {**p, "inicio": _iso(p["inicio"]), "fim": _iso(p["fim"])} for p in presets_periodo(date.today())
    ]


# ---------------------------------------------------------------------------
# Lista e resumo
# ---------------------------------------------------------------------------


def _linha_lista(a: svc.ApuracaoOwner) -> Dict[str, Any]:
    tot = a.totais
    modelo = a.fechamento.modelo if a.fechamento is not None else a.config.modelo
    divs = a.divergencias
    return {
        "owner": _owner_dict(a.owner),
        "modelo": modelo,
        "modelo_label": MODELO_LABELS.get(modelo, modelo),
        "config_confirmada": a.config.status == "confirmado",
        "pacotes": tot["faturavel"],
        "cancelados": tot["cancelados"],
        "desconsiderados": tot["desconsiderados"],
        "preco_unitario": _preco(tot["preco_unitario"]),
        "valor_total": _money(tot["valor_total"]),
        "status": a.status,
        "status_label": STATUS_LABELS.get(a.status, a.status),
        "fonte": "snapshot" if a.fechamento is not None else "previa",
        "fechamento_id": a.fechamento.id if a.fechamento is not None else None,
        "divergencias": _contagem_divergencias(divs),
    }


def _montar_lista(
    db: Session,
    ini: date,
    fim: date,
    *,
    owner_id: Optional[int],
    incluir_teste: bool,
    so_divergencia: bool,
) -> List[Dict[str, Any]]:
    linhas = []
    for owner in svc.listar_owners(db, incluir_teste=incluir_teste, owner_id=owner_id):
        linha = _linha_lista(svc.apurar_owner(db, owner, ini, fim))
        if so_divergencia:
            c = linha["divergencias"]
            if not (c[NIVEL_BLOQUEANTE] or c[NIVEL_ATENCAO]):
                continue
        linhas.append(linha)
    return linhas


def _resumo(linhas: List[Dict[str, Any]], status_ativo: str) -> Dict[str, Any]:
    contagem = {s: 0 for s in svc.STATUS_LISTA}
    for l in linhas:
        contagem[l["status"]] = contagem.get(l["status"], 0) + 1
    aba = [l for l in linhas if status_ativo == "todos" or l["status"] == status_ativo]
    fechadas = [l for l in linhas if l["status"] in ("fechado", "informado")]
    soma = lambda ls: sum((Decimal(l["valor_total"]) for l in ls), start=Decimal("0"))
    return {
        "status_ativo": status_ativo,
        "valor_aba": _money(soma(aba)),
        "valor_fechado": _money(soma(fechadas)),
        "owners_pendentes": sum(1 for l in linhas if l["status"] not in ("fechado", "informado")),
        "total_owners": len(linhas),
        "contagem": contagem,
        "composicao": {
            "faturavel": sum(l["pacotes"] for l in aba),
            "cancelados": sum(l["cancelados"] for l in aba),
            "desconsiderados": sum(l["desconsiderados"] for l in aba),
        },
    }


StatusAba = Literal["em_apuracao", "conferido", "fechado", "informado", "todos"]


@router.get("/resumo")
def get_resumo(
    data_inicio: Optional[date] = Query(None),
    data_fim: Optional[date] = Query(None),
    owner_id: Optional[int] = Query(None),
    status: StatusAba = Query("em_apuracao"),
    so_divergencia: bool = Query(False),
    incluir_teste: bool = Query(False),
    db: Session = Depends(get_db),
):
    ini, fim = _periodo(data_inicio, data_fim)
    linhas = _montar_lista(
        db, ini, fim, owner_id=owner_id, incluir_teste=incluir_teste, so_divergencia=so_divergencia
    )
    return {"periodo": {"inicio": _iso(ini), "fim": _iso(fim)}, "resumo": _resumo(linhas, status)}


@router.get("/apuracoes")
def get_apuracoes(
    data_inicio: Optional[date] = Query(None),
    data_fim: Optional[date] = Query(None),
    owner_id: Optional[int] = Query(None),
    status: StatusAba = Query("em_apuracao"),
    so_divergencia: bool = Query(False),
    incluir_teste: bool = Query(False),
    db: Session = Depends(get_db),
):
    ini, fim = _periodo(data_inicio, data_fim)
    linhas = _montar_lista(
        db, ini, fim, owner_id=owner_id, incluir_teste=incluir_teste, so_divergencia=so_divergencia
    )
    itens = [l for l in linhas if status == "todos" or l["status"] == status]
    itens.sort(key=lambda l: (-Decimal(l["valor_total"]), l["owner"]["nome"].lower()))
    owners = [
        {"id_owner": o.id_owner, "nome": svc.nome_owner(o)}
        for o in svc.listar_owners(db, incluir_teste=incluir_teste)
    ]
    return {
        "periodo": {"inicio": _iso(ini), "fim": _iso(fim)},
        "presets": _presets(),
        "owners": owners,
        "resumo": _resumo(linhas, status),
        "itens": itens,
    }


# ---------------------------------------------------------------------------
# Detalhe
# ---------------------------------------------------------------------------


def _detalhe(db: Session, a: svc.ApuracaoOwner) -> Dict[str, Any]:
    owner = a.owner
    tot = a.totais
    if a.fechamento is not None:
        f = a.fechamento
        composicao = [
            _composicao_dict(c.classificacao, c.quantidade, c.valor) for c in svc.composicao_fechamento(db, f)
        ]
        divergencias = (f.config_snapshot or {}).get("divergencias") or []
        aj = svc.ajustes_do_fechamento(db, f)
        ajustes = [_ajuste_dict(x) for x in aj["aplicados"]]
        lancados = [_ajuste_dict(x) for x in aj["lancados"]]
        modelo = f.modelo
        hash_ = f.hash
    else:
        ap = a.apuracao
        composicao = [_composicao_dict(c.classificacao, c.quantidade, c.valor) for c in ap.composicao]
        divergencias = _divergencias_dict(a.divergencias)
        ajustes = [
            _ajuste_dict(x) for x in svc.ajustes_pendentes(db, owner.id_owner, a.periodo_inicio)
        ]
        lancados = []
        modelo = a.config.modelo
        hash_ = ap.hash

    decisoes = [
        {
            "id": d.id,
            "tipo": d.tipo,
            "tipo_label": DECISAO_LABELS.get(d.tipo, d.tipo),
            "id_coleta": d.id_coleta,
            "servico": d.servico,
            "servico_label": SERVICO_LABELS.get(d.servico, d.servico),
            "codigos": d.codigos or [],
            "quantidade_ajustada": d.quantidade_ajustada,
            "motivo": d.motivo,
            "criado_por": d.criado_por_username,
            "criado_em": _iso(d.criado_em),
        }
        for d in svc.decisoes_ativas(db, owner.id_owner, a.periodo_inicio, a.periodo_fim)
    ]
    versoes = [
        _fechamento_resumo(v) for v in svc.versoes(db, owner.id_owner, a.periodo_inicio, a.periodo_fim)
    ]
    historico = [
        {
            "acao": h.acao,
            "acao_label": ACAO_LABELS.get(h.acao, h.acao),
            "status_anterior": h.status_anterior,
            "status_novo": h.status_novo,
            "motivo": h.motivo,
            "username": h.username,
            "criado_em": _iso(h.criado_em),
        }
        for h in svc.historico(db, owner.id_owner, a.periodo_inicio, a.periodo_fim)
    ]
    conferencia = None
    if a.conferencia is not None and a.status == "conferido":
        conferencia = {
            "conferido_em": _iso(a.conferencia.conferido_em),
            "conferido_por": a.conferencia.conferido_por_username,
        }
    return {
        "owner": _owner_dict(owner),
        "periodo": {"inicio": _iso(a.periodo_inicio), "fim": _iso(a.periodo_fim), "fechavel": a.fechavel},
        "config": _config_dict(a.config),
        "modelo": modelo,
        "modelo_label": MODELO_LABELS.get(modelo, modelo),
        "status": a.status,
        "status_label": STATUS_LABELS.get(a.status, a.status),
        "fonte": "snapshot" if a.fechamento is not None else "previa",
        "fechamento": _fechamento_resumo(a.fechamento) if a.fechamento is not None else None,
        "totais": {
            "bruto": tot["bruto"],
            "cancelados": tot["cancelados"],
            "faturavel": tot["faturavel"],
            "desconsiderados": tot["desconsiderados"],
            "preco_unitario": _preco(tot["preco_unitario"]),
            "valor_pacotes": _money(tot["valor_pacotes"]),
            "valor_ajustes": _money(tot["valor_ajustes"]),
            "valor_total": _money(tot["valor_total"]),
        },
        "composicao": composicao,
        "divergencias": divergencias,
        "ajustes": ajustes,
        "ajustes_lancados": lancados,
        "decisoes": decisoes,
        "versoes": versoes,
        "historico": historico,
        "conferencia": conferencia,
        "hash": hash_,
        "pode_conferir": a.fechamento is None and a.pode_conferir,
        "pode_fechar": a.fechamento is None and a.pode_fechar,
        "motivo_bloqueio_fechar": a.motivo_bloqueio_fechar,
    }


@router.get("/apuracoes/{owner_id}")
def get_apuracao(
    owner_id: int,
    data_inicio: Optional[date] = Query(None),
    data_fim: Optional[date] = Query(None),
    db: Session = Depends(get_db),
):
    owner = svc.carregar_owner(db, owner_id)
    ini, fim = _periodo(data_inicio, data_fim)
    return _detalhe(db, svc.apurar_owner(db, owner, ini, fim))


FiltroItens = Literal["faturavel", "cancelado", "duplicado", "manual", "reconciliado", "desconsiderado"]


@router.get("/apuracoes/{owner_id}/itens")
def get_itens(
    owner_id: int,
    data_inicio: Optional[date] = Query(None),
    data_fim: Optional[date] = Query(None),
    fechamento_id: Optional[int] = Query(None, description="Versão específica (somente leitura)"),
    codigo: Optional[str] = Query(None, max_length=80),
    data: Optional[date] = Query(None),
    servico: Optional[Literal["shopee", "mercado_livre", "avulso"]] = Query(None),
    origem: Optional[Literal["coleta", "entrada", "saida", "manual"]] = Query(None),
    filtro: Optional[FiltroItens] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    owner = svc.carregar_owner(db, owner_id)
    ini, fim = _periodo(data_inicio, data_fim)
    fech: Optional[OwnerFechamento] = None
    if fechamento_id:
        fech = db.get(OwnerFechamento, int(fechamento_id))
        if fech is None or fech.owner_id != owner.id_owner:
            raise HTTPException(404, "Fechamento não encontrado.")
    else:
        fech = svc.fechamento_vigente(db, owner.id_owner, ini, fim)

    filtros = dict(codigo=codigo, data=data, servico=servico, origem=origem, filtro=filtro)
    if fech is not None:
        stmt = svc.itens_fechamento_query(fech.id, **filtros)
        total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
        rows = db.scalars(
            stmt.order_by(
                OwnerFechamentoItem.data_competencia, OwnerFechamentoItem.tipo.desc(),
                OwnerFechamentoItem.codigo, OwnerFechamentoItem.id,
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        itens = [_item_dict(r) for r in rows]
        fonte = "snapshot"
    else:
        a = svc.apurar_owner(db, owner, ini, fim, usar_snapshot=False)
        lista = filtrar_itens(a.apuracao.itens, **filtros)
        total = len(lista)
        itens = [_item_dict(it) for it in lista[(page - 1) * page_size : page * page_size]]
        fonte = "previa"
    return {"fonte": fonte, "page": page, "page_size": page_size, "total": total, "itens": itens}


@router.get("/apuracoes/{owner_id}/divergencias")
def get_divergencias(
    owner_id: int,
    data_inicio: Optional[date] = Query(None),
    data_fim: Optional[date] = Query(None),
    db: Session = Depends(get_db),
):
    owner = svc.carregar_owner(db, owner_id)
    ini, fim = _periodo(data_inicio, data_fim)
    a = svc.apurar_owner(db, owner, ini, fim)
    if a.fechamento is not None:
        return {"fonte": "snapshot", "divergencias": (a.fechamento.config_snapshot or {}).get("divergencias") or []}
    return {"fonte": "previa", "divergencias": _divergencias_dict(a.divergencias)}


# ---------------------------------------------------------------------------
# Configuração comercial e preço
# ---------------------------------------------------------------------------


def _config_resposta(db: Session, owner) -> Dict[str, Any]:
    cfg = svc.obter_config(db, owner)
    precos = svc.listar_precos(db, owner.id_owner)
    hoje = date.today()
    vigente = svc.preco_vigente(precos, hoje)
    ini_atual, fim_atual = periodo_de(hoje, cfg.periodicidade)
    return {
        "owner": _owner_dict(owner),
        "config": _config_dict(cfg),
        "modelos": [{"id": k, "label": v} for k, v in MODELO_LABELS.items()],
        "flags_operacionais": {
            "ignorar_coleta": bool(owner.ignorar_coleta),
            "entrada_obrigatoria_habilitada": bool(getattr(owner, "entrada_obrigatoria_habilitada", False)),
            "modo_operacao": owner.modo_operacao or "codigo",
        },
        "preco_vigente": _preco(vigente.valor) if vigente else None,
        "periodo_atual": {"inicio": _iso(ini_atual), "fim": _iso(fim_atual)},
        "precos": [
            {
                "id": p.id,
                "valor": _preco(p.valor),
                "inicio": _iso(p.inicio),
                "fim": _iso(p.fim),
                "motivo": p.motivo,
                "criado_em": _iso(p.criado_em),
            }
            for p in precos
        ],
    }


class ConfiguracaoIn(BaseModel):
    modelo: Optional[Literal["coleta_saida", "entrada_saida", "coleta_entrada_saida", "somente_saida"]] = None
    periodicidade: Optional[Literal["quinzenal", "mensal"]] = None
    confirmar: bool = False
    razao_social: Optional[str] = Field(None, max_length=200)
    documento: Optional[str] = Field(None, max_length=40)
    telefone: Optional[str] = Field(None, max_length=40)
    endereco: Optional[str] = Field(None, max_length=300)
    contato_financeiro: Optional[str] = Field(None, max_length=200)
    observacao: Optional[str] = Field(None, max_length=1000)


class PrecoIn(BaseModel):
    valor: Decimal = Field(..., ge=0, max_digits=12, decimal_places=4)
    inicio: date
    motivo: Optional[str] = Field(None, max_length=300)


@router.get("/owners/{owner_id}/configuracao")
def get_configuracao(owner_id: int, db: Session = Depends(get_db)):
    return _config_resposta(db, svc.carregar_owner(db, owner_id))


@router.put("/owners/{owner_id}/configuracao")
def put_configuracao(
    owner_id: int,
    body: ConfiguracaoIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_root),
):
    owner = svc.carregar_owner(db, owner_id)
    svc.salvar_config(db, owner, user, body.model_dump(exclude_unset=True))
    db.commit()
    return _config_resposta(db, owner)


@router.post("/owners/{owner_id}/precos", status_code=201)
def post_preco(
    owner_id: int,
    body: PrecoIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_root),
):
    owner = svc.carregar_owner(db, owner_id)
    svc.adicionar_preco(db, owner, user, valor=body.valor, inicio=body.inicio, motivo=body.motivo)
    db.commit()
    return _config_resposta(db, owner)


# ---------------------------------------------------------------------------
# Decisões, conferência e fechamento
# ---------------------------------------------------------------------------


class PeriodoIn(BaseModel):
    data_inicio: date
    data_fim: date


class DecisaoIn(PeriodoIn):
    tipo: Literal["volumes_distintos", "vinculo_codigos", "ajuste_quantidade"]
    id_coleta: Optional[int] = None
    servico: Optional[Literal["shopee", "mercado_livre", "avulso"]] = None
    codigos: List[str] = Field(default_factory=list, max_length=5000)
    quantidade_ajustada: Optional[int] = Field(None, ge=0)
    motivo: str = Field(..., min_length=5, max_length=500)
    aplicar_todos_ambiguos: bool = False


class FecharIn(PeriodoIn):
    hash: str = Field(..., min_length=10, max_length=128)
    observacao: Optional[str] = Field(None, max_length=1000)


@router.post("/apuracoes/{owner_id}/decisoes", status_code=201)
def post_decisao(
    owner_id: int,
    body: DecisaoIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_root),
):
    owner = svc.carregar_owner(db, owner_id)
    ini, fim = _periodo(body.data_inicio, body.data_fim)
    svc.registrar_decisao(
        db,
        owner,
        user,
        ini=ini,
        fim=fim,
        tipo=body.tipo,
        id_coleta=body.id_coleta,
        servico=body.servico,
        motivo=body.motivo,
        codigos=body.codigos,
        quantidade_ajustada=body.quantidade_ajustada,
        aplicar_todos_ambiguos=body.aplicar_todos_ambiguos,
    )
    db.commit()
    return _detalhe(db, svc.apurar_owner(db, owner, ini, fim))


@router.delete("/apuracoes/{owner_id}/decisoes/{decisao_id}")
def delete_decisao(
    owner_id: int,
    decisao_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_root),
):
    owner = svc.carregar_owner(db, owner_id)
    dec = svc.revogar_decisao(db, owner, user, decisao_id)
    db.commit()
    return _detalhe(db, svc.apurar_owner(db, owner, dec.periodo_inicio, dec.periodo_fim))


@router.post("/apuracoes/{owner_id}/conferir")
def post_conferir(
    owner_id: int,
    body: PeriodoIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_root),
):
    owner = svc.carregar_owner(db, owner_id)
    ini, fim = _periodo(body.data_inicio, body.data_fim)
    svc.conferir(db, owner, user, ini, fim)
    db.commit()
    return _detalhe(db, svc.apurar_owner(db, owner, ini, fim))


@router.post("/apuracoes/{owner_id}/fechar", status_code=201)
def post_fechar(
    owner_id: int,
    body: FecharIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_root),
):
    owner = svc.carregar_owner(db, owner_id)
    ini, fim = _periodo(body.data_inicio, body.data_fim)
    svc.fechar(db, owner, user, ini, fim, hash_confirmado=body.hash, observacao=body.observacao)
    db.commit()
    return _detalhe(db, svc.apurar_owner(db, owner, ini, fim))


# ---------------------------------------------------------------------------
# Fechamentos
# ---------------------------------------------------------------------------


@router.get("/fechamentos")
def get_fechamentos(
    owner_id: Optional[int] = Query(None),
    data_inicio: Optional[date] = Query(None),
    data_fim: Optional[date] = Query(None),
    status: Optional[Literal["fechado", "informado", "reaberto", "anulado"]] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    stmt = select(OwnerFechamento)
    if owner_id:
        stmt = stmt.where(OwnerFechamento.owner_id == int(owner_id))
    if data_inicio:
        stmt = stmt.where(OwnerFechamento.periodo_fim >= data_inicio)
    if data_fim:
        stmt = stmt.where(OwnerFechamento.periodo_inicio <= data_fim)
    if status:
        stmt = stmt.where(OwnerFechamento.status == status)
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = db.scalars(
        stmt.order_by(OwnerFechamento.periodo_inicio.desc(), OwnerFechamento.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {"total": total, "page": page, "page_size": page_size, "itens": [_fechamento_resumo(f) for f in rows]}


@router.get("/fechamentos/{fechamento_id}")
def get_fechamento(fechamento_id: int, db: Session = Depends(get_db)):
    fech, owner = svc.carregar_fechamento(db, fechamento_id)
    aj = svc.ajustes_do_fechamento(db, fech)
    return {
        **_fechamento_resumo(fech),
        "owner": _owner_dict(owner),
        "owner_snapshot": fech.owner_snapshot or {},
        "config_snapshot": fech.config_snapshot or {},
        "totais": {
            "bruto": fech.qtd_bruto,
            "cancelados": fech.qtd_cancelados,
            "faturavel": fech.qtd_faturavel,
            "desconsiderados": fech.qtd_desconsiderados,
            "valor_pacotes": _money(fech.valor_pacotes),
            "valor_ajustes": _money(fech.valor_ajustes),
            "valor_total": _money(fech.valor_total),
        },
        "composicao": [
            _composicao_dict(c.classificacao, c.quantidade, c.valor) for c in svc.composicao_fechamento(db, fech)
        ],
        "ajustes": [_ajuste_dict(x) for x in aj["aplicados"]],
        "ajustes_lancados": [_ajuste_dict(x) for x in aj["lancados"]],
        "hash": fech.hash,
        "observacao": fech.observacao,
    }


class AjusteIn(BaseModel):
    tipo: Literal["acrescimo", "desconto", "credito", "correcao"]
    valor: Decimal = Field(..., max_digits=14, decimal_places=2)
    motivo: str = Field(..., min_length=5, max_length=500)
    chave_idempotencia: str = Field(..., min_length=8, max_length=120)


class MotivoIn(BaseModel):
    motivo: str = Field(..., min_length=5, max_length=500)


@router.post("/fechamentos/{fechamento_id}/ajustes")
def post_ajuste(
    fechamento_id: int,
    body: AjusteIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_root),
):
    fech, _owner = svc.carregar_fechamento(db, fechamento_id)
    aj, criado = svc.lancar_ajuste(
        db, fech, user, tipo=body.tipo, valor=body.valor, motivo=body.motivo,
        chave_idempotencia=body.chave_idempotencia,
    )
    db.commit()
    return {"criado": criado, "ajuste": _ajuste_dict(aj)}


@router.post("/fechamentos/{fechamento_id}/informar")
def post_informar(fechamento_id: int, db: Session = Depends(get_db), user: User = Depends(require_root)):
    fech, _owner = svc.carregar_fechamento(db, fechamento_id)
    svc.informar(db, fech, user)
    db.commit()
    return _fechamento_resumo(fech)


@router.post("/fechamentos/{fechamento_id}/reabrir")
def post_reabrir(
    fechamento_id: int, body: MotivoIn, db: Session = Depends(get_db), user: User = Depends(require_root)
):
    fech, _owner = svc.carregar_fechamento(db, fechamento_id)
    svc.reabrir(db, fech, user, body.motivo)
    db.commit()
    return _fechamento_resumo(fech)


@router.post("/fechamentos/{fechamento_id}/anular")
def post_anular(
    fechamento_id: int, body: MotivoIn, db: Session = Depends(get_db), user: User = Depends(require_root)
):
    fech, _owner = svc.carregar_fechamento(db, fechamento_id)
    svc.anular(db, fech, user, body.motivo)
    db.commit()
    return _fechamento_resumo(fech)


@router.get("/fechamentos/{fechamento_id}/demonstrativo.pdf")
def get_demonstrativo(fechamento_id: int, db: Session = Depends(get_db)):
    from owner_cobranca_pdf import gerar_demonstrativo_pdf, nome_arquivo_demonstrativo

    fech, _owner = svc.carregar_fechamento(db, fechamento_id)
    if fech.status == "anulado":
        raise HTTPException(409, "Fechamento anulado não gera demonstrativo.")
    pdf = gerar_demonstrativo_pdf(
        fech,
        composicao=svc.composicao_fechamento(db, fech),
        ajustes=svc.ajustes_do_fechamento(db, fech)["aplicados"],
    )
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{nome_arquivo_demonstrativo(fech)}"'},
    )


CSV_COLUNAS = [
    ("tipo", "Tipo"),
    ("codigo", "Código"),
    ("id_saida", "ID saída"),
    ("id_coleta", "ID coleta"),
    ("data", "Data de competência"),
    ("origem_label", "Origem"),
    ("servico_label", "Serviço"),
    ("base", "Base"),
    ("quantidade", "Quantidade"),
    ("status_financeiro_label", "Situação"),
    ("motivo_label", "Motivo"),
    ("status_operacional", "Status operacional"),
    ("is_grande", "Pacote G"),
]


@router.get("/fechamentos/{fechamento_id}/itens.csv")
def get_itens_csv(fechamento_id: int, db: Session = Depends(get_db)):
    fech, _owner = svc.carregar_fechamento(db, fechamento_id)

    def gerar():
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        buf.write("\ufeff")
        w.writerow([h for _k, h in CSV_COLUNAS])
        yield buf.getvalue()
        ultimo_id = 0
        while True:
            rows = db.scalars(
                select(OwnerFechamentoItem)
                .where(OwnerFechamentoItem.fechamento_id == fech.id, OwnerFechamentoItem.id > ultimo_id)
                .order_by(OwnerFechamentoItem.id)
                .limit(5000)
            ).all()
            if not rows:
                break
            buf = io.StringIO()
            w = csv.writer(buf, delimiter=";")
            for r in rows:
                d = _item_dict(r)
                d["is_grande"] = "Sim" if d["is_grande"] else "Não"
                w.writerow(["" if d.get(k) is None else d.get(k) for k, _h in CSV_COLUNAS])
            ultimo_id = rows[-1].id
            yield buf.getvalue()

    from owner_cobranca_pdf import nome_arquivo_csv

    nome = nome_arquivo_csv(fech)
    return StreamingResponse(
        gerar(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{nome}"'},
    )
