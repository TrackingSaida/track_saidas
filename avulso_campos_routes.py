"""CRUD de campos dinâmicos de Avulso + listagem de pendentes."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth import get_current_user
from avulso_campos_service import (
    CONTEXTOS_META,
    TIPOS_META,
    build_label_amigavel,
    contexto_meta,
    list_pendentes,
    motoboy_nome_saida,
    normalize_contexto_avulso,
    normalize_tipo_campo,
    origem_amigavel,
    parse_opcoes_json,
    resolve_campos_ativos,
    status_avulso_label,
    tipo_meta,
    valores_por_saida,
    _slug_chave,
)
from db import get_db
from models import AvulsoCampoConfig, AvulsoLote, Saida, User

router_config = APIRouter(
    prefix="/configuracoes/campos-avulso",
    tags=["Configuração - Campos Avulso"],
)
router_avulsos = APIRouter(prefix="/avulsos", tags=["Avulsos"])


class CampoAvulsoIn(BaseModel):
    contexto: str = "TODOS_AVULSO"
    chave: Optional[str] = None
    label: str = Field(min_length=1, max_length=80)
    tipo: str = "texto"
    obrigatorio: bool = False
    usar_na_identificacao: bool = False
    exibir_na_selecao: bool = True
    ordem: int = 0
    ativo: bool = True
    opcoes: List[str] = Field(default_factory=list)


class CampoAvulsoOut(BaseModel):
    id: int
    sub_base: str
    contexto: str
    contexto_label: str
    contexto_badges: List[str] = Field(default_factory=list)
    chave: str
    label: str
    tipo: str
    tipo_label: str
    tipo_hint: str = ""
    placeholder: str = ""
    input_mode: str = "text"
    obrigatorio: bool
    usar_na_identificacao: bool
    exibir_na_selecao: bool
    ordem: int
    ativo: bool
    opcoes: List[str] = Field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class AvulsoPendenteOut(BaseModel):
    id_saida: int
    codigo: Optional[str] = None
    status: Optional[str] = None
    status_label: str = ""
    base: Optional[str] = None
    label: str
    campos: Dict[str, str] = Field(default_factory=dict)
    avulso_lote_id: Optional[int] = None
    avulso_criado_excepcional: bool = False
    motoboy_id: Optional[int] = None
    motoboy_nome: Optional[str] = None


class AvulsoDetalheOut(AvulsoPendenteOut):
    servico: Optional[str] = None
    timestamp: Optional[datetime] = None
    origem: Optional[str] = None
    origem_label: Optional[str] = None


def _assert_admin(current_user: User) -> None:
    role = int(getattr(current_user, "role", 0) or 0)
    if role not in (0, 1):
        raise HTTPException(403, "Acesso restrito a administradores.")


def _sub_base(current_user: User) -> str:
    sub = (getattr(current_user, "sub_base", None) or "").strip()
    if not sub:
        raise HTTPException(401, "Usuário sem sub_base.")
    return sub


def _row_to_out(row: AvulsoCampoConfig) -> CampoAvulsoOut:
    ctx = contexto_meta(row.contexto)
    tipo = tipo_meta(row.tipo)
    return CampoAvulsoOut(
        id=int(row.id),
        sub_base=row.sub_base,
        contexto=row.contexto,
        contexto_label=str(ctx.get("label") or row.contexto),
        contexto_badges=list(ctx.get("badges") or []),
        chave=row.chave,
        label=row.label,
        tipo=row.tipo,
        tipo_label=str(tipo.get("label") or row.tipo),
        tipo_hint=str(tipo.get("hint") or ""),
        placeholder=str(tipo.get("placeholder") or ""),
        input_mode=str(tipo.get("input_mode") or "text"),
        obrigatorio=bool(row.obrigatorio),
        usar_na_identificacao=bool(row.usar_na_identificacao),
        exibir_na_selecao=bool(row.exibir_na_selecao),
        ordem=int(row.ordem or 0),
        ativo=bool(row.ativo),
        opcoes=parse_opcoes_json(row.opcoes_json),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router_config.get("/meta")
def meta_campos_avulso(
    current_user: User = Depends(get_current_user),
):
    _assert_admin(current_user)
    return {
        "contextos": CONTEXTOS_META,
        "tipos": TIPOS_META,
    }


@router_config.get("", response_model=List[CampoAvulsoOut])
def list_campos_avulso(
    contexto: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _assert_admin(current_user)
    sub_base = _sub_base(current_user)
    q = select(AvulsoCampoConfig).where(AvulsoCampoConfig.sub_base == sub_base)
    if contexto:
        ctx = normalize_contexto_avulso(contexto)
        q = q.where(AvulsoCampoConfig.contexto.in_([ctx, "TODOS_AVULSO"]))
    rows = db.scalars(q.order_by(AvulsoCampoConfig.ordem.asc(), AvulsoCampoConfig.id.asc())).all()
    return [_row_to_out(r) for r in rows]


@router_config.get("/schema")
def schema_campos_avulso(
    contexto: str = Query("TODOS_AVULSO"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Schema ativo para formulários operacionais (roles operação)."""
    role = int(getattr(current_user, "role", -1) or -1)
    if role not in (0, 1, 2, 3, 4):
        raise HTTPException(403, "Acesso restrito.")
    sub_base = _sub_base(current_user)
    campos = resolve_campos_ativos(db, sub_base=sub_base, contexto=contexto)
    return {
        "contexto": normalize_contexto_avulso(contexto),
        "campos": [_row_to_out(c) for c in campos],
    }


