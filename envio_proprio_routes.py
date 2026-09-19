"""Rotas de envio próprio / etiquetas comerciais."""
from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from auth import _coerce_role_int, get_current_user
from db import get_db
from envio_proprio_service import (
    cancelar_envio_proprio,
    criar_envio_proprio,
    pdf_from_envio,
    require_owner_tipo_base,
    status_etiqueta_amigavel,
)
from models import BasePreco, BaseSellerDados, EnvioProprio, Saida, User
from base import _resolve_user_sub_base

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/etiquetas", tags=["Etiquetas"])

_UF_RE = re.compile(r"^[A-Za-z]{2}$")
_CEP_RE = re.compile(r"^\d{8}$")
_PHONE_RE = re.compile(r"^\d{10,11}$")


def _digits(value: Optional[str], max_len: int) -> Optional[str]:
    if value is None:
        return None
    digits = re.sub(r"\D+", "", str(value))
    if not digits:
        return None
    return digits[:max_len]


def _clean_text(value: Optional[str], max_len: int) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:max_len]


class EnderecoIn(BaseModel):
    nome: Optional[str] = Field(default=None, max_length=120)
    telefone: Optional[str] = Field(default=None, max_length=20)
    cep: Optional[str] = Field(default=None, max_length=9)
    rua: Optional[str] = Field(default=None, max_length=180)
    numero: Optional[str] = Field(default=None, max_length=20)
    complemento: Optional[str] = Field(default=None, max_length=80)
    bairro: Optional[str] = Field(default=None, max_length=80)
    cidade: Optional[str] = Field(default=None, max_length=80)
    uf: Optional[str] = Field(default=None, max_length=2)

    @field_validator("nome", "rua", "numero", "complemento", "bairro", "cidade", mode="before")
    @classmethod
    def _trim_text(cls, v):
        if v is None:
            return None
        text = str(v).strip()
        return text or None

    @field_validator("telefone", mode="before")
    @classmethod
    def _norm_telefone(cls, v):
        return _digits(v, 11)

    @field_validator("cep", mode="before")
    @classmethod
    def _norm_cep(cls, v):
        return _digits(v, 8)

    @field_validator("uf", mode="before")
    @classmethod
    def _norm_uf(cls, v):
        if v is None:
            return None
        uf = re.sub(r"[^A-Za-z]", "", str(v)).upper()[:2]
        return uf or None

    @model_validator(mode="after")
    def _validate_formats(self):
        if self.telefone is not None and not _PHONE_RE.match(self.telefone):
            raise ValueError("Telefone inválido. Use DDD + número (10 ou 11 dígitos).")
        if self.cep is not None and not _CEP_RE.match(self.cep):
            raise ValueError("CEP inválido. Use 8 dígitos.")
        if self.uf is not None and not _UF_RE.match(self.uf):
            raise ValueError("UF inválida. Use 2 letras (ex.: SP).")
        return self


class EnvioProprioCreateIn(BaseModel):
    origem_remetente: str = Field(description="seller | manual")
    id_base: Optional[int] = None
    remetente_telefone: Optional[str] = Field(default=None, max_length=20)
    remetente: Optional[EnderecoIn] = None
    destinatario: EnderecoIn
    peso_kg: Optional[float] = Field(default=None, ge=0, le=9999)
    dimensoes: Optional[str] = Field(default=None, max_length=40)
    observacao: Optional[str] = Field(default=None, max_length=500)

    @field_validator("remetente_telefone", mode="before")
    @classmethod
    def _norm_rem_tel(cls, v):
        return _digits(v, 11)

    @field_validator("dimensoes", mode="before")
    @classmethod
    def _trim_dimensoes(cls, v):
        return _clean_text(v, 40)

    @field_validator("observacao", mode="before")
    @classmethod
    def _trim_obs(cls, v):
        return _clean_text(v, 500)

    @model_validator(mode="after")
    def _validate_rem_tel(self):
        if self.remetente_telefone is not None and not _PHONE_RE.match(self.remetente_telefone):
            raise ValueError("Telefone do remetente inválido. Use DDD + número (10 ou 11 dígitos).")
        return self


class RemetenteOut(BaseModel):
    id_base: int
    nome: str
    tem_endereco_completo: bool
    telefone: Optional[str] = None
    cep: Optional[str] = None
    rua: Optional[str] = None
    numero: Optional[str] = None
    complemento: Optional[str] = None
    bairro: Optional[str] = None
    cidade: Optional[str] = None
    uf: Optional[str] = None


def _assert_operacao_etiqueta(current_user: User) -> None:
    role = _coerce_role_int(getattr(current_user, "role", None))
    if role not in (0, 1, 2):
        raise HTTPException(403, "Acesso restrito a administradores e operadores.")


