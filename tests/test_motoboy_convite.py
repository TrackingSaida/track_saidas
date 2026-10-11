"""PRD-004 — autocadastro de motoboy por link de convite."""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("SECRET_KEY", "test-secret")

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import motoboy_convite_routes as routes
from auth import verify_password
from models import Motoboy, MotoboyCadastroConvite, MotoboySubBase, User
from motoboy_cadastro_service import criar_motoboy, resolver_permissoes_motoboy
from validators_br import is_cnpj_valido, is_cpf_valido, normalizar_celular

CPF_OK = "52998224725"
CNPJ_OK = "11222333000181"


def _payload(**over):
    base = dict(
        nome="joão",
        sobrenome="da silva",
        documento="529.982.247-25",
        data_nascimento="1990-05-20",
        contato="(11) 98765-4321",
        email="",
        cnpj="",
        chave_pix="",
        cep="01310-100",
        rua="Av. Paulista",
        numero="1000",
        complemento="",
        bairro="Bela Vista",
        cidade="São Paulo",
        estado="sp",
        username="joao.silva",
        senha="moto2026!",
        confirmar_senha="moto2026!",
        aceite_privacidade=True,
        website="",
    )
    base.update(over)
    return routes.CadastroMotoboyIn(**base)


def _field(exc: HTTPException) -> str:
    return exc.detail["field"]


# ---------- validadores BR ----------

def test_cpf_valido_e_invalido():
    assert is_cpf_valido(CPF_OK)
    assert is_cpf_valido("529.982.247-25")
    assert not is_cpf_valido("52998224724")
    assert not is_cpf_valido("11111111111")
    assert not is_cpf_valido("123")


def test_cnpj_valido_e_invalido():
    assert is_cnpj_valido(CNPJ_OK)
    assert not is_cnpj_valido("11222333000182")
    assert not is_cnpj_valido("00000000000000")


def test_celular_exige_11_digitos_com_9():
    assert normalizar_celular("(11) 98765-4321") == "11987654321"
    assert normalizar_celular("+55 11 98765-4321") == "11987654321"
    assert normalizar_celular("(11) 3333-4444") is None  # fixo
    assert normalizar_celular("(00) 98765-4321") is None  # DDD inválido


# ---------- validar_cadastro ----------

def test_payload_valido_normaliza_dados():
    d = routes.validar_cadastro(_payload(), hoje=date(2026, 10, 11))
    assert d["documento"] == CPF_OK
    assert d["contato"] == "11987654321"
    assert d["cep"] == "01310100"
    assert d["estado"] == "SP"
    assert d["data_nascimento"] == date(1990, 5, 20)
    assert d["email"] is None and d["cnpj"] is None


@pytest.mark.parametrize(
    "over,campo",
    [
        ({"nome": ""}, "nome"),
        ({"documento": "52998224724"}, "documento"),
        ({"data_nascimento": "2030-01-01"}, "data_nascimento"),
        ({"data_nascimento": "abc"}, "data_nascimento"),
        ({"contato": "(11) 3333-4444"}, "contato"),
        ({"email": "sem-arroba"}, "email"),
        ({"cnpj": "11222333000182"}, "cnpj"),
        ({"cep": "123"}, "cep"),
        ({"numero": ""}, "numero"),
        ({"estado": "XX"}, "estado"),
        ({"username": "Jo"}, "username"),
        ({"username": "joão silva"}, "username"),
        ({"senha": "123", "confirmar_senha": "123"}, "senha"),
        ({"senha": "123456", "confirmar_senha": "123456"}, "senha"),
        ({"senha": "000000", "confirmar_senha": "000000"}, "senha"),
        ({"senha": "joao.silva", "confirmar_senha": "joao.silva"}, "senha"),
        ({"confirmar_senha": "outra-senha"}, "confirmar_senha"),
        ({"aceite_privacidade": False}, "aceite_privacidade"),
        ({"aceite_privacidade": None}, "aceite_privacidade"),
    ],
)
def test_erros_de_validacao_por_campo(over, campo):
    with pytest.raises(HTTPException) as exc:
        routes.validar_cadastro(_payload(**over), hoje=date(2026, 10, 11))
    assert exc.value.status_code == 422
    assert _field(exc.value) == campo
    assert exc.value.detail["message"]


