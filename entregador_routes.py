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
    senha: Optional[str] = Field(default=None, validation_alias=AliasChoices("senha"))

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
    senha: Optional[str] = None
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
    """Resolve a sub_base do usuário autenticado"""
    user_id = getattr(current_user, "id", None)
    if user_id is not None:
        u = db.get(User, user_id)
        if u and getattr(u, "sub_base", None):
            return u.sub_base

    uname = getattr(current_user, "username", None)
    if uname:
        u = db.scalars(select(User).where(User.username == uname)).first()
        if u and getattr(u, "sub_base", None):
            return u.sub_base

    raise HTTPException(status_code=400, detail="sub_base não definida para o usuário em 'users'.")


def _get_owned_entregador(db: Session, sub_base_user: str, id_entregador: int) -> Entregador:
    """Busca o entregador e valida se pertence à mesma sub_base"""
    obj = db.get(Entregador, id_entregador)
    if not obj or obj.sub_base != sub_base_user:
        raise HTTPException(status_code=404, detail="Não encontrado")
    return obj


def _find_matching_user(db: Session, sub_base: str, username_ent: Optional[str]) -> Optional[User]:
    """Localiza um user vinculado ao entregador"""
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
    Cria um entregador e, se 'coletador=True', também cria um novo usuário em 'users':
      - password_hash recebe o hash da senha
      - username = username_entregador
      - contato = telefone
      - nome = nome
      - coletador = True
      - role = 3
      - sub_base = do solicitante
      - status = True
    """
    sub_base_user = _resolve_user_base(db, current_user)

    # normalização
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

    if coletador_flag:
        if not username_ent:
            raise HTTPException(status_code=400, detail="Informe 'username_entregador' para coletador.")
        if not senha_raw:
            raise HTTPException(status_code=400, detail="Informe 'senha' para coletador.")

        # unicidade do username
        if db.scalars(select(User).where(User.username == username_ent)).first():
            raise HTTPException(status_code=409, detail="Já existe um usuário com este username.")

    # documento único por sub_base
    exists = db.scalars(
        select(Entregador).where(
            Entregador.sub_base == sub_base_user,
            Entregador.documento == documento,
        )
    ).first()
    if exists:
        raise HTTPException(status_code=409, detail="Já existe um entregador com esse documento nesta sub_base.")

    try:
        # 1️⃣ Cria ENTREGADOR
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

        # 2️⃣ Se coletador → cria USER
        if coletador_flag:
            new_user = User(
                password_hash=get_password_hash(senha_raw),
                username=username_ent,
                contato=telefone or "",
                nome=nome or None,
                status=True,
                sub_base=sub_base_user,
                coletador=True,
                username_entregador=username_ent,
                role=3,
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
    """Lista entregadores da sub_base do solicitante, filtrando por status"""
    sub_base_user = _resolve_user_base(db, current_user)

    stmt = select(Entregador).where(Entregador.sub_base == sub_base_user)
    if status == "ativo":
        stmt = stmt.where(Entregador.ativo.is_(True))
    elif status == "inativo":
        stmt = stmt.where(Entregador.ativo.is_(False))
    stmt = stmt.order_by(Entregador.nome)

    return db.scalars(stmt).all()


@router.get("/{id_entregador}", response_model=EntregadorOut)
def get_entregador(
    id_entregador: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Retorna um entregador específico"""
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
    Atualização parcial do ENTREGADOR + promoção opcional a COLETADOR:

    - Atualiza dados do entregador.
    - username alvo = body.username_entregador (se enviado) senão o atual do entregador.
    - Procura/corrige o User correspondente. Se *não* existir e:
        (a) coletador está sendo marcado como True (no body) OU o entregador já é coletador,
        (b) e existir username_alvo e senha no PATCH,
      então CRIA o User com password_hash, username, sub_base, nome/contato, coletador e role=3.
    - Se o User existir, sincroniza: username, coletador, senha (hash), nome, contato, role.
    - 'ativo' só altera na tabela entregador (não mexe no status do user).
    """
    sub_base_user = _resolve_user_base(db, current_user)
    obj = _get_owned_entregador(db, sub_base_user, id_entregador)

    try:
        # =======================
        # 1) Atualiza ENTREGADOR
        # =======================
        if body.nome is not None:
            obj.nome = body.nome.strip()
        if body.telefone is not None:
            obj.telefone = body.telefone.strip()

        if body.documento is not None:
            novo_doc = (body.documento or "").strip()
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

        if body.rua is not None:         obj.rua = body.rua.strip()
        if body.numero is not None:      obj.numero = body.numero.strip()
        if body.complemento is not None: obj.complemento = body.complemento.strip()
        if body.cep is not None:         obj.cep = body.cep.strip()
        if body.cidade is not None:      obj.cidade = body.cidade.strip()
        if body.bairro is not None:      obj.bairro = body.bairro.strip()

        # Ativa/desativa ENTREGADOR (não mexe no status do User)
        if body.ativo is not None:
            obj.ativo = bool(body.ativo)

        # username alvo para vincular/atualizar o User
        username_alvo = (body.username_entregador or obj.username_entregador or "").strip()

        # Se o PATCH pediu mudança de username do ENTREGADOR, já grava aqui
        if body.username_entregador is not None:
            obj.username_entregador = username_alvo or None

        # Coletador desejado (se não veio, mantém o atual do entregador)
        coletador_desejado = bool(obj.coletador) if body.coletador is None else bool(body.coletador)

        # ==========================
        # 2) Localiza/Cria o USER
        # ==========================
        user = _find_matching_user(db, sub_base_user, username_alvo if username_alvo else obj.username_entregador)

        # (A) Se não existe user e devemos promover/criar:
        deve_criar_user = (
            user is None and
            coletador_desejado is True and               # está virando ou já é coletador
            bool(username_alvo) and                      # temos username alvo
            (body.senha is not None and body.senha.strip() != "")  # e temos senha para hash
        )

        if deve_criar_user:
            # Unicidade do username (global)
            clash = db.scalars(select(User).where(User.username == username_alvo)).first()
            if clash:
                raise HTTPException(status_code=409, detail="Já existe um usuário com este username.")

            user = User(
                password_hash=get_password_hash(body.senha.strip()),
                username=username_alvo,
                username_entregador=username_alvo,
                sub_base=sub_base_user,
                nome=(body.nome or obj.nome) or None,
                contato=(body.telefone or obj.telefone) or "",
                coletador=True,
                role=3,
                status=True,
            )
            db.add(user)

        # (B) Se o user já existe, sincroniza alterações
        if user is not None:
            houve_alteracao_user = False

            # Se o PATCH trocou o username_entregador, precisamos garantir unicidade
            if body.username_entregador is not None:
                if username_alvo:
                    outro = db.scalars(select(User).where(User.username == username_alvo)).first()
                    if outro and outro is not user:
                        raise HTTPException(status_code=409, detail="Já existe um usuário com este username.")
                    if user.username != username_alvo:
                        user.username = username_alvo
                        houve_alteracao_user = True
                    if user.username_entregador != username_alvo:
                        user.username_entregador = username_alvo
                        houve_alteracao_user = True

            # Sincroniza coletador
            if user.coletador != coletador_desejado:
                user.coletador = coletador_desejado
                houve_alteracao_user = True

            # Atualiza nome/contato a partir do PATCH (se enviados)
            if body.nome is not None and body.nome.strip():
                if user.nome != body.nome.strip():
                    user.nome = body.nome.strip()
                    houve_alteracao_user = True
            if body.telefone is not None and body.telefone.strip():
                if user.contato != body.telefone.strip():
                    user.contato = body.telefone.strip()
                    houve_alteracao_user = True

            # Atualiza senha (hash) se enviada
            if body.senha is not None:
                raw = body.senha.strip()
                if not raw:
                    raise HTTPException(status_code=400, detail="A nova 'senha' não pode ser vazia.")
                user.password_hash = get_password_hash(raw)
                houve_alteracao_user = True

            # Marca role=3 quando houver alterações relevantes
            if houve_alteracao_user:
                user.role = 3

        # Por fim, espelha o coletador no ENTREGADOR (depois de criar/sincronizar user)
        if body.coletador is not None:
            obj.coletador = coletador_desejado

        db.commit()
        db.refresh(obj)
        return obj

    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="Erro ao atualizar o entregador/coletador.")
@router.delete("/{id_entregador}", status_code=status.HTTP_204_NO_CONTENT)
def delete_entregador(
    id_entregador: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Remove o entregador, sem deletar o user"""
    sub_base_user = _resolve_user_base(db, current_user)
    obj = _get_owned_entregador(db, sub_base_user, id_entregador)
    db.delete(obj)
    db.commit()
    return
