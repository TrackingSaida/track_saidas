from __future__ import annotations
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, AliasChoices, field_validator
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, joinedload

from db import get_db
from name_normalizer import normalize_display_name, normalize_person_name, person_name_sort_key
from auth import get_current_user, get_password_hash, DEFAULT_PASSWORD
from conferencia_saida_pure import LABEL_CONFERENCIA_DIA, STATUS_CONFERIDA, status_conferencia_dia
from conferencia_saida_service import owner_conferencia_habilitada, status_conferencia_registrada_motoboys
from entregador_legado_sync import filtrar_entregadores_operacionais
from fechamento_resumo_utils import base_filtro_ativo, saida_pertence_base
from fechamento_valor_pure import (
    adicional_g,
    contagem_canais_agregada,
    valor_entregas_agregadas,
    valor_g_adicional,
    valor_saida,
)
from pacote_g_categorias import categoria_da_saida, padrao_id_ou_none, regras_por_categoria
from models import Entregador, EntregadorFechamento, EntregadorPreco, EntregadorPrecoGlobal, Motoboy, MotoboySubBase, Saida, User
from saida_operacional_utils import (
    agregar_saidas_fechamento_resumo,
    carregar_saidas_fechamento_leve,
    timestamp_operacional_marcos,
)

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


class PrecoExecutorOption(BaseModel):
    entregador_id: int
    nome: str
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

    @field_validator("nome", mode="after")
    @classmethod
    def _normalize_nome(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        from name_normalizer import normalize_display_name
        return normalize_display_name(v) or None


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
    g_por_categoria: Dict[str, int] = Field(default_factory=dict)  # nome da categoria -> quantidade
    valor_g: Decimal = Decimal("0.00")  # adicional de Pacote G já somado em valor_feitos
    fechamento_status: Optional[str] = None  # PENDENTE | GERADO | REAJUSTADO | PAGO
    id_fechamento: Optional[int] = None  # quando existe fechamento
    pode_reajustar: Optional[bool] = None  # True quando GERADO/REAJUSTADO e valor_base diverge
    alerta_pos_pago: Optional[bool] = None  # True quando PAGO e valor_base diverge (só alerta)
    valor_base_atual: Optional[Decimal] = None  # valor_base recalculado no período
    valor_base_fechado: Optional[Decimal] = None  # valor_base gravado no fechamento
    periodo_inicio: Optional[str] = None  # YYYY-MM-DD, quando existe fechamento
    periodo_fim: Optional[str] = None  # YYYY-MM-DD, quando existe fechamento
    conferencia_status: Optional[str] = None  # conferida | pendente | reconferir (só com conferência habilitada)
    conferencia_label: Optional[str] = None
    preco_origem: Optional[Dict[str, str]] = None  # {"flex"|"shopee": "motoboy"|"global"}


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


class ResumoExecutorItem(BaseModel):
    """Uma linha por motoboy/entregador no período (resumo?agrupar=executor)."""
    executor_tipo: str  # "m" | "e"
    executor_id: int
    entregador_id: Optional[int] = None
    motoboy_id: Optional[int] = None
    nome: str
    qtde_shopee: int = 0
    qtde_flex: int = 0
    qtde_avulso: int = 0
    total_feitos: int = 0
    total_cancelado: int = 0
    g_total: int = 0
    g_por_categoria: Dict[str, int] = Field(default_factory=dict)
    valor_entregas: Decimal = Decimal("0.00")
    valor_g: Decimal = Decimal("0.00")
    valor_cancelados: Decimal = Decimal("0.00")
    qtd_dias_coleta: int = 0
    valor_coletas: Decimal = Decimal("0.00")
    valor_base: Decimal = Decimal("0.00")
    fechamento_status: str = "PENDENTE"
    parcial: bool = False  # parte dos dias tem fechamento e parte não
    dias_pendentes: int = 0
    id_fechamento: Optional[int] = None
    valor_final: Optional[Decimal] = None
    pode_reajustar: bool = False
    alerta_pos_pago: bool = False
    valor_base_atual: Optional[Decimal] = None
    valor_base_fechado: Optional[Decimal] = None
    periodo_inicio: Optional[str] = None
    periodo_fim: Optional[str] = None
    dias_com_entrega: int = 0
    dias_sem_conferencia: int = 0  # pendente ou reconferir
    dias: List[EntregadorResumoItem] = Field(default_factory=list)


class ResumoExecutorTotais(BaseModel):
    pendentes_qtd: int = 0
    pendentes_valor: Decimal = Decimal("0.00")
    gerados_qtd: int = 0
    gerados_valor: Decimal = Decimal("0.00")
    pagos_qtd: int = 0
    pagos_valor: Decimal = Decimal("0.00")
    revisao_qtd: int = 0


class ResumoExecutorResponse(BaseModel):
    page: int
    pageSize: int
    totalPages: int
    totalItems: int
    agrupado_por: str = "executor"
    items: List[ResumoExecutorItem]
    totais: ResumoExecutorTotais
    sumValor: Decimal = Decimal("0.00")
    conferencia_habilitada: bool = False


class PrecoGlobalOut(BaseModel):
    shopee_valor: Decimal
    ml_valor: Decimal
    avulso_valor: Decimal
    coleta_valor: Decimal = Decimal("0.00")
    considerar_pacote_g_adicional: bool = False


class PrecoGlobalUpdate(BaseModel):
    shopee_valor: Optional[Decimal] = Field(None, ge=0)
    ml_valor: Optional[Decimal] = Field(None, ge=0)
    avulso_valor: Optional[Decimal] = Field(None, ge=0)
    coleta_valor: Optional[Decimal] = Field(None, ge=0)
    considerar_pacote_g_adicional: Optional[bool] = None


class PrecoIndividualItem(BaseModel):
    entregador_id: int
    entregador_nome: Optional[str] = None
    motoboy_id: Optional[int] = None
    motoboy_nome: Optional[str] = None
    shopee_valor: Optional[Decimal] = None
    ml_valor: Optional[Decimal] = None
    avulso_valor: Optional[Decimal] = None
    coleta_valor: Optional[Decimal] = None


class PrecoIndividuaisResponse(BaseModel):
    items: List[PrecoIndividualItem]


class PrecoEntregadorUpdate(BaseModel):
    motoboy_id: Optional[int] = Field(None, ge=1)
    shopee_valor: Optional[Decimal] = Field(None, ge=0)
    ml_valor: Optional[Decimal] = Field(None, ge=0)
    avulso_valor: Optional[Decimal] = Field(None, ge=0)
    coleta_valor: Optional[Decimal] = Field(None, ge=0)


_CAMPOS_PRECO_EXCECAO = ("shopee_valor", "ml_valor", "avulso_valor", "coleta_valor")


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


def usuario_financeiro(current_user=Depends(get_current_user)):
    """Fechamento de motoboys: somente admin/root, como no fechamento de bases."""
    try:
        role = int(getattr(current_user, "role", -1))
    except (TypeError, ValueError):
        role = -1
    if role not in (0, 1):
        raise HTTPException(status_code=403, detail="Apenas admin/root pode acessar o fechamento de motoboys.")
    return current_user


def usuario_admin_precos(current_user=Depends(get_current_user)):
    try:
        role = int(getattr(current_user, "role", -1))
    except (TypeError, ValueError):
        role = -1
    if role not in (0, 1):
        raise HTTPException(status_code=403, detail="Apenas admin/root pode alterar preços de entrega.")
    return current_user


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


ORIGEM_GLOBAL = {"flex": "global", "shopee": "global"}


def precos_e_origem_entregador(
    db: Session,
    id_entregador: int,
    sub_base: str,
) -> tuple[Dict[str, Decimal], Dict[str, str]]:
    """Preços do entregador e, para Flex e Shopee, se vieram da exceção ("motoboy") ou do global."""
    preco = db.scalars(
        select(EntregadorPreco).where(EntregadorPreco.id_entregador == id_entregador)
    ).first()
    precos = resolver_precos_entregador(db, id_entregador, sub_base, preco_row=preco)
    if preco and preco.usa_preco_global is False:
        return precos, {
            "flex": "motoboy" if preco.ml_valor is not None else "global",
            "shopee": "motoboy" if preco.shopee_valor is not None else "global",
        }
    return precos, dict(ORIGEM_GLOBAL)


def resolver_precos_entregador(
    db: Session,
    id_entregador: int,
    sub_base: str,
    *,
    preco_row: Optional[EntregadorPreco] = None,
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
    if preco_row is not None:
        preco = preco_row
    else:
        preco = db.scalars(
            select(EntregadorPreco).where(EntregadorPreco.id_entregador == id_entregador)
        ).first()

    global_row = db.scalars(
        select(EntregadorPrecoGlobal).where(EntregadorPrecoGlobal.sub_base == sub_base)
    ).first()

    shopee_global = global_row.shopee_valor if global_row else zero
    ml_global = global_row.ml_valor if global_row else zero
    avulso_global = global_row.avulso_valor if global_row else zero
    coleta_global = global_row.coleta_valor if global_row else zero

    if preco and preco.usa_preco_global is False:
        shopee_valor = preco.shopee_valor if preco.shopee_valor is not None else shopee_global
        ml_valor = preco.ml_valor if preco.ml_valor is not None else ml_global
        avulso_valor = preco.avulso_valor if preco.avulso_valor is not None else avulso_global
        coleta_valor = preco.coleta_valor if preco.coleta_valor is not None else coleta_global
        return {"shopee_valor": shopee_valor, "ml_valor": ml_valor, "avulso_valor": avulso_valor, "coleta_valor": coleta_valor}

    if global_row:
        return {"shopee_valor": shopee_global, "ml_valor": ml_global, "avulso_valor": avulso_global, "coleta_valor": coleta_global}
    return {"shopee_valor": zero, "ml_valor": zero, "avulso_valor": zero, "coleta_valor": zero}


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


def _resolver_entregador_principal_do_motoboy_robusto(
    db: Session,
    sub_base: str,
    motoboy_id: int,
) -> Optional[int]:
    """
    Resolve entregador principal para um motoboy com fallback robusto:
    1) vínculo por username/documento (_resolve_executor_scope_ids);
    2) match por nome normalizado (User.nome+sobrenome vs Entregador.nome).
    """
    entregador_id = _resolver_entregador_principal_do_motoboy(
        db=db,
        sub_base=sub_base,
        motoboy_id=motoboy_id,
    )
    if entregador_id is not None:
        return entregador_id

    motoboy = db.get(Motoboy, motoboy_id)
    if not motoboy:
        return None

    pertence_sub_base = bool((motoboy.sub_base or "").strip() == (sub_base or "").strip())
    if not pertence_sub_base:
        pertence_sub_base = (
            db.scalar(
                select(func.count())
                .select_from(MotoboySubBase)
                .where(
                    MotoboySubBase.motoboy_id == motoboy_id,
                    MotoboySubBase.sub_base == sub_base,
                    MotoboySubBase.ativo.is_(True),
                )
            )
            or 0
        ) > 0
    if not pertence_sub_base:
        return None

    u = db.get(User, motoboy.user_id) if motoboy.user_id else None
    nome_user = (f"{(u.nome or '').strip()} {(u.sobrenome or '').strip()}".strip() if u else "")
    nome_user_norm = " ".join(nome_user.lower().split()) if nome_user else ""
    if not nome_user_norm:
        return None

    entregadores = db.scalars(
        select(Entregador).where(
            Entregador.sub_base == sub_base,
            Entregador.ativo.is_(True),
        )
    ).all()
    for ent in entregadores:
        nome_ent = (ent.nome or "").strip()
        if not nome_ent:
            continue
        nome_ent_norm = " ".join(nome_ent.lower().split())
        if nome_ent_norm == nome_user_norm:
            return int(ent.id_entregador)

    return None


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
    return precos_e_origem_motoboy(db, sub_base, motoboy_id)[0]


def precos_e_origem_motoboy(
    db: Session,
    sub_base: str,
    motoboy_id: Optional[int] = None,
) -> tuple[Dict[str, Decimal], Dict[str, str]]:
    """Mesmo preço de resolver_precos_motoboy, com a origem de Flex e Shopee."""
    zero = Decimal("0.00")
    if motoboy_id is not None:
        entregador_id = _resolver_entregador_principal_do_motoboy_robusto(
            db=db,
            sub_base=sub_base,
            motoboy_id=motoboy_id,
        )
        if entregador_id is not None:
            return precos_e_origem_entregador(db, entregador_id, sub_base)

    return _precos_globais(db, sub_base, zero), dict(ORIGEM_GLOBAL)


def _precos_globais(db: Session, sub_base: str, zero: Decimal) -> Dict[str, Decimal]:
    global_row = db.scalars(
        select(EntregadorPrecoGlobal).where(EntregadorPrecoGlobal.sub_base == sub_base)
    ).first()
    if global_row:
        return {
            "shopee_valor": global_row.shopee_valor or zero,
            "ml_valor": global_row.ml_valor or zero,
            "avulso_valor": global_row.avulso_valor or zero,
            "coleta_valor": global_row.coleta_valor or zero,
        }
    return {"shopee_valor": zero, "ml_valor": zero, "avulso_valor": zero, "coleta_valor": zero}


def _toggle_pacote_g_ativo(db: Session, sub_base: str) -> bool:
    row = db.scalars(
        select(EntregadorPrecoGlobal).where(EntregadorPrecoGlobal.sub_base == sub_base)
    ).first()
    return bool(getattr(row, "considerar_pacote_g_adicional", False)) if row else False


def _somar_g_por_categoria(mapas) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for m in mapas:
        for nome, qtd in (m or {}).items():
            out[nome] = out.get(nome, 0) + int(qtd or 0)
    return out


def _regras_g_motoboy(db: Session, sub_base: str) -> tuple[Dict[int, Dict[str, Any]], Optional[int]]:
    """Regras "Pagar ao motoboy" por categoria e a categoria padrão (pacote G legado sem categoria)."""
    return regras_por_categoria(db, sub_base, "motoboy"), padrao_id_ou_none(db, sub_base)


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
    nome        = normalize_person_name((body.nome or "").strip()) or ""
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
    status: Optional[str] = Query("ativo", description="Filtrar por status: ativo, inativo ou todos"),
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

    rows = list(db.scalars(stmt).all())
    # Em listagens operacionais (ativo), omite espelho de usuário excluído/inativo.
    if status == "ativo":
        rows = filtrar_entregadores_operacionais(db, rows)
    rows.sort(key=lambda e: (e.nome or "").casefold())
    return rows


@router.get("/executores", response_model=List[ExecutorItem])
def list_executores(
    status: Optional[str] = Query("ativo", description="Para entregadores: ativo, inativo ou todos"),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Lista executores (entregadores + motoboys) da sub_base para dropdown de Fechamento de Motoboys."""
    sub_base_user = _resolve_user_base(db, current_user)
    out: List[ExecutorItem] = []
    nomes_motoboy_keys: set[str] = set()

    # Motoboys vinculados à sub_base (somente User ativo)
    stmt_mb = (
        select(Motoboy)
        .join(User, User.id == Motoboy.user_id)
        .join(MotoboySubBase, MotoboySubBase.motoboy_id == Motoboy.id_motoboy)
        .where(
            MotoboySubBase.sub_base == sub_base_user,
            MotoboySubBase.ativo.is_(True),
            Motoboy.ativo.is_(True),
            User.status.is_(True),
            User.role == 4,
        )
        .order_by(Motoboy.id_motoboy)
    )
    if status == "inativo":
        motoboys = []
    else:
        motoboys = db.scalars(stmt_mb).all()
    nomes_motoboy = _carregar_nomes_motoboy_ids(db, [m.id_motoboy for m in motoboys])
    for motoboy in motoboys:
        nome = nomes_motoboy.get(int(motoboy.id_motoboy), f"Motoboy {motoboy.id_motoboy}")
        nomes_motoboy_keys.add(person_name_sort_key(nome))
        out.append(ExecutorItem(
            id_entregador=None,
            id_motoboy=motoboy.id_motoboy,
            nome=nome,
            tipo="motoboy",
            executor_tipo="m",
            executor_id=motoboy.id_motoboy,
            executor_key=f"m_{motoboy.id_motoboy}",
        ))

    # Entregadores legado: só entram se tiverem titular operacional ativo
    # e não duplicarem um motoboy já listado (mesmo nome).
    stmt_ent = select(Entregador).where(Entregador.sub_base == sub_base_user)
    if status == "ativo":
        stmt_ent = stmt_ent.where(Entregador.ativo.is_(True))
    elif status == "inativo":
        stmt_ent = stmt_ent.where(Entregador.ativo.is_(False))
    stmt_ent = stmt_ent.order_by(Entregador.nome)
    entregadores = list(db.scalars(stmt_ent).all())
    if status == "ativo":
        entregadores = filtrar_entregadores_operacionais(db, entregadores)
    for ent in entregadores:
        nome = normalize_display_name(
            (ent.nome or f"Entregador {ent.id_entregador}").strip(),
            fallback=f"Entregador {ent.id_entregador}",
        )
        if status == "ativo" and person_name_sort_key(nome) in nomes_motoboy_keys:
            continue
        out.append(ExecutorItem(
            id_entregador=ent.id_entregador,
            id_motoboy=None,
            nome=nome,
            tipo="entregador",
            executor_tipo="e",
            executor_id=ent.id_entregador,
            executor_key=f"e_{ent.id_entregador}",
        ))

    out.sort(key=lambda x: (x.nome or "").casefold())
    return out


def _normalizar_servico(servico: Optional[str]) -> str:
    s = (servico or "").lower()
    if "shopee" in s:
        return "shopee"
    if "ml" in s or "mercado" in s:
        return "flex"
    return "avulso"


def _carregar_nomes_motoboy_ids(db: Session, motoboy_ids: List[int]) -> Dict[int, str]:
    """Resolve nomes de motoboy em lote para evitar N+1."""
    from motoboy_nome_utils import carregar_nomes_motoboy_ids
    return carregar_nomes_motoboy_ids(db, motoboy_ids)


def _get_motoboy_nome(db: Session, motoboy_id: int) -> str:
    """Nome do motoboy (User) para exibição no resumo."""
    from motoboy_nome_utils import get_motoboy_display_name
    return get_motoboy_display_name(db, motoboy_id)


def _carregar_motoboys_ativos_da_subbase(db: Session, sub_base_user: str) -> List[Motoboy]:
    stmt = (
        select(Motoboy)
        .join(User, User.id == Motoboy.user_id)
        .outerjoin(MotoboySubBase, MotoboySubBase.motoboy_id == Motoboy.id_motoboy)
        .where(
            User.role == 4,
            User.status.is_(True),
            Motoboy.ativo.is_(True),
            or_(
                Motoboy.sub_base == sub_base_user,
                and_(
                    MotoboySubBase.sub_base == sub_base_user,
                    MotoboySubBase.ativo.is_(True),
                ),
            ),
        )
        .order_by(Motoboy.id_motoboy.asc())
    )
    motoboys = db.scalars(stmt).all()
    # dedupe por id_motoboy (join com subbase pode repetir)
    seen = set()
    out: List[Motoboy] = []
    for m in motoboys:
        mid = int(m.id_motoboy)
        if mid in seen:
            continue
        seen.add(mid)
        out.append(m)
    return out


def _preco_item_from_motoboy(
    db: Session,
    sub_base_user: str,
    motoboy: Motoboy,
    ep: Optional[EntregadorPreco],
) -> PrecoIndividualItem:
    mid = int(motoboy.id_motoboy)
    nome_mb = _carregar_nomes_motoboy_ids(db, [mid]).get(mid, f"Motoboy {mid}")
    entregador_id = _resolver_entregador_principal_do_motoboy_robusto(
        db=db,
        sub_base=sub_base_user,
        motoboy_id=mid,
    )
    entregador_nome: Optional[str] = None
    if entregador_id is not None:
        ent = db.get(Entregador, int(entregador_id))
        if ent and ent.sub_base == sub_base_user:
            from name_normalizer import normalize_display_name
            entregador_nome = normalize_display_name((ent.nome or "").strip()) or None

    return PrecoIndividualItem(
        entregador_id=int(entregador_id or 0),
        entregador_nome=entregador_nome,
        motoboy_id=mid,
        motoboy_nome=nome_mb,
        shopee_valor=(ep.shopee_valor if ep else None),
        ml_valor=(ep.ml_valor if ep else None),
        avulso_valor=(ep.avulso_valor if ep else None),
        coleta_valor=(ep.coleta_valor if ep else None),
    )


def _obter_ou_criar_entregador_legado_para_motoboy(
    db: Session,
    sub_base_user: str,
    id_motoboy: int,
) -> int:
    """
    Garante um entregador legado para persistir exceção de preço por motoboy.
    Regra:
    1) tenta resolver vínculo existente;
    2) se não existir, cria entregador legado automaticamente.
    """
    existente = _resolver_entregador_principal_do_motoboy_robusto(
        db=db,
        sub_base=sub_base_user,
        motoboy_id=int(id_motoboy),
    )
    if existente is not None:
        return int(existente)

    motoboy = db.get(Motoboy, int(id_motoboy))
    if not motoboy:
        raise HTTPException(status_code=404, detail="Motoboy não encontrado.")

    user = db.get(User, motoboy.user_id) if motoboy.user_id else None
    username_ref = ((getattr(user, "username", None) or f"motoboy.{id_motoboy}").strip())
    nome_ref = (
        f"{(getattr(user, 'nome', None) or '').strip()} {(getattr(user, 'sobrenome', None) or '').strip()}".strip()
        or username_ref
        or f"Motoboy {id_motoboy}"
    )
    documento_ref = (motoboy.documento or "").strip() or None

    by_username = db.scalars(
        select(Entregador).where(
            Entregador.sub_base == sub_base_user,
            Entregador.username_entregador == username_ref,
        )
    ).first()
    if by_username:
        return int(by_username.id_entregador)

    if documento_ref:
        by_documento = db.scalars(
            select(Entregador).where(
                Entregador.sub_base == sub_base_user,
                Entregador.documento == documento_ref,
            )
        ).first()
        if by_documento:
            return int(by_documento.id_entregador)

    novo_entregador = Entregador(
        sub_base=sub_base_user,
        nome=nome_ref,
        telefone=((getattr(user, "contato", None) or "").strip() or "Não informado"),
        documento=documento_ref,
        ativo=True,
        rua=(motoboy.rua or "").strip() or "Não informado",
        numero=(motoboy.numero or "").strip() or "S/N",
        complemento=(motoboy.complemento or "").strip(),
        cep=(motoboy.cep or "").strip() or "00000000",
        cidade=(motoboy.cidade or "").strip() or "Não informado",
        bairro=(motoboy.bairro or "").strip() or "Não informado",
        coletador=False,
        username_entregador=username_ref,
    )
    db.add(novo_entregador)
    db.flush()
    return int(novo_entregador.id_entregador)


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
    "encerrado",
    "encerrado_sistema",
    "encerrado sistema",
    "encerrado pelo sistema",
    "encerrado_pelo_sistema",
]


def _calcular_valor_base_periodo(
    db: Session,
    sub_base_user: str,
    entregador_id: int,
    periodo_inicio: date,
    periodo_fim: date,
    contagem: Optional[Dict[str, int]] = None,
) -> Decimal:
    """Calcula valor_base por escopo equivalente do entregador (legacy + motoboy vinculado).

    contagem: quando informado ({"flex": 0, "shopee": 0}), recebe as saídas pagas de cada canal.
    """
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

    rows, _ = carregar_saidas_fechamento_leve(
        db,
        sub_base=sub_base_user,
        status_validos=STATUS_VALOR_BASE_VALIDOS,
        periodo_inicio=periodo_inicio,
        periodo_fim=periodo_fim,
        motoboy_ids=sorted(motoboy_ids) if motoboy_ids else None,
        entregador_ids=sorted(entregador_ids) if entregador_ids else None,
        exigir_executor=False,
    )
    precos = resolver_precos_entregador(db, entregador_id, sub_base_user)
    toggle_pacote_g = _toggle_pacote_g_ativo(db, sub_base_user)
    regras_g, padrao_g = _regras_g_motoboy(db, sub_base_user)
    total = Decimal("0.00")
    for saida in rows:
        total += valor_saida(
            precos, saida.servico, saida.status, bool(getattr(saida, "is_grande", False)), toggle_pacote_g,
            regras_g.get(categoria_da_saida(saida, padrao_g)),
        )
        if contagem is not None and "cancel" not in (saida.status or "").strip().lower():
            tipo = _normalizar_servico(saida.servico)
            if tipo in contagem:
                contagem[tipo] += 1
    return total.quantize(Decimal("0.01"))


def _calcular_valor_base_motoboy_periodo(
    db: Session,
    sub_base_user: str,
    motoboy_id: int,
    periodo_inicio: date,
    periodo_fim: date,
    contagem: Optional[Dict[str, int]] = None,
) -> Decimal:
    """Calcula o valor_base a partir das saídas do motoboy no período (resumo/fechamento).

    contagem: quando informado ({"flex": 0, "shopee": 0}), recebe as saídas pagas de cada canal.
    """
    por_motoboy: Optional[Dict[int, Dict[str, int]]] = {} if contagem is not None else None
    valor = _calcular_valores_base_motoboys_periodo(
        db, sub_base_user, [motoboy_id], periodo_inicio, periodo_fim, contagem=por_motoboy,
    ).get(int(motoboy_id), Decimal("0.00"))
    if contagem is not None:
        for tipo, qtd in (por_motoboy or {}).get(int(motoboy_id), {}).items():
            if tipo in contagem:
                contagem[tipo] += qtd
    return valor


def precos_motoboy_cache(
    db: Session,
    sub_base: str,
    motoboy_id: int,
    cache: Optional[Dict[int, Dict[str, Decimal]]],
    origens: Optional[Dict[int, Dict[str, str]]] = None,
) -> Dict[str, Decimal]:
    """origens: quando informado, também guarda de onde veio o preço de Flex e Shopee."""
    mid = int(motoboy_id)
    if origens is None:
        if cache is None:
            return resolver_precos_motoboy(db, sub_base, motoboy_id=mid)
        if mid not in cache:
            cache[mid] = resolver_precos_motoboy(db, sub_base, motoboy_id=mid)
        return cache[mid]
    if cache is None or mid not in cache or mid not in origens:
        precos, origens[mid] = precos_e_origem_motoboy(db, sub_base, motoboy_id=mid)
        if cache is None:
            return precos
        cache[mid] = precos
    return cache[mid]


def _valores_base_motoboys_agregados(
    db: Session,
    sub_base_user: str,
    ids: List[int],
    periodo_inicio: date,
    periodo_fim: date,
    *,
    precos_cache: Optional[Dict[int, Dict[str, Decimal]]],
    contagem: Optional[Dict[int, Dict[str, int]]],
) -> Dict[int, Decimal]:
    """Soma entregas pela agregação do período, só dos motoboys pedidos nesta sub_base."""
    zero = Decimal("0.00")
    agregados = agregar_saidas_fechamento_resumo(
        db,
        sub_base=sub_base_user,
        status_validos=STATUS_VALOR_BASE_VALIDOS,
        periodo_inicio=periodo_inicio,
        periodo_fim=periodo_fim,
        motoboy_ids=ids,
        exigir_executor=False,
    )
    pedidos = set(ids)
    if contagem is not None:
        for mid, canais in contagem_canais_agregada(agregados).items():
            if mid in pedidos:
                contagem[mid] = canais
    precos = precos_cache if precos_cache is not None else {}
    presentes = {
        int(getattr(agg, "motoboy_id"))
        for agg in agregados
        if getattr(agg, "motoboy_id", None) is not None and not getattr(agg, "is_cancelado", False)
    }
    for mid in presentes:
        if mid in pedidos:
            precos_motoboy_cache(db, sub_base_user, mid, precos)
    toggle_pacote_g = _toggle_pacote_g_ativo(db, sub_base_user)
    regras_g, padrao_g = _regras_g_motoboy(db, sub_base_user)
    valores = valor_entregas_agregadas(
        agregados,
        precos,
        toggle_pacote_g=toggle_pacote_g,
        regras_g=regras_g,
        padrao_g=padrao_g,
    )
    return {mid: valores.get(mid, zero).quantize(Decimal("0.01")) for mid in ids}


def _calcular_valores_base_motoboys_periodo(
    db: Session,
    sub_base_user: str,
    motoboy_ids: Iterable[int],
    periodo_inicio: date,
    periodo_fim: date,
    *,
    precos_cache: Optional[Dict[int, Dict[str, Decimal]]] = None,
    saidas_periodo: Optional[List[Saida]] = None,
    contagem: Optional[Dict[int, Dict[str, int]]] = None,
) -> Dict[int, Decimal]:
    """Valor das entregas de vários motoboys no mesmo período.

    Sem saídas pré-carregadas, soma a agregação SQL do período (mesmo caminho da lista).
    saidas_periodo: saídas já filtradas; evita nova consulta quando o chamador já as tem.
    contagem: quando informado, recebe {motoboy_id: {"flex": n, "shopee": n}} das saídas pagas.
    """
    ids = sorted({int(m) for m in motoboy_ids if m is not None})
    zero = Decimal("0.00")
    if not ids:
        return {}
    if periodo_inicio > periodo_fim:
        return {mid: zero for mid in ids}
    if saidas_periodo is None:
        return _valores_base_motoboys_agregados(
            db,
            sub_base_user,
            ids,
            periodo_inicio,
            periodo_fim,
            precos_cache=precos_cache,
            contagem=contagem,
        )
    toggle_pacote_g = _toggle_pacote_g_ativo(db, sub_base_user)
    regras_g, padrao_g = _regras_g_motoboy(db, sub_base_user)
    totais = {mid: zero for mid in ids}
    for saida in saidas_periodo:
        mid = getattr(saida, "motoboy_id", None)
        if mid is None or int(mid) not in totais:
            continue
        mid = int(mid)
        status_norm = (saida.status or "").strip().lower()
        # Cancelado não paga e não gera multa (contribui R$ 0,00), inclusive Pacote G.
        if "cancel" in status_norm:
            continue
        precos = precos_motoboy_cache(db, sub_base_user, mid, precos_cache)
        tipo = _normalizar_servico(saida.servico)
        if contagem is not None and tipo in ("flex", "shopee"):
            contagem.setdefault(mid, {"flex": 0, "shopee": 0})[tipo] += 1
        if tipo == "shopee":
            delta = precos["shopee_valor"]
        elif tipo == "flex":
            delta = precos["ml_valor"]
        else:
            delta = precos["avulso_valor"]
        total = totais[mid] + delta
        if toggle_pacote_g and bool(getattr(saida, "is_grande", False)):
            total += adicional_g(Decimal(str(delta or 0)), regras_g.get(categoria_da_saida(saida, padrao_g)))
        totais[mid] = total
    return {mid: v.quantize(Decimal("0.01")) for mid, v in totais.items()}


def valores_base_atuais_motoboys(
    db: Session,
    sub_base: str,
    motoboy_ids: Iterable[int],
    periodo_inicio: date,
    periodo_fim: date,
    *,
    precos_cache: Optional[Dict[int, Dict[str, Decimal]]] = None,
    saidas_periodo: Optional[List[Saida]] = None,
) -> Dict[int, Decimal]:
    """Valor base recalculado agora (entregas + diárias de coleta) para vários motoboys do mesmo período."""
    from entregador_fechamento_routes import calcular_diarias_coleta_motoboys

    if precos_cache is None:
        precos_cache = {}
    entregas = _calcular_valores_base_motoboys_periodo(
        db, sub_base, motoboy_ids, periodo_inicio, periodo_fim,
        precos_cache=precos_cache, saidas_periodo=saidas_periodo,
    )
    coletas = calcular_diarias_coleta_motoboys(
        db, sub_base, list(entregas), periodo_inicio, periodo_fim, precos_cache=precos_cache
    )
    return {
        mid: (valor + coletas[mid][1]).quantize(Decimal("0.01"))
        for mid, valor in entregas.items()
    }


_ORDEM_STATUS_LINHA = {"GERADO": 0, "REAJUSTADO": 1, "PAGO": 2}


def _status_fech(fech: EntregadorFechamento) -> str:
    st = (fech.status or "").upper()
    return "GERADO" if st in ("", "FECHADO") else st


def _iso_ou_none(d) -> Optional[str]:
    if d is None:
        return None
    return d.isoformat() if hasattr(d, "isoformat") else str(d)


def _motoboys_com_diarias_coleta(db: Session, sub_base: str, ini: date, fim: date) -> List[int]:
    from models import ColetaExecucao, ColetaExecucaoParticipante

    ids = db.scalars(
        select(ColetaExecucaoParticipante.motoboy_id)
        .join(ColetaExecucao, ColetaExecucaoParticipante.execucao_id == ColetaExecucao.id_execucao)
        .where(
            ColetaExecucao.sub_base == sub_base,
            ColetaExecucaoParticipante.sub_base == sub_base,
            ColetaExecucaoParticipante.status == "finalizado",
            ColetaExecucaoParticipante.motoboy_id.isnot(None),
            ColetaExecucao.data_operacao >= ini,
            ColetaExecucao.data_operacao <= fim,
        )
        .distinct()
    ).all()
    return sorted(int(i) for i in ids)


def _diarias_coleta_em_aberto_motoboys(
    db: Session,
    sub_base: str,
    motoboy_ids: List[int],
    ini: date,
    fim: date,
    *,
    precos_cache: Optional[Dict[int, Dict[str, Decimal]]] = None,
) -> Dict[int, tuple]:
    """Diárias de coleta do período que ainda não pertencem a nenhum fechamento, por motoboy."""
    from entregador_fechamento_routes import calcular_diarias_coleta_motoboys
    from models import EntregadorFechamentoColetaItem

    por_motoboy = {
        mid: itens
        for mid, (_, _, itens) in calcular_diarias_coleta_motoboys(
            db, sub_base, motoboy_ids, ini, fim, precos_cache=precos_cache
        ).items()
        if itens
    }
    if not por_motoboy:
        return {}
    pagos = {
        (int(motoboy_id), data)
        for motoboy_id, data in db.execute(
            select(EntregadorFechamentoColetaItem.motoboy_id, EntregadorFechamentoColetaItem.data).where(
                EntregadorFechamentoColetaItem.sub_base == sub_base,
                EntregadorFechamentoColetaItem.motoboy_id.in_(sorted(por_motoboy)),
                EntregadorFechamentoColetaItem.data >= ini,
                EntregadorFechamentoColetaItem.data <= fim,
            )
        ).all()
    }
    out: Dict[int, tuple] = {}
    for mid, itens in por_motoboy.items():
        abertos = [i for i in itens if (mid, i["data"]) not in pagos]
        valor = sum((Decimal(str(i["valor_diaria"] or 0)) for i in abertos), Decimal("0.00"))
        out[mid] = (len(abertos), valor.quantize(Decimal("0.01")))
    return out


def _valor_base_atual_fechamento(db: Session, sub_base: str, tipo: str, xid: int, fech: EntregadorFechamento) -> Decimal:
    """Valor base recalculado agora para o período do fechamento (entregas + diárias de coleta)."""
    if tipo == "m":
        from entregador_fechamento_routes import _calcular_diarias_coleta_motoboy

        entregas = _calcular_valor_base_motoboy_periodo(db, sub_base, xid, fech.periodo_inicio, fech.periodo_fim)
        _, coletas, _ = _calcular_diarias_coleta_motoboy(db, sub_base, xid, fech.periodo_inicio, fech.periodo_fim)
        return (entregas + coletas).quantize(Decimal("0.01"))
    return Decimal(str(_calcular_valor_base_periodo(db, sub_base, xid, fech.periodo_inicio, fech.periodo_fim))).quantize(
        Decimal("0.01")
    )


def _juntar_legado_ao_motoboy(
    db: Session,
    sub_base: str,
    linhas: Dict[tuple, Dict[str, Any]],
    nomes_motoboy: Dict[int, str],
    *,
    fechs_por_executor: Optional[Dict[tuple, List[EntregadorFechamento]]] = None,
) -> None:
    """Soma o entregador antigo na linha do motoboy da mesma pessoa.

    O vínculo é usuário/documento. Se não houver, o nome exibido igual ao do
    motoboy já listado também junta. Linha antiga que já tem fechamento fica separada.
    """
    motoboy_ids = {xid for (tipo, xid) in linhas if tipo == "m"}
    if not motoboy_ids:
        return

    def _nome_motoboy(xid: int) -> str:
        ln = linhas.get(("m", xid)) or {}
        return (ln.get("nome") or nomes_motoboy.get(xid) or "").strip()

    nome_para_motoboy: Dict[str, int] = {}
    nomes_ambiguos: set[str] = set()
    for mid in motoboy_ids:
        chave = person_name_sort_key(_nome_motoboy(mid))
        if not chave:
            continue
        if chave in nome_para_motoboy and nome_para_motoboy[chave] != mid:
            nomes_ambiguos.add(chave)
        else:
            nome_para_motoboy[chave] = mid

    fechs_map = fechs_por_executor or {}

    def _fechs(mid: int) -> list:
        return list(fechs_map.get(("m", mid)) or [])

    remover: List[tuple] = []
    for (tipo, xid), ln in list(linhas.items()):
        if tipo != "e":
            continue
        dias = ln.get("dias") or []
        if any(getattr(d, "id_fechamento", None) for d in dias):
            continue
        comuns: set[int] = set()
        if xid > 0:
            _, mids = _resolve_executor_scope_ids(db, sub_base, entregador_id=xid)
            comuns = {int(m) for m in mids if int(m) in motoboy_ids}
        destino = next(iter(comuns)) if len(comuns) == 1 else None
        if destino is None:
            chave = person_name_sort_key((ln.get("nome") or "").strip())
            if chave and chave not in nomes_ambiguos:
                destino = nome_para_motoboy.get(chave)
        if destino is None:
            continue
        alvo = linhas[("m", destino)]
        for dia in dias:
            dia.motoboy_id = destino
            try:
                data_ref = date.fromisoformat(str(dia.data)[:10])
            except ValueError:
                data_ref = None
            fech = None
            if data_ref is not None:
                fech = next(
                    (
                        f for f in _fechs(destino)
                        if f.periodo_inicio <= data_ref <= f.periodo_fim
                    ),
                    None,
                )
            if fech is not None:
                st = (fech.status or "").strip().upper()
                dia.fechamento_status = "GERADO" if st in ("", "FECHADO") else st
                dia.id_fechamento = fech.id_fechamento
                dia.periodo_inicio = fech.periodo_inicio.isoformat()
                dia.periodo_fim = fech.periodo_fim.isoformat()
            alvo["dias"].append(dia)
        if not (alvo.get("nome") or "").strip() and (ln.get("nome") or "").strip():
            alvo["nome"] = ln["nome"]
        remover.append(("e", xid))
    for chave in remover:
        linhas.pop(chave, None)


def _resumo_agrupado_por_executor(
    db: Session,
    sub_base_user: str,
    lista: List[EntregadorResumoItem],
    *,
    data_inicio: date,
    data_fim: date,
    fechamento_status: Optional[str],
    filtro_motoboy_id: Optional[int],
    filtro_entregador_id: Optional[int],
    page: int,
    pageSize: int,
    cache_valor_base: Optional[Dict[tuple, Decimal]] = None,
    precos_motoboy: Optional[Dict[int, Dict[str, Decimal]]] = None,
    conferencia_habilitada: bool = False,
    fechs_por_executor: Optional[Dict[tuple, List[EntregadorFechamento]]] = None,
    fechs_por_id: Optional[Dict[int, EntregadorFechamento]] = None,
) -> ResumoExecutorResponse:
    zero = Decimal("0.00")
    cache_valor_base = cache_valor_base if cache_valor_base is not None else {}
    precos_motoboy = precos_motoboy if precos_motoboy is not None else {}
    fechs_por_executor = fechs_por_executor if fechs_por_executor is not None else {}
    fechs_por_id = fechs_por_id if fechs_por_id is not None else {}
    linhas: Dict[tuple, Dict[str, Any]] = {}
    for it in lista:
        if it.motoboy_id is not None:
            chave = ("m", int(it.motoboy_id))
        elif it.entregador_id is not None and it.entregador_id > 0:
            chave = ("e", int(it.entregador_id))
        else:
            continue
        linhas.setdefault(chave, {"nome": it.entregador_nome, "dias": []})["dias"].append(it)

    if filtro_entregador_id is None:
        for mid in _motoboys_com_diarias_coleta(db, sub_base_user, data_inicio, data_fim):
            if filtro_motoboy_id is not None and mid != int(filtro_motoboy_id):
                continue
            linhas.setdefault(("m", mid), {"nome": None, "dias": []})
    sem_nome = [xid for (tipo, xid), ln in linhas.items() if tipo == "m" and not ln["nome"]]
    nomes = _carregar_nomes_motoboy_ids(db, sem_nome) if sem_nome else {}
    _juntar_legado_ao_motoboy(
        db, sub_base_user, linhas, nomes, fechs_por_executor=fechs_por_executor,
    )
    coletas_abertas = _diarias_coleta_em_aberto_motoboys(
        db, sub_base_user, [xid for (tipo, xid) in linhas if tipo == "m"], data_inicio, data_fim,
        precos_cache=precos_motoboy,
    )
    conferencia_registrada = (
        status_conferencia_registrada_motoboys(
            db,
            sub_base=sub_base_user,
            motoboy_ids=[xid for (tipo, xid) in linhas if tipo == "m"],
            periodo_inicio=data_inicio,
            periodo_fim=data_fim,
        )
        if conferencia_habilitada
        else {}
    )

    def _aplicar_conferencia(xid: int, dias: List[EntregadorResumoItem]) -> tuple:
        com_entrega = sem_conferencia = 0
        for d in dias:
            if d.total_feitos <= 0:
                continue
            st = status_conferencia_dia(conferencia_registrada.get((xid, date.fromisoformat(d.data))))
            d.conferencia_status = st
            d.conferencia_label = LABEL_CONFERENCIA_DIA[st]
            com_entrega += 1
            if st != STATUS_CONFERIDA:
                sem_conferencia += 1
        return com_entrega, sem_conferencia

    def _valor_base_atual(tipo: str, xid: int, fech: EntregadorFechamento) -> Decimal:
        chave = ("m", xid, fech.periodo_inicio, fech.periodo_fim) if tipo == "m" else (xid, fech.periodo_inicio, fech.periodo_fim)
        if chave not in cache_valor_base:
            cache_valor_base[chave] = _valor_base_atual_fechamento(db, sub_base_user, tipo, xid, fech)
        return Decimal(str(cache_valor_base[chave])).quantize(Decimal("0.01"))

    rows: List[ResumoExecutorItem] = []
    for (tipo, xid), ln in linhas.items():
        dias: List[EntregadorResumoItem] = sorted(ln["dias"], key=lambda d: d.data)
        status_dias = {(d.fechamento_status or "PENDENTE").upper() for d in dias}
        fechs = []
        for fid in {d.id_fechamento for d in dias if d.id_fechamento}:
            f = fechs_por_id.get(int(fid))
            if f is not None and f.sub_base == sub_base_user:
                fechs.append(f)
        if not dias and tipo == "m":
            fechs = [
                f
                for f in (fechs_por_executor.get(("m", xid)) or [])
                if f.periodo_inicio <= data_fim and f.periodo_fim >= data_inicio
            ]
        fechs.sort(key=lambda f: (_ORDEM_STATUS_LINHA.get(_status_fech(f), 9), f.periodo_inicio))
        fech = fechs[0] if fechs else None
        pendente = "PENDENTE" in status_dias or fech is None
        status_linha = "PENDENTE" if pendente else _status_fech(fech)

        qtd_col, val_col = 0, zero
        if tipo == "m":
            if pendente:
                qtd_col, val_col = coletas_abertas.get(xid, (0, zero))
            elif fech is not None:
                qtd_col = int(getattr(fech, "qtd_dias_coleta", 0) or 0)
                val_col = Decimal(str(getattr(fech, "valor_coletas", 0) or 0))
        if not dias and pendente and qtd_col == 0:
            continue

        dias_com_entrega, dias_sem_conferencia = (
            _aplicar_conferencia(xid, dias) if conferencia_habilitada and tipo == "m" else (0, 0)
        )
        valor_entregas = sum((d.valor_feitos for d in dias), zero)
        valor_base_atual = None
        pode_reajustar = alerta_pos_pago = False
        if fech is not None and not pendente:
            valor_base_atual = _valor_base_atual(tipo, xid, fech)
            if valor_base_atual != Decimal(str(fech.valor_base or 0)).quantize(Decimal("0.01")):
                pode_reajustar = status_linha in ("GERADO", "REAJUSTADO")
                alerta_pos_pago = status_linha == "PAGO"
        rows.append(
            ResumoExecutorItem(
                executor_tipo=tipo,
                executor_id=xid,
                motoboy_id=xid if tipo == "m" else None,
                entregador_id=xid if tipo == "e" else None,
                nome=ln["nome"] or nomes.get(xid) or f"Motoboy {xid}",
                qtde_shopee=sum(int(d.shopee.get("qtde") or 0) for d in dias),
                qtde_flex=sum(int(d.flex.get("qtde") or 0) for d in dias),
                qtde_avulso=sum(int(d.avulso.get("qtde") or 0) for d in dias),
                total_feitos=sum(d.total_feitos for d in dias),
                total_cancelado=sum(d.total_cancelado for d in dias),
                g_total=sum(d.g_total for d in dias),
                g_por_categoria=_somar_g_por_categoria(d.g_por_categoria for d in dias),
                valor_entregas=valor_entregas.quantize(Decimal("0.01")),
                valor_g=sum((d.valor_g for d in dias), zero).quantize(Decimal("0.01")),
                valor_cancelados=sum((d.valor_cancelados for d in dias), zero).quantize(Decimal("0.01")),
                qtd_dias_coleta=qtd_col,
                valor_coletas=val_col.quantize(Decimal("0.01")),
                valor_base=(valor_entregas + val_col).quantize(Decimal("0.01")),
                fechamento_status=status_linha,
                parcial=pendente and len(status_dias) > 1,
                dias_pendentes=sum(1 for d in dias if (d.fechamento_status or "PENDENTE").upper() == "PENDENTE"),
                id_fechamento=fech.id_fechamento if fech is not None else None,
                valor_final=Decimal(str(fech.valor_final or 0)) if fech is not None else None,
                pode_reajustar=pode_reajustar,
                alerta_pos_pago=alerta_pos_pago,
                valor_base_atual=valor_base_atual,
                valor_base_fechado=Decimal(str(fech.valor_base or 0)) if fech is not None else None,
                periodo_inicio=_iso_ou_none(fech.periodo_inicio) if fech is not None else None,
                periodo_fim=_iso_ou_none(fech.periodo_fim) if fech is not None else None,
                dias_com_entrega=dias_com_entrega,
                dias_sem_conferencia=dias_sem_conferencia,
                dias=dias,
            )
        )

    rows.sort(key=lambda r: person_name_sort_key(r.nome))
    totais = ResumoExecutorTotais()
    for r in rows:
        if r.fechamento_status == "PENDENTE":
            totais.pendentes_qtd += 1
            totais.pendentes_valor += r.valor_base
        elif r.fechamento_status == "PAGO":
            totais.pagos_qtd += 1
            totais.pagos_valor += r.valor_final or zero
        else:
            totais.gerados_qtd += 1
            totais.gerados_valor += r.valor_final or zero
        if r.pode_reajustar or r.alerta_pos_pago:
            totais.revisao_qtd += 1

    filtro = (fechamento_status or "").strip().upper()
    if filtro == "REVISAO":
        rows = [r for r in rows if r.pode_reajustar or r.alerta_pos_pago]
    elif filtro:
        rows = [r for r in rows if r.fechamento_status == filtro]

    total_items = len(rows)
    total_pages = (total_items + pageSize - 1) // pageSize if total_items else 0
    start = (page - 1) * pageSize
    return ResumoExecutorResponse(
        page=page,
        pageSize=pageSize,
        totalPages=total_pages,
        totalItems=total_items,
        items=rows[start:start + pageSize],
        totais=totais,
        sumValor=sum(
            ((r.valor_base if r.fechamento_status == "PENDENTE" else (r.valor_final or zero)) for r in rows),
            zero,
        ),
        conferencia_habilitada=conferencia_habilitada,
    )


@router.get("/resumo", response_model=Union[EntregadorResumoResponse, ResumoExecutorResponse])
def resumo_entregadores(
    data_inicio: Optional[date] = Query(None),
    data_fim: Optional[date] = Query(None),
    entregador_id: Optional[int] = Query(None),
    motoboy_id: Optional[int] = Query(None),
    executor_tipo: Optional[str] = Query(None),
    executor_id: Optional[int] = Query(None),
    fechamento_status: Optional[str] = Query(None),
    base: Optional[str] = Query(None),
    agrupar: Optional[str] = Query(None, description="'executor' = uma linha por motoboy no período"),
    page: int = Query(1, ge=1),
    pageSize: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user=Depends(usuario_financeiro),
):
    sub_base_user = _resolve_user_base(db, current_user)
    agrupar_executor = (agrupar or "").strip().lower() == "executor"
    if agrupar_executor and (data_inicio is None or data_fim is None):
        raise HTTPException(400, "Informe data_inicio e data_fim para agrupar por motoboy.")
    toggle_pacote_g = _toggle_pacote_g_ativo(db, sub_base_user)
    regras_g, padrao_g = _regras_g_motoboy(db, sub_base_user)
    base_filtro_norm = base_filtro_ativo(base)
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
    entregador_ids_filtro = None
    motoboy_ids_filtro = None
    if entregador_id is not None or motoboy_id is not None:
        entregador_ids_filtro, motoboy_ids_filtro = _resolve_executor_scope_ids(
            db=db,
            sub_base_user=sub_base_user,
            entregador_id=entregador_id,
            motoboy_id=motoboy_id,
        )
        if not entregador_ids_filtro and not motoboy_ids_filtro:
            entregador_ids_filtro, motoboy_ids_filtro = set(), set()

    # Pré-filtro de base no SQL; com período usa agregação SQL (sem pacote a pacote).
    base_prefiltro = base if base_filtro_norm is not None else None
    agregados_periodo = None
    agrupado: Dict[str, Dict[str, Any]] = {}

    def _ensure_grupo(key: str, *, dia: str, entregador_id, motoboy_id, entregador_nome: str) -> Dict[str, Any]:
        if key not in agrupado:
            agrupado[key] = {
                "data": dia,
                "entregador_id": entregador_id,
                "motoboy_id": motoboy_id,
                "entregador_nome": entregador_nome,
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
        return agrupado[key]

    if data_inicio is not None and data_fim is not None:
        agregados_periodo = agregar_saidas_fechamento_resumo(
            db,
            sub_base=sub_base_user,
            status_validos=status_validos,
            periodo_inicio=data_inicio,
            periodo_fim=data_fim,
            motoboy_ids=sorted(motoboy_ids_filtro) if motoboy_ids_filtro is not None else None,
            entregador_ids=sorted(entregador_ids_filtro) if entregador_ids_filtro is not None else None,
            exigir_executor=True,
            base_prefiltro=base_prefiltro,
        )
        agregados = agregados_periodo
        if base_filtro_norm is not None:
            agregados = [a for a in agregados if saida_pertence_base(a.base, base_filtro_norm)]

        nomes_motoboy_map = _carregar_nomes_motoboy_ids(
            db,
            [int(a.motoboy_id) for a in agregados if a.motoboy_id is not None],
        )
        for agg in agregados:
            dia = agg.data.isoformat()
            if agg.motoboy_id is not None:
                mid = agg.motoboy_id
                key = f"{dia}_m_{mid}"
                _ensure_grupo(
                    key,
                    dia=dia,
                    entregador_id=None,
                    motoboy_id=mid,
                    entregador_nome=nomes_motoboy_map.get(int(mid), f"Motoboy {mid}"),
                )
            else:
                ent_id = agg.entregador_id
                ent_nome = agg.entregador or "Sem nome"
                if ent_id is None:
                    ent_id = -abs(hash(ent_nome))
                key = f"{dia}_{ent_id}"
                _ensure_grupo(
                    key,
                    dia=dia,
                    entregador_id=ent_id,
                    motoboy_id=None,
                    entregador_nome=ent_nome,
                )
            tipo = agg.tipo_servico if agg.tipo_servico in ("shopee", "flex", "avulso") else "avulso"
            qtd = int(agg.qtd or 0)
            if agg.is_cancelado:
                agrupado[key]["total_cancelado"] = agrupado[key].get("total_cancelado", 0) + qtd
                agrupado[key][f"cancel_{tipo}"] += qtd
            else:
                agrupado[key]["total_feitos"] = agrupado[key].get("total_feitos", 0) + qtd
                agrupado[key][f"qtde_{tipo}"] += qtd
                if agg.is_grande:
                    g_feitos = agrupado[key].setdefault("g_feitos", {})
                    cat_id = agg.categoria_g_id if agg.categoria_g_id is not None else padrao_g
                    chave_g = (cat_id, tipo)
                    g_feitos[chave_g] = g_feitos.get(chave_g, 0) + qtd
                    agrupado[key]["g_total"] = agrupado[key].get("g_total", 0) + qtd
    else:
        rows, marcos_map = carregar_saidas_fechamento_leve(
            db,
            sub_base=sub_base_user,
            status_validos=status_validos,
            periodo_inicio=data_inicio,
            periodo_fim=data_fim,
            motoboy_ids=sorted(motoboy_ids_filtro) if motoboy_ids_filtro is not None else None,
            entregador_ids=sorted(entregador_ids_filtro) if entregador_ids_filtro is not None else None,
            exigir_executor=True,
            base_prefiltro=base_prefiltro,
        )
        if base_filtro_norm is not None:
            rows = [s for s in rows if saida_pertence_base(s.base, base_filtro_norm)]
        nomes_motoboy_map = _carregar_nomes_motoboy_ids(
            db,
            [int(getattr(s, "motoboy_id")) for s in rows if getattr(s, "motoboy_id", None) is not None],
        )
        for saida in rows:
            op_ts = timestamp_operacional_marcos(marcos_map.get(int(saida.id_saida)), saida.timestamp)
            if op_ts is None:
                continue
            dia = op_ts.date().isoformat()
            if getattr(saida, "motoboy_id", None) is not None:
                mid = saida.motoboy_id
                key = f"{dia}_m_{mid}"
                _ensure_grupo(
                    key,
                    dia=dia,
                    entregador_id=None,
                    motoboy_id=mid,
                    entregador_nome=nomes_motoboy_map.get(int(mid), f"Motoboy {mid}"),
                )
            else:
                ent_id = saida.entregador_id
                ent_nome = saida.entregador or "Sem nome"
                if ent_id is None:
                    ent_id = -abs(hash(ent_nome))
                key = f"{dia}_{ent_id}"
                _ensure_grupo(
                    key,
                    dia=dia,
                    entregador_id=ent_id,
                    motoboy_id=None,
                    entregador_nome=ent_nome,
                )
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
                    g_feitos = agrupado[key].setdefault("g_feitos", {})
                    chave_g = (categoria_da_saida(saida, padrao_g), tipo)
                    g_feitos[chave_g] = g_feitos.get(chave_g, 0) + 1
                    agrupado[key]["g_total"] = agrupado[key].get("g_total", 0) + 1

    cache_precos: Dict[tuple, Dict[str, Decimal]] = {}  # ("e", eid) ou ("m", mid) -> precos
    cache_origem: Dict[tuple, Dict[str, str]] = {}
    precos_motoboy: Dict[int, Dict[str, Decimal]] = {}
    origens_motoboy: Dict[int, Dict[str, str]] = {}
    cache_valor_base: Dict[tuple, Decimal] = {}  # (eid, periodo_inicio, periodo_fim) ou ("m", mid, ini, fim) -> valor_base

    fech_stmt = select(EntregadorFechamento).where(EntregadorFechamento.sub_base == sub_base_user)
    if data_inicio is not None:
        fech_stmt = fech_stmt.where(EntregadorFechamento.periodo_fim >= data_inicio)
    if data_fim is not None:
        fech_stmt = fech_stmt.where(EntregadorFechamento.periodo_inicio <= data_fim)
    fechs_por_executor: Dict[tuple, List[EntregadorFechamento]] = {}
    fechs_por_id: Dict[int, EntregadorFechamento] = {}
    for f in db.scalars(fech_stmt.order_by(EntregadorFechamento.id_fechamento)).all():
        fechs_por_id[int(f.id_fechamento)] = f
        if getattr(f, "id_motoboy", None) is not None:
            fechs_por_executor.setdefault(("m", int(f.id_motoboy)), []).append(f)
        if f.id_entregador is not None:
            fechs_por_executor.setdefault(("e", int(f.id_entregador)), []).append(f)

    def _get_fechamento(eid: Optional[int], mid: Optional[int], data_str: str) -> tuple:
        """Retorna (status, id_fechamento, fechamento ou None). Entregador (id > 0) ou motoboy."""
        from datetime import datetime as dt
        try:
            data_ref = dt.strptime(data_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return ("PENDENTE", None, None)
        if mid is not None:
            chave = ("m", int(mid))
        elif eid is not None and eid > 0:
            chave = ("e", int(eid))
        else:
            return ("PENDENTE", None, None)
        fech = next(
            (f for f in fechs_por_executor.get(chave, []) if f.periodo_inicio <= data_ref <= f.periodo_fim),
            None,
        )
        if fech:
            st = (fech.status or "").upper()
            if st == "FECHADO":
                st = "GERADO"
            return (st or "GERADO", fech.id_fechamento, fech)
        return ("PENDENTE", None, None)

    # Valor oficial a partir das contagens. Sem filtro de base e no mesmo período,
    # reaproveita a agregação da lista. Com base, recalcula só os motoboys afetados.
    from entregador_fechamento_routes import calcular_diarias_coleta_motoboys

    motoboys_por_periodo: Dict[tuple, set] = {}
    for item in agrupado.values():
        mid = item.get("motoboy_id")
        if mid is None:
            continue
        st, id_f, f = _get_fechamento(None, mid, item["data"])
        if st in ("GERADO", "REAJUSTADO", "PAGO") and f is not None and id_f is not None:
            motoboys_por_periodo.setdefault((f.periodo_inicio, f.periodo_fim), set()).add(int(mid))
    for (p_ini, p_fim), mids in motoboys_por_periodo.items():
        mesmo_recorte = (
            agregados_periodo is not None
            and base_filtro_norm is None
            and p_ini == data_inicio
            and p_fim == data_fim
        )
        fonte = agregados_periodo if mesmo_recorte else agregar_saidas_fechamento_resumo(
            db,
            sub_base=sub_base_user,
            status_validos=status_validos,
            periodo_inicio=p_ini,
            periodo_fim=p_fim,
            motoboy_ids=sorted(mids),
            exigir_executor=False,
        )
        for mid in mids:
            precos_motoboy_cache(db, sub_base_user, int(mid), precos_motoboy)
        entregas = valor_entregas_agregadas(
            fonte,
            precos_motoboy,
            toggle_pacote_g=toggle_pacote_g,
            regras_g=regras_g,
            padrao_g=padrao_g,
        )
        coletas = calcular_diarias_coleta_motoboys(
            db, sub_base_user, sorted(mids), p_ini, p_fim, precos_cache=precos_motoboy,
        )
        for mid in mids:
            mid = int(mid)
            valor_coletas = coletas.get(mid, (0, Decimal("0.00")))[1]
            cache_valor_base[("m", mid, p_ini, p_fim)] = (
                entregas.get(mid, Decimal("0.00")) + valor_coletas
            ).quantize(Decimal("0.01"))

    lista: List[EntregadorResumoItem] = []
    for key, item in agrupado.items():
        eid = item["entregador_id"]
        mid = item.get("motoboy_id")
        precos_key = ("m", mid) if mid is not None else ("e", eid)
        if precos_key not in cache_precos:
            if mid is not None:
                cache_precos[precos_key] = precos_motoboy_cache(
                    db, sub_base_user, mid, precos_motoboy, origens_motoboy,
                )
                cache_origem[precos_key] = origens_motoboy.get(int(mid), dict(ORIGEM_GLOBAL))
            elif eid is not None and eid > 0:
                cache_precos[precos_key], cache_origem[precos_key] = precos_e_origem_entregador(
                    db, eid, sub_base_user,
                )
            else:
                cache_precos[precos_key] = resolver_precos_motoboy(db, sub_base_user)
                cache_origem[precos_key] = dict(ORIGEM_GLOBAL)
        precos = cache_precos[precos_key]
        valor_shopee = item["qtde_shopee"] * precos["shopee_valor"]
        valor_flex = item["qtde_flex"] * precos["ml_valor"]
        valor_avulso = item["qtde_avulso"] * precos["avulso_valor"]
        valor_g = valor_g_adicional(item.get("g_feitos") or {}, precos, toggle_pacote_g, regras_g)
        g_por_categoria: Dict[str, int] = {}
        for (cat_id, _tipo), qtd in (item.get("g_feitos") or {}).items():
            nome_cat = (regras_g.get(cat_id) or {}).get("nome") or "G"
            g_por_categoria[nome_cat] = g_por_categoria.get(nome_cat, 0) + int(qtd)
        valor_feitos = valor_shopee + valor_flex + valor_avulso + valor_g
        # valor_cancelados é informativo (o que não foi pago); não desconta de novo.
        valor_cancelados = (
            Decimal(item.get("cancel_shopee", 0)) * precos["shopee_valor"]
            + Decimal(item.get("cancel_flex", 0)) * precos["ml_valor"]
            + Decimal(item.get("cancel_avulso", 0)) * precos["avulso_valor"]
        ).quantize(Decimal("0.01"))
        total_dia = valor_feitos.quantize(Decimal("0.01"))

        fech_status, id_fech, fech = _get_fechamento(eid, mid, item["data"])

        pode_reajustar = None
        alerta_pos_pago = None
        valor_base_atual = None
        valor_base_fechado = None
        periodo_inicio_str = None
        periodo_fim_str = None
        if fech_status in ("GERADO", "REAJUSTADO", "PAGO") and fech is not None and id_fech is not None:
            if mid is not None:
                key_vb = ("m", mid, fech.periodo_inicio, fech.periodo_fim)
                valor_base_atual = cache_valor_base.get(key_vb)
            else:
                key_vb = (eid, fech.periodo_inicio, fech.periodo_fim)
                if key_vb not in cache_valor_base:
                    cache_valor_base[key_vb] = _calcular_valor_base_periodo(
                        db, sub_base_user, eid, fech.periodo_inicio, fech.periodo_fim
                    )
                valor_base_atual = cache_valor_base[key_vb]
            valor_base_fechado = fech.valor_base
            if valor_base_atual is not None and valor_base_atual != valor_base_fechado:
                if fech_status in ("GERADO", "REAJUSTADO"):
                    pode_reajustar = True
                elif fech_status == "PAGO":
                    alerta_pos_pago = True
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
                g_por_categoria=g_por_categoria,
                valor_g=valor_g,
                fechamento_status=fech_status,
                id_fechamento=id_fech,
                pode_reajustar=pode_reajustar,
                alerta_pos_pago=alerta_pos_pago,
                valor_base_atual=valor_base_atual,
                valor_base_fechado=valor_base_fechado,
                periodo_inicio=periodo_inicio_str,
                periodo_fim=periodo_fim_str,
                preco_origem=cache_origem.get(precos_key),
            )
        )

    lista.sort(key=lambda x: (x.data, x.entregador_nome))

    if agrupar_executor:
        return _resumo_agrupado_por_executor(
            db,
            sub_base_user,
            lista,
            data_inicio=data_inicio,
            data_fim=data_fim,
            fechamento_status=fechamento_status,
            filtro_motoboy_id=motoboy_id,
            filtro_entregador_id=entregador_id,
            page=page,
            pageSize=pageSize,
            cache_valor_base=cache_valor_base,
            precos_motoboy=precos_motoboy,
            conferencia_habilitada=owner_conferencia_habilitada(db, sub_base_user, current_user),
            fechs_por_executor=fechs_por_executor,
            fechs_por_id=fechs_por_id,
        )

    if fechamento_status and str(fechamento_status).strip():
        status_upper = str(fechamento_status).strip().upper()
        lista = [i for i in lista if (i.fechamento_status or "PENDENTE").upper() == status_upper]

    # Totalizadores globais: soma sobre TODOS os registros filtrados (lista completa).
    # Paginação afeta APENAS o array "items". Filtros já aplicados: data_inicio, data_fim, entregador_id, base, fechamento_status.
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
            coleta_valor=row.coleta_valor,
            considerar_pacote_g_adicional=bool(getattr(row, "considerar_pacote_g_adicional", False)),
        )
    return PrecoGlobalOut(
        shopee_valor=_zero,
        ml_valor=_zero,
        avulso_valor=_zero,
        coleta_valor=_zero,
        considerar_pacote_g_adicional=False,
    )


@router.patch("/precos/global", response_model=PrecoGlobalOut)
def patch_preco_global(
    body: PrecoGlobalUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(usuario_admin_precos),
):
    """Cria ou atualiza valores globais da sub_base do owner. Payload não vazio."""
    if (
        body.shopee_valor is None
        and body.ml_valor is None
        and body.avulso_valor is None
        and body.coleta_valor is None
        and body.considerar_pacote_g_adicional is None
    ):
        raise HTTPException(
            status_code=422,
            detail="Envie ao menos um campo de preço ou considerar_pacote_g_adicional.",
        )
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
        if body.coleta_valor is not None:
            row.coleta_valor = body.coleta_valor
        if body.considerar_pacote_g_adicional is not None:
            row.considerar_pacote_g_adicional = bool(body.considerar_pacote_g_adicional)
    else:
        row = EntregadorPrecoGlobal(
            sub_base=sub_base_user,
            shopee_valor=body.shopee_valor if body.shopee_valor is not None else _zero,
            ml_valor=body.ml_valor if body.ml_valor is not None else _zero,
            avulso_valor=body.avulso_valor if body.avulso_valor is not None else _zero,
            coleta_valor=body.coleta_valor if body.coleta_valor is not None else _zero,
            considerar_pacote_g_adicional=bool(body.considerar_pacote_g_adicional) if body.considerar_pacote_g_adicional is not None else False,
        )
        db.add(row)
    db.commit()
    db.refresh(row)
    return PrecoGlobalOut(
        shopee_valor=row.shopee_valor,
        ml_valor=row.ml_valor,
        avulso_valor=row.avulso_valor,
        coleta_valor=row.coleta_valor,
        considerar_pacote_g_adicional=bool(getattr(row, "considerar_pacote_g_adicional", False)),
    )


@router.get("/precos/individuais", response_model=PrecoIndividuaisResponse)
def get_precos_individuais(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Lista exceções por motoboy ativo (com fallback legado por entregador)."""
    sub_base_user = _resolve_user_base(db, current_user)
    motoboys = _carregar_motoboys_ativos_da_subbase(db, sub_base_user)
    cache_ep: Dict[int, Optional[EntregadorPreco]] = {}
    items: List[PrecoIndividualItem] = []
    for motoboy in motoboys:
        mid = int(motoboy.id_motoboy)
        entregador_id = _resolver_entregador_principal_do_motoboy_robusto(
            db=db,
            sub_base=sub_base_user,
            motoboy_id=mid,
        )
        ep = None
        if entregador_id is not None:
            eid = int(entregador_id)
            if eid not in cache_ep:
                cache_ep[eid] = db.scalars(
                    select(EntregadorPreco).where(
                        EntregadorPreco.id_entregador == eid,
                        EntregadorPreco.usa_preco_global.is_(False),
                    )
                ).first()
            ep = cache_ep[eid]
        # A grade de exceções deve listar apenas motoboys com exceção ativa.
        if ep is None:
            continue
        items.append(_preco_item_from_motoboy(db, sub_base_user, motoboy, ep))
    return PrecoIndividuaisResponse(items=items)


@router.get("/precos/executores", response_model=List[PrecoExecutorOption])
def get_precos_executores(
    status: Optional[str] = Query("ativo", description="Para entregadores: ativo, inativo ou todos"),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Lista opções de motoboy ativo para cadastro de exceção de preço.
    Usa fonte equivalente às telas de registros/leitura (users role=4 ativos).
    """
    sub_base_user = _resolve_user_base(db, current_user)
    motoboys = _carregar_motoboys_ativos_da_subbase(db, sub_base_user)
    nomes_motoboy = _carregar_nomes_motoboy_ids(db, [m.id_motoboy for m in motoboys])
    out: List[PrecoExecutorOption] = []
    for motoboy in motoboys:
        mid = int(motoboy.id_motoboy)
        nome_mb = nomes_motoboy.get(mid, f"Motoboy {mid}")
        out.append(
            PrecoExecutorOption(
                entregador_id=0,  # compat legado no front; seleção real por motoboy_id
                nome=nome_mb,
                executor_tipo="m",
                executor_id=mid,
                executor_key=f"m_{mid}",
            )
        )
    out.sort(key=lambda x: (x.nome or "").strip().lower())
    return out


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
            obj.nome = normalize_person_name(_optional(body.nome))

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
                nome_val = normalize_person_name(_optional(body.nome))
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


def _salvar_preco_por_entregador(
    db: Session,
    sub_base_user: str,
    id_entregador: int,
    body: PrecoEntregadorUpdate,
) -> PrecoIndividualItem:
    ent = _get_owned_entregador(db, sub_base_user, id_entregador)
    ep = db.scalars(
        select(EntregadorPreco).where(EntregadorPreco.id_entregador == id_entregador)
    ).first()
    if ep:
        ep.usa_preco_global = False
        # null explícito volta o campo para o preço padrão
        for campo in _CAMPOS_PRECO_EXCECAO:
            if campo in body.model_fields_set:
                setattr(ep, campo, getattr(body, campo))
        if all(getattr(ep, campo) is None for campo in _CAMPOS_PRECO_EXCECAO):
            db.delete(ep)
            db.commit()
            return PrecoIndividualItem(entregador_id=id_entregador, entregador_nome=ent.nome)
    else:
        ep = EntregadorPreco(
            id_entregador=id_entregador,
            shopee_valor=body.shopee_valor,
            ml_valor=body.ml_valor,
            avulso_valor=body.avulso_valor,
            coleta_valor=body.coleta_valor,
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
        coleta_valor=ep.coleta_valor,
    )


@router.post("/{id_entregador}/precos", response_model=PrecoIndividualItem)
def post_entregador_precos(
    id_entregador: int,
    body: PrecoEntregadorUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(usuario_admin_precos),
):
    """Compatibilidade legado: salva exceção por entregador."""
    if not (body.model_fields_set & set(_CAMPOS_PRECO_EXCECAO)):
        raise HTTPException(status_code=422, detail="Envie ao menos um campo de preço.")
    sub_base_user = _resolve_user_base(db, current_user)
    return _salvar_preco_por_entregador(db, sub_base_user, id_entregador, body)


@router.post("/motoboys/{id_motoboy}/precos", response_model=PrecoIndividualItem)
def post_motoboy_precos(
    id_motoboy: int,
    body: PrecoEntregadorUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(usuario_admin_precos),
):
    """Novo contrato: salva exceção selecionando motoboy ativo."""
    if not (body.model_fields_set & set(_CAMPOS_PRECO_EXCECAO)):
        raise HTTPException(status_code=422, detail="Envie ao menos um campo de preço.")
    sub_base_user = _resolve_user_base(db, current_user)
    motoboys = _carregar_motoboys_ativos_da_subbase(db, sub_base_user)
    mb_ids = {int(m.id_motoboy) for m in motoboys}
    if int(id_motoboy) not in mb_ids:
        raise HTTPException(status_code=404, detail="Motoboy não encontrado na sub_base.")

    entregador_id = _obter_ou_criar_entregador_legado_para_motoboy(
        db=db,
        sub_base_user=sub_base_user,
        id_motoboy=int(id_motoboy),
    )
    out = _salvar_preco_por_entregador(db, sub_base_user, int(entregador_id), body)
    nome_mb = _carregar_nomes_motoboy_ids(db, [int(id_motoboy)]).get(int(id_motoboy), f"Motoboy {id_motoboy}")
    out.motoboy_id = int(id_motoboy)
    out.motoboy_nome = nome_mb
    return out


@router.delete("/{id_entregador}/precos", status_code=status.HTTP_204_NO_CONTENT)
def delete_entregador_precos(
    id_entregador: int,
    db: Session = Depends(get_db),
    current_user=Depends(usuario_admin_precos),
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


@router.delete("/motoboys/{id_motoboy}/precos", status_code=status.HTTP_204_NO_CONTENT)
def delete_motoboy_precos(
    id_motoboy: int,
    db: Session = Depends(get_db),
    current_user=Depends(usuario_admin_precos),
):
    """Novo contrato: remove exceção selecionando motoboy."""
    sub_base_user = _resolve_user_base(db, current_user)
    motoboys = _carregar_motoboys_ativos_da_subbase(db, sub_base_user)
    mb_ids = {int(m.id_motoboy) for m in motoboys}
    if int(id_motoboy) not in mb_ids:
        raise HTTPException(status_code=404, detail="Motoboy não encontrado na sub_base.")
    entregador_id = _resolver_entregador_principal_do_motoboy_robusto(
        db=db,
        sub_base=sub_base_user,
        motoboy_id=int(id_motoboy),
    )
    if entregador_id is None:
        return
    ep = db.scalars(
        select(EntregadorPreco).where(EntregadorPreco.id_entregador == int(entregador_id))
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
    """Remove o entregador e a exceção de preço, sem deletar o user"""
    sub_base_user = _resolve_user_base(db, current_user)
    obj = _get_owned_entregador(db, sub_base_user, id_entregador)
    ep = db.scalars(
        select(EntregadorPreco).where(EntregadorPreco.id_entregador == id_entregador)
    ).first()
    if ep:
        db.delete(ep)
    db.delete(obj)
    db.commit()
    return
