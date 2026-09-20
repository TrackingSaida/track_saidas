"""Portal do Seller: auth isolada, emissão e gestão de acesso pelo staff."""
from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from auth import _coerce_role_int, get_current_user, get_password_hash
from cobertura_cep_service import avaliar_cobertura_detalhada
from db import get_db
from envio_proprio_service import (
    MSG_CEP_FORA_COBERTURA,
    assert_limite_diario,
    cancelar_envio_proprio,
    criar_envio_proprio,
    pdf_from_envio,
    require_owner_tipo_base,
)
from etiqueta_identidade_service import resolver_nome_exibicao, resolver_slogan
from models import BasePreco, BaseSellerDados, EnvioProprio, Owner, Saida, SellerPortalAccess, User
from seller_portal_auth import (
    SellerContext,
    authenticate_seller,
    get_current_seller,
    issue_seller_token,
)
from seller_portal_dashboard_service import dashboard_seller
from seller_portal_pedidos_service import detalhe_pedido_seller, listar_pedidos_seller, status_pedido_amigavel

router = APIRouter(prefix="/portal", tags=["Portal Seller"])

_ADDR_REQUIRED = ("rua", "numero", "bairro", "cidade", "cep")


def _assert_admin(current_user: User) -> None:
    if _coerce_role_int(getattr(current_user, "role", None)) not in (0, 1):
        raise HTTPException(403, "Acesso restrito a administradores.")


def _seller_endereco_ok(seller: Optional[BaseSellerDados]) -> bool:
    if not seller:
        return False
    return all((getattr(seller, f, None) or "").strip() for f in _ADDR_REQUIRED)


def _nova_senha() -> str:
    return secrets.token_urlsafe(9)


def _acesso_out(access, senha: Optional[str] = None) -> Dict[str, Any]:
    out = {
        "id": int(access.id),
        "id_base": int(access.id_base),
        "login": access.login,
        "ativo": bool(access.ativo),
        "must_change_password": bool(access.must_change_password),
        "status": "ativo" if access.ativo else "desativado",
    }
    if senha:
        out["senha_temporaria"] = senha
    return out


def _parse_date_bound(raw: Optional[str], *, end_of_day: bool = False) -> Optional[datetime]:
    txt = (raw or "").strip()[:10]
    if not txt:
        return None
    try:
        d = datetime.strptime(txt, "%Y-%m-%d").date()
    except ValueError:
        return None
    if end_of_day:
        return datetime.combine(d, datetime.max.time().replace(microsecond=0))
    return datetime.combine(d, datetime.min.time())


class SellerLoginIn(BaseModel):
    login: str
    password: str


class SellerPasswordIn(BaseModel):
    current_password: Optional[str] = None
    new_password: str = Field(min_length=8)


class PortalDestinatarioIn(BaseModel):
    nome: str
    telefone: Optional[str] = None
    cep: str
    rua: str
    numero: str
    complemento: Optional[str] = None
    bairro: str
    cidade: str
    uf: str


class PortalEmitirIn(BaseModel):
    destinatario: PortalDestinatarioIn
    id_base: Optional[int] = None
    origem_remetente: Optional[str] = None
    peso_kg: Optional[float] = None
    dimensoes: Optional[str] = None
    observacao: Optional[str] = None


class LiberarPortalIn(BaseModel):
    id_base: int
    etiqueta_limite_diario: Optional[int] = Field(default=None, ge=1, le=9999)


class PortalAcessoPatch(BaseModel):
    ativo: Optional[bool] = None
    etiqueta_limite_diario: Optional[int] = Field(default=None, ge=1, le=9999)


@router.post("/auth/login")
def portal_login(body: SellerLoginIn, db: Session = Depends(get_db)):
    access = authenticate_seller(db, body.login, body.password)
    token = issue_seller_token(access)
    return {
        "access_token": token,
        "token_type": "bearer",
        "must_change_password": bool(access.must_change_password),
        "login": access.login,
    }


@router.get("/me")
def portal_me(
    db: Session = Depends(get_db),
    seller: SellerContext = Depends(get_current_seller),
):
    base = db.get(BasePreco, seller.id_base)
    seller_nome = (base.base or "").strip() if base else ""
    owner = getattr(seller, "owner", None)
    if owner is None:
        owner = db.scalar(select(Owner).where(Owner.sub_base == seller.sub_base).limit(1))
    transportadora_nome = resolver_nome_exibicao(owner)
    slogan = resolver_slogan(owner)
    out: Dict[str, Any] = {
        "login": seller.login,
        "id_base": seller.id_base,
        "sub_base": seller.sub_base,
        "must_change_password": seller.must_change_password,
        "seller_nome": seller_nome or None,
        "transportadora_nome": transportadora_nome,
    }
    if slogan and len(slogan) <= 80:
        out["transportadora_slogan"] = slogan
    return out