@router_config.post("", response_model=CampoAvulsoOut, status_code=201)
def create_campo_avulso(
    body: CampoAvulsoIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _assert_admin(current_user)
    sub_base = _sub_base(current_user)
    ctx = normalize_contexto_avulso(body.contexto)
    tipo = normalize_tipo_campo(body.tipo)
    chave = _slug_chave(body.chave or body.label)
    existing = db.scalar(
        select(AvulsoCampoConfig).where(
            AvulsoCampoConfig.sub_base == sub_base,
            AvulsoCampoConfig.contexto == ctx,
            AvulsoCampoConfig.chave == chave,
        )
    )
    if existing:
        raise HTTPException(409, "Já existe um campo com esta chave neste contexto.")
    opcoes = [str(x).strip() for x in (body.opcoes or []) if str(x).strip()]
    if tipo == "lista" and not opcoes:
        raise HTTPException(422, "Tipo lista exige opções.")
    row = AvulsoCampoConfig(
        sub_base=sub_base,
        contexto=ctx,
        chave=chave,
        label=body.label.strip(),
        tipo=tipo,
        obrigatorio=bool(body.obrigatorio),
        usar_na_identificacao=bool(body.usar_na_identificacao),
        exibir_na_selecao=bool(body.exibir_na_selecao),
        ordem=int(body.ordem or 0),
        ativo=bool(body.ativo),
        opcoes_json=json.dumps(opcoes, ensure_ascii=False) if opcoes else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _row_to_out(row)


@router_config.put("/{campo_id}", response_model=CampoAvulsoOut)
def update_campo_avulso(
    campo_id: int,
    body: CampoAvulsoIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _assert_admin(current_user)
    sub_base = _sub_base(current_user)
    row = db.get(AvulsoCampoConfig, campo_id)
    if not row or row.sub_base != sub_base:
        raise HTTPException(404, "Campo não encontrado.")
    row.contexto = normalize_contexto_avulso(body.contexto)
    row.label = body.label.strip()
    row.tipo = normalize_tipo_campo(body.tipo)
    if body.chave:
        row.chave = _slug_chave(body.chave)
    row.obrigatorio = bool(body.obrigatorio)
    row.usar_na_identificacao = bool(body.usar_na_identificacao)
    row.exibir_na_selecao = bool(body.exibir_na_selecao)
    row.ordem = int(body.ordem or 0)
    row.ativo = bool(body.ativo)
    opcoes = [str(x).strip() for x in (body.opcoes or []) if str(x).strip()]
    if row.tipo == "lista" and not opcoes:
        raise HTTPException(422, "Tipo lista exige opções.")
    row.opcoes_json = json.dumps(opcoes, ensure_ascii=False) if opcoes else None
    db.commit()
    db.refresh(row)
    return _row_to_out(row)


@router_config.delete("/{campo_id}", status_code=204)
def delete_campo_avulso(
    campo_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _assert_admin(current_user)
    sub_base = _sub_base(current_user)
    row = db.get(AvulsoCampoConfig, campo_id)
    if not row or row.sub_base != sub_base:
        raise HTTPException(404, "Campo não encontrado.")
    db.delete(row)
    db.commit()
    return None


def _saida_to_pendente(db: Session, row: Saida, campos_cfg) -> AvulsoPendenteOut:
    vals = valores_por_saida(db, int(row.id_saida))
    motoboy_nome = motoboy_nome_saida(db, row)
    return AvulsoPendenteOut(
        id_saida=int(row.id_saida),
        codigo=row.codigo,
        status=row.status,
        status_label=status_avulso_label(row.status),
        base=row.base,
        label=build_label_amigavel(
            row.codigo,
            base_legado=row.base,
            campos_cfg=campos_cfg,
            valores=vals,
        ),
        campos=vals,
        avulso_lote_id=int(row.avulso_lote_id) if getattr(row, "avulso_lote_id", None) else None,
        avulso_criado_excepcional=bool(getattr(row, "avulso_criado_excepcional", False)),
        motoboy_id=int(row.motoboy_id) if getattr(row, "motoboy_id", None) else None,
        motoboy_nome=motoboy_nome,
    )


@router_avulsos.get("/pendentes")
def get_avulsos_pendentes(
    q: Optional[str] = Query(None),
    identificadores: Optional[str] = Query(None, description="JSON {chave: valor} para busca contém"),
    todos_do_dia: bool = Query(False, description="Lista todos os avulsos de coleta/entrada de hoje"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    role = int(getattr(current_user, "role", -1) or -1)
    if role not in (0, 1, 2, 3, 4):
        raise HTTPException(403, "Acesso restrito.")
    sub_base = _sub_base(current_user)
    ids_map: Dict[str, str] = {}
    if identificadores and str(identificadores).strip():
        try:
            parsed = json.loads(identificadores)
            if isinstance(parsed, dict):
                ids_map = {str(k): str(v) for k, v in parsed.items() if str(v).strip()}
        except Exception:
            raise HTTPException(422, "identificadores inválidos.")
    listing = list_pendentes(
        db,
        sub_base=sub_base,
        q=q,
        identificadores=ids_map,
        todos_do_dia=bool(todos_do_dia),
        limit=limit,
        offset=offset,
    )
    campos_cfg = resolve_campos_ativos(db, sub_base=sub_base, contexto="SAIDA_AVULSO")
    campos_busca = [
        {
            "chave": c.chave,
            "label": c.label,
            "tipo": c.tipo,
            "tipo_label": tipo_meta(c.tipo).get("label"),
            "placeholder": tipo_meta(c.tipo).get("placeholder") or c.label,
            "input_mode": tipo_meta(c.tipo).get("input_mode") or "text",
        }
        for c in campos_cfg
        if c.usar_na_identificacao or c.exibir_na_selecao
    ]
    return {
        "total": listing.total,
        "modo": listing.modo,
        "ambiguo": listing.ambiguo,
        "mensagem": listing.mensagem,
        "campos_busca": campos_busca,
        "items": [_saida_to_pendente(db, r, campos_cfg) for r in listing.rows],
    }


@router_avulsos.get("/{id_saida}", response_model=AvulsoDetalheOut)
def get_avulso_detalhe(
    id_saida: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    role = int(getattr(current_user, "role", -1) or -1)
    if role not in (0, 1, 2, 3, 4):
        raise HTTPException(403, "Acesso restrito.")
    sub_base = _sub_base(current_user)
    row = db.get(Saida, id_saida)
    if not row or (row.sub_base or "").strip() != sub_base:
        raise HTTPException(404, "Avulso não encontrado.")
    campos_cfg = resolve_campos_ativos(db, sub_base=sub_base, contexto="TODOS_AVULSO")
    base = _saida_to_pendente(db, row, campos_cfg)
    origem = None
    if getattr(row, "avulso_lote_id", None):
        lote = db.get(AvulsoLote, int(row.avulso_lote_id))
        if lote:
            origem = lote.origem
    excepcional = bool(getattr(row, "avulso_criado_excepcional", False))
    return AvulsoDetalheOut(
        **base.model_dump(),
        servico=row.servico,
        timestamp=row.timestamp,
        origem=origem or ("saida_excecao" if excepcional else None),
        origem_label=origem_amigavel(origem, excepcional=excepcional),
    )