def test_data_em_formato_brasileiro_aceita():
    d = routes.validar_cadastro(_payload(data_nascimento="20/05/1990"), hoje=date(2026, 10, 11))
    assert d["data_nascimento"] == date(1990, 5, 20)


def test_username_maiusculo_e_normalizado():
    d = routes.validar_cadastro(_payload(username="Joao.Silva"), hoje=date(2026, 10, 11))
    assert d["username"] == "joao.silva"


# ---------- status do convite ----------

AGORA = datetime(2026, 10, 11, 12, 0)


def _convite(**over):
    base = dict(id=1, sub_base="SB1", token="tk", ativo=True, expira_em=AGORA + timedelta(days=3), limite_usos=20)
    base.update(over)
    return SimpleNamespace(**base)


def test_status_convite():
    owner = SimpleNamespace(ativo=True)
    assert routes.status_convite(_convite(), owner, 0, AGORA) is None
    assert routes.status_convite(None, owner, 0, AGORA) == "invalido"
    assert routes.status_convite(_convite(ativo=False), owner, 0, AGORA) == "invalido"
    assert routes.status_convite(_convite(expira_em=AGORA - timedelta(seconds=1)), owner, 0, AGORA) == "expirado"
    assert routes.status_convite(_convite(limite_usos=2), owner, 2, AGORA) == "esgotado"
    assert routes.status_convite(_convite(limite_usos=None), owner, 999, AGORA) is None
    assert routes.status_convite(_convite(), SimpleNamespace(ativo=False), 0, AGORA) == "owner_inativo"
    assert routes.status_convite(_convite(), None, 0, AGORA) == "owner_inativo"


# ---------- IP / rate limit / sugestões ----------

def test_client_ip_com_e_sem_forwarded():
    req = SimpleNamespace(headers={"x-forwarded-for": "200.1.2.3, 10.0.0.1"}, client=SimpleNamespace(host="10.0.0.1"))
    assert routes._client_ip(req) == "200.1.2.3"
    req2 = SimpleNamespace(headers={}, client=SimpleNamespace(host="10.0.0.9"))
    assert routes._client_ip(req2) == "10.0.0.9"


def test_limiter_em_memoria():
    lim = routes._JanelaLimiter(2, 60.0)
    assert lim.permitir("ip", 0.0)
    assert lim.permitir("ip", 1.0)
    assert not lim.permitir("ip", 2.0)
    assert lim.permitir("outro", 2.0)
    assert lim.permitir("ip", 70.0)


def test_sugestoes_ignoram_usernames_em_uso():
    em_uso = {"joao.silva", "joao.silva1"}
    sug = routes.sugerir_usernames("joao.silva", lambda c: c in em_uso)
    assert sug == ["joao.silva2", "joao.silva3", "joao.silva4"]


def test_senha_trivial():
    for s in ("123456", "654321", "aaaaaa", "234567", "senha123"):
        assert routes.senha_trivial(s)
    assert not routes.senha_trivial("moto2026!")


# ---------- endpoint de cadastro (sessão fake) ----------

class FakeDB:
    def __init__(self, scalars=None):
        self.scalars_queue = list(scalars or [])
        self.added = []
        self.executed = []
        self.committed = False
        self.rolled_back = False
        self._next_id = 100

    def scalar(self, _stmt):
        return self.scalars_queue.pop(0) if self.scalars_queue else None

    def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params))

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        for obj in self.added:
            if isinstance(obj, User) and obj.id is None:
                obj.id = self._next_id
                self._next_id += 1
            if isinstance(obj, Motoboy) and obj.id_motoboy is None:
                obj.id_motoboy = self._next_id
                self._next_id += 1

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