@router.get("/dashboard")
def portal_dashboard(
    periodo: Optional[str] = "hoje",
    de: Optional[str] = None,
    ate: Optional[str] = None,
    db: Session = Depends(get_db),
    seller: SellerContext = Depends(get_current_seller),
):
    return dashboard_seller(db, seller, periodo=periodo, de=de, ate=ate)


@router.post("/auth/password")
def portal_change_password(
    body: SellerPasswordIn,
    db: Session = Depends(get_db),
    seller: SellerContext = Depends(get_current_seller),
):
    access = seller.access
    from auth import verify_password

    if not access.must_change_password:
        if not body.current_password or not verify_password(body.current_password, access.password_hash):
            raise HTTPException(403, "Senha atual incorreta.")
    access.password_hash = get_password_hash(body.new_password)
    access.must_change_password = False
    db.commit()
    return {"ok": True}


@router.get("/remetente")
def portal_remetente(
    db: Session = Depends(get_db),
    seller: SellerContext = Depends(get_current_seller),
):
    base = db.get(BasePreco, seller.id_base)
    dados = db.scalar(select(BaseSellerDados).where(BaseSellerDados.base_id == seller.id_base).limit(1))
    if not base or (base.sub_base or "").strip() != seller.sub_base:
        raise HTTPException(404, "Seller não encontrado.")
    return {
        "id_base": seller.id_base,
        "nome": (base.base or "").strip(),
        "cep": getattr(dados, "cep", None),
        "rua": getattr(dados, "rua", None),
        "numero": getattr(dados, "numero", None),
        "complemento": getattr(dados, "complemento", None),
        "bairro": getattr(dados, "bairro", None),
        "cidade": getattr(dados, "cidade", None),
        "uf": getattr(dados, "estado", None),
    }


@router.get("/cobertura")
def portal_cobertura(
    cep: Optional[str] = None,
    db: Session = Depends(get_db),
    seller: SellerContext = Depends(get_current_seller),
):
    detalhe = avaliar_cobertura_detalhada(db, seller.sub_base, cep)
    out = {
        "coberto": detalhe["coberto"] if cep else True,
        "cep": detalhe.get("cep") or None,
        "message": None if (not cep or detalhe["coberto"]) else MSG_CEP_FORA_COBERTURA,
        "regiao_nome": detalhe.get("regiao_nome"),
        "prefixo_match": detalhe.get("prefixo_match"),
        "modo": detalhe.get("modo"),
        "regioes": detalhe.get("regioes") or [],
        "prefixos_sem_regiao": detalhe.get("prefixos_sem_regiao") or [],
    }
    return out


@router.post("/envios")
def portal_emitir(
    body: PortalEmitirIn,
    db: Session = Depends(get_db),
    seller: SellerContext = Depends(get_current_seller),
):
    if seller.must_change_password:
        raise HTTPException(403, "Troque a senha temporária antes de emitir etiquetas.")
    assert_limite_diario(db, seller.sub_base, seller.id_base)
    payload = {
        "origem_remetente": "seller",
        "id_base": seller.id_base,
        "destinatario": body.destinatario.model_dump(),
        "peso_kg": body.peso_kg,
        "dimensoes": body.dimensoes,
        "observacao": body.observacao,
    }
    envio, saida, pdf, _aviso = criar_envio_proprio(
        db,
        payload=payload,
        origem_emissao="portal",
        force_id_base=seller.id_base,
        sub_base_override=seller.sub_base,
    )
    filename = f"etq-envio-{envio.codigo}.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Envio-Id": str(envio.id_envio),
            "X-Codigo": envio.codigo,
            "X-Id-Saida": str(saida.id_saida),
            "Access-Control-Expose-Headers": "X-Envio-Id, X-Codigo, X-Id-Saida, Content-Disposition",
        },
    )


