"""PRD-004 — Autocadastro de motoboy por link de convite.

Rotas públicas (/public/convite-motoboy/...) para o motoboy e rotas de admin
(/convite-motoboy) para o owner gerar, consultar e revogar o link.
"""
from __future__ import annotations

import logging
import re
import secrets
import threading
import time
import unicodedata
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth import get_current_user, get_password_hash
from db import get_db
from models import Motoboy, MotoboyCadastroConvite, MotoboyConvite, Owner, User
from motoboy_cadastro_service import criar_motoboy
from name_normalizer import normalize_person_name
from validators_br import (
    is_cep_valido,
    is_cnpj_valido,
    is_cpf_valido,
    is_uf_valida,
    normalizar_celular,
    only_digits,
)

logger = logging.getLogger("routes.motoboy_convite")

public_router = APIRouter(prefix="/public", tags=["Convite Motoboy"])
admin_router = APIRouter(prefix="/convite-motoboy", tags=["Convite Motoboy"])

VALIDADE_PADRAO_DIAS = 7
LIMITE_USOS_PADRAO = 20
CADASTROS_POR_HORA_POR_IP = 5
CONSULTAS_USERNAME_POR_MINUTO = 30

USERNAME_RE = re.compile(r"^[a-z0-9._]{3,30}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SENHAS_TRIVIAIS = frozenset(
    {"123456", "1234567", "12345678", "123456789", "654321", "000000", "111111",
     "123123", "abcdef", "abc123", "qwerty", "senha", "senha123", "password"}
)


# ============================================================
# Helpers
# ============================================================