@router.get("/remetentes", response_model=List[RemetenteOut])
def listar_remetentes(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Sellers da sub_base autenticada com endereço estruturado (tenant-safe)."""
    _assert_operacao_etiqueta(current_user)
    require_owner_tipo_base(db, current_user)
    sub_base = _resolve_user_sub_base(db, current_user)
    bases = list(
        db.scalars(
            select(BasePreco)
            .where(BasePreco.sub_base == sub_base, BasePreco.ativo.is_(True))
            .order_by(BasePreco.base)
        ).all()
    )
    base_ids = [int(b.id_base) for b in bases]
    sellers = {}
    if base_ids:
        rows = db.scalars(select(BaseSellerDados).where(BaseSellerDados.base_id.in_(base_ids))).all()
        for s in rows:
            if s.base_id is not None:
                sellers[int(s.base_id)] = s

    out: List[RemetenteOut] = []
    for b in bases:
        s = sellers.get(int(b.id_base))
        completo = bool(
            s
            and (s.rua or "").strip()
            and (s.numero or "").strip()
            and (s.bairro or "").strip()
            and (s.cidade or "").strip()
            and (s.cep or "").strip()
        )
        out.append(
            RemetenteOut(
                id_base=int(b.id_base),
                nome=(b.base or "").strip() or f"Base {b.id_base}",
                tem_endereco_completo=completo,
                cep=getattr(s, "cep", None) if s else None,
                rua=getattr(s, "rua", None) if s else None,
                numero=getattr(s, "numero", None) if s else None,
                complemento=getattr(s, "complemento", None) if s else None,
                bairro=getattr(s, "bairro", None) if s else None,
                cidade=getattr(s, "cidade", None) if s else None,
                uf=getattr(s, "estado", None) if s else None,
            )
        )
    return out


class EnvioProprioListItemOut(BaseModel):
    id_envio: int
    codigo: str
    id_saida: Optional[int] = None
    id_base: Optional[int] = None
    origem_emissao: Optional[str] = None
    dest_nome: Optional[str] = None
    dest_cep: Optional[str] = None
    dest_cidade: Optional[str] = None
    status: Optional[str] = None
    status_label: Optional[str] = None
    created_at: Optional[str] = None


@router.get("/envios-proprios")
def listar_envios_proprios(
    page: int = 1,
    per_page: int = 20,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _assert_operacao_etiqueta(current_user)
    require_owner_tipo_base(db, current_user)
    sub_base = _resolve_user_sub_base(db, current_user)
    page = max(1, int(page or 1))
    per_page = min(100, max(1, int(per_page or 20)))
    q = (
        select(EnvioProprio)
        .where(EnvioProprio.sub_base == sub_base)
        .order_by(EnvioProprio.created_at.desc())
    )
    total = int(
        db.scalar(
            select(func.count(EnvioProprio.id_envio)).where(EnvioProprio.sub_base == sub_base)
        )
        or 0
    )
    rows = list(db.scalars(q.offset((page - 1) * per_page).limit(per_page)).all())
    saida_ids = [int(r.id_saida) for r in rows if r.id_saida]
    saidas = {}
    if saida_ids:
        for s in db.scalars(select(Saida).where(Saida.id_saida.in_(saida_ids))).all():
            saidas[int(s.id_saida)] = s
    items = []
    for envio in rows:
        saida = saidas.get(int(envio.id_saida)) if envio.id_saida else None
        st = getattr(saida, "status", None) if saida else None
        items.append(
            EnvioProprioListItemOut(
                id_envio=int(envio.id_envio),
                codigo=envio.codigo,
                id_saida=envio.id_saida,
                id_base=envio.id_base,
                origem_emissao=getattr(envio, "origem_emissao", None),
                dest_nome=envio.dest_nome,
                dest_cep=envio.dest_cep,
                dest_cidade=envio.dest_cidade,
                status=st,
                status_label=status_etiqueta_amigavel(st),
                created_at=envio.created_at.isoformat() if envio.created_at else None,
            )
        )
    return {"total": total, "page": page, "per_page": per_page, "items": items}


@router.post("/envios-proprios/{id_envio}/cancelar")
def cancelar_envio_staff(
    id_envio: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _assert_operacao_etiqueta(current_user)
    require_owner_tipo_base(db, current_user)
    sub_base = _resolve_user_sub_base(db, current_user)
    envio = db.get(EnvioProprio, id_envio)
    if not envio or (envio.sub_base or "").strip() != sub_base:
        raise HTTPException(404, "Envio não encontrado.")
    cancelar_envio_proprio(
        db,
        envio,
        cancelado_por=getattr(current_user, "username", None) or str(current_user.id),
    )
    return {"ok": True, "id_envio": id_envio, "status_label": "Cancelada"}


@router.post("/envios-proprios")
def criar_envio(
    body: EnvioProprioCreateIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _assert_operacao_etiqueta(current_user)
    require_owner_tipo_base(db, current_user)
    payload: Dict[str, Any] = body.model_dump()
    if body.remetente:
        payload["remetente"] = body.remetente.model_dump()
    if body.destinatario:
        payload["destinatario"] = body.destinatario.model_dump()

    envio, saida, pdf, cobertura_aviso = criar_envio_proprio(
        db, current_user=current_user, payload=payload, origem_emissao="staff"
    )
    filename = f"etq-envio-{envio.codigo}.pdf"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Envio-Id": str(envio.id_envio),
        "X-Codigo": envio.codigo,
        "X-Id-Saida": str(saida.id_saida),
    }
    if cobertura_aviso:
        headers["X-Cobertura-Aviso"] = cobertura_aviso
        headers["Access-Control-Expose-Headers"] = (
            "X-Envio-Id, X-Codigo, X-Id-Saida, X-Cobertura-Aviso, Content-Disposition"
        )
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers=headers,
    )


@router.get("/envios-proprios/reimpressao/{codigo}")
def reimprimir_envio_por_codigo(
    codigo: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Reimprime a etiqueta comercial de um envio próprio (RTE…).
    Exige que a sub_base atual tenha a Saida correspondente (tenant-safe).
    Declarado antes de /{id_envio} para não colidir com path param inteiro.
    """
    from models import Saida
    from envio_proprio_service import get_envio_by_codigo_global, is_codigo_rte

    _assert_operacao_etiqueta(current_user)
    require_owner_tipo_base(db, current_user)
    sub_base = _resolve_user_sub_base(db, current_user)
    cod = (codigo or "").strip().upper()
    if not is_codigo_rte(cod):
        raise HTTPException(422, "Informe um código de envio próprio (RTE…).")

    saida = db.scalar(
        select(Saida)
        .where(Saida.sub_base == sub_base, Saida.codigo == cod)
        .limit(1)
    )
    if not saida:
        raise HTTPException(404, "Pedido não encontrado nesta base.")

    envio = get_envio_by_codigo_global(db, cod)
    if not envio:
        raise HTTPException(404, "Envio próprio não encontrado para este código.")

    try:
        pdf = pdf_from_envio(db, envio)
    except Exception as e:
        logger.exception("erro_reimpressao_envio codigo=%s", cod)
        raise HTTPException(500, "Falha ao gerar PDF da etiqueta.") from e

    filename = f"etq-envio-{envio.codigo}.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Envio-Id": str(envio.id_envio),
            "X-Codigo": envio.codigo,
            "X-Id-Saida": str(saida.id_saida),
        },
    )