@router.get("/envios")
def portal_listar_envios(
    page: int = 1,
    per_page: int = 20,
    q: Optional[str] = None,
    status: Optional[str] = None,
    de: Optional[str] = None,
    ate: Optional[str] = None,
    db: Session = Depends(get_db),
    seller: SellerContext = Depends(get_current_seller),
):
    page = max(1, int(page or 1))
    per_page = min(100, max(1, int(per_page or 20)))
    filt = [
        EnvioProprio.sub_base == seller.sub_base,
        EnvioProprio.id_base == seller.id_base,
    ]
    term = (q or "").strip()[:80]
    if term:
        like = f"%{term}%"
        filt.append((EnvioProprio.codigo.ilike(like)) | (EnvioProprio.dest_nome.ilike(like)))
    start = _parse_date_bound(de, end_of_day=False)
    end = _parse_date_bound(ate, end_of_day=True)
    if start is not None:
        filt.append(EnvioProprio.created_at >= start)
    if end is not None:
        filt.append(EnvioProprio.created_at <= end)

    status_key = (status or "").strip().lower()
    status_map = {
        "aguardando_coleta": ("ETIQUETADO",),
        "etiquetado": ("ETIQUETADO",),
        "cancelado": ("CANCELADO",),
        "entregue": ("ENTREGUE",),
        "ausente": ("AUSENTE",),
        "em_rota": ("EM_ROTA", "SAIU_PARA_ENTREGA", "SAIU_PRA_ENTREGA"),
        "em_entrega": ("EM_ROTA", "SAIU_PARA_ENTREGA", "SAIU_PRA_ENTREGA"),
        "coletado": ("COLETADO", "SAIU"),
    }
    status_vals = status_map.get(status_key)

    if status_vals:
        base_q = (
            select(EnvioProprio)
            .join(Saida, Saida.id_saida == EnvioProprio.id_saida)
            .where(*filt, func.upper(func.coalesce(Saida.status, "")).in_(status_vals))
        )
        count_q = (
            select(func.count(EnvioProprio.id_envio))
            .select_from(EnvioProprio)
            .join(Saida, Saida.id_saida == EnvioProprio.id_saida)
            .where(*filt, func.upper(func.coalesce(Saida.status, "")).in_(status_vals))
        )
    else:
        base_q = select(EnvioProprio).where(*filt)
        count_q = select(func.count(EnvioProprio.id_envio)).where(*filt)

    total = int(db.scalar(count_q) or 0)
    rows = list(
        db.scalars(
            base_q.order_by(EnvioProprio.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        ).all()
    )
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
            {
                "id_envio": int(envio.id_envio),
                "id_saida": int(envio.id_saida) if envio.id_saida else None,
                "codigo": envio.codigo,
                "dest_nome": envio.dest_nome,
                "dest_cidade": envio.dest_cidade,
                "status": st,
                "status_label": status_pedido_amigavel(st),
                "created_at": envio.created_at.isoformat() if envio.created_at else None,
                "pode_cancelar": (st or "").strip().upper() == "ETIQUETADO",
            }
        )
    return {"total": total, "page": page, "per_page": per_page, "items": items}


@router.get("/pedidos")
def portal_listar_pedidos(
    page: int = 1,
    per_page: int = 20,
    q: Optional[str] = None,
    canal: Optional[str] = None,
    status: Optional[str] = None,
    de: Optional[str] = None,
    ate: Optional[str] = None,
    db: Session = Depends(get_db),
    seller: SellerContext = Depends(get_current_seller),
):
    return listar_pedidos_seller(
        db,
        seller,
        page=page,
        per_page=per_page,
        q=q,
        canal=canal,
        status=status,
        de=de,
        ate=ate,
    )


@router.get("/pedidos/{id_saida}")
def portal_detalhe_pedido(
    id_saida: int,
    db: Session = Depends(get_db),
    seller: SellerContext = Depends(get_current_seller),
):
    return detalhe_pedido_seller(db, seller, id_saida)


def _envio_do_seller(db: Session, seller: SellerContext, id_envio: int) -> EnvioProprio:
    envio = db.get(EnvioProprio, id_envio)
    if (
        not envio
        or (envio.sub_base or "").strip() != seller.sub_base
        or int(envio.id_base or 0) != seller.id_base
    ):
        raise HTTPException(404, "Envio não encontrado.")
    return envio


@router.get("/envios/{id_envio}/pdf")
def portal_pdf(
    id_envio: int,
    db: Session = Depends(get_db),
    seller: SellerContext = Depends(get_current_seller),
):
    envio = _envio_do_seller(db, seller, id_envio)
    pdf = pdf_from_envio(db, envio)
    filename = f"etq-envio-{envio.codigo}.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/envios/{id_envio}/cancelar")
def portal_cancelar(
    id_envio: int,
    db: Session = Depends(get_db),
    seller: SellerContext = Depends(get_current_seller),
):
    envio = _envio_do_seller(db, seller, id_envio)
    cancelar_envio_proprio(db, envio, cancelado_por=seller.login)
    return {"ok": True, "status_label": "Cancelado"}


