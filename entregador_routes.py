from __future__ import annotations
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, AliasChoices
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user, get_password_hash
from models import Entregador, EntregadorFechamento, EntregadorPreco, EntregadorPrecoGlobal, Saida, User

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
    entregador_id: int
    entregador_nome: str
    shopee: Dict[str, Any]  # {qtde, valor_unit, total}
    flex: Dict[str, Any]
    avulso: Dict[str, Any]
    total_dia: Decimal
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


def _normalizar_servico(servico: Optional[str]) -> str:
    s = (servico or "").lower()
    if "shopee" in s:
        return "shopee"
    if "ml" in s or "mercado" in s:
        return "flex"
    return "avulso"


def _calcular_valor_base_periodo(
    db: Session,
    sub_base_user: str,
    entregador_id: int,
    periodo_inicio: date,
    periodo_fim: date,
) -> Decimal:
    """Calcula o valor_base a partir das saídas do entregador no período (para resumo e modal)."""
    if periodo_inicio > periodo_fim:
        return Decimal("0.00")
    status_validos = ["saiu", "saiu pra entrega", "saiu_pra_entrega", "entregue"]
    stmt = select(Saida).where(
        Saida.sub_base == sub_base_user,
        Saida.entregador_id == entregador_id,
        Saida.codigo.isnot(None),
        Saida.timestamp >= datetime.combine(periodo_inicio, time.min),
        Saida.timestamp <= datetime.combine(periodo_fim, time(23, 59, 59)),
        func.lower(Saida.status).in_(status_validos),
    )
    rows = db.scalars(stmt).all()
    precos = resolver_precos_entregador(db, entregador_id, sub_base_user)
    total = Decimal("0.00")
    for saida in rows:
        tipo = _normalizar_servico(saida.servico)
        if tipo == "shopee":
            total += precos["shopee_valor"]
        elif tipo == "flex":
            total += precos["ml_valor"]
        else:
            total += precos["avulso_valor"]
    return total.quantize(Decimal("0.01"))


@router.get("/fechamentos/calcular")
def calcular_valor_base_fechamento(
    entregador_id: int = Query(...),
    periodo_inicio: date = Query(...),
    periodo_fim: date = Query(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Retorna valor_base calculado para o período (para modal de fechamento)."""
    sub_base_user = _resolve_user_base(db, current_user)
    if periodo_inicio > periodo_fim:
        raise HTTPException(400, "periodo_inicio deve ser anterior a periodo_fim.")
    ent = db.get(Entregador, entregador_id)
    if not ent or (ent.sub_base and ent.sub_base != sub_base_user):
        raise HTTPException(404, "Entregador não encontrado.")
    valor_base = _calcular_valor_base_periodo(db, sub_base_user, entregador_id, periodo_inicio, periodo_fim)
    return {
        "valor_base": valor_base,
        "entregador_id": entregador_id,
        "entregador_nome": ent.nome,
        "periodo_inicio": periodo_inicio.isoformat(),
        "periodo_fim": periodo_fim.isoformat(),
    }


@router.get("/resumo", response_model=EntregadorResumoResponse)
def resumo_entregadores(
    data_inicio: Optional[date] = Query(None),
    data_fim: Optional[date] = Query(None),
    entregador_id: Optional[int] = Query(None),
    fechamento_status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    pageSize: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    sub_base_user = _resolve_user_base(db, current_user)

    # Inclui saída e entrega; exclui cancelados
    status_validos = ["saiu", "saiu pra entrega", "saiu_pra_entrega", "entregue"]
    stmt = select(Saida).where(
        Saida.sub_base == sub_base_user,
        Saida.codigo.isnot(None),
        func.lower(Saida.status).in_(status_validos),
        or_(
            Saida.entregador_id.isnot(None),
            Saida.entregador.isnot(None),
        ),
    )

    if data_inicio is not None:
        stmt = stmt.where(Saida.timestamp >= datetime.combine(data_inicio, time.min))
    if data_fim is not None:
        stmt = stmt.where(Saida.timestamp <= datetime.combine(data_fim, time(23, 59, 59)))
    if entregador_id is not None:
        stmt = stmt.where(Saida.entregador_id == entregador_id)

    rows = db.scalars(stmt).all()

    agrupado: Dict[str, Dict[str, Any]] = {}
    for saida in rows:
        ent_id = saida.entregador_id
        ent_nome = saida.entregador or "Sem nome"
        if ent_id is None:
            # fallback: usa hash do nome como "id" fictício para agrupamento
            ent_id = -abs(hash(ent_nome))

        dia = saida.timestamp.date().isoformat()
        key = f"{dia}_{ent_id}"
        if key not in agrupado:
            agrupado[key] = {
                "data": dia,
                "entregador_id": ent_id,
                "entregador_nome": ent_nome,
                "qtde_shopee": 0,
                "qtde_flex": 0,
                "qtde_avulso": 0,
            }
        tipo = _normalizar_servico(saida.servico)
        agrupado[key][f"qtde_{tipo}"] += 1

    cache_precos: Dict[int, Dict[str, Decimal]] = {}
    cache_fechamento: Dict[tuple, Optional[EntregadorFechamento]] = {}
    cache_valor_base: Dict[tuple, Decimal] = {}  # (eid, periodo_inicio, periodo_fim) -> valor_base

    def _get_fechamento(eid: int, data_str: str) -> tuple:
        """Retorna (status, id_fechamento, fechamento ou None). Apenas entregadores reais (id > 0)."""
        if eid <= 0:
            return ("PENDENTE", None, None)
        from datetime import datetime as dt
        try:
            data_ref = dt.strptime(data_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return ("PENDENTE", None, None)
        key_cache = (sub_base_user, eid, data_str)
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
                st = "GERADO"  # legado
            return (st or "GERADO", fech.id_fechamento, fech)
        return ("PENDENTE", None, None)

    lista: List[EntregadorResumoItem] = []
    for key, item in agrupado.items():
        eid = item["entregador_id"]
        if eid not in cache_precos:
            cache_precos[eid] = resolver_precos_entregador(db, eid, sub_base_user)
        precos = cache_precos[eid]
        valor_shopee = item["qtde_shopee"] * precos["shopee_valor"]
        valor_flex = item["qtde_flex"] * precos["ml_valor"]
        valor_avulso = item["qtde_avulso"] * precos["avulso_valor"]
        total_dia = valor_shopee + valor_flex + valor_avulso

        fech_status, id_fech, fech = _get_fechamento(eid, item["data"])

        pode_reajustar = None
        valor_base_atual = None
        valor_base_fechado = None
        periodo_inicio_str = None
        periodo_fim_str = None
        if fech_status == "GERADO" and fech is not None and id_fech is not None:
            key_vb = (eid, fech.periodo_inicio, fech.periodo_fim)
            if key_vb not in cache_valor_base:
                cache_valor_base[key_vb] = _calcular_valor_base_periodo(
                    db, sub_base_user, eid, fech.periodo_inicio, fech.periodo_fim
                )
            valor_base_atual = cache_valor_base[key_vb]
            valor_base_fechado = fech.valor_base
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
    sumValor = sum((i.total_dia for i in lista), Decimal("0.00"))
    sumTotalEntregas = sumShopee + sumFlex + sumAvulso

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
