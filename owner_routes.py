from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional, List

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from sqlalchemy import select

from db import get_db
from auth import _coerce_role_int, get_current_user
from models import Owner, User, OwnerCobrancaItem, BaseSellerDados
from etiqueta_identidade_service import resolver_nome_exibicao
from upload_storage_utils import B2_BUCKET_NAME, get_s3_client_optional, purge_b2_keys

router = APIRouter(prefix="/owner", tags=["Owner"])
MODOS_OPERACAO = {"codigo", "coleta_manual", "ambos"}

# ============================================================
# SCHEMAS
# ============================================================

def _normalize_tipo_owner(value: Optional[str]) -> str:
    v = (value or "subbase").strip().lower()
    if v not in ("base", "subbase"):
        return "subbase"
    return v


class OwnerCreate(BaseModel):
    email: Optional[str] = None
    username: Optional[str] = None
    valor: Optional[float] = Field(default=None)
    sub_base: Optional[str] = None
    contato: Optional[str] = None
    teste: Optional[bool] = None
    modo_operacao: Optional[str] = None
    tipo_owner: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class OwnerUpdate(BaseModel):
    email: Optional[str] = None
    username: Optional[str] = None
    valor: Optional[float] = None
    contato: Optional[str] = None
    nome_fantasia: Optional[str] = None
    ativo: Optional[bool] = None
    ignorar_coleta: Optional[bool] = None
    teste: Optional[bool] = None
    modo_operacao: Optional[str] = None
    tipo_owner: Optional[str] = None
    devolucao_sub_base_habilitada: Optional[bool] = None
    entrada_obrigatoria_habilitada: Optional[bool] = None
    conferencia_saida_habilitada: Optional[bool] = None
    bloquear_saida_sem_coleta: Optional[bool] = None

    model_config = ConfigDict(from_attributes=True)


class OwnerOut(BaseModel):
    id_owner: int
    email: Optional[str]
    username: Optional[str]
    valor: Optional[float]
    nome_fantasia: Optional[str] = None
    slogan: Optional[str] = None
    tem_logo: bool = False
    sub_base: Optional[str]
    contato: Optional[str]
    ativo: bool
    ignorar_coleta: bool
    teste: bool
    modo_operacao: Optional[str] = None
    tipo_owner: Optional[str] = None
    devolucao_sub_base_habilitada: bool = False
    entrada_obrigatoria_habilitada: bool = False
    conferencia_saida_habilitada: bool = False
    bloquear_saida_sem_coleta: bool = False

    model_config = ConfigDict(from_attributes=True)


class OwnerIdentidadeOut(BaseModel):
    id_owner: int
    sub_base: Optional[str] = None
    nome_exibicao: str
    nome_fantasia: Optional[str] = None
    slogan: Optional[str] = None
    tem_logo: bool = False
    logo_filename: Optional[str] = None
    logo_updated_at: Optional[datetime] = None


class OwnerIdentidadePatch(BaseModel):
    nome_fantasia: Optional[str] = None
    slogan: Optional[str] = None


class LogoPresignGetOut(BaseModel):
    download_url: Optional[str] = None
    expires_in: int = 60
    tem_logo: bool = False


# ============================================================
# HELPERS
# ============================================================

def _get_owner_by_sub_base(db: Session, sub_base: str) -> Optional[Owner]:
    return db.scalar(select(Owner).where(Owner.sub_base == sub_base))


def _owner_to_out(owner: Owner) -> OwnerOut:
    return OwnerOut(
        id_owner=owner.id_owner,
        email=owner.email,
        username=owner.username,
        valor=float(owner.valor or 0) if owner.valor is not None else None,
        nome_fantasia=getattr(owner, "nome_fantasia", None),
        slogan=getattr(owner, "slogan", None),
        tem_logo=bool((getattr(owner, "logo_object_key", None) or "").strip()),
        sub_base=owner.sub_base,
        contato=owner.contato,
        ativo=bool(owner.ativo),
        ignorar_coleta=bool(owner.ignorar_coleta),
        teste=bool(owner.teste),
        modo_operacao=owner.modo_operacao,
        tipo_owner=getattr(owner, "tipo_owner", None),
        devolucao_sub_base_habilitada=bool(getattr(owner, "devolucao_sub_base_habilitada", False)),
        entrada_obrigatoria_habilitada=bool(getattr(owner, "entrada_obrigatoria_habilitada", False)),
        conferencia_saida_habilitada=bool(getattr(owner, "conferencia_saida_habilitada", False)),
        bloquear_saida_sem_coleta=bool(getattr(owner, "bloquear_saida_sem_coleta", False)),
    )