OWNER = SimpleNamespace(
    ativo=True,
    sub_base="SB1",
    nome_fantasia="Express Entregas",
    username="express",
    ignorar_coleta=False,
    default_pode_realizar_coleta=False,
    default_pode_ler_saida=True,
    default_pode_digitar_codigo_manual=False,
    default_pode_lancar_avulso=True,
    default_pode_criar_avulso_coleta=True,
    default_pode_criar_avulso_saida=False,
    default_avulso_exige_foto=True,
)

REQ = SimpleNamespace(headers={"x-forwarded-for": "200.1.2.3"}, client=SimpleNamespace(host="10.0.0.1"))


@pytest.fixture
def convite_valido(monkeypatch):
    convite = _convite(expira_em=routes._now() + timedelta(days=3))
    monkeypatch.setattr(routes, "_carregar_convite", lambda db, token, travar=False: convite if token == "tk" else None)
    monkeypatch.setattr(routes, "_owner_da_sub_base", lambda db, sb: OWNER if sb == "SB1" else None)
    monkeypatch.setattr(routes, "_usos", lambda db, cid: 0)
    monkeypatch.setattr(routes, "_username_em_uso", lambda db, u: u == "ocupado")
    return convite


def test_cadastro_cria_motoboy_ativo_na_sub_base_do_convite(convite_valido):
    # scalars: rate-limit count, contato existente, cpf existente
    db = FakeDB(scalars=[0, None, None])
    out = routes.cadastrar_motoboy("tk", _payload(), REQ, db)

    assert out == {"username": "joao.silva", "nome": "João", "nome_owner": "Express Entregas"}
    assert db.committed
    user = next(o for o in db.added if isinstance(o, User))
    assert user.sub_base == "SB1" and user.role == 4 and user.status is True
    assert user.must_change_password is False
    assert user.password_hash != "moto2026!" and verify_password("moto2026!", user.password_hash)
    assert user.contato == "11987654321"
    moto = next(o for o in db.added if isinstance(o, Motoboy))
    assert moto.sub_base == "SB1" and moto.documento == CPF_OK and moto.ativo is True
    sb = next(o for o in db.added if isinstance(o, MotoboySubBase))
    assert sb.sub_base == "SB1" and sb.ativo is True
    log = next(o for o in db.added if isinstance(o, MotoboyCadastroConvite))
    assert log.ip_origem == "200.1.2.3" and log.aceite_privacidade_em is not None
    assert any("pg_advisory_xact_lock" in sql for sql, _ in db.executed)


def test_cadastro_username_em_uso_retorna_409_com_sugestoes(convite_valido):
    db = FakeDB(scalars=[0])
    with pytest.raises(HTTPException) as exc:
        routes.cadastrar_motoboy("tk", _payload(username="ocupado"), REQ, db)
    assert exc.value.status_code == 409
    assert _field(exc.value) == "username"
    assert exc.value.detail["sugestoes"]
    assert db.rolled_back and not db.committed


def test_cadastro_contato_duplicado_na_sub_base(convite_valido):
    db = FakeDB(scalars=[0, 123])
    with pytest.raises(HTTPException) as exc:
        routes.cadastrar_motoboy("tk", _payload(), REQ, db)
    assert exc.value.status_code == 409 and _field(exc.value) == "contato"


def test_cadastro_cpf_duplicado_na_sub_base(convite_valido):
    db = FakeDB(scalars=[0, None, 77])
    with pytest.raises(HTTPException) as exc:
        routes.cadastrar_motoboy("tk", _payload(), REQ, db)
    assert exc.value.status_code == 409 and _field(exc.value) == "documento"