@router.get("/envios-proprios/{id_envio}")
def get_envio(
    id_envio: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _assert_operacao_etiqueta(current_user)
    require_owner_tipo_base(db, current_user)
    sub_base = _resolve_user_sub_base(db, current_user)
    envio = db.get(EnvioProprio, id_envio)
    if not envio or (envio.sub_base or "").strip() != sub_base:
        raise HTTPException(404, "Envio não encontrado.")
    return {
        "id_envio": envio.id_envio,
        "codigo": envio.codigo,
        "id_saida": envio.id_saida,
        "origem_remetente": envio.origem_remetente,
        "remetente": {
            "nome": envio.remetente_nome,
            "telefone": envio.remetente_telefone,
            "cep": envio.remetente_cep,
            "rua": envio.remetente_rua,
            "numero": envio.remetente_numero,
            "complemento": envio.remetente_complemento,
            "bairro": envio.remetente_bairro,
            "cidade": envio.remetente_cidade,
            "uf": envio.remetente_uf,
        },
        "destinatario": {
            "nome": envio.dest_nome,
            "telefone": envio.dest_telefone,
            "cep": envio.dest_cep,
            "rua": envio.dest_rua,
            "numero": envio.dest_numero,
            "complemento": envio.dest_complemento,
            "bairro": envio.dest_bairro,
            "cidade": envio.dest_cidade,
            "uf": envio.dest_uf,
        },
        "peso_kg": float(envio.peso_kg) if envio.peso_kg is not None else None,
        "dimensoes": envio.dimensoes,
        "observacao": envio.observacao,
        "created_at": envio.created_at.isoformat() if envio.created_at else None,
    }


@router.get("/envios-proprios/{id_envio}/pdf")
def get_envio_pdf(
    id_envio: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _assert_operacao_etiqueta(current_user)
    require_owner_tipo_base(db, current_user)
    sub_base = _resolve_user_sub_base(db, current_user)
    envio = db.get(EnvioProprio, id_envio)
    if not envio or (envio.sub_base or "").strip() != sub_base:
        raise HTTPException(404, "Envio não encontrado.")
    try:
        pdf = pdf_from_envio(db, envio)
    except Exception as e:
        logger.exception("erro_pdf_envio id=%s", id_envio)
        raise HTTPException(500, "Falha ao gerar PDF da etiqueta.") from e
    filename = f"etq-envio-{envio.codigo}.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Envio-Id": str(envio.id_envio),
            "X-Codigo": envio.codigo,
        },
    )