def _assert_role_01(current_user: User) -> None:
    role = _coerce_role_int(getattr(current_user, "role", None))
    if role not in (0, 1):
        raise HTTPException(403, "Acesso restrito a administradores.")


def _owner_for_me(db: Session, current_user: User) -> Owner:
    sub_base = (getattr(current_user, "sub_base", None) or "").strip()
    if not sub_base:
        raise HTTPException(403, "Usuário sem sub_base definida.")
    owner = _get_owner_by_sub_base(db, sub_base)
    if not owner:
        raise HTTPException(404, "Owner não encontrado para esta sub_base.")
    return owner


def _identidade_out(owner: Owner) -> OwnerIdentidadeOut:
    return OwnerIdentidadeOut(
        id_owner=owner.id_owner,
        sub_base=owner.sub_base,
        nome_exibicao=resolver_nome_exibicao(owner),
        nome_fantasia=getattr(owner, "nome_fantasia", None),
        slogan=getattr(owner, "slogan", None),
        tem_logo=bool((getattr(owner, "logo_object_key", None) or "").strip()),
        logo_filename=getattr(owner, "logo_filename", None),
        logo_updated_at=getattr(owner, "logo_updated_at", None),
    )


_LOGO_EXTS = {"png", "jpg", "jpeg", "webp"}
_LOGO_MIMES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
}
_LOGO_MAX_BYTES = 5 * 1024 * 1024


def _validate_and_read_logo(file: UploadFile) -> tuple[bytes, str, str]:
    """Retorna (content, ext, content_type)."""
    filename = (file.filename or "logo.png").strip()
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    content_type = (file.content_type or "").strip().lower()
    if ext not in _LOGO_EXTS and content_type not in _LOGO_MIMES:
        raise HTTPException(422, "Formato inválido. Use PNG, JPG ou WEBP.")
    if content_type in _LOGO_MIMES:
        ext = _LOGO_MIMES[content_type]
    elif ext == "jpeg":
        ext = "jpg"
    if ext not in _LOGO_EXTS:
        raise HTTPException(422, "Formato inválido. Use PNG, JPG ou WEBP.")

    content = file.file.read()
    if not content:
        raise HTTPException(422, "Arquivo vazio.")
    if len(content) > _LOGO_MAX_BYTES:
        raise HTTPException(422, "Logo deve ter no máximo 5 MB.")

    from io import BytesIO
    from PIL import Image

    try:
        with Image.open(BytesIO(content)) as img:
            img.verify()
        with Image.open(BytesIO(content)) as img2:
            img2.load()
            if img2.width <= 0 or img2.height <= 0:
                raise ValueError("dimensões inválidas")
    except Exception:
        raise HTTPException(422, "Arquivo não é uma imagem válida.")

    mime = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
    }.get(ext, "image/png")
    return content, ext, mime


