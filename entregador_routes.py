from __future__ import annotations
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, AliasChoices
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user, get_password_hash, DEFAULT_PASSWORD
from models import Entregador, EntregadorFechamento, EntregadorPreco, EntregadorPrecoGlobal, Motoboy, MotoboySubBase, Saida, User
from saida_operacional_utils import filtrar_saidas_por_periodo_operacional

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
    complemento: Optional[str] = None
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


class ExecutorItem(BaseModel):
    """Item para dropdown de Fechamento de Motoboys: entregador ou motoboy."""
    id_entregador: Optional[int] = None
    id_motoboy: Optional[int] = None
    nome: str
    tipo: str  # "entregador" | "motoboy"
    executor_tipo: Optional[str] = None  # "e" | "m"
    executor_id: Optional[int] = None
    executor_key: Optional[str] = None

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


class EntregadorResumoItem(BaseModel):
    data: str  # YYYY-MM-DD
    entregador_id: Optional[int] = None
    motoboy_id: Optional[int] = None
    entregador_nome: str
    shopee: Dict[str, Any]  # {qtde, valor_unit, total}
    flex: Dict[str, Any]
    avulso: Dict[str, Any]
    total_dia: Decimal
    total_feitos: int = 0
    total_cancelado: int = 0
    valor_feitos: Decimal = Decimal("0.00")
    valor_cancelados: Decimal = Decimal("0.00")
    valor_total: Decimal = Decimal("0.00")
    g_total: int = 0
    fechamento_status: Optional[str] = None  # PENDENTE | GERADO | REAJUSTADO
    id_fechamento: Optional[int] = None  # quando existe fechamento
    pode_reajustar: Optional[bool] = None  # True quando GERADO e valor_base atual != valor_base fechado
    valor_base_atual: Optional[Decimal] = None  # valor_base recalculado no período
    valor_base_fechado: Optional[Decimal] = None  # valor_base gravado no fechamento
    periodo_inicio: Optional[str] = None  # YYYY-MM-DD, quando existe fechamento
    periodo_fim: Optional[str] = None  # YYYY-MM-DD, quando existe fechamento


class EntregadorResumoResponse(BaseModel):
    page: int
    pageSize: int
    totalPages: int
    totalItems: int
    items: List[EntregadorResumoItem]
    sumShopee: int
    sumFlex: int
    sumAvulso: int
    sumValor: Decimal
    sumTotalEntregas: int
    sumTotalCancelado: int


class PrecoGlobalOut(BaseModel):
    shopee_valor: Decimal
    ml_valor: Decimal
    avulso_valor: Decimal


class PrecoGlobalUpdate(BaseModel):
    shopee_valor: Optional[Decimal] = Field(None, ge=0)
    ml_valor: Optional[Decimal] = Field(None, ge=0)
    avulso_valor: Optional[Decimal] = Field(None, ge=0)


class PrecoIndividualItem(BaseModel):
    entregador_id: int
    entregador_nome: Optional[str] = None
    shopee_valor: Optional[Decimal] = None
    ml_valor: Optional[Decimal] = None
    avulso_valor: Optional[Decimal] = None


class PrecoIndividuaisResponse(BaseModel):
    items: List[PrecoIndividualItem]


class PrecoEntregadorUpdate(BaseModel):
    shopee_valor: Optional[Decimal] = Field(None, ge=0)
    ml_valor: Optional[Decimal] = Field(None, ge=0)
    avulso_valor: Optional[Decimal] = Field(None, ge=0)


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


def resolver_precos_entregador(
    db: Session,
    id_entregador: int,
    sub_base: str,
) -> Dict[str, Decimal]:
    """
    Retorna {shopee_valor, ml_valor, avulso_valor} para o entregador.

    Lógica:
    1. Buscar EntregadorPreco onde id_entregador = X
    2. Se existir E usa_preco_global = False E valor != NULL:
       usar valor específico do entregador
    3. Caso contrário:
       usar EntregadorPrecoGlobal da sub_base
    """
    zero = Decimal("0.00")

    preco = db.scalars(
        select(EntregadorPreco).where(EntregadorPreco.id_entregador == id_entregador)
    ).first()

    global_row = db.scalars(
        select(EntregadorPrecoGlobal).where(EntregadorPrecoGlobal.sub_base == sub_base)
    ).first()

    shopee_global = global_row.shopee_valor if global_row else zero
    ml_global = global_row.ml_valor if global_row else zero
    avulso_global = global_row.avulso_valor if global_row else zero

    if preco and preco.usa_preco_global is False:
        shopee_valor = preco.shopee_valor if preco.shopee_valor is not None else shopee_global
        ml_valor = preco.ml_valor if preco.ml_valor is not None else ml_global
        avulso_valor = preco.avulso_valor if preco.avulso_valor is not None else avulso_global
        return {"shopee_valor": shopee_valor, "ml_valor": ml_valor, "avulso_valor": avulso_valor}

    if global_row:
        return {"shopee_valor": shopee_global, "ml_valor": ml_global, "avulso_valor": avulso_global}
    return {"shopee_valor": zero, "ml_valor": zero, "avulso_valor": zero}


