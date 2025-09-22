from __future__ import annotations

from typing import Optional, List
from datetime import datetime, date, timedelta

from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from models import User, Owner, Saida

router = APIRouter(prefix="/saidas", tags=["Saídas"])

# ---------- SCHEMAS ----------
class SaidaCreate(BaseModel):
    entregador: str = Field(min_length=1)
    codigo: str = Field(min_length=1)
    servico: str = Field(min_length=1)  # vem do front

class SaidaOut(BaseModel):
    id_saida: int
    timestamp: datetime
    data: date
    sub_base: Optional[str]
    username: Optional[str]
    entregador: Optional[str]
    codigo: Optional[str]
    servico: Optional[str]
    status: Optional[str]
    model_config = ConfigDict(from_attributes=True)

# Saídas para a grid (listar)
class SaidaGridItem(BaseModel):
    timestamp: datetime   # Data/Hora
    entregador: Optional[str]
    codigo: Optional[str]
    servico: Optional[str]
    status: Optional[str]
    model_config = ConfigDict(from_attributes=True)

# Atualização parcial
class SaidaUpdate(BaseModel):
    entregador: Optional[str] = Field(None, description="Novo entregador")
    status: Optional[str] = Field(None, description="Novo status")

# ---------- HELPERS ----------
def _resolve_user_base(db: Session, current_user: User) -> str:
    """
    Determina a sub_base (v2) do usuário, sem fallback frouxo.
    Tenta por id, depois por email e por username.
    Exige que 'users.sub_base' esteja preenchido.
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

    username = getattr(current_user, "username", None)
    if username:
        u = db.scalars(select(User).where(User.username == username)).first()
        if u and getattr(u, "sub_base", None):
            return u.sub_base

    raise HTTPException(status_code=401, detail="Usuário sem 'sub_base' definida em 'users'.")

def _get_owner_for_base_or_user(
    db: Session,
    sub_base_user: str,
    email: str | None,
    username: str | None
) -> Owner:
    """
    Retorna o Owner responsável pela sub_base do usuário (estrito).
    Exige que 'owner.sub_base' exista e esteja preenchida.
    """
    owner = db.scalars(select(Owner).where(Owner.sub_base == sub_base_user)).first()
    if owner:
        return owner
    raise HTTPException(status_code=404, detail="Owner não encontrado para esta 'sub_base'.")

# ---------- POST: REGISTRAR ----------
@router.post("/registrar", status_code=status.HTTP_201_CREATED,
    responses={
        409: {"description": "Conflitos (duplicidade / créditos)"},
        402: {"description": "Mensalidade vencida"},
        404: {"description": "Owner não encontrado"},
        401: {"description": "Não autenticado"},
        422: {"description": "Validação"},
        500: {"description": "Erro interno"},
    }
)
def registrar_saida(
    payload: SaidaCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    username = getattr(current_user, "username", None)
    email = getattr(current_user, "email", None)
    if not username:
        raise HTTPException(status_code=401, detail="Usuário sem 'username'.")

    # sub_base e owner (usados para fins de cobrança)
    sub_base_user = _resolve_user_base(db, current_user)
    owner = _get_owner_for_base_or_user(db, sub_base_user, email, username)

    # Regras de cobrança
    try:
        cobranca = int(str(owner.cobranca or "0"))
    except Exception:
        cobranca = 0

    valor_un = float(owner.valor or 0.0)
    creditos = float(owner.creditos or 0.0)
    mensalidade = owner.mensalidade

    # Dados do payload (servico vem do front)
    codigo = payload.codigo.strip()
    entregador = payload.entregador.strip()
    servico = payload.servico.strip()

    # 🔎 Checa duplicidade por sub_base + código (409 estruturado)
    existente = db.scalars(
        select(Saida).where(Saida.sub_base == sub_base_user, Saida.codigo == codigo)
    ).first()
    if existente:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "DUPLICATE_SAIDA",
                    "message": f"O código '{codigo}' já foi registrado anteriormente."}
        )

    try:
        # 1) Cobrança
        if cobranca == 0:  # pré-pago
            custo = round(valor_un * 1, 2)
            if creditos < custo:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={"code": "INSUFFICIENT_CREDITS",
                            "message": f"Créditos insuficientes. Necessário {custo:.2f}, saldo {creditos:.2f}."}
                )
            owner.creditos = round(creditos - custo, 2)
            db.add(owner)

        elif cobranca == 1:  # mensalidade
            if not mensalidade or date.today() > mensalidade:
                raise HTTPException(status_code=402, detail="Mensalidade vencida ou não configurada.")
        else:
            raise HTTPException(status_code=422, detail="Valor inválido em 'cobranca' (use 0 ou 1).")

        # 2) Insert único
        row = Saida(
            sub_base=sub_base_user,
            username=username,
            entregador=entregador,
            codigo=codigo,
            servico=servico,   # grava exatamente o que veio do front
            status="saiu",
        )
        db.add(row)
        db.commit()
        db.refresh(row)

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Erro ao registrar saída: {e}")

    return SaidaOut.model_validate(row)

# ---------- GET: LISTAR COM FILTROS ----------
@router.get("/listar", response_model=List[SaidaGridItem])
def listar_saidas(
    # Filtros da UI
    de: Optional[date] = Query(None, description="Data inicial (yyyy-mm-dd)"),
    ate: Optional[date] = Query(None, description="Data final (yyyy-mm-dd)"),
    entregador: Optional[str] = Query(None, description="Filtra por entregador (texto exato)"),
    status_: Optional[str] = Query(None, alias="status", description="Filtra por status (texto exato)"),
    codigo: Optional[str] = Query(None, description="Filtro 'contém' no código"),
    # Paginação simples (opcional)
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Retorna saídas da sub_base do usuário autenticado, aplicando filtros opcionais:
    - de / ate: filtra por Saida.data (datas inclusivas)
    - entregador: igualdade exata (ignore se vazio ou '(Todos)')
    - status: igualdade exata (ignore se vazio ou '(Todos)')
    - codigo: contém (ILIKE)
    Ordenado por timestamp desc.
    """
    # Descobre a sub_base do usuário
    sub_base_user = _resolve_user_base(db, current_user)

    # Monta consulta base
    stmt = select(Saida).where(Saida.sub_base == sub_base_user)

    # Filtro por datas na coluna DATE (inclusivo)
    if de:
        stmt = stmt.where(Saida.data >= de)
    if ate:
        stmt = stmt.where(Saida.data <= ate)

    # Entregador (ignora campo vazio ou "(Todos)")
    if entregador and entregador.strip() and entregador.strip().lower() != "(todos)":
        stmt = stmt.where(Saida.entregador == entregador.strip())

    # Status (ignora campo vazio ou "(Todos)")
    if status_ and status_.strip() and status_.strip().lower() != "(todos)":
        stmt = stmt.where(Saida.status == status_.strip())

    # Código contém (ILIKE)
    if codigo and codigo.strip():
        like = f"%{codigo.strip()}%"
        stmt = stmt.where(Saida.codigo.ilike(like))

    # Ordenação + paginação
    stmt = stmt.order_by(Saida.timestamp.desc()).offset(offset).limit(limit)

    rows = db.execute(stmt).scalars().all()

    # Mapeia apenas as colunas mostradas na grid
    return [
        SaidaGridItem(
            timestamp=r.timestamp,
            entregador=r.entregador,
            codigo=r.codigo,
            servico=r.servico,
            status=r.status,
        )
        for r in rows
    ]

