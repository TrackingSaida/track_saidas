# base.py
from __future__ import annotations

from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, status, Query, UploadFile, File
from fastapi.responses import Response
from pydantic import BaseModel, Field, ConfigDict, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from models import User, BasePreco, BaseSellerDados, MotoboySubBase  # classe do models.py com __tablename__ = "base"
from base_import_service import (
    MODELO_FILENAME,
    XLSX_MEDIA_TYPE,
    confirmar_importacao,
    gerar_modelo_xlsx,
    preview_importacao,
)

router = APIRouter(prefix="/base", tags=["Base"])

# =========================
# Schemas
# =========================
class BaseCreate(BaseModel):
    base: str = Field(min_length=1)
    shopee: float = Field(ge=0)
    ml: float = Field(ge=0)
    avulso: float = Field(ge=0)
    # novo: toggle opcional; se não vier, usamos False (segue server_default)
    ativo: Optional[bool] = None
    dias_coleta: List[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5, 6])
    agenda_coleta_confirmada: bool = False

    @field_validator("dias_coleta")
    @classmethod
    def validar_dias_coleta(cls, value: List[int]) -> List[int]:
        dias = sorted(set(value))
        if not dias or any(dia < 1 or dia > 7 for dia in dias):
            raise ValueError("dias_coleta deve conter dias ISO entre 1 e 7")
        return dias
    model_config = ConfigDict(from_attributes=True)

class BaseOut(BaseModel):
    id_base: int
    base: Optional[str]
    sub_base: Optional[str]
    username: Optional[str]
    shopee: float
    ml: float
    avulso: float
    # novo: expor status
    ativo: bool
    dias_coleta: List[int]
    agenda_coleta_confirmada: bool
    # Endereço cadastrado em base_seller_dados (opcional; aditivo)
    endereco_completo: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)

class BaseUpdate(BaseModel):
    base: Optional[str] = None
    shopee: Optional[float] = Field(default=None, ge=0)
    ml: Optional[float]     = Field(default=None, ge=0)
    avulso: Optional[float] = Field(default=None, ge=0)
    # novo: permitir alterar status
    ativo: Optional[bool]   = None
    dias_coleta: Optional[List[int]] = None
    agenda_coleta_confirmada: Optional[bool] = None

    @field_validator("dias_coleta")
    @classmethod
    def validar_dias_coleta(cls, value: Optional[List[int]]) -> Optional[List[int]]:
        if value is None:
            return value
        dias = sorted(set(value))
        if not dias or any(dia < 1 or dia > 7 for dia in dias):
            raise ValueError("dias_coleta deve conter dias ISO entre 1 e 7")
        return dias
    model_config = ConfigDict(from_attributes=True)


class BaseImportConfirmIn(BaseModel):
    linhas: List[Dict[str, Any]] = Field(default_factory=list)

# =========================
# Helper
# =========================
def _resolve_user_sub_base(db: Session, current_user: User) -> str:
    # Preferir claim da sessão (JWT) — crítico para root com sub_base selecionada no login.
    # Motoboy (role=4): só aceita claim se estiver vinculada em MotoboySubBase (anti cross-tenant).
    token_sub_base = (getattr(current_user, "sub_base", None) or "").strip()
    role = getattr(current_user, "role", None)
    try:
        role_int = int(role) if role is not None and role != "" else None
    except (TypeError, ValueError):
        role_int = None

    if token_sub_base and role_int == 4:
        motoboy_id = getattr(current_user, "motoboy_id", None)
        if motoboy_id is not None:
            vinculado = db.scalar(
                select(MotoboySubBase.id).where(
                    MotoboySubBase.motoboy_id == int(motoboy_id),
                    MotoboySubBase.sub_base == token_sub_base,
                    MotoboySubBase.ativo.is_(True),
                )
            )
            if vinculado:
                return token_sub_base
        # Claim inválida/fora do vínculo: não vazar outro tenant — cai no fallback seguro
    elif token_sub_base:
        return token_sub_base

    user_id = getattr(current_user, "id", None)
    if role_int == 4 and user_id is not None:
        # Fallback motoboy: só vínculos ativos (nunca users.sub_base stale de outro tenant)
        from models import Motoboy

        motoboy = db.scalar(select(Motoboy).where(Motoboy.user_id == int(user_id)))
        if motoboy:
            rows = db.scalars(
                select(MotoboySubBase.sub_base).where(
                    MotoboySubBase.motoboy_id == motoboy.id_motoboy,
                    MotoboySubBase.ativo.is_(True),
                )
            ).all()
            sub_bases = sorted({(s or "").strip() for s in rows if (s or "").strip()})
            preferred = (getattr(current_user, "sub_base", None) or "").strip()
            if preferred and preferred in sub_bases:
                return preferred
            u = db.get(User, user_id)
            db_pref = (getattr(u, "sub_base", None) or "").strip() if u else ""
            if db_pref and db_pref in sub_bases:
                return db_pref
            if len(sub_bases) == 1:
                return sub_bases[0]
        raise HTTPException(status_code=401, detail="Usuário sem 'sub_base' válida vinculada.")

    if user_id is not None:
        u = db.get(User, user_id)
        if u and getattr(u, "sub_base", None):
            return u.sub_base
    email = getattr(current_user, "email", None)
    if email:
        u = db.scalars(select(User).where(User.email == email)).first()
        if u and getattr(u, "sub_base", None):
            return u.sub_base
    username = getattr(current_user, "username", None)
    if username:
        u = db.scalars(select(User).where(User.username == username)).first()
        if u and getattr(u, "sub_base", None):
            return u.sub_base
    raise HTTPException(status_code=401, detail="Usuário sem 'sub_base' definida em 'users'.")


