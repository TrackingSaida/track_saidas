from __future__ import annotations
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, AliasChoices
from sqlalchemy import select, or_
from sqlalchemy.orm import Session
from datetime import date

from db import get_db
from auth import get_current_user, get_password_hash
from models import User, Entregador

router = APIRouter(prefix="/entregadores", tags=["Entregadores"])

# =========================================================
# SCHEMAS
# =========================================================
class EntregadorCreate(BaseModel):
    # --- dados do entregador (obrigatórios) ---
    nome: str = Field(min_length=1)
    telefone: str = Field(min_length=1)
    documento: str = Field(min_length=1)

    # --- endereço (obrigatórios) ---
    rua: str = Field(min_length=1)
    numero: str = Field(min_length=1)
    complemento: str = Field(min_length=1)
    cep: str = Field(min_length=1)
    cidade: str = Field(min_length=1)
    bairro: str = Field(min_length=1)

    # --- perfil coletador (opcional) ---
    coletador: Optional[bool] = False
    username_entregador: Optional[str] = None

    # se coletador=True: senha obrigatória (hash vai p/ users.password_hash)
    senha: Optional[str] = Field(default=None, validation_alias=AliasChoices("senha"))
    # se coletador=True: e-mail obrigatório para criar o user
    email: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class EntregadorUpdate(BaseModel):
    # atualização parcial: só altera o que vier
    nome: Optional[str] = None
    telefone: Optional[str] = None
    documento: Optional[str] = None

    rua: Optional[str] = None
    numero: Optional[str] = None
    complemento: Optional[str] = None
    cep: Optional[str] = None
    cidade: Optional[str] = None
    bairro: Optional[str] = None

    coletador: Optional[bool] = None
    username_entregador: Optional[str] = None

    # atualizar a senha do USER correspondente (password_hash)
    senha: Optional[str] = None

    # ativação do entregador (não mexe no status do user)
    ativo: Optional[bool] = None

    model_config = ConfigDict(from_attributes=True)


class EntregadorOut(BaseModel):
    id_entregador: int

    nome: Optional[str] = None
    telefone: Optional[str] = None
    documento: Optional[str] = None
    ativo: bool
    data_cadastro: Optional[date] = None

    rua: Optional[str] = None
    numero: Optional[str] = None
    complemento: Optional[str] = None
    cep: Optional[str] = None
    cidade: Optional[str] = None
    bairro: Optional[str] = None

    coletador: bool
    username_entregador: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

# =========================================================
# HELPERS
# =========================================================
def _resolve_user_base(db: Session, current_user) -> str:
    """
    Resolve a sub_base do usuário autenticado:
      1) tenta por id
      2) tenta por email
      3) tenta por username
    """
    user_id = getattr(current_user, "id", None)
    if user_id is not None:
        u = db.get(User, user_id)
        if u and getattr(u, "sub_base", None):
            return u.sub_base

    email = getattr(current_user, "email", None)
    if email:
        u = db.scalars(select(User).where(User.email == email)).first()
        if u and getattr(u, "sub_base", None):
            return u.sub_base

    uname = getattr(current_user, "username", None)
    if uname:
        u = db.scalars(select(User).where(User.username == uname)).first()
        if u and getattr(u, "sub_base", None):
            return u.sub_base

    raise HTTPException(status_code=400, detail="sub_base não definida para o usuário em 'users'.")


def _get_owned_entregador(db: Session, sub_base_user: str, id_entregador: int) -> Entregador:
    """
    Busca o entregador e valida se pertence à mesma sub_base do solicitante.
    """
    obj = db.get(Entregador, id_entregador)
    if not obj or obj.sub_base != sub_base_user:
        raise HTTPException(status_code=404, detail="Não encontrado")
    return obj


def _find_matching_user(db: Session, sub_base: str, username_ent: Optional[str]) -> Optional[User]:
    """
    Localiza um User que represente esse entregador:
      - mesma sub_base
      - username == username_entregador OU username_entregador == username_entregador
    """
    if not username_ent:
        return None
    stmt = select(User).where(
        User.sub_base == sub_base,
        or_(
            User.username == username_ent,
            User.username_entregador == username_ent,
        ),
    )
    return db.scalars(stmt).first()

