"""Rotas de envio próprio / etiquetas comerciais."""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth import get_current_user
from db import get_db
from envio_proprio_service import criar_envio_proprio, pdf_from_envio
from models import BasePreco, BaseSellerDados, EnvioProprio, User
from base import _resolve_user_sub_base

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/etiquetas", tags=["Etiquetas"])


class EnderecoIn(BaseModel):
    nome: Optional[str] = None
    telefone: Optional[str] = None
    cep: Optional[str] = None
    rua: Optional[str] = None
    numero: Optional[str] = None
    complemento: Optional[str] = None
    bairro: Optional[str] = None
    cidade: Optional[str] = None
    uf: Optional[str] = None


class EnvioProprioCreateIn(BaseModel):
    origem_remetente: str = Field(description="seller | manual")
    id_base: Optional[int] = None
    remetente_telefone: Optional[str] = None
    remetente: Optional[EnderecoIn] = None
    destinatario: EnderecoIn
    peso_kg: Optional[float] = None
    dimensoes: Optional[str] = None
    observacao: Optional[str] = None


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
    role = int(getattr(current_user, "role", -1) or -1)
    if role not in (0, 1, 2):
        raise HTTPException(403, "Acesso restrito a administradores e operadores.")


@router.get("/remetentes", response_model=List[RemetenteOut])
def listar_remetentes(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Sellers da sub_base autenticada com endereço estruturado (tenant-safe)."""
    _assert_operacao_etiqueta(current_user)
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


@router.post("/envios-proprios")
def criar_envio(
    body: EnvioProprioCreateIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    payload: Dict[str, Any] = body.model_dump()
    if body.remetente:
        payload["remetente"] = body.remetente.model_dump()
    if body.destinatario:
        payload["destinatario"] = body.destinatario.model_dump()

    envio, saida, pdf = criar_envio_proprio(db, current_user=current_user, payload=payload)
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