@router.get("/acessos")
def staff_get_acesso(
    id_base: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _assert_admin(current_user)
    require_owner_tipo_base(db, current_user)
    from base import _resolve_user_sub_base

    sub_base = _resolve_user_sub_base(db, current_user)
    base = db.get(BasePreco, int(id_base))
    if not base or (base.sub_base or "").strip() != sub_base:
        raise HTTPException(404, "Seller não encontrado.")
    access = db.scalar(
        select(SellerPortalAccess).where(
            SellerPortalAccess.id_base == int(id_base),
            SellerPortalAccess.sub_base == sub_base,
        ).order_by(SellerPortalAccess.id.desc())
    )
    dados = db.scalar(select(BaseSellerDados).where(BaseSellerDados.base_id == int(id_base)).limit(1))
    status = "sem_acesso"
    payload: Dict[str, Any] = {
        "id_base": int(id_base),
        "endereco_completo": _seller_endereco_ok(dados),
        "seller_ativo": bool(base.ativo),
        "etiqueta_limite_diario": getattr(base, "etiqueta_limite_diario", None),
        "acesso": None,
        "status": status,
    }
    if access:
        payload["acesso"] = _acesso_out(access)
        payload["status"] = "ativo" if access.ativo else "desativado"
    return payload


@router.post("/acessos")
def staff_liberar(
    body: LiberarPortalIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _assert_admin(current_user)
    require_owner_tipo_base(db, current_user)
    from base import _resolve_user_sub_base

    sub_base = _resolve_user_sub_base(db, current_user)
    base = db.get(BasePreco, int(body.id_base))
    if not base or (base.sub_base or "").strip() != sub_base:
        raise HTTPException(404, "Seller não encontrado.")
    if not base.ativo:
        raise HTTPException(422, "Ative o seller antes de liberar o portal.")
    dados = db.scalar(select(BaseSellerDados).where(BaseSellerDados.base_id == int(body.id_base)).limit(1))
    if not _seller_endereco_ok(dados):
        raise HTTPException(422, "Complete o endereço do seller antes de liberar o portal.")

    existente = db.scalar(
        select(SellerPortalAccess).where(
            SellerPortalAccess.id_base == int(body.id_base),
            SellerPortalAccess.sub_base == sub_base,
            SellerPortalAccess.ativo.is_(True),
        )
    )
    if existente:
        raise HTTPException(409, "Este seller já possui acesso ao portal.")

    senha = _nova_senha()
    login = f"s{int(body.id_base)}"
    clash = db.scalar(select(SellerPortalAccess.id).where(SellerPortalAccess.login == login))
    if clash:
        login = f"s{int(body.id_base)}-{secrets.token_hex(2)}"
    access = SellerPortalAccess(
        sub_base=sub_base,
        id_base=int(body.id_base),
        login=login,
        password_hash=get_password_hash(senha),
        ativo=True,
        must_change_password=True,
        criado_por_user_id=getattr(current_user, "id", None),
    )
    db.add(access)
    if body.etiqueta_limite_diario is not None:
        base.etiqueta_limite_diario = int(body.etiqueta_limite_diario)
    db.commit()
    db.refresh(access)
    return _acesso_out(access, senha=senha)


@router.post("/acessos/{access_id}/reset-senha")
def staff_reset_senha(
    access_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _assert_admin(current_user)
    from base import _resolve_user_sub_base

    sub_base = _resolve_user_sub_base(db, current_user)
    access = db.get(SellerPortalAccess, access_id)
    if not access or (access.sub_base or "").strip() != sub_base:
        raise HTTPException(404, "Acesso não encontrado.")
    senha = _nova_senha()
    access.password_hash = get_password_hash(senha)
    access.must_change_password = True
    access.ativo = True
    db.commit()
    return _acesso_out(access, senha=senha)


@router.patch("/acessos/{access_id}")
def staff_patch_acesso(
    access_id: int,
    body: PortalAcessoPatch,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _assert_admin(current_user)
    from base import _resolve_user_sub_base

    sub_base = _resolve_user_sub_base(db, current_user)
    access = db.get(SellerPortalAccess, access_id)
    if not access or (access.sub_base or "").strip() != sub_base:
        raise HTTPException(404, "Acesso não encontrado.")
    if body.ativo is not None:
        access.ativo = bool(body.ativo)
    if body.etiqueta_limite_diario is not None:
        base = db.get(BasePreco, int(access.id_base))
        if base:
            base.etiqueta_limite_diario = int(body.etiqueta_limite_diario)
    db.commit()
    db.refresh(access)
    return _acesso_out(access)