def test_cadastro_rate_limit_por_ip(convite_valido):
    db = FakeDB(scalars=[routes.CADASTROS_POR_HORA_POR_IP])
    with pytest.raises(HTTPException) as exc:
        routes.cadastrar_motoboy("tk", _payload(), REQ, db)
    assert exc.value.status_code == 429


def test_cadastro_convite_invalido_retorna_410(convite_valido):
    with pytest.raises(HTTPException) as exc:
        routes.cadastrar_motoboy("outro-token", _payload(), REQ, FakeDB())
    assert exc.value.status_code == 410
    assert exc.value.detail["motivo"] == "invalido"


def test_cadastro_convite_esgotado(convite_valido, monkeypatch):
    monkeypatch.setattr(routes, "_usos", lambda db, cid: 20)
    with pytest.raises(HTTPException) as exc:
        routes.cadastrar_motoboy("tk", _payload(), REQ, FakeDB())
    assert exc.value.status_code == 410 and exc.value.detail["motivo"] == "esgotado"


def test_honeypot_nao_cria_nada(convite_valido):
    db = FakeDB()
    routes.cadastrar_motoboy("tk", _payload(website="http://spam"), REQ, db)
    assert not db.added and not db.committed


def test_username_disponivel_exige_convite_valido(convite_valido):
    with pytest.raises(HTTPException) as exc:
        routes.username_disponivel("outro-token", REQ, u="joao", db=FakeDB())
    assert exc.value.status_code == 404
    assert routes.username_disponivel("tk", REQ, u="joao.novo", db=FakeDB())["disponivel"] is True
    ocupado = routes.username_disponivel("tk", REQ, u="ocupado", db=FakeDB())
    assert ocupado["disponivel"] is False and ocupado["sugestoes"]


def test_consulta_convite_nao_expoe_sub_base(convite_valido):
    out = routes.consultar_convite("tk", FakeDB())
    assert out == {"valido": True, "nome_owner": "Express Entregas"}
    assert routes.consultar_convite("x", FakeDB())["valido"] is False


def test_admin_exige_role_admin():
    with pytest.raises(HTTPException) as exc:
        routes._admin_sub_base(SimpleNamespace(role=4, sub_base="SB1"))
    assert exc.value.status_code == 403
    assert routes._admin_sub_base(SimpleNamespace(role=1, sub_base="SB1")) == "SB1"


# ---------- helper compartilhado (regressão do POST /users) ----------

def test_permissoes_seguem_defaults_do_owner():
    p = resolver_permissoes_motoboy(OWNER)
    assert p == {
        "pode_ler_coleta": False,
        "pode_realizar_coleta": False,
        "pode_ler_saida": True,
        "pode_digitar_codigo_manual": False,
        "pode_lancar_avulso": True,
        "pode_criar_avulso_coleta": True,
        "pode_criar_avulso_saida": False,
        "avulso_exige_foto": True,
    }


def test_permissoes_overrides_e_ignorar_coleta():
    owner = SimpleNamespace(**{**vars(OWNER), "ignorar_coleta": True})
    p = resolver_permissoes_motoboy(owner, {"pode_ler_coleta": True, "pode_lancar_avulso": False})
    assert p["pode_ler_coleta"] is False and p["pode_realizar_coleta"] is False
    assert p["pode_lancar_avulso"] is False and p["avulso_exige_foto"] is False


def test_criar_motoboy_nao_faz_commit():
    db = FakeDB()
    user = criar_motoboy(
        db,
        owner=OWNER,
        sub_base="SB1",
        username="x.y",
        password_hash="h",
        contato="11987654321",
        nome="ana",
        sobrenome="lima",
        email=None,
        data_nascimento=None,
        must_change_password=True,
        dados={"rua": "R", "numero": "1", "bairro": "B", "cidade": "C", "cep": "01310100"},
    )
    assert user.nome == "Ana" and user.must_change_password is True
    assert not db.committed
    assert sum(isinstance(o, MotoboySubBase) for o in db.added) == 1
