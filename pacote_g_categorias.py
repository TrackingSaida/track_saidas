"""Categorias de pacote G (G1, G2...) por sub_base: regras de valor, categoria padrão e CRUD."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from auth import get_current_user
from db import get_db
from models import PacoteGCategoria, Saida, User

Modo = Literal["multiplicador", "valor_fixo"]
SERVICOS_G = ("shopee", "ml", "avulso")


def _role(user) -> int:
    try:
        return int(getattr(user, "role", -1))
    except (TypeError, ValueError):
        return -1


def resolver_sub_base(db: Session, current_user) -> str:
    sub_base = getattr(current_user, "sub_base", None)
    if not sub_base and getattr(current_user, "id", None) is not None:
        u = db.get(User, current_user.id)
        sub_base = getattr(u, "sub_base", None) if u else None
    if not sub_base:
        raise HTTPException(status_code=400, detail="sub_base não definida para o usuário.")
    return sub_base


def listar_categorias(db: Session, sub_base: str, somente_ativas: bool = False) -> List[PacoteGCategoria]:
    stmt = select(PacoteGCategoria).where(PacoteGCategoria.sub_base == sub_base)
    if somente_ativas:
        stmt = stmt.where(PacoteGCategoria.ativo.is_(True))
    return list(db.scalars(stmt.order_by(PacoteGCategoria.ordem, PacoteGCategoria.id)).all())


def garantir_categoria_padrao(db: Session, sub_base: str) -> PacoteGCategoria:
    """Primeira categoria ativa; cria "G" (+1x ao motoboy) quando a sub_base ainda não tem nenhuma."""
    ativas = listar_categorias(db, sub_base, somente_ativas=True)
    if ativas:
        return ativas[0]
    existente = db.scalars(
        select(PacoteGCategoria).where(PacoteGCategoria.sub_base == sub_base, PacoteGCategoria.nome == "G")
    ).first()
    if existente:
        existente.ativo = True
        db.flush()
        return existente
    cat = PacoteGCategoria(sub_base=sub_base, nome="G", descricao="Pacote grande")
    db.add(cat)
    db.flush()
    return cat


def obter_categoria(db: Session, sub_base: str, categoria_id: int, exigir_ativa: bool = True) -> PacoteGCategoria:
    cat = db.get(PacoteGCategoria, int(categoria_id))
    if not cat or cat.sub_base != sub_base:
        raise HTTPException(status_code=404, detail="Categoria de pacote G não encontrada.")
    if exigir_ativa and not cat.ativo:
        raise HTTPException(status_code=422, detail=f"A categoria {cat.nome} está desativada.")
    return cat


def regras_por_categoria(db: Session, sub_base: str, alvo: Literal["motoboy", "base"]) -> Dict[int, Dict[str, Any]]:
    """{categoria_id: {modo, valor, nome}} incluindo desativadas (pacotes antigos mantêm o valor)."""
    out: Dict[int, Dict[str, Any]] = {}
    for c in listar_categorias(db, sub_base):
        modo = c.motoboy_modo if alvo == "motoboy" else c.base_modo
        valor = c.motoboy_valor if alvo == "motoboy" else c.base_valor
        out[int(c.id)] = {"modo": modo, "valor": Decimal(str(valor or 0)), "nome": c.nome}
    return out


def nomes_por_categoria(db: Session, sub_base: str) -> Dict[int, str]:
    return {int(c.id): c.nome for c in listar_categorias(db, sub_base)}


def categoria_da_saida(saida: Any, padrao_id: Optional[int]) -> Optional[int]:
    """Categoria efetiva de um pacote G; legado sem categoria cai na padrão."""
    if not bool(getattr(saida, "is_grande", False)):
        return None
    cid = getattr(saida, "categoria_g_id", None)
    return int(cid) if cid is not None else padrao_id


def padrao_id_ou_none(db: Session, sub_base: str) -> Optional[int]:
    ativas = listar_categorias(db, sub_base, somente_ativas=True)
    return int(ativas[0].id) if ativas else None


def aplicar_categoria_saida(db: Session, saida: Saida, sub_base: str, categoria_id: Optional[int]) -> None:
    """Define a categoria (None remove o G) mantendo is_grande sincronizado."""
    if categoria_id is None:
        saida.categoria_g_id = None
        saida.is_grande = False
        return
    cat = obter_categoria(db, sub_base, categoria_id)
    saida.categoria_g_id = cat.id
    saida.is_grande = True


def normalizar_g_categorias(
    db: Session, sub_base: str, itens: Optional[List[Dict[str, Any]]]
) -> Tuple[Optional[Dict[str, Dict[str, int]]], Dict[str, int]]:
    """Converte [{categoria_g_id, shopee, ml, avulso}] em JSON por categoria e totais por serviço."""
    totais = {s: 0 for s in SERVICOS_G}
    if not itens:
        return None, totais
    mapa: Dict[str, Dict[str, int]] = {}
    for item in itens:
        cid = item.get("categoria_g_id")
        if cid is None:
            continue
        cat = obter_categoria(db, sub_base, cid, exigir_ativa=False)
        bucket = mapa.setdefault(str(cat.id), {s: 0 for s in SERVICOS_G})
        for s in SERVICOS_G:
            qtd = int(item.get(s) or 0)
            bucket[s] += qtd
            totais[s] += qtd
    mapa = {k: v for k, v in mapa.items() if any(v.values())}
    return (mapa or None), totais


def g_categorias_com_padrao(
    g_categorias: Optional[Dict[str, Any]], g_shopee: int, g_ml: int, g_avulso: int, padrao_id: Optional[int]
) -> Dict[str, Dict[str, int]]:
    """G de uma coleta por categoria; registros antigos (só totais) vão para a categoria padrão."""
    if g_categorias:
        return {str(k): {s: int((v or {}).get(s) or 0) for s in SERVICOS_G} for k, v in g_categorias.items()}
    if (g_shopee or g_ml or g_avulso) and padrao_id is not None:
        return {str(padrao_id): {"shopee": int(g_shopee or 0), "ml": int(g_ml or 0), "avulso": int(g_avulso or 0)}}
    return {}


# =========================================================
# API
# =========================================================
router = APIRouter(prefix="/entregadores/precos/categorias-g", tags=["Pacotes G"])


class CategoriaGOut(BaseModel):
    id: int
    nome: str
    descricao: Optional[str] = None
    ordem: int = 0
    ativo: bool = True
    motoboy_modo: Optional[Modo] = None
    motoboy_valor: Optional[Decimal] = None
    base_modo: Optional[Modo] = None
    base_valor: Optional[Decimal] = None
    qtd_pacotes: Optional[int] = None


class CategoriaGIn(BaseModel):
    nome: str = Field(..., min_length=1, max_length=12)
    descricao: Optional[str] = Field(None, max_length=80)
    motoboy_modo: Modo = "multiplicador"
    motoboy_valor: Decimal = Field(Decimal("1.00"), ge=0, le=Decimal("99999"))
    base_modo: Modo = "valor_fixo"
    base_valor: Decimal = Field(Decimal("0.00"), ge=0, le=Decimal("99999"))
    ordem: Optional[int] = None
    ativo: bool = True

    @field_validator("nome")
    @classmethod
    def _nome(cls, v: str) -> str:
        v = (v or "").strip().upper()
        if not v:
            raise ValueError("Informe o nome da categoria.")
        return v

    @field_validator("descricao")
    @classmethod
    def _descricao(cls, v: Optional[str]) -> Optional[str]:
        v = (v or "").strip()
        return v or None


class CategoriaGPatch(BaseModel):
    nome: Optional[str] = Field(None, min_length=1, max_length=12)
    descricao: Optional[str] = Field(None, max_length=80)
    motoboy_modo: Optional[Modo] = None
    motoboy_valor: Optional[Decimal] = Field(None, ge=0, le=Decimal("99999"))
    base_modo: Optional[Modo] = None
    base_valor: Optional[Decimal] = Field(None, ge=0, le=Decimal("99999"))
    ordem: Optional[int] = None
    ativo: Optional[bool] = None

    @field_validator("nome")
    @classmethod
    def _nome(cls, v: Optional[str]) -> Optional[str]:
        return (v or "").strip().upper() or None


def _exigir_admin(current_user) -> None:
    if _role(current_user) not in (0, 1):
        raise HTTPException(status_code=403, detail="Apenas admin/root pode alterar categorias de pacote G.")


def _out(cat: PacoteGCategoria, com_valores: bool, qtd: Optional[int] = None) -> CategoriaGOut:
    data: Dict[str, Any] = {
        "id": int(cat.id), "nome": cat.nome, "descricao": cat.descricao,
        "ordem": int(cat.ordem or 0), "ativo": bool(cat.ativo), "qtd_pacotes": qtd,
    }
    if com_valores:
        data.update(
            motoboy_modo=cat.motoboy_modo, motoboy_valor=cat.motoboy_valor,
            base_modo=cat.base_modo, base_valor=cat.base_valor,
        )
    return CategoriaGOut(**data)


def _nome_disponivel(db: Session, sub_base: str, nome: str, ignorar_id: Optional[int] = None) -> None:
    stmt = select(PacoteGCategoria.id).where(
        PacoteGCategoria.sub_base == sub_base, func.upper(PacoteGCategoria.nome) == nome.upper()
    )
    if ignorar_id is not None:
        stmt = stmt.where(PacoteGCategoria.id != ignorar_id)
    if db.scalars(stmt).first() is not None:
        raise HTTPException(status_code=409, detail=f"Já existe a categoria {nome}.")


@router.get("", response_model=List[CategoriaGOut])
def listar(
    incluir_inativas: bool = False,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Qualquer usuário da sub_base vê nome/descrição (para marcar); valores só admin/root."""
    sub_base = resolver_sub_base(db, current_user)
    com_valores = _role(current_user) in (0, 1)
    if not listar_categorias(db, sub_base):
        garantir_categoria_padrao(db, sub_base)
        db.commit()
    cats = listar_categorias(db, sub_base, somente_ativas=not (incluir_inativas and com_valores))
    qtds: Dict[int, int] = {}
    if com_valores and cats:
        rows = db.execute(
            select(Saida.categoria_g_id, func.count())
            .where(Saida.sub_base == sub_base, Saida.categoria_g_id.in_([c.id for c in cats]))
            .group_by(Saida.categoria_g_id)
        ).all()
        qtds = {int(cid): int(n) for cid, n in rows}
    return [_out(c, com_valores, qtds.get(int(c.id), 0) if com_valores else None) for c in cats]


