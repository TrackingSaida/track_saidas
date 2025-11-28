from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from typing import Optional

from db import get_db
from db import SessionLocal
from models import Saida, SaidaDetail, Entregador

# Funções utilitárias do token
from auth import create_access_token
from auth import oauth2_scheme
from jose import jwt
from auth import SECRET_KEY, ALGORITHM


router = APIRouter(
    prefix="/entregador/entregas",
    tags=["Entregador - Entregas"]
)


# ============================================================
# Aux: obter entregador_id do token
# ============================================================
def get_entregador_id_from_token(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("sub") != "entregador":
            raise HTTPException(403, "Token não é de entregador")
        return payload["entregador_id"]
    except:
        raise HTTPException(401, "Token inválido")


# ============================================================
# LOGIN SIMPLES DO ENTREGADOR (SEM SENHA)
# ============================================================
@router.post("/login-simples")
def login_simples(payload: dict, db: Session = Depends(get_db)):

    telefone = (payload.get("telefone") or "").strip()
    if not telefone:
        raise HTTPException(422, "Telefone é obrigatório.")

    entregador = db.scalars(
        select(Entregador).where(Entregador.telefone == telefone)
    ).first()

    if not entregador:
        raise HTTPException(404, "Entregador não encontrado.")

    if not entregador.ativo:
        raise HTTPException(403, "Entregador inativo.")

    # Gerar token independente
    token = create_access_token({
        "sub": "entregador",
        "entregador_id": entregador.id_entregador
    })

    return {
        "ok": True,
        "token": token,
        "entregador": {
            "id": entregador.id_entregador,
            "nome": entregador.nome,
            "telefone": entregador.telefone,
            "sub_base": entregador.sub_base
        }
    }


# ============================================================
# SCAN DE CÓDIGO (bipa o pacote)
# ============================================================
@router.post("/scan")
def scan_codigo(payload: dict,
                entregador_id: int = Depends(get_entregador_id_from_token),
                db: Session = Depends(get_db)):

    codigo = (payload.get("codigo") or "").strip()

    if not codigo:
        raise HTTPException(422, "Código é obrigatório.")

    saida = db.scalars(
        select(Saida).where(Saida.codigo == codigo)
    ).first()

    if not saida:
        raise HTTPException(404, "Pacote não encontrado em SAIDAS.")

    # Se já está atribuído a outro entregador
    if saida.entregador_id and saida.entregador_id != entregador_id:
        raise HTTPException(403, f"Pacote já está com outro entregador (ID {saida.entregador_id}).")

    # Registra a atribuição
    saida.entregador_id = entregador_id

    # Mantém o campo histórico texto para painéis antigos
    entregador = db.scalars(
        select(Entregador).where(Entregador.id_entregador == entregador_id)
    ).first()
    saida.entregador = entregador.nome

    db.commit()

    return {"ok": True, "msg": "Pacote assumido via scan", "id_saida": saida.id_saida}


# ============================================================
# ASSUMIR PACOTE (quando já existe e está com outro EXCEÇÃO)
# ============================================================
@router.post("/assumir")
def assumir_codigo(payload: dict,
                   entregador_id: int = Depends(get_entregador_id_from_token),
                   db: Session = Depends(get_db)):

    codigo = (payload.get("codigo") or "").strip()

    if not codigo:
        raise HTTPException(422, "Código é obrigatório.")

    saida = db.scalars(
        select(Saida).where(Saida.codigo == codigo)
    ).first()

    if not saida:
        raise HTTPException(404, "Pacote não encontrado.")

    # Atribuir sempre ao entregador logado
    saida.entregador_id = entregador_id

    entregador = db.scalars(
        select(Entregador).where(Entregador.id_entregador == entregador_id)
    ).first()
    saida.entregador = entregador.nome

    db.commit()

    return {"ok": True, "msg": "Pacote assumido", "id_saida": saida.id_saida}


# ============================================================
# LISTAR PENDENTES DO ENTREGADOR
# ============================================================
@router.get("/pendentes")
def listar_pendentes(entregador_id: int = Depends(get_entregador_id_from_token),
                     db: Session = Depends(get_db)):

    pendentes = db.scalars(
        select(Saida).where(
            Saida.entregador_id == entregador_id,
            Saida.status == "saiu"
        )
    ).all()

    return [{"id_saida": s.id_saida, "codigo": s.codigo} for s in pendentes]


# ============================================================
# DETALHES DO PACOTE
# ============================================================
@router.get("/detalhe/{id_saida}")
def detalhe(id_saida: int,
            entregador_id: int = Depends(get_entregador_id_from_token),
            db: Session = Depends(get_db)):

    saida = db.get(Saida, id_saida)
    if not saida:
        raise HTTPException(404, "Pacote não encontrado.")

    if saida.entregador_id != entregador_id:
        raise HTTPException(403, "Este pacote não pertence a você.")

    return {
        "id_saida": saida.id_saida,
        "codigo": saida.codigo,
        "status": saida.status
    }


# ============================================================
# ENTREGAR PACOTE
# ============================================================
@router.post("/entregar")
def entregar_pacote(payload: dict,
                    entregador_id: int = Depends(get_entregador_id_from_token),
                    db: Session = Depends(get_db)):

    id_saida = payload.get("id_saida")

    saida = db.get(Saida, id_saida)
    if not saida:
        raise HTTPException(404, "Saída não encontrada.")

    if saida.entregador_id != entregador_id:
        raise HTTPException(403, "Este pacote não pertence a você.")

    detail = SaidaDetail(
        id_saida=id_saida,
        id_entregador=entregador_id,
        status="entregue",
        tipo_recebedor=payload.get("tipo_recebedor"),
        nome_recebedor=payload.get("nome_recebedor"),
        tipo_documento=payload.get("tipo_documento"),
        numero_documento=payload.get("numero_documento"),
        observacao_entrega=payload.get("observacao_entrega"),
        foto_url=payload.get("foto_url")
    )

    saida.status = "entregue"

    db.add(detail)
    db.commit()

    return {"ok": True, "msg": "Pacote entregue"}


# ============================================================
# REGISTRAR OCORRÊNCIA
# ============================================================
@router.post("/ocorrencia")
def registrar_ocorrencia(payload: dict,
                         entregador_id: int = Depends(get_entregador_id_from_token),
                         db: Session = Depends(get_db)):

    id_saida = payload.get("id_saida")

    saida = db.get(Saida, id_saida)
    if not saida:
        raise HTTPException(404, "Saída não encontrada.")

    if saida.entregador_id != entregador_id:
        raise HTTPException(403, "Este pacote não pertence a você.")

    detail = SaidaDetail(
        id_saida=id_saida,
        id_entregador=entregador_id,
        status="ocorrencia",
        motivo_ocorrencia=payload.get("motivo"),
        observacao_ocorrencia=payload.get("observacao"),
        foto_url=payload.get("foto_url")
    )

    saida.status = "ocorrencia"

    db.add(detail)
    db.commit()

    return {"ok": True, "msg": "Ocorrência registrada"}


# ============================================================
# NOVA TENTATIVA
# ============================================================
@router.post("/tentativa")
def nova_tentativa(payload: dict,
                   entregador_id: int = Depends(get_entregador_id_from_token),
                   db: Session = Depends(get_db)):

    id_saida = payload.get("id_saida")

    saida = db.get(Saida, id_saida)
    if not saida:
        raise HTTPException(404, "Saída não encontrada.")

    if saida.entregador_id != entregador_id:
        raise HTTPException(403, "Este pacote não pertence a você.")

    # marcar status voltou para pendente
    saida.status = "saiu"

    detail = SaidaDetail(
        id_saida=id_saida,
        id_entregador=entregador_id,
        status="tentativa"
    )

    db.add(detail)
    db.commit()

    return {"ok": True, "msg": "Nova tentativa registrada"}


# ============================================================
# FINALIZADAS
# ============================================================
@router.get("/finalizadas")
def finalizadas(entregador_id: int = Depends(get_entregador_id_from_token),
                db: Session = Depends(get_db)):

    final = db.scalars(
        select(Saida).where(
            Saida.entregador_id == entregador_id,
            Saida.status == "entregue"
        )
    ).all()

    return [{"id_saida": s.id_saida, "codigo": s.codigo} for s in final]


# ============================================================
# OCORRÊNCIAS
# ============================================================
@router.get("/ocorrencias")
def ocorrencias(entregador_id: int = Depends(get_entregador_id_from_token),
                db: Session = Depends(get_db)):

    occ = db.scalars(
        select(Saida).where(
            Saida.entregador_id == entregador_id,
            Saida.status == "ocorrencia"
        )
    ).all()

    return [{"id_saida": s.id_saida, "codigo": s.codigo} for s in occ]