# ---------- PATCH: ATUALIZAR CAMPOS (status / entregador) ----------
@router.patch(
    "/{id_saida}",
    response_model=SaidaOut,
    responses={
        200: {"description": "Atualizado com sucesso"},
        401: {"description": "Não autenticado"},
        404: {"description": "Saída não encontrada"},
        422: {"description": "Nenhum campo para atualizar"},
        500: {"description": "Erro interno"},
    },
)
def atualizar_saida(
    id_saida: int,
    payload: SaidaUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    obj = db.get(Saida, id_saida)
    if not obj:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "SAIDA_NOT_FOUND", "message": "Saída não encontrada."}
        )

    # Pelo menos um campo deve ser enviado
    if payload.entregador is None and payload.status is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "NO_FIELDS_TO_UPDATE", "message": "Informe ao menos um campo para atualizar (status ou entregador)."}
        )

    try:
        if payload.entregador is not None:
            obj.entregador = payload.entregador.strip()
        if payload.status is not None:
            obj.status = payload.status.strip()

        db.add(obj)
        db.commit()
        db.refresh(obj)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail={"code": "UPDATE_FAILED", "message": "Erro ao atualizar a saída."})

    return SaidaOut.model_validate(obj)

# ---------- DELETE: REMOVER (com janela de 1 dia + estorno) ----------
@router.delete(
    "/{id_saida}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        204: {"description": "Removido com sucesso"},
        401: {"description": "Não autenticado"},
        404: {"description": "Saída não encontrada"},
        409: {"description": "Janela de exclusão expirada"},
        500: {"description": "Erro interno"},
    },
)
def deletar_saida(
    id_saida: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Remove uma saída pelo ID **apenas se criada há <= 1 dia**.
    Se o plano do Owner for pré-pago (cobranca=0), estorna os créditos no valor unitário vigente.
    """
    obj = db.get(Saida, id_saida)
    if not obj:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "SAIDA_NOT_FOUND", "message": "Saída não encontrada."}
        )

    # Verifica janela de 1 dia
    if obj.timestamp is None:
        # Sem timestamp não conseguimos garantir janela; bloquear por segurança
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "DELETE_WINDOW_EXPIRED", "message": "Exclusão não permitida: janela de 1 dia expirada."}
        )

    agora = datetime.utcnow()
    # Se seu timestamp já está em timezone-aware/UTC, esse cálculo funciona;
    # caso contrário, ajuste para o mesmo timezone que grava no banco.
    if agora - obj.timestamp > timedelta(days=1):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "DELETE_WINDOW_EXPIRED", "message": "Exclusão não permitida: janela de 1 dia expirada."}
        )

    # Recupera Owner pela sub_base do usuário atual (para estorno)
    sub_base_user = _resolve_user_base(db, current_user)
    owner = _get_owner_for_base_or_user(db, sub_base_user, getattr(current_user, "email", None), getattr(current_user, "username", None))

    try:
        # Se pré-pago, estorna o mesmo valor debitado no POST (valor_un * 1)
        try:
            cobranca = int(str(owner.cobranca or "0"))
        except Exception:
            cobranca = 0

        if cobranca == 0:
            valor_un = float(owner.valor or 0.0)
            owner.creditos = round(float(owner.creditos or 0.0) + round(valor_un * 1, 2), 2)
            db.add(owner)

        # Exclui a saída
        db.delete(obj)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail={"code": "DELETE_FAILED", "message": "Erro ao deletar a saída."})

    return