def _format_endereco_seller(seller: BaseSellerDados) -> Optional[str]:
    """Monta endereço corrido a partir de base_seller_dados (campos vazios são omitidos)."""
    partes: List[str] = []
    if seller.rua:
        rua_num = str(seller.rua).strip()
        if seller.numero:
            rua_num = f"{rua_num}, {str(seller.numero).strip()}"
        partes.append(rua_num)
    if seller.complemento:
        partes.append(str(seller.complemento).strip())
    bairro_cidade: List[str] = []
    if seller.bairro:
        bairro_cidade.append(str(seller.bairro).strip())
    if seller.cidade:
        bairro_cidade.append(str(seller.cidade).strip())
    if bairro_cidade:
        partes.append(" - ".join(bairro_cidade))
    uf_cep: List[str] = []
    if seller.estado:
        uf_cep.append(str(seller.estado).strip())
    if seller.cep:
        uf_cep.append(str(seller.cep).strip())
    if uf_cep:
        partes.append(" ".join(uf_cep))
    texto = ", ".join([p for p in partes if p])
    return texto or None


def _base_to_out(obj: BasePreco, endereco_completo: Optional[str] = None) -> BaseOut:
    return BaseOut(
        id_base=obj.id_base,
        base=obj.base,
        sub_base=obj.sub_base,
        username=obj.username,
        shopee=float(obj.shopee or 0),
        ml=float(obj.ml or 0),
        avulso=float(obj.avulso or 0),
        ativo=bool(obj.ativo),
        dias_coleta=list(obj.dias_coleta or []),
        agenda_coleta_confirmada=bool(obj.agenda_coleta_confirmada),
        endereco_completo=endereco_completo,
    )


def _enderecos_por_base_ids(db: Session, base_ids: List[int]) -> Dict[int, str]:
    if not base_ids:
        return {}
    rows = db.scalars(
        select(BaseSellerDados).where(BaseSellerDados.base_id.in_(base_ids))
    ).all()
    out: Dict[int, str] = {}
    for seller in rows:
        if seller.base_id is None:
            continue
        texto = _format_endereco_seller(seller)
        if texto:
            out[int(seller.base_id)] = texto
    return out