def _resolver_entregador_principal_do_motoboy(
    db: Session,
    sub_base: str,
    motoboy_id: int,
) -> Optional[int]:
    """
    Retorna o id_entregador mais provável para um motoboy da sub_base.
    """
    entregador_ids, _ = _resolve_executor_scope_ids(
        db=db,
        sub_base_user=sub_base,
        motoboy_id=motoboy_id,
    )
    if not entregador_ids:
        return None
    return sorted(entregador_ids)[0]


def resolver_precos_motoboy(
    db: Session,
    sub_base: str,
    motoboy_id: Optional[int] = None,
) -> Dict[str, Decimal]:
    """
    Retorna {shopee_valor, ml_valor, avulso_valor} para motoboy.
    Regra:
    1) quando houver motoboy_id, tenta mapear para entregador e usar exceção de entregador;
    2) fallback para preço global da sub_base.
    """
    zero = Decimal("0.00")
    if motoboy_id is not None:
        entregador_id = _resolver_entregador_principal_do_motoboy(
            db=db,
            sub_base=sub_base,
            motoboy_id=motoboy_id,
        )
        if entregador_id is not None:
            return resolver_precos_entregador(
                db=db,
                id_entregador=entregador_id,
                sub_base=sub_base,
            )

    global_row = db.scalars(
        select(EntregadorPrecoGlobal).where(EntregadorPrecoGlobal.sub_base == sub_base)
    ).first()
    if global_row:
        return {
            "shopee_valor": global_row.shopee_valor or zero,
            "ml_valor": global_row.ml_valor or zero,
            "avulso_valor": global_row.avulso_valor or zero,
        }
    return {"shopee_valor": zero, "ml_valor": zero, "avulso_valor": zero}


# =========================================================
# ROTAS
# =========================================================

def _optional(value):
    if value is None:
        return None
    value = value.strip()
    return value or None


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

    # normalização (obrigatórios)
    nome        = (body.nome or "").strip()
    telefone    = (body.telefone or "").strip()
    documento   = (body.documento or "").strip()

    if not documento:
        raise HTTPException(status_code=400, detail="O campo 'documento' é obrigatório.")

    # normalização (opcionais)
    rua         = _optional(body.rua)
    numero      = _optional(body.numero)
    complemento = _optional(body.complemento)
    cep         = _optional(body.cep)
    cidade      = _optional(body.cidade)
    bairro      = _optional(body.bairro)

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
                must_change_password=True,
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