def _upload_owner_logo(owner: Owner, content: bytes, ext: str, content_type: str, filename: str) -> None:
    import uuid

    client = get_s3_client_optional()
    if client is None:
        raise HTTPException(
            503,
            "Upload de logo indisponível: armazenamento (B2) não configurado neste ambiente.",
        )

    old_key = (getattr(owner, "logo_object_key", None) or "").strip()
    object_key = f"owner/{owner.id_owner}/logo/{uuid.uuid4().hex}.{ext}"
    try:
        client.put_object(
            Bucket=B2_BUCKET_NAME,
            Key=object_key,
            Body=content,
            ContentType=content_type,
        )
    except Exception as exc:
        err = str(exc or "").strip()
        low = err.lower()
        if "credential" in low or "accessdenied" in low or "invalidaccesskey" in low or "signature" in low:
            raise HTTPException(
                503,
                "Upload de logo falhou: credenciais do armazenamento inválidas ou sem permissão.",
            ) from exc
        if "nosuchbucket" in low or ("bucket" in low and "exist" in low):
            raise HTTPException(
                503,
                "Upload de logo falhou: bucket de armazenamento não encontrado.",
            ) from exc
        raise HTTPException(
            502,
            "Não foi possível enviar a logo ao armazenamento. Tente novamente em instantes.",
        ) from exc
    owner.logo_object_key = object_key
    owner.logo_filename = (filename or f"logo.{ext}")[:200]
    owner.logo_content_type = content_type
    owner.logo_updated_at = datetime.utcnow()
    if old_key and old_key != object_key:
        purge_b2_keys([old_key])


def _delete_owner_logo(owner: Owner) -> None:
    old_key = (getattr(owner, "logo_object_key", None) or "").strip()
    owner.logo_object_key = None
    owner.logo_filename = None
    owner.logo_content_type = None
    owner.logo_updated_at = None
    if old_key:
        purge_b2_keys([old_key])


def _presign_logo(owner: Owner) -> LogoPresignGetOut:
    key = (getattr(owner, "logo_object_key", None) or "").strip()
    if not key:
        return LogoPresignGetOut(download_url=None, expires_in=60, tem_logo=False)
    client = get_s3_client_optional()
    if client is None:
        raise HTTPException(
            503,
            "Pré-visualização indisponível: armazenamento (B2) não configurado neste ambiente.",
        )
    expires_in = 60
    try:
        url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": B2_BUCKET_NAME, "Key": key},
            ExpiresIn=expires_in,
        )
    except Exception as exc:
        raise HTTPException(
            502,
            "Não foi possível gerar o link de pré-visualização da logo.",
        ) from exc
    return LogoPresignGetOut(download_url=url, expires_in=expires_in, tem_logo=True)


# ============================================================
# CREATE OWNER
# ============================================================