def _now() -> datetime:
    """UTC naive, mesmo padrão das colunas TIMESTAMP sem timezone."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return f"{dt.isoformat()}Z" if dt else None


def _client_ip(request: Request) -> str:
    """IP real atrás do proxy do Render (uvicorn roda sem --forwarded-allow-ips)."""
    fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if fwd:
        return fwd
    client = getattr(request, "client", None)
    return getattr(client, "host", None) or "desconhecido"


def field_error(status_code: int, field: Optional[str], message: str, **extra: Any) -> HTTPException:
    detail: dict[str, Any] = {"field": field, "message": message}
    detail.update(extra)
    return HTTPException(status_code=status_code, detail=detail)


class _JanelaLimiter:
    """Rate limit simples em memória (por processo)."""

    def __init__(self, limite: int, janela_s: float):
        self.limite = limite
        self.janela_s = janela_s
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def permitir(self, chave: str, agora: Optional[float] = None) -> bool:
        agora = time.monotonic() if agora is None else agora
        with self._lock:
            q = self._hits[chave]
            while q and agora - q[0] > self.janela_s:
                q.popleft()
            if len(q) >= self.limite:
                return False
            q.append(agora)
            return True


_username_limiter = _JanelaLimiter(CONSULTAS_USERNAME_POR_MINUTO, 60.0)


def nome_exibicao_owner(owner: Optional[Owner]) -> str:
    if not owner:
        return ""
    return (getattr(owner, "nome_fantasia", None) or "").strip() or (owner.username or "").strip()


def senha_trivial(senha: str) -> bool:
    s = (senha or "").lower()
    if s in SENHAS_TRIVIAIS or len(set(s)) == 1:
        return True
    if s.isdigit() and (s in "01234567890123456789" or s in "98765432109876543210"):
        return True
    return False


def _sem_acentos(value: str) -> str:
    nfkd = unicodedata.normalize("NFKD", value or "")
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalizar_username(value: Optional[str]) -> str:
    return _sem_acentos((value or "").strip().lower())


def _parse_data(value: Optional[str]) -> Optional[date]:
    raw = (value or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def status_convite(
    convite: Optional[MotoboyConvite],
    owner: Optional[Owner],
    usos: int,
    agora: Optional[datetime] = None,
) -> Optional[str]:
    """None quando válido; senão o motivo: invalido | expirado | esgotado | owner_inativo."""
    agora = agora or _now()
    if convite is None or not convite.ativo:
        return "invalido"
    if convite.expira_em is None or convite.expira_em <= agora:
        return "expirado"
    if convite.limite_usos is not None and usos >= int(convite.limite_usos):
        return "esgotado"
    if owner is None or not owner.ativo:
        return "owner_inativo"
    return None


MENSAGEM_CONVITE_INVALIDO = {
    "invalido": "Este link não está mais ativo.",
    "expirado": "Este link expirou.",
    "esgotado": "Este link já atingiu o limite de cadastros.",
    "owner_inativo": "Este link não está mais ativo.",
}


def _usos(db: Session, convite_id: int) -> int:
    return int(
        db.scalar(
            select(func.count()).select_from(MotoboyCadastroConvite).where(
                MotoboyCadastroConvite.convite_id == convite_id
            )
        )
        or 0
    )


def _owner_da_sub_base(db: Session, sub_base: str) -> Optional[Owner]:
    return db.scalar(select(Owner).where(Owner.sub_base == sub_base).limit(1))


def _carregar_convite(db: Session, token: str, *, travar: bool = False) -> Optional[MotoboyConvite]:
    tk = (token or "").strip()
    if not tk or len(tk) > 128:
        return None
    stmt = select(MotoboyConvite).where(MotoboyConvite.token == tk)
    if travar:
        stmt = stmt.with_for_update()
    return db.scalar(stmt)


def _username_em_uso(db: Session, username: str) -> bool:
    """Login aceita email/username/contato em todas as sub_bases: nenhum pode coincidir."""
    u = normalizar_username(username)
    if not u:
        return False
    stmt = select(User.id).where(
        (func.lower(func.trim(User.username)) == u)
        | (func.lower(func.trim(User.email)) == u)
        | (func.trim(User.contato) == u)
    )
    return db.scalar(stmt.limit(1)) is not None


def sugerir_usernames(base: str, em_uso, limite: int = 3) -> list[str]:
    raiz = re.sub(r"[^a-z0-9._]", "", normalizar_username(base)).strip("._")[:26] or "entregador"
    if len(raiz) < 3:
        raiz = f"{raiz}.moto"
    candidatos = [raiz] + [f"{raiz}{n}" for n in range(1, 30)]
    out: list[str] = []
    for cand in candidatos:
        if not USERNAME_RE.match(cand) or cand == base:
            continue
        if not em_uso(cand):
            out.append(cand)
        if len(out) >= limite:
            break
    return out


# ============================================================
# Schemas
# ============================================================


class CadastroMotoboyIn(BaseModel):
    nome: Optional[str] = None
    sobrenome: Optional[str] = None
    documento: Optional[str] = None
    data_nascimento: Optional[str] = None
    contato: Optional[str] = None
    email: Optional[str] = None
    cnpj: Optional[str] = None
    chave_pix: Optional[str] = None
    cep: Optional[str] = None
    rua: Optional[str] = None
    numero: Optional[str] = None
    complemento: Optional[str] = None
    bairro: Optional[str] = None
    cidade: Optional[str] = None
    estado: Optional[str] = None
    username: Optional[str] = None
    senha: Optional[str] = None
    confirmar_senha: Optional[str] = None
    aceite_privacidade: Optional[bool] = None
    website: Optional[str] = None  # honeypot: humanos não veem este campo


class ConviteGerarIn(BaseModel):
    validade_dias: int = Field(default=VALIDADE_PADRAO_DIAS, ge=1, le=30)
    limite_usos: Optional[int] = Field(default=LIMITE_USOS_PADRAO, ge=1, le=100)


def _texto(value: Optional[str]) -> str:
    return " ".join((value or "").split())


def validar_cadastro(body: CadastroMotoboyIn, hoje: Optional[date] = None) -> dict:
    """Valida o formulário na ordem das etapas. Levanta 422 com o primeiro campo inválido."""
    hoje = hoje or date.today()

    def erro(field: str, message: str) -> HTTPException:
        return field_error(status.HTTP_422_UNPROCESSABLE_ENTITY, field, message)

    nome = _texto(body.nome)
    if not 2 <= len(nome) <= 60:
        raise erro("nome", "Informe seu nome.")
    sobrenome = _texto(body.sobrenome)
    if not 2 <= len(sobrenome) <= 60:
        raise erro("sobrenome", "Informe seu sobrenome.")

    cpf = only_digits(body.documento)
    if not is_cpf_valido(cpf):
        raise erro("documento", "CPF inválido. Confira os números.")

    nascimento = _parse_data(body.data_nascimento)
    if nascimento is None:
        raise erro("data_nascimento", "Informe a data de nascimento.")
    if nascimento >= hoje or nascimento.year < 1900:
        raise erro("data_nascimento", "Data de nascimento inválida.")

    contato = normalizar_celular(body.contato)
    if not contato:
        raise erro("contato", "Informe um celular válido com DDD.")

    email = (body.email or "").strip() or None
    if email and (len(email) > 254 or not EMAIL_RE.match(email)):
        raise erro("email", "E-mail inválido.")

    cnpj = only_digits(body.cnpj) or None
    if cnpj and not is_cnpj_valido(cnpj):
        raise erro("cnpj", "CNPJ inválido. Confira os números.")

    chave_pix = (body.chave_pix or "").strip() or None
    if chave_pix and len(chave_pix) > 140:
        raise erro("chave_pix", "Chave PIX muito longa.")

    cep = only_digits(body.cep)
    if not is_cep_valido(cep):
        raise erro("cep", "Informe um CEP com 8 números.")
    endereco = {}
    for campo, rotulo in (("rua", "a rua"), ("numero", "o número"), ("bairro", "o bairro"), ("cidade", "a cidade")):
        valor = _texto(getattr(body, campo))
        if not valor or len(valor) > 120:
            raise erro(campo, f"Informe {rotulo}.")
        endereco[campo] = valor
    estado = (body.estado or "").strip().upper()
    if not is_uf_valida(estado):
        raise erro("estado", "Informe a UF (ex.: SP).")
    complemento = _texto(body.complemento)[:120] or None

    username = normalizar_username(body.username)
    if not USERNAME_RE.match(username):
        raise erro("username", "Use de 3 a 30 letras minúsculas, números, ponto ou _.")

    senha = body.senha or ""
    if not 6 <= len(senha) <= 72:
        raise erro("senha", "A senha precisa ter de 6 a 72 caracteres.")
    if senha_trivial(senha) or senha.lower() == username:
        raise erro("senha", "Escolha uma senha mais difícil de adivinhar.")
    if senha != (body.confirmar_senha or ""):
        raise erro("confirmar_senha", "As senhas não são iguais.")

    if body.aceite_privacidade is not True:
        raise erro("aceite_privacidade", "Aceite a Política de Privacidade para continuar.")

    return {
        "nome": nome,
        "sobrenome": sobrenome,
        "documento": cpf,
        "data_nascimento": nascimento,
        "contato": contato,
        "email": email,
        "cnpj": cnpj,
        "chave_pix": chave_pix,
        "cep": cep,
        "complemento": complemento,
        "estado": estado,
        "username": username,
        "senha": senha,
        **endereco,
    }


def _convite_out(convite: Optional[MotoboyConvite], usos: int) -> Optional[dict]:
    if convite is None:
        return None
    agora = _now()
    return {
        "id": convite.id,
        "token": convite.token,
        "expira_em": _iso(convite.expira_em),
        "limite_usos": convite.limite_usos,
        "usos": usos,
        "ativo": bool(convite.ativo),
        "expirado": bool(convite.expira_em and convite.expira_em <= agora),
        "esgotado": bool(convite.limite_usos is not None and usos >= int(convite.limite_usos)),
        "created_at": _iso(convite.created_at),
    }


# ============================================================
# Rotas públicas
# ============================================================


@public_router.get("/convite-motoboy/{token}")
def consultar_convite(token: str, db: Session = Depends(get_db)):
    convite = _carregar_convite(db, token)
    owner = _owner_da_sub_base(db, convite.sub_base) if convite else None
    usos = _usos(db, convite.id) if convite else 0
    motivo = status_convite(convite, owner, usos)
    if motivo:
        return {"valido": False, "motivo": motivo, "mensagem": MENSAGEM_CONVITE_INVALIDO[motivo]}
    return {"valido": True, "nome_owner": nome_exibicao_owner(owner)}


@public_router.get("/convite-motoboy/{token}/username-disponivel")
def username_disponivel(token: str, request: Request, u: str = "", db: Session = Depends(get_db)):
    if not _username_limiter.permitir(_client_ip(request)):
        raise field_error(status.HTTP_429_TOO_MANY_REQUESTS, "username", "Muitas consultas. Aguarde um instante.")
    convite = _carregar_convite(db, token)
    owner = _owner_da_sub_base(db, convite.sub_base) if convite else None
    if status_convite(convite, owner, _usos(db, convite.id) if convite else 0):
        raise field_error(status.HTTP_404_NOT_FOUND, None, "Este link não está mais ativo.")

    username = normalizar_username(u)
    if not USERNAME_RE.match(username):
        return {"disponivel": False, "valido": False, "sugestoes": []}
    if not _username_em_uso(db, username):
        return {"disponivel": True, "valido": True, "sugestoes": []}
    return {
        "disponivel": False,
        "valido": True,
        "sugestoes": sugerir_usernames(username, lambda c: _username_em_uso(db, c)),
    }


@public_router.post("/convite-motoboy/{token}/cadastro", status_code=status.HTTP_201_CREATED)
def cadastrar_motoboy(token: str, body: CadastroMotoboyIn, request: Request, db: Session = Depends(get_db)):
    dados = validar_cadastro(body)

    if (body.website or "").strip():
        logger.warning("Honeypot preenchido no convite de motoboy; cadastro ignorado.")
        return {"username": dados["username"], "nome": dados["nome"], "nome_owner": ""}

    ip = _client_ip(request)
    try:
        convite = _carregar_convite(db, token, travar=True)
        owner = _owner_da_sub_base(db, convite.sub_base) if convite else None
        motivo = status_convite(convite, owner, _usos(db, convite.id) if convite else 0)
        if motivo:
            raise field_error(status.HTTP_410_GONE, None, MENSAGEM_CONVITE_INVALIDO[motivo], motivo=motivo)
        sub_base = convite.sub_base

        recentes = db.scalar(
            select(func.count()).select_from(MotoboyCadastroConvite).where(
                MotoboyCadastroConvite.ip_origem == ip,
                MotoboyCadastroConvite.created_at >= _now() - timedelta(hours=1),
            )
        ) or 0
        if int(recentes) >= CADASTROS_POR_HORA_POR_IP:
            raise field_error(
                status.HTTP_429_TOO_MANY_REQUESTS, None, "Muitos cadastros feitos desta rede. Tente mais tarde."
            )

        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:u))"), {"u": dados["username"]})

        if _username_em_uso(db, dados["username"]):
            raise field_error(
                status.HTTP_409_CONFLICT,
                "username",
                "Este usuário já está em uso. Escolha outro.",
                sugestoes=sugerir_usernames(dados["username"], lambda c: _username_em_uso(db, c)),
            )
        if dados["email"] and db.scalar(
            select(User.id).where(func.lower(func.trim(User.email)) == dados["email"].lower()).limit(1)
        ):
            raise field_error(status.HTTP_409_CONFLICT, "email", "Este e-mail já está cadastrado.")
        if db.scalar(
            select(User.id).where(User.sub_base == sub_base, User.contato == dados["contato"]).limit(1)
        ):
            raise field_error(
                status.HTTP_409_CONFLICT, "contato", "Este celular já está cadastrado nesta base. Fale com o responsável."
            )
        if db.scalar(
            select(Motoboy.id_motoboy).where(
                Motoboy.sub_base == sub_base,
                func.regexp_replace(func.coalesce(Motoboy.documento, ""), r"\D", "", "g") == dados["documento"],
            ).limit(1)
        ):
            raise field_error(
                status.HTTP_409_CONFLICT, "documento", "Este CPF já está cadastrado nesta base. Fale com o responsável."
            )

        user = criar_motoboy(
            db,
            owner=owner,
            sub_base=sub_base,
            username=dados["username"],
            password_hash=get_password_hash(dados["senha"]),
            contato=dados["contato"],
            nome=dados["nome"],
            sobrenome=dados["sobrenome"],
            email=dados["email"],
            data_nascimento=dados["data_nascimento"],
            must_change_password=False,
            dados=dados,
        )
        agora = _now()
        db.add(
            MotoboyCadastroConvite(
                convite_id=convite.id,
                sub_base=sub_base,
                user_id=user.id,
                ip_origem=ip,
                aceite_privacidade_em=agora,
                created_at=agora,
            )
        )
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as e:
        db.rollback()
        logger.warning("Conflito ao autocadastrar motoboy: %s", e)
        raise field_error(status.HTTP_409_CONFLICT, None, "Não foi possível concluir o cadastro. Revise os dados.")
    except Exception:
        db.rollback()
        logger.exception("Erro ao autocadastrar motoboy")
        raise field_error(status.HTTP_500_INTERNAL_SERVER_ERROR, None, "Erro ao criar o cadastro. Tente novamente.")

    return {
        "username": user.username,
        "nome": normalize_person_name(dados["nome"]),
        "nome_owner": nome_exibicao_owner(owner),
    }


# ============================================================
# Rotas de admin (owner)
# ============================================================


def _admin_sub_base(current_user: User) -> str:
    if getattr(current_user, "role", None) not in (0, 1):
        raise HTTPException(403, "Acesso negado.")
    sub_base = getattr(current_user, "sub_base", None)
    if not sub_base:
        raise HTTPException(400, "Usuário atual não possui sub_base.")
    return sub_base


def _convite_ativo(db: Session, sub_base: str) -> Optional[MotoboyConvite]:
    return db.scalar(
        select(MotoboyConvite)
        .where(MotoboyConvite.sub_base == sub_base, MotoboyConvite.ativo.is_(True))
        .order_by(MotoboyConvite.id.desc())
        .limit(1)
    )


def _desativar_convites(db: Session, sub_base: str) -> None:
    for c in db.scalars(
        select(MotoboyConvite).where(MotoboyConvite.sub_base == sub_base, MotoboyConvite.ativo.is_(True))
    ):
        c.ativo = False
    db.flush()


@admin_router.get("")
def obter_convite(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    sub_base = _admin_sub_base(current_user)
    convite = _convite_ativo(db, sub_base)
    owner = _owner_da_sub_base(db, sub_base)
    return {
        "convite": _convite_out(convite, _usos(db, convite.id) if convite else 0),
        "nome_owner": nome_exibicao_owner(owner),
    }


@admin_router.post("", status_code=status.HTTP_201_CREATED)
def gerar_convite(
    body: ConviteGerarIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base = _admin_sub_base(current_user)
    owner = _owner_da_sub_base(db, sub_base)
    if not owner or not owner.ativo:
        raise HTTPException(403, "Owner desta sub_base está inativo.")
    try:
        _desativar_convites(db, sub_base)
        agora = _now()
        convite = MotoboyConvite(
            sub_base=sub_base,
            token=secrets.token_urlsafe(24),
            criado_por_user_id=getattr(current_user, "id", None),
            expira_em=agora + timedelta(days=body.validade_dias),
            limite_usos=body.limite_usos,
            ativo=True,
            created_at=agora,
        )
        db.add(convite)
        db.commit()
        db.refresh(convite)
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Outro link foi gerado ao mesmo tempo. Atualize a tela.")
    return {"convite": _convite_out(convite, 0), "nome_owner": nome_exibicao_owner(owner)}


@admin_router.delete("")
def revogar_convite(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    sub_base = _admin_sub_base(current_user)
    _desativar_convites(db, sub_base)
    db.commit()
    return {"ok": True}


@admin_router.get("/cadastros")
def listar_cadastros(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    sub_base = _admin_sub_base(current_user)
    rows = db.execute(
        select(MotoboyCadastroConvite, User)
        .join(User, User.id == MotoboyCadastroConvite.user_id)
        .where(MotoboyCadastroConvite.sub_base == sub_base, User.sub_base == sub_base)
        .order_by(MotoboyCadastroConvite.created_at.desc())
        .limit(200)
    ).all()
    return {
        "itens": [
            {
                "user_id": u.id,
                "nome": " ".join(p for p in (u.nome, u.sobrenome) if p),
                "username": u.username,
                "contato": u.contato,
                "ativo": bool(u.status),
                "convite_id": log.convite_id,
                "cadastrado_em": _iso(log.created_at),
            }
            for log, u in rows
        ]
    }