# =========================================================
# ROTAS
# =========================================================
@router.post("/", status_code=status.HTTP_201_CREATED)
def create_entregador(
    body: EntregadorCreate,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """
    Cria um entregador e, se 'coletador=true', também cria um NOVO usuário em 'users':
      - password_hash recebe o hash de 'senha' (sem coluna de senha no entregador!)
      - username = username_entregador
      - contato = telefone
      - nome = nome
      - coletador = true
      - username_entregador = username_entregador (espelho)
      - tipo_de_cadastro = 3
      - sub_base = do solicitante
      - status = true
    """
    sub_base_user = _resolve_user_base(db, current_user)

    # normalização rápida
    nome        = (body.nome or "").strip()
    telefone    = (body.telefone or "").strip()
    documento   = (body.documento or "").strip()
    rua         = (body.rua or "").strip()
    numero      = (body.numero or "").strip()
    complemento = (body.complemento or "").strip()
    cep         = (body.cep or "").strip()
    cidade      = (body.cidade or "").strip()
    bairro      = (body.bairro or "").strip()

    if not documento:
        raise HTTPException(status_code=400, detail="O campo 'documento' é obrigatório.")

    coletador_flag = bool(body.coletador)
    username_ent = (body.username_entregador or "").strip() if coletador_flag else None
    senha_raw = (body.senha or "").strip() if coletador_flag else None
    email_new = (body.email or "").strip() if coletador_flag else None

    if coletador_flag:
        # Exigências quando coletador=True
        if not username_ent:
            raise HTTPException(status_code=400, detail="Informe 'username_entregador' para coletador.")
        if not senha_raw:
            raise HTTPException(status_code=400, detail="Informe 'senha' para coletador.")
        if not email_new:
            raise HTTPException(status_code=400, detail="Informe 'email' para criar o usuário coletador.")

        # unicidade do novo user
        if db.scalars(select(User).where(User.email == email_new)).first():
            raise HTTPException(status_code=409, detail="Já existe um usuário com este e-mail.")
        if db.scalars(select(User).where(User.username == username_ent)).first():
            raise HTTPException(status_code=409, detail="Já existe um usuário com este username.")

    # documento único por sub_base (se desejar a regra)
    exists = db.scalars(
        select(Entregador).where(
            Entregador.sub_base == sub_base_user,
            Entregador.documento == documento,
        )
    ).first()
    if exists:
        raise HTTPException(status_code=409, detail="Já existe um entregador com esse documento nesta sub_base.")

    try:
        # 1) cria ENTREGADOR
        ent = Entregador(
            sub_base=sub_base_user,
            nome=nome,
            telefone=telefone,
            documento=documento,
            ativo=True,
            rua=rua,
            numero=numero,
            complemento=complemento,
            cep=cep,
            cidade=cidade,
            bairro=bairro,
            coletador=coletador_flag,
            username_entregador=username_ent,
        )
        db.add(ent)

        # 2) se coletador => cria NOVO USER
        if coletador_flag:
            new_user = User(
                email=email_new,
                password_hash=get_password_hash(senha_raw),  # hash da senha p/ users
                username=username_ent,
                contato=telefone or "",
                nome=nome or None,
                sobrenome=None,
                status=True,
                sub_base=sub_base_user,
                coletador=True,
                username_entregador=username_ent,
                tipo_de_cadastro=3,
            )
            db.add(new_user)

        db.commit()
        db.refresh(ent)
        return {"ok": True, "action": "created", "id": ent.id_entregador}
    except Exception:
        db.rollback()
        raise


@router.get("/", response_model=List[EntregadorOut])
def list_entregadores(
    status: Optional[str] = Query("todos", description="Filtrar por status: ativo, inativo ou todos"),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """
    Lista entregadores da sub_base do solicitante, com filtro opcional por status (ativo/inativo).
    """
    sub_base_user = _resolve_user_base(db, current_user)

    stmt = select(Entregador).where(Entregador.sub_base == sub_base_user)

    if status == "ativo":
        stmt = stmt.where(Entregador.ativo.is_(True))
    elif status == "inativo":
        stmt = stmt.where(Entregador.ativo.is_(False))
    # "todos" => sem filtro adicional

    stmt = stmt.order_by(Entregador.nome)
    return db.scalars(stmt).all()


@router.get("/{id_entregador}", response_model=EntregadorOut)
def get_entregador(
    id_entregador: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """
    Obtém 1 entregador por id, validando a sub_base de quem solicita.
    """
    sub_base_user = _resolve_user_base(db, current_user)
    return _get_owned_entregador(db, sub_base_user, id_entregador)


@router.patch("/{id_entregador}", response_model=EntregadorOut)
def patch_entregador(
    id_entregador: int,
    body: EntregadorUpdate,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """
    Atualização parcial:
      - Sincroniza 'coletador' nas DUAS tabelas (entregador e users).
      - Se vier 'senha', atualiza users.password_hash (hash).
      - Se 'username_entregador' mudar, espelha em users.username e users.username_entregador.
      - Se 'nome'/'telefone' mudarem, espelha em users.nome/users.contato (quando existir user correspondente).
      - 'ativo' só mexe na tabela entregador (não toca status do user).
    """
    sub_base_user = _resolve_user_base(db, current_user)
    obj = _get_owned_entregador(db, sub_base_user, id_entregador)

    try:
        # dados básicos
        if body.nome is not None:
            obj.nome = body.nome.strip()
        if body.telefone is not None:
            obj.telefone = body.telefone.strip()
        if body.documento is not None:
            novo_doc = body.documento.strip()
            if not novo_doc:
                raise HTTPException(status_code=400, detail="O campo 'documento' não pode ficar vazio.")
            if novo_doc != obj.documento:
                exists = db.scalars(
                    select(Entregador).where(
                        Entregador.sub_base == sub_base_user,
                        Entregador.documento == novo_doc,
                        Entregador.id_entregador != obj.id_entregador,
                    )
                ).first()
                if exists:
                    raise HTTPException(status_code=409, detail="Já existe um entregador com esse documento nesta sub_base.")
            obj.documento = novo_doc

        # endereço
        if body.rua is not None:         obj.rua = body.rua.strip()
        if body.numero is not None:      obj.numero = body.numero.strip()
        if body.complemento is not None: obj.complemento = body.complemento.strip()
        if body.cep is not None:         obj.cep = body.cep.strip()
        if body.cidade is not None:      obj.cidade = body.cidade.strip()
        if body.bairro is not None:      obj.bairro = body.bairro.strip()

        # localizar user correspondente, se houver
        user = _find_matching_user(db, sub_base_user, obj.username_entregador)

        # espelhamentos em users
        if user and body.nome is not None and body.nome.strip():
            user.nome = body.nome.strip()
        if user and body.telefone is not None and body.telefone.strip():
            user.contato = body.telefone.strip()

        # alteração de username_entregador
        if body.username_entregador is not None:
            novo_user = (body.username_entregador or "").strip() or None
            obj.username_entregador = novo_user
            if user and novo_user:
                user.username = novo_user
                user.username_entregador = novo_user

        # coletador → sincroniza nas duas tabelas
        if body.coletador is not None:
            obj.coletador = bool(body.coletador)
            if user:
                user.coletador = bool(body.coletador)

        # senha → atualiza users.password_hash
        if body.senha is not None:
            raw = body.senha.strip()
            if not raw:
                raise HTTPException(status_code=400, detail="A nova 'senha' não pode ser vazia.")
            if not user:
                # não criamos user no PATCH; exigimos que já exista
                raise HTTPException(status_code=404, detail="Usuário correspondente não encontrado para atualizar a senha.")
            user.password_hash = get_password_hash(raw)

        # qualquer mudança significativa → tipo_de_cadastro = 3
        if user and any(v is not None for v in [body.nome, body.telefone, body.username_entregador, body.coletador, body.senha]):
            user.tipo_de_cadastro = 3

        # ativo (apenas em entregador)
        if body.ativo is not None:
            obj.ativo = bool(body.ativo)

        db.commit()
        db.refresh(obj)
        return obj
    except Exception:
        db.rollback()
        raise


@router.delete("/{id_entregador}", status_code=status.HTTP_204_NO_CONTENT)
def delete_entregador(
    id_entregador: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """
    Remove o entregador (valida sub_base). Não remove o user.
    """
    sub_base_user = _resolve_user_base(db, current_user)
    obj = _get_owned_entregador(db, sub_base_user, id_entregador)
    db.delete(obj)
    db.commit()
    return