@router.post("", response_model=CategoriaGOut, status_code=201)
def criar(body: CategoriaGIn, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    _exigir_admin(current_user)
    sub_base = resolver_sub_base(db, current_user)
    _nome_disponivel(db, sub_base, body.nome)
    ordem = body.ordem
    if ordem is None:
        ordem = int(db.scalar(
            select(func.coalesce(func.max(PacoteGCategoria.ordem), -1)).where(PacoteGCategoria.sub_base == sub_base)
        )) + 1
    cat = PacoteGCategoria(
        sub_base=sub_base, nome=body.nome, descricao=body.descricao,
        motoboy_modo=body.motoboy_modo, motoboy_valor=body.motoboy_valor,
        base_modo=body.base_modo, base_valor=body.base_valor,
        ordem=ordem, ativo=body.ativo,
    )
    db.add(cat)
    db.commit()
    db.refresh(cat)
    return _out(cat, True, 0)


@router.patch("/{categoria_id}", response_model=CategoriaGOut)
def atualizar(
    categoria_id: int,
    body: CategoriaGPatch,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _exigir_admin(current_user)
    sub_base = resolver_sub_base(db, current_user)
    cat = obter_categoria(db, sub_base, categoria_id, exigir_ativa=False)
    dados = body.model_dump(exclude_unset=True)
    if dados.get("nome"):
        _nome_disponivel(db, sub_base, dados["nome"], ignorar_id=cat.id)
    if "descricao" in dados:
        dados["descricao"] = (dados["descricao"] or "").strip() or None
    if dados.get("ativo") is False:
        outras_ativas = [c for c in listar_categorias(db, sub_base, somente_ativas=True) if c.id != cat.id]
        if not outras_ativas:
            raise HTTPException(status_code=422, detail="Mantenha ao menos uma categoria ativa.")
    for campo, valor in dados.items():
        if valor is None and campo != "descricao":
            continue
        setattr(cat, campo, valor)
    cat.updated_at = datetime.now()
    db.commit()
    db.refresh(cat)
    qtd = db.scalar(select(func.count()).where(Saida.sub_base == sub_base, Saida.categoria_g_id == cat.id)) or 0
    return _out(cat, True, int(qtd))