@router.post("/", status_code=201)
def create_owner(
    body: OwnerCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    email = body.email or current_user.email
    username = body.username or current_user.username

    if not body.sub_base:
        raise HTTPException(422, "sub_base é obrigatória.")

    exists = db.scalar(select(Owner).where(Owner.sub_base == body.sub_base))
    if exists:
        raise HTTPException(409, "Já existe um Owner para esta sub_base.")

    modo_recebido = (body.modo_operacao or "codigo").strip().lower()
    # Compatibilidade com clientes antigos que enviavam modo=saida.
    ignorar_coleta = modo_recebido == "saida"
    modo_operacao = "codigo" if ignorar_coleta else modo_recebido
    if modo_operacao not in MODOS_OPERACAO:
        raise HTTPException(422, "modo_operacao deve ser 'codigo', 'coleta_manual' ou 'ambos'.")

    tipo_owner = _normalize_tipo_owner(body.tipo_owner)

    obj = Owner(
        email=email,
        username=username,
        valor=body.valor or 0.0,
        sub_base=body.sub_base,
        contato=body.contato,
        ativo=True,
        ignorar_coleta=ignorar_coleta,
        teste=bool(body.teste) if body.teste is not None else False,
        modo_operacao=modo_operacao,
        tipo_owner=tipo_owner,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)

    return {"ok": True, "id_owner": obj.id_owner}


# ============================================================
# GET /owner/me
# ============================================================

@router.get("/me", response_model=OwnerOut)
def get_owner_for_current_user(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not current_user.sub_base:
        raise HTTPException(404, "Usuário não possui sub_base associada.")

    owner = _get_owner_by_sub_base(db, current_user.sub_base)
    if not owner:
        raise HTTPException(404, "Owner não encontrado para esta sub_base.")

    return _owner_to_out(owner)

# ============================================================
# LISTAR TODOS (ADMIN)
# ============================================================

@router.get("/", response_model=List[OwnerOut])
def list_owners(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != 0:
        raise HTTPException(403, "Acesso restrito ao administrador.")

    return [_owner_to_out(o) for o in db.scalars(select(Owner)).all()]

# ============================================================
# UPDATE (PATCH ÚNICO)
# ============================================================

@router.patch("/{id_owner}", response_model=OwnerOut)
def update_owner(
    id_owner: int,
    body: OwnerUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    owner = db.get(Owner, id_owner)
    if not owner:
        raise HTTPException(404, "Owner não encontrado.")

    if current_user.role != 0:
        raise HTTPException(403, "Apenas administradores podem editar Owner.")

    # Campos editáveis
    if body.email is not None:
        owner.email = body.email

    if body.username is not None:
        owner.username = body.username

    if body.valor is not None:
        owner.valor = body.valor

    if body.contato is not None:
        owner.contato = body.contato

    if body.nome_fantasia is not None:
        owner.nome_fantasia = (body.nome_fantasia or "").strip() or None

    # 🔥 Campos adicionados agora
    if body.ativo is not None:
        owner.ativo = body.ativo

    if body.teste is not None:
        owner.teste = body.teste

    if body.modo_operacao is not None:
        modo = body.modo_operacao.strip().lower()
        if modo == "saida":
            # Compatibilidade: desativa coleta sem gravar um quarto modo.
            owner.ignorar_coleta = True
        elif modo not in MODOS_OPERACAO:
            raise HTTPException(422, "modo_operacao deve ser 'codigo', 'coleta_manual' ou 'ambos'.")
        else:
            owner.modo_operacao = modo

    if body.ignorar_coleta is not None:
        owner.ignorar_coleta = bool(body.ignorar_coleta)

    if body.tipo_owner is not None:
        owner.tipo_owner = _normalize_tipo_owner(body.tipo_owner)

    if body.devolucao_sub_base_habilitada is not None:
        owner.devolucao_sub_base_habilitada = bool(body.devolucao_sub_base_habilitada)

    if body.entrada_obrigatoria_habilitada is not None:
        owner.entrada_obrigatoria_habilitada = bool(body.entrada_obrigatoria_habilitada)

    if body.conferencia_saida_habilitada is not None:
        owner.conferencia_saida_habilitada = bool(body.conferencia_saida_habilitada)

    if body.bloquear_saida_sem_coleta is not None:
        owner.bloquear_saida_sem_coleta = bool(body.bloquear_saida_sem_coleta)

    db.commit()
    db.refresh(owner)
    return _owner_to_out(owner)


# ============================================================
# DADOS DO SELLER (CNPJ/ENDEREÇO) POR OWNER
# ============================================================

class SellerDadosBase(BaseModel):
    cnpj: Optional[str] = None
    rua: Optional[str] = None
    numero: Optional[str] = None
    complemento: Optional[str] = None
    bairro: Optional[str] = None
    cidade: Optional[str] = None
    estado: Optional[str] = None
    cep: Optional[str] = None
    chave_pix: Optional[str] = None
    base_id: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)


class SellerDadosOut(SellerDadosBase):
    id_seller: int
    owner_id: int


@router.get("/{id_owner}/seller-dados", response_model=SellerDadosOut)
def get_seller_dados(
    id_owner: int,
    base_id: Optional[int] = Query(default=None, description="Filtrar dados do seller por id_base associado"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in (0, 1):
        raise HTTPException(403, "Acesso negado.")

    stmt = select(BaseSellerDados).where(BaseSellerDados.owner_id == id_owner)
    if base_id is not None:
        stmt = stmt.where(BaseSellerDados.base_id == base_id)

    seller = db.scalar(stmt)
    if not seller:
        raise HTTPException(404, "Dados de seller não encontrados para este owner.")
    return seller


@router.patch("/{id_owner}/seller-dados", response_model=SellerDadosOut)
def upsert_seller_dados(
    id_owner: int,
    body: SellerDadosBase,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in (0, 1):
        raise HTTPException(403, "Acesso negado.")

    owner = db.get(Owner, id_owner)
    if not owner:
        raise HTTPException(404, "Owner não encontrado.")

    data = body.model_dump(exclude_unset=True)
    base_id = data.get("base_id")

    # Buscar por (owner_id, base_id) para permitir um registro de seller por base
    stmt = select(BaseSellerDados).where(BaseSellerDados.owner_id == id_owner)
    if base_id is not None:
        stmt = stmt.where(BaseSellerDados.base_id == base_id)
    seller = db.scalar(stmt)

    if not seller:
        cnpj = (data.get("cnpj") or "").strip()
        rua = (data.get("rua") or "").strip()
        numero = (data.get("numero") or "").strip()
        bairro = (data.get("bairro") or "").strip()
        cidade = (data.get("cidade") or "").strip()
        cep = (data.get("cep") or "").strip()
        chave_pix = (data.get("chave_pix") or "").strip() or None
        tipo_owner = (getattr(owner, "tipo_owner", None) or "subbase").strip().lower()
        # Owner tipo Base (Seller) exige CNPJ e endereço. Subbase: todos opcionais, inclusive PIX.
        if tipo_owner == "base" and not all([cnpj, rua, numero, bairro, cidade, cep]):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Campos obrigatórios para criar seller: cnpj, rua, numero, bairro, cidade, cep.",
            )

        seller = BaseSellerDados(
            owner_id=id_owner,
            base_id=base_id,
            cnpj=cnpj,
            rua=rua,
            numero=numero,
            complemento=(data.get("complemento") or "").strip() or None,
            bairro=bairro,
            cidade=cidade,
            estado=(data.get("estado") or "").strip() or None,
            cep=cep,
            chave_pix=chave_pix,
        )
        db.add(seller)

    else:
        # atualização parcial
        if "base_id" in data:
            seller.base_id = data["base_id"]
        if "cnpj" in data:
            seller.cnpj = (data["cnpj"] or "").strip() or seller.cnpj
        if "rua" in data:
            seller.rua = (data["rua"] or "").strip() or seller.rua
        if "numero" in data:
            seller.numero = (data["numero"] or "").strip() or seller.numero
        if "complemento" in data:
            seller.complemento = (data["complemento"] or "").strip() or None
        if "bairro" in data:
            seller.bairro = (data["bairro"] or "").strip() or seller.bairro
        if "cidade" in data:
            seller.cidade = (data["cidade"] or "").strip() or seller.cidade
        if "estado" in data:
            seller.estado = (data["estado"] or "").strip() or None
        if "cep" in data:
            seller.cep = (data["cep"] or "").strip() or seller.cep
        if "chave_pix" in data:
            seller.chave_pix = (data["chave_pix"] or "").strip() or None

    db.commit()
    db.refresh(seller)
    return seller


# ============================================================
# ENDPOINTS DE ATIVAR/DESATIVAR (opcionais)
# ============================================================

@router.patch("/{id_owner}/ativar")
def ativar_owner(id_owner: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    owner = db.get(Owner, id_owner)
    if not owner:
        raise HTTPException(404)
    if current_user.role != 0:
        raise HTTPException(403)
    owner.ativo = True
    db.commit()
    return {"ok": True}


@router.patch("/{id_owner}/desativar")
def desativar_owner(id_owner: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    owner = db.get(Owner, id_owner)
    if not owner:
        raise HTTPException(404)
    if current_user.role != 0:
        raise HTTPException(403)
    owner.ativo = False
    db.commit()
    return {"ok": True}


# ============================================================
# IDENTIDADE VISUAL (logo + nome + slogan)
# ============================================================

@router.get("/me/identidade", response_model=OwnerIdentidadeOut)
def get_identidade_me(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _assert_role_01(current_user)
    return _identidade_out(_owner_for_me(db, current_user))


@router.patch("/me/identidade", response_model=OwnerIdentidadeOut)
def patch_identidade_me(
    body: OwnerIdentidadePatch,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _assert_role_01(current_user)
    owner = _owner_for_me(db, current_user)
    if body.nome_fantasia is not None:
        owner.nome_fantasia = (body.nome_fantasia or "").strip() or None
    if body.slogan is not None:
        owner.slogan = (body.slogan or "").strip() or None
    db.commit()
    db.refresh(owner)
    return _identidade_out(owner)


@router.post("/me/logo", response_model=OwnerIdentidadeOut)
async def upload_logo_me(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _assert_role_01(current_user)
    owner = _owner_for_me(db, current_user)
    content, ext, content_type = _validate_and_read_logo(file)
    _upload_owner_logo(owner, content, ext, content_type, file.filename or f"logo.{ext}")
    db.commit()
    db.refresh(owner)
    return _identidade_out(owner)


@router.delete("/me/logo", response_model=OwnerIdentidadeOut)
def delete_logo_me(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _assert_role_01(current_user)
    owner = _owner_for_me(db, current_user)
    _delete_owner_logo(owner)
    db.commit()
    db.refresh(owner)
    return _identidade_out(owner)


@router.post("/me/logo/presign-get", response_model=LogoPresignGetOut)
def presign_logo_me(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _assert_role_01(current_user)
    return _presign_logo(_owner_for_me(db, current_user))


@router.get("/{id_owner}/identidade", response_model=OwnerIdentidadeOut)
def get_identidade_owner(
    id_owner: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if _coerce_role_int(getattr(current_user, "role", None)) != 0:
        raise HTTPException(403, "Acesso restrito ao administrador.")
    owner = db.get(Owner, id_owner)
    if not owner:
        raise HTTPException(404, "Owner não encontrado.")
    return _identidade_out(owner)


@router.patch("/{id_owner}/identidade", response_model=OwnerIdentidadeOut)
def patch_identidade_owner(
    id_owner: int,
    body: OwnerIdentidadePatch,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if _coerce_role_int(getattr(current_user, "role", None)) != 0:
        raise HTTPException(403, "Acesso restrito ao administrador.")
    owner = db.get(Owner, id_owner)
    if not owner:
        raise HTTPException(404, "Owner não encontrado.")
    if body.nome_fantasia is not None:
        owner.nome_fantasia = (body.nome_fantasia or "").strip() or None
    if body.slogan is not None:
        owner.slogan = (body.slogan or "").strip() or None
    db.commit()
    db.refresh(owner)
    return _identidade_out(owner)


@router.post("/{id_owner}/logo", response_model=OwnerIdentidadeOut)
async def upload_logo_owner(
    id_owner: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if _coerce_role_int(getattr(current_user, "role", None)) != 0:
        raise HTTPException(403, "Acesso restrito ao administrador.")
    owner = db.get(Owner, id_owner)
    if not owner:
        raise HTTPException(404, "Owner não encontrado.")
    content, ext, content_type = _validate_and_read_logo(file)
    _upload_owner_logo(owner, content, ext, content_type, file.filename or f"logo.{ext}")
    db.commit()
    db.refresh(owner)
    return _identidade_out(owner)


@router.delete("/{id_owner}/logo", response_model=OwnerIdentidadeOut)
def delete_logo_owner(
    id_owner: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if _coerce_role_int(getattr(current_user, "role", None)) != 0:
        raise HTTPException(403, "Acesso restrito ao administrador.")
    owner = db.get(Owner, id_owner)
    if not owner:
        raise HTTPException(404, "Owner não encontrado.")
    _delete_owner_logo(owner)
    db.commit()
    db.refresh(owner)
    return _identidade_out(owner)


@router.post("/{id_owner}/logo/presign-get", response_model=LogoPresignGetOut)
def presign_logo_owner(
    id_owner: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if _coerce_role_int(getattr(current_user, "role", None)) != 0:
        raise HTTPException(403, "Acesso restrito ao administrador.")
    owner = db.get(Owner, id_owner)
    if not owner:
        raise HTTPException(404, "Owner não encontrado.")
    return _presign_logo(owner)