# =========================
# POST /base
# =========================
@router.post("/", status_code=status.HTTP_201_CREATED)
def criar_precos_base(
    payload: BaseCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base_user = _resolve_user_sub_base(db, current_user)

    # Normaliza nome (trim)
    nome = (payload.base or "").strip()
    if not nome:
        raise HTTPException(status_code=400, detail="O campo 'base' não pode ficar vazio.")

    # Verificar duplicidade: mesma base dentro da mesma sub_base
    dup = db.scalars(
        select(BasePreco).where(
            BasePreco.sub_base == sub_base_user,
            BasePreco.base == nome
        )
    ).first()

    if dup:
        raise HTTPException(
            status_code=409,
            detail="Já existe um registro de preços para essa 'base' nesta sub_base."
        )

    # Criar objeto se não houver duplicidade
    obj = BasePreco(
        base=nome,
        sub_base=sub_base_user,
        username=getattr(current_user, "username", None),
        shopee=payload.shopee,
        ml=payload.ml,
        avulso=payload.avulso,
        ativo=bool(payload.ativo) if payload.ativo is not None else True,
        dias_coleta=payload.dias_coleta,
        agenda_coleta_confirmada=bool(payload.agenda_coleta_confirmada),
    )

    db.add(obj)
    db.commit()
    db.refresh(obj)

    return {"ok": True, "action": "created", "id_base": obj.id_base}

# =========================
# GET /base/
# =========================
@router.get("/", response_model=List[BaseOut])
def list_bases(
    q: Optional[str] = Query(None, description="Filtro por texto em 'base' (contém)"),
    status_flag: Optional[str] = Query(
        "todos",
        alias="status",
        description="Filtrar por status: ativo, inativo ou todos"
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base_user = _resolve_user_sub_base(db, current_user)

    stmt = select(BasePreco).where(BasePreco.sub_base == sub_base_user)

    if q:
        stmt = stmt.where(BasePreco.base.ilike(f"%{q.strip()}%"))

    # novo: filtro por ativo/inativo (opcional)
    if status_flag == "ativo":
        stmt = stmt.where(BasePreco.ativo.is_(True))
    elif status_flag in ("inativo", "inativos"):
        stmt = stmt.where(BasePreco.ativo.is_(False))
    # "todos" mantém sem filtro

    stmt = stmt.order_by(BasePreco.base)
    rows = db.scalars(stmt).all()
    enderecos = _enderecos_por_base_ids(db, [int(r.id_base) for r in rows])
    return [
        _base_to_out(r, enderecos.get(int(r.id_base)))
        for r in rows
    ]


# =========================
# Importação em massa (PRD-002) — rotas estáticas antes de /{id_base}
# =========================
@router.get("/import/modelo")
def baixar_modelo_import_bases(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Garante sessão válida / sub_base (mesmo escopo do create)
    _resolve_user_sub_base(db, current_user)
    content = gerar_modelo_xlsx()
    return Response(
        content=content,
        media_type=XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{MODELO_FILENAME}"',
        },
    )


@router.post("/import/preview")
async def preview_import_bases(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base_user = _resolve_user_sub_base(db, current_user)
    filename = (file.filename or "").lower()
    if filename and not filename.endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Envie um arquivo .xlsx.")
    content = await file.read()
    return preview_importacao(db, sub_base_user, content)


@router.post("/import/confirmar")
def confirmar_import_bases(
    body: BaseImportConfirmIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base_user = _resolve_user_sub_base(db, current_user)
    return confirmar_importacao(db, sub_base_user, current_user, body.linhas)


# =========================
# GET /base/{id_base}
# =========================
@router.get("/{id_base}", response_model=BaseOut)
def get_base(
    id_base: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base_user = _resolve_user_sub_base(db, current_user)
    obj = db.get(BasePreco, id_base)
    if not obj or obj.sub_base != sub_base_user:
        raise HTTPException(status_code=404, detail="Não encontrado")
    enderecos = _enderecos_por_base_ids(db, [int(obj.id_base)])
    return _base_to_out(obj, enderecos.get(int(obj.id_base)))

# =========================
# PATCH /base/{id_base}
# =========================
@router.patch("/{id_base}", response_model=BaseOut)
def patch_base(
    id_base: int,
    body: BaseUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base_user = _resolve_user_sub_base(db, current_user)
    obj = db.get(BasePreco, id_base)
    if not obj or obj.sub_base != sub_base_user:
        raise HTTPException(status_code=404, detail="Não encontrado")

    # Renomear "base" (opcional)
    if body.base is not None:
        new_base = (body.base or "").strip()
        if not new_base:
            raise HTTPException(status_code=400, detail="O campo 'base' não pode ficar vazio.")
        if new_base != obj.base:
            dup = db.scalars(
                select(BasePreco).where(
                    BasePreco.sub_base == sub_base_user,
                    BasePreco.base == new_base,
                    BasePreco.id_base != obj.id_base
                )
            ).first()
            if dup:
                raise HTTPException(
                    status_code=409,
                    detail="Já existe um registro de preços para essa 'base' nesta sub_base."
                )
            obj.base = new_base

    # Atualizações parciais de preço
    if body.shopee is not None:
        obj.shopee = float(body.shopee)
    if body.ml is not None:
        obj.ml = float(body.ml)
    if body.avulso is not None:
        obj.avulso = float(body.avulso)

    # novo: toggle de status
    if body.ativo is not None:
        obj.ativo = bool(body.ativo)

    if body.dias_coleta is not None:
        obj.dias_coleta = body.dias_coleta
    if body.agenda_coleta_confirmada is not None:
        obj.agenda_coleta_confirmada = bool(body.agenda_coleta_confirmada)

    db.commit()
    db.refresh(obj)
    enderecos = _enderecos_por_base_ids(db, [int(obj.id_base)])
    return _base_to_out(obj, enderecos.get(int(obj.id_base)))

# =========================
# DELETE /base/{id_base}
# =========================
@router.delete("/{id_base}", status_code=status.HTTP_204_NO_CONTENT)
def delete_base(
    id_base: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base_user = _resolve_user_sub_base(db, current_user)
    obj = db.get(BasePreco, id_base)
    if not obj or obj.sub_base != sub_base_user:
        raise HTTPException(status_code=404, detail="Não encontrado")
    db.delete(obj)
    db.commit()
    return