@router.get("/executores", response_model=List[ExecutorItem])
def list_executores(
    status: Optional[str] = Query("ativo", description="Para entregadores: ativo, inativo ou todos"),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Lista executores (entregadores + motoboys) da sub_base para dropdown de Fechamento de Motoboys."""
    sub_base_user = _resolve_user_base(db, current_user)
    out: List[ExecutorItem] = []

    # Entregadores
    stmt_ent = select(Entregador).where(Entregador.sub_base == sub_base_user)
    if status == "ativo":
        stmt_ent = stmt_ent.where(Entregador.ativo.is_(True))
    elif status == "inativo":
        stmt_ent = stmt_ent.where(Entregador.ativo.is_(False))
    stmt_ent = stmt_ent.order_by(Entregador.nome)
    for ent in db.scalars(stmt_ent).all():
        out.append(ExecutorItem(
            id_entregador=ent.id_entregador,
            id_motoboy=None,
            nome=(ent.nome or f"Entregador {ent.id_entregador}").strip(),
            tipo="entregador",
            executor_tipo="e",
            executor_id=ent.id_entregador,
            executor_key=f"e_{ent.id_entregador}",
        ))

    # Motoboys vinculados à sub_base
    stmt_mb = (
        select(Motoboy)
        .join(MotoboySubBase, MotoboySubBase.motoboy_id == Motoboy.id_motoboy)
        .where(
            MotoboySubBase.sub_base == sub_base_user,
            MotoboySubBase.ativo.is_(True),
        )
        .order_by(Motoboy.id_motoboy)
    )
    for motoboy in db.scalars(stmt_mb).all():
        nome = _get_motoboy_nome(db, motoboy.id_motoboy)
        out.append(ExecutorItem(
            id_entregador=None,
            id_motoboy=motoboy.id_motoboy,
            nome=nome,
            tipo="motoboy",
            executor_tipo="m",
            executor_id=motoboy.id_motoboy,
            executor_key=f"m_{motoboy.id_motoboy}",
        ))

    return out


def _normalizar_servico(servico: Optional[str]) -> str:
    s = (servico or "").lower()
    if "shopee" in s:
        return "shopee"
    if "ml" in s or "mercado" in s:
        return "flex"
    return "avulso"


def _get_motoboy_nome(db: Session, motoboy_id: int) -> str:
    """Nome do motoboy (User) para exibição no resumo."""
    motoboy = db.get(Motoboy, motoboy_id)
    if not motoboy or not motoboy.user_id:
        return f"Motoboy {motoboy_id}"
    u = db.get(User, motoboy.user_id)
    if not u:
        return f"Motoboy {motoboy_id}"
    nome = (f"{u.nome or ''} {u.sobrenome or ''}".strip() or u.username or "").strip()
    return nome or f"Motoboy {motoboy_id}"


def _resolve_executor_scope_ids(
    db: Session,
    sub_base_user: str,
    entregador_id: Optional[int] = None,
    motoboy_id: Optional[int] = None,
) -> tuple[set[int], set[int]]:
    """
    Resolve IDs equivalentes de um mesmo profissional entre dados legados (entregador_id)
    e modelo atual (motoboy_id), usando username/documento como vínculo.
    """
    entregador_ids: set[int] = set()
    motoboy_ids: set[int] = set()
    usernames: set[str] = set()
    documentos: set[str] = set()

    if entregador_id is not None and entregador_id > 0:
        ent = db.get(Entregador, entregador_id)
        if ent and ent.sub_base == sub_base_user:
            entregador_ids.add(ent.id_entregador)
            if (ent.username_entregador or "").strip():
                usernames.add(ent.username_entregador.strip())
            if (ent.documento or "").strip():
                documentos.add(ent.documento.strip())

    if motoboy_id is not None and motoboy_id > 0:
        mb = db.get(Motoboy, motoboy_id)
        if mb:
            pertence_sub_base = bool(mb.sub_base == sub_base_user)
            if not pertence_sub_base:
                pertence_sub_base = db.scalar(
                    select(func.count())
                    .select_from(MotoboySubBase)
                    .where(
                        MotoboySubBase.motoboy_id == motoboy_id,
                        MotoboySubBase.sub_base == sub_base_user,
                    )
                ) > 0
            if pertence_sub_base:
                motoboy_ids.add(mb.id_motoboy)
                if (mb.documento or "").strip():
                    documentos.add(mb.documento.strip())
                if mb.user_id:
                    u = db.get(User, mb.user_id)
                    if u and u.sub_base == sub_base_user and (u.username or "").strip():
                        usernames.add(u.username.strip())

    if usernames or documentos:
        stmt_ent = select(Entregador.id_entregador).where(Entregador.sub_base == sub_base_user)
        conds_ent = []
        if usernames:
            conds_ent.append(Entregador.username_entregador.in_(sorted(usernames)))
        if documentos:
            conds_ent.append(Entregador.documento.in_(sorted(documentos)))
        if conds_ent:
            stmt_ent = stmt_ent.where(or_(*conds_ent))
            for eid in db.scalars(stmt_ent).all():
                if eid is not None:
                    entregador_ids.add(int(eid))

        stmt_mb = (
            select(Motoboy.id_motoboy)
            .join(User, User.id == Motoboy.user_id, isouter=True)
            .outerjoin(MotoboySubBase, MotoboySubBase.motoboy_id == Motoboy.id_motoboy)
            .where(
                or_(
                    Motoboy.sub_base == sub_base_user,
                    MotoboySubBase.sub_base == sub_base_user,
                )
            )
        )
        conds_mb = []
        if usernames:
            conds_mb.append(User.username.in_(sorted(usernames)))
        if documentos:
            conds_mb.append(Motoboy.documento.in_(sorted(documentos)))
        if conds_mb:
            stmt_mb = stmt_mb.where(or_(*conds_mb))
            for mid in db.scalars(stmt_mb).all():
                if mid is not None:
                    motoboy_ids.add(int(mid))

    return entregador_ids, motoboy_ids


STATUS_VALOR_BASE_VALIDOS = [
    "saiu",
    "saiu pra entrega",
    "saiu_pra_entrega",
    "saiu_para_entrega",
    "em_rota",
    "entregue",
    "ausente",
    "cancelado",
    "cancelados",
]


def _calcular_valor_base_periodo(
    db: Session,
    sub_base_user: str,
    entregador_id: int,
    periodo_inicio: date,
    periodo_fim: date,
) -> Decimal:
    """Calcula valor_base por escopo equivalente do entregador (legacy + motoboy vinculado)."""
    if periodo_inicio > periodo_fim:
        return Decimal("0.00")

    entregador_ids, motoboy_ids = _resolve_executor_scope_ids(
        db=db,
        sub_base_user=sub_base_user,
        entregador_id=entregador_id,
    )
    conds_executor = []
    if entregador_ids:
        conds_executor.append(Saida.entregador_id.in_(sorted(entregador_ids)))
    if motoboy_ids:
        conds_executor.append(Saida.motoboy_id.in_(sorted(motoboy_ids)))
    if not conds_executor:
        return Decimal("0.00")

    stmt = select(Saida).where(
        Saida.sub_base == sub_base_user,
        Saida.codigo.isnot(None),
        func.lower(Saida.status).in_(STATUS_VALOR_BASE_VALIDOS),
        or_(*conds_executor),
    )
    rows_raw = db.scalars(stmt).all()
    rows, _ = filtrar_saidas_por_periodo_operacional(db, rows_raw, periodo_inicio, periodo_fim)
    precos = resolver_precos_entregador(db, entregador_id, sub_base_user)
    total = Decimal("0.00")
    for saida in rows:
        status_norm = (saida.status or "").strip().lower()
        is_cancelado = "cancel" in status_norm
        tipo = _normalizar_servico(saida.servico)
        delta = Decimal("0.00")
        if tipo == "shopee":
            delta = precos["shopee_valor"]
        elif tipo == "flex":
            delta = precos["ml_valor"]
        else:
            delta = precos["avulso_valor"]
        total += (-delta if is_cancelado else delta)
    return total.quantize(Decimal("0.01"))


def _calcular_valor_base_motoboy_periodo(
    db: Session,
    sub_base_user: str,
    motoboy_id: int,
    periodo_inicio: date,
    periodo_fim: date,
) -> Decimal:
    """Calcula o valor_base a partir das saídas do motoboy no período (resumo/fechamento)."""
    if periodo_inicio > periodo_fim:
        return Decimal("0.00")
    stmt = select(Saida).where(
        Saida.sub_base == sub_base_user,
        Saida.motoboy_id == motoboy_id,
        Saida.codigo.isnot(None),
        func.lower(Saida.status).in_(STATUS_VALOR_BASE_VALIDOS),
    )
    rows_raw = db.scalars(stmt).all()
    rows, _ = filtrar_saidas_por_periodo_operacional(db, rows_raw, periodo_inicio, periodo_fim)
    precos = resolver_precos_motoboy(db, sub_base_user, motoboy_id=motoboy_id)
    total = Decimal("0.00")
    for saida in rows:
        status_norm = (saida.status or "").strip().lower()
        is_cancelado = "cancel" in status_norm
        tipo = _normalizar_servico(saida.servico)
        delta = Decimal("0.00")
        if tipo == "shopee":
            delta = precos["shopee_valor"]
        elif tipo == "flex":
            delta = precos["ml_valor"]
        else:
            delta = precos["avulso_valor"]
        total += (-delta if is_cancelado else delta)
    return total.quantize(Decimal("0.01"))


@router.get("/resumo", response_model=EntregadorResumoResponse)
def resumo_entregadores(
    data_inicio: Optional[date] = Query(None),
    data_fim: Optional[date] = Query(None),
    entregador_id: Optional[int] = Query(None),
    motoboy_id: Optional[int] = Query(None),
    executor_tipo: Optional[str] = Query(None),
    executor_id: Optional[int] = Query(None),
    fechamento_status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    pageSize: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    sub_base_user = _resolve_user_base(db, current_user)
    if entregador_id is not None and motoboy_id is not None:
        raise HTTPException(400, "Informe apenas um de entregador_id ou motoboy_id.")

    if executor_id is not None and executor_tipo:
        tipo = executor_tipo.strip().lower()
        if tipo in ("e", "entregador"):
            if motoboy_id is not None:
                raise HTTPException(400, "Não combine executor_tipo=entregador com motoboy_id.")
            entregador_id = executor_id
        elif tipo in ("m", "motoboy"):
            if entregador_id is not None:
                raise HTTPException(400, "Não combine executor_tipo=motoboy com entregador_id.")
            motoboy_id = executor_id
        else:
            raise HTTPException(400, "executor_tipo inválido. Use 'e' ou 'm'.")

    # Inclui saída e entrega; mantém consistência com STATUS_VALOR_BASE_VALIDOS.
    status_validos = STATUS_VALOR_BASE_VALIDOS
    stmt = select(Saida).where(
        Saida.sub_base == sub_base_user,
        Saida.codigo.isnot(None),
        func.lower(Saida.status).in_(status_validos),
        or_(
            Saida.entregador_id.isnot(None),
            Saida.entregador.isnot(None),
            Saida.motoboy_id.isnot(None),
        ),
    )

    if entregador_id is not None or motoboy_id is not None:
        entregador_ids, motoboy_ids = _resolve_executor_scope_ids(
            db=db,
            sub_base_user=sub_base_user,
            entregador_id=entregador_id,
            motoboy_id=motoboy_id,
        )
        conds_executor = []
        if entregador_ids:
            conds_executor.append(Saida.entregador_id.in_(sorted(entregador_ids)))
        if motoboy_ids:
            conds_executor.append(Saida.motoboy_id.in_(sorted(motoboy_ids)))
        if not conds_executor:
            stmt = stmt.where(Saida.id_saida == -1)
        else:
            stmt = stmt.where(or_(*conds_executor))

    rows_raw = db.scalars(stmt).all()
    rows, op_ctx_map = filtrar_saidas_por_periodo_operacional(db, rows_raw, data_inicio, data_fim)

    agrupado: Dict[str, Dict[str, Any]] = {}
    for saida in rows:
        ctx = op_ctx_map.get(saida.id_saida)
        op_ts = (ctx.operacional_ts if ctx and ctx.operacional_ts else None) or saida.timestamp
        dia = op_ts.date().isoformat()
        if getattr(saida, "motoboy_id", None) is not None:
            mid = saida.motoboy_id
            key = f"{dia}_m_{mid}"
            if key not in agrupado:
                agrupado[key] = {
                    "data": dia,
                    "entregador_id": None,
                    "motoboy_id": mid,
                    "entregador_nome": _get_motoboy_nome(db, mid),
                    "qtde_shopee": 0,
                    "qtde_flex": 0,
                    "qtde_avulso": 0,
                    "cancel_shopee": 0,
                    "cancel_flex": 0,
                    "cancel_avulso": 0,
                    "total_feitos": 0,
                    "total_cancelado": 0,
                    "g_total": 0,
                }
        else:
            ent_id = saida.entregador_id
            ent_nome = saida.entregador or "Sem nome"
            if ent_id is None:
                ent_id = -abs(hash(ent_nome))
            key = f"{dia}_{ent_id}"
            if key not in agrupado:
                agrupado[key] = {
                    "data": dia,
                    "entregador_id": ent_id,
                    "motoboy_id": None,
                    "entregador_nome": ent_nome,
                    "qtde_shopee": 0,
                    "qtde_flex": 0,
                    "qtde_avulso": 0,
                    "cancel_shopee": 0,
                    "cancel_flex": 0,
                    "cancel_avulso": 0,
                    "total_feitos": 0,
                    "total_cancelado": 0,
                    "g_total": 0,
                }
        status_norm = (saida.status or "").strip().lower()
        is_cancelado = "cancel" in status_norm
        tipo = _normalizar_servico(saida.servico)
        if is_cancelado:
            agrupado[key]["total_cancelado"] = agrupado[key].get("total_cancelado", 0) + 1
            agrupado[key][f"cancel_{tipo}"] += 1
        else:
            agrupado[key]["total_feitos"] = agrupado[key].get("total_feitos", 0) + 1
            agrupado[key][f"qtde_{tipo}"] += 1
        if getattr(saida, "is_grande", False):
            agrupado[key]["g_total"] = agrupado[key].get("g_total", 0) + 1

    cache_precos: Dict[tuple, Dict[str, Decimal]] = {}  # ("e", eid) ou ("m", mid) -> precos
    cache_fechamento: Dict[tuple, Optional[EntregadorFechamento]] = {}
    cache_valor_base: Dict[tuple, Decimal] = {}  # (eid, periodo_inicio, periodo_fim) ou (mid,) -> valor_base

    def _get_fechamento(eid: Optional[int], mid: Optional[int], data_str: str) -> tuple:
        """Retorna (status, id_fechamento, fechamento ou None). Entregador (id > 0) ou motoboy."""
        from datetime import datetime as dt
        try:
            data_ref = dt.strptime(data_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return ("PENDENTE", None, None)
        if mid is not None:
            key_cache = (sub_base_user, "m", mid, data_str)
            if key_cache not in cache_fechamento:
                if hasattr(EntregadorFechamento, "id_motoboy"):
                    fech = db.scalars(
                        select(EntregadorFechamento).where(
                            EntregadorFechamento.sub_base == sub_base_user,
                            EntregadorFechamento.id_motoboy == mid,
                            EntregadorFechamento.periodo_inicio <= data_ref,
                            EntregadorFechamento.periodo_fim >= data_ref,
                        )
                    ).first()
                else:
                    fech = None
                cache_fechamento[key_cache] = fech
            fech = cache_fechamento[key_cache]
            if fech:
                st = (fech.status or "").upper()
                if st == "FECHADO":
                    st = "GERADO"
                return (st or "GERADO", fech.id_fechamento, fech)
            return ("PENDENTE", None, None)
        if eid is None or eid <= 0:
            return ("PENDENTE", None, None)
        key_cache = (sub_base_user, "e", eid, data_str)
        if key_cache not in cache_fechamento:
            fech = db.scalars(
                select(EntregadorFechamento).where(
                    EntregadorFechamento.sub_base == sub_base_user,
                    EntregadorFechamento.id_entregador == eid,
                    EntregadorFechamento.periodo_inicio <= data_ref,
                    EntregadorFechamento.periodo_fim >= data_ref,
                )
            ).first()
            cache_fechamento[key_cache] = fech
        fech = cache_fechamento[key_cache]
        if fech:
            st = (fech.status or "").upper()
            if st == "FECHADO":
                st = "GERADO"
            return (st or "GERADO", fech.id_fechamento, fech)
        return ("PENDENTE", None, None)

    lista: List[EntregadorResumoItem] = []
    for key, item in agrupado.items():
        eid = item["entregador_id"]
        mid = item.get("motoboy_id")
        precos_key = ("m", mid) if mid is not None else ("e", eid)
        if precos_key not in cache_precos:
            if mid is not None:
                cache_precos[precos_key] = resolver_precos_motoboy(
                    db,
                    sub_base_user,
                    motoboy_id=mid,
                )
            elif eid is not None and eid > 0:
                cache_precos[precos_key] = resolver_precos_entregador(db, eid, sub_base_user)
            else:
                cache_precos[precos_key] = resolver_precos_motoboy(db, sub_base_user)
        precos = cache_precos[precos_key]
        valor_shopee = item["qtde_shopee"] * precos["shopee_valor"]
        valor_flex = item["qtde_flex"] * precos["ml_valor"]
        valor_avulso = item["qtde_avulso"] * precos["avulso_valor"]
        valor_feitos = valor_shopee + valor_flex + valor_avulso
        valor_cancelados = (
            Decimal(item.get("cancel_shopee", 0)) * precos["shopee_valor"]
            + Decimal(item.get("cancel_flex", 0)) * precos["ml_valor"]
            + Decimal(item.get("cancel_avulso", 0)) * precos["avulso_valor"]
        ).quantize(Decimal("0.01"))
        total_dia = (valor_feitos - valor_cancelados).quantize(Decimal("0.01"))

        fech_status, id_fech, fech = _get_fechamento(eid, mid, item["data"])

        pode_reajustar = None
        valor_base_atual = None
        valor_base_fechado = None
        periodo_inicio_str = None
        periodo_fim_str = None
        if fech_status == "GERADO" and fech is not None and id_fech is not None:
            if mid is not None:
                key_vb = ("m", mid, fech.periodo_inicio, fech.periodo_fim)
                if key_vb not in cache_valor_base and hasattr(EntregadorFechamento, "id_motoboy"):
                    cache_valor_base[key_vb] = _calcular_valor_base_motoboy_periodo(
                        db, sub_base_user, mid, fech.periodo_inicio, fech.periodo_fim
                    )
                valor_base_atual = cache_valor_base.get(key_vb)
            else:
                key_vb = (eid, fech.periodo_inicio, fech.periodo_fim)
                if key_vb not in cache_valor_base:
                    cache_valor_base[key_vb] = _calcular_valor_base_periodo(
                        db, sub_base_user, eid, fech.periodo_inicio, fech.periodo_fim
                    )
                valor_base_atual = cache_valor_base[key_vb]
            valor_base_fechado = fech.valor_base
            if valor_base_atual is not None:
                pode_reajustar = valor_base_atual != valor_base_fechado
            periodo_inicio_str = fech.periodo_inicio.isoformat() if hasattr(fech.periodo_inicio, "isoformat") else str(fech.periodo_inicio)
            periodo_fim_str = fech.periodo_fim.isoformat() if hasattr(fech.periodo_fim, "isoformat") else str(fech.periodo_fim)
        if fech is not None and id_fech is not None and periodo_inicio_str is None:
            periodo_inicio_str = fech.periodo_inicio.isoformat() if hasattr(fech.periodo_inicio, "isoformat") else str(fech.periodo_inicio)
            periodo_fim_str = fech.periodo_fim.isoformat() if hasattr(fech.periodo_fim, "isoformat") else str(fech.periodo_fim)

        lista.append(
            EntregadorResumoItem(
                data=item["data"],
                entregador_id=item["entregador_id"],
                motoboy_id=item.get("motoboy_id"),
                entregador_nome=item["entregador_nome"],
                shopee={
                    "qtde": item["qtde_shopee"],
                    "valor_unit": precos["shopee_valor"],
                    "total": valor_shopee,
                },
                flex={
                    "qtde": item["qtde_flex"],
                    "valor_unit": precos["ml_valor"],
                    "total": valor_flex,
                },
                avulso={
                    "qtde": item["qtde_avulso"],
                    "valor_unit": precos["avulso_valor"],
                    "total": valor_avulso,
                },
                total_dia=total_dia,
                total_feitos=item.get("total_feitos", 0),
                total_cancelado=item.get("total_cancelado", 0),
                valor_feitos=valor_feitos.quantize(Decimal("0.01")),
                valor_cancelados=valor_cancelados.quantize(Decimal("0.01")),
                valor_total=total_dia,
                g_total=item.get("g_total", 0),
                fechamento_status=fech_status,
                id_fechamento=id_fech,
                pode_reajustar=pode_reajustar,
                valor_base_atual=valor_base_atual,
                valor_base_fechado=valor_base_fechado,
                periodo_inicio=periodo_inicio_str,
                periodo_fim=periodo_fim_str,
            )
        )

    lista.sort(key=lambda x: (x.data, x.entregador_nome))

    if fechamento_status and str(fechamento_status).strip():
        status_upper = str(fechamento_status).strip().upper()
        lista = [i for i in lista if (i.fechamento_status or "PENDENTE").upper() == status_upper]

    # Totalizadores globais: soma sobre TODOS os registros filtrados (lista completa).
    # Paginação afeta APENAS o array "items". Filtros já aplicados: data_inicio, data_fim, entregador_id, fechamento_status.
    sumShopee = sum(i.shopee["qtde"] for i in lista)
    sumFlex = sum(i.flex["qtde"] for i in lista)
    sumAvulso = sum(i.avulso["qtde"] for i in lista)
    sumValor = sum((i.valor_total for i in lista), Decimal("0.00"))
    sumTotalEntregas = sumShopee + sumFlex + sumAvulso
    sumTotalCancelado = sum(i.total_cancelado for i in lista)

    totalItems = len(lista)
    totalPages = (totalItems + pageSize - 1) // pageSize if totalItems else 0
    start = (page - 1) * pageSize
    end = start + pageSize
    items = lista[start:end]

    return EntregadorResumoResponse(
        page=page,
        pageSize=pageSize,
        totalPages=totalPages,
        totalItems=totalItems,
        items=items,
        sumShopee=sumShopee,
        sumFlex=sumFlex,
        sumAvulso=sumAvulso,
        sumValor=sumValor,
        sumTotalEntregas=sumTotalEntregas,
        sumTotalCancelado=sumTotalCancelado,
    )


# =========================================================
# PREÇOS (global e individuais)
# =========================================================
_zero = Decimal("0.00")


@router.get("/precos/global", response_model=PrecoGlobalOut)
def get_preco_global(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Retorna valores globais de preço da sub_base do owner."""
    sub_base_user = _resolve_user_base(db, current_user)
    row = db.scalars(
        select(EntregadorPrecoGlobal).where(EntregadorPrecoGlobal.sub_base == sub_base_user)
    ).first()
    if row:
        return PrecoGlobalOut(
            shopee_valor=row.shopee_valor,
            ml_valor=row.ml_valor,
            avulso_valor=row.avulso_valor,
        )
    return PrecoGlobalOut(shopee_valor=_zero, ml_valor=_zero, avulso_valor=_zero)


@router.patch("/precos/global", response_model=PrecoGlobalOut)
def patch_preco_global(
    body: PrecoGlobalUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Cria ou atualiza valores globais da sub_base do owner. Payload não vazio."""
    if body.shopee_valor is None and body.ml_valor is None and body.avulso_valor is None:
        raise HTTPException(status_code=422, detail="Envie ao menos um campo: shopee_valor, ml_valor ou avulso_valor.")
    sub_base_user = _resolve_user_base(db, current_user)
    row = db.scalars(
        select(EntregadorPrecoGlobal).where(EntregadorPrecoGlobal.sub_base == sub_base_user)
    ).first()
    if row:
        if body.shopee_valor is not None:
            row.shopee_valor = body.shopee_valor
        if body.ml_valor is not None:
            row.ml_valor = body.ml_valor
        if body.avulso_valor is not None:
            row.avulso_valor = body.avulso_valor
    else:
        row = EntregadorPrecoGlobal(
            sub_base=sub_base_user,
            shopee_valor=body.shopee_valor if body.shopee_valor is not None else _zero,
            ml_valor=body.ml_valor if body.ml_valor is not None else _zero,
            avulso_valor=body.avulso_valor if body.avulso_valor is not None else _zero,
        )
        db.add(row)
    db.commit()
    db.refresh(row)
    return PrecoGlobalOut(shopee_valor=row.shopee_valor, ml_valor=row.ml_valor, avulso_valor=row.avulso_valor)


@router.get("/precos/individuais", response_model=PrecoIndividuaisResponse)
def get_precos_individuais(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Lista apenas exceções (usa_preco_global=False) da sub_base do owner."""
    sub_base_user = _resolve_user_base(db, current_user)
    stmt = (
        select(EntregadorPreco)
        .join(Entregador, EntregadorPreco.id_entregador == Entregador.id_entregador)
        .where(
            Entregador.sub_base == sub_base_user,
            EntregadorPreco.usa_preco_global.is_(False),
        )
    )
    rows = db.scalars(stmt).all()
    items = [
        PrecoIndividualItem(
            entregador_id=ep.id_entregador,
            entregador_nome=ep.entregador.nome if ep.entregador else None,
            shopee_valor=ep.shopee_valor,
            ml_valor=ep.ml_valor,
            avulso_valor=ep.avulso_valor,
        )
        for ep in rows
    ]
    return PrecoIndividuaisResponse(items=items)


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
        (a) coletador=True no body OU o entregador já é coletador,
        (b) e existir username_alvo e senha no PATCH,
      então CRIA um User (role=3).
    - Se o User existir, sincroniza tudo.
    """
    def _optional(v):
        if v is None:
            return None
        v = v.strip()
        return v or None

    sub_base_user = _resolve_user_base(db, current_user)
    obj = _get_owned_entregador(db, sub_base_user, id_entregador)

    try:
        # =======================
        # 1) Atualiza ENTREGADOR
        # =======================
        if body.nome is not None:
            obj.nome = _optional(body.nome)

        if body.telefone is not None:
            obj.telefone = _optional(body.telefone)

        # --- documento ---
        if body.documento is not None:
            novo_doc = (body.documento or "").strip()
            if not novo_doc:
                raise HTTPException(status_code=400, detail="O campo 'documento' não pode ser vazio.")
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

        # opcionais (rua, número, cep, cidade, etc)
        if body.rua is not None:         obj.rua = _optional(body.rua)
        if body.numero is not None:      obj.numero = _optional(body.numero)
        if body.complemento is not None: obj.complemento = _optional(body.complemento)
        if body.cep is not None:         obj.cep = _optional(body.cep)
        if body.cidade is not None:      obj.cidade = _optional(body.cidade)
        if body.bairro is not None:      obj.bairro = _optional(body.bairro)

        # --- ativo ---
        if body.ativo is not None:
            obj.ativo = bool(body.ativo)

        # username para vincular ao user
        username_alvo = (
            (body.username_entregador or "").strip()
            if body.username_entregador is not None
            else (obj.username_entregador or "").strip()
        ) or None

        # Se foi enviado no PATCH, grava no entregador
        if body.username_entregador is not None:
            obj.username_entregador = username_alvo

        # coletador desejado
        coletador_desejado = (
            obj.coletador if body.coletador is None else bool(body.coletador)
        )

        # ==========================
        # 2) Localiza / Cria USER
        # ==========================
        user = _find_matching_user(db, sub_base_user, username_alvo)

        deve_criar_user = (
            user is None and
            coletador_desejado is True and
            username_alvo and
            (body.senha and body.senha.strip())
        )

        if deve_criar_user:
            # unicidade global
            clash = db.scalars(select(User).where(User.username == username_alvo)).first()
            if clash:
                raise HTTPException(status_code=409, detail="Já existe um usuário com este username.")

            user = User(
                password_hash=get_password_hash(body.senha.strip()),
                username=username_alvo,
                username_entregador=username_alvo,
                sub_base=sub_base_user,
                nome=obj.nome,
                contato=obj.telefone or "",
                coletador=True,
                role=3,
                status=True,
            )
            db.add(user)

        # =============================
        # 3) Atualiza USER se existir
        # =============================
        if user is not None:
            mudou = False

            # username
            if body.username_entregador is not None:
                if username_alvo:
                    outro = db.scalars(select(User).where(User.username == username_alvo)).first()
                    if outro and outro is not user:
                        raise HTTPException(status_code=409, detail="Já existe um usuário com este username.")

                if user.username != username_alvo:
                    user.username = username_alvo
                    mudou = True
                if user.username_entregador != username_alvo:
                    user.username_entregador = username_alvo
                    mudou = True

            # coletador
            if user.coletador != coletador_desejado:
                user.coletador = coletador_desejado
                mudou = True

            # sincronizar nome/telefone (quando enviados)
            if body.nome is not None:
                nome_val = _optional(body.nome)
                if nome_val and user.nome != nome_val:
                    user.nome = nome_val
                    mudou = True

            if body.telefone is not None:
                tel_val = _optional(body.telefone)
                if tel_val and user.contato != tel_val:
                    user.contato = tel_val
                    mudou = True

            # senha
            if body.senha is not None:
                raw = body.senha.strip()
                if not raw:
                    raise HTTPException(status_code=400, detail="A nova senha não pode ser vazia.")
                user.password_hash = get_password_hash(raw)
                mudou = True

            # sempre role=3
            if mudou:
                user.role = 3

        # espelhar coletador no entregador
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


@router.post("/{id_entregador}/precos", response_model=PrecoIndividualItem)
def post_entregador_precos(
    id_entregador: int,
    body: PrecoEntregadorUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Cria ou atualiza exceção de preço para um entregador. Força usa_preco_global=False."""
    if body.shopee_valor is None and body.ml_valor is None and body.avulso_valor is None:
        raise HTTPException(status_code=422, detail="Envie ao menos um campo: shopee_valor, ml_valor ou avulso_valor.")
    sub_base_user = _resolve_user_base(db, current_user)
    ent = _get_owned_entregador(db, sub_base_user, id_entregador)
    ep = db.scalars(
        select(EntregadorPreco).where(EntregadorPreco.id_entregador == id_entregador)
    ).first()
    if ep:
        ep.usa_preco_global = False
        if body.shopee_valor is not None:
            ep.shopee_valor = body.shopee_valor
        if body.ml_valor is not None:
            ep.ml_valor = body.ml_valor
        if body.avulso_valor is not None:
            ep.avulso_valor = body.avulso_valor
    else:
        ep = EntregadorPreco(
            id_entregador=id_entregador,
            shopee_valor=body.shopee_valor,
            ml_valor=body.ml_valor,
            avulso_valor=body.avulso_valor,
            usa_preco_global=False,
        )
        db.add(ep)
    db.commit()
    db.refresh(ep)
    return PrecoIndividualItem(
        entregador_id=id_entregador,
        entregador_nome=ent.nome,
        shopee_valor=ep.shopee_valor,
        ml_valor=ep.ml_valor,
        avulso_valor=ep.avulso_valor,
    )


@router.delete("/{id_entregador}/precos", status_code=status.HTTP_204_NO_CONTENT)
def delete_entregador_precos(
    id_entregador: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Remove exceção de preço; entregador volta a usar valores globais."""
    sub_base_user = _resolve_user_base(db, current_user)
    _get_owned_entregador(db, sub_base_user, id_entregador)
    ep = db.scalars(
        select(EntregadorPreco).where(EntregadorPreco.id_entregador == id_entregador)
    ).first()
    if ep:
        db.delete(ep)
    db.commit()
    return


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
