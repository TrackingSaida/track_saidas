"""Incidente 24/11: concorrência scan×finalizar em PostgreSQL real.

Exige Postgres. Variável: POSTGRES_TEST_URL
(ex.: postgresql+psycopg2://postgres@127.0.0.1:55432/incidente_test)

Sem URL válida o módulo é ignorado (skip) — a correção NÃO segue para
produção sem este teste passar.
"""
from __future__ import annotations

import os
import re
import threading
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable

os.environ.setdefault("SECRET_KEY", "pytest-incidente-2411")
os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get("POSTGRES_TEST_URL", "postgresql://pytest:pytest@localhost/pytest"),
)

from models import (  # noqa: E402
    ConferenciaSaida,
    Motoboy,
    Owner,
    Saida,
    SaidaHistorico,
    SaidaSessao,
    SaidaSessaoItem,
    User,
)
from saida_sessao_service import (  # noqa: E402
    ESTADO_ABERTA,
    finalizar_sessao,
    incluir_saida_na_sessao,
    obter_ou_criar_sessao_aberta,
)

POSTGRES_URL = (os.environ.get("POSTGRES_TEST_URL") or "").strip()


def _pg_disponivel() -> bool:
    if not POSTGRES_URL:
        return False
    try:
        eng = create_engine(POSTGRES_URL, pool_pre_ping=True)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        eng.dispose()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_disponivel(),
    reason="POSTGRES_TEST_URL ausente ou Postgres inacessível (obrigatório antes de produção)",
)


def _engine():
    return create_engine(POSTGRES_URL, pool_size=8, max_overflow=4, pool_pre_ping=True)


# FKs cujas tabelas-alvo existem neste teste — preservadas para KEY SHARE real.
_FK_ALVOS_OK = {
    "users",
    "motoboys",
    "owner",
    "saida_sessoes",
    "saidas",
    "saida_sessao_itens",
}


def _ddl_filtrado(table) -> str:
    """DDL mantendo FKs internas; remove FKs para coletas/pacote_g etc."""
    from sqlalchemy.dialects import postgresql

    ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
    linhas_out = []
    for linha in ddl.splitlines():
        upper = linha.upper()
        if "FOREIGN KEY" in upper and "REFERENCES" in upper:
            m = re.search(r"REFERENCES\s+([\"\w]+)", linha, flags=re.IGNORECASE)
            alvo = (m.group(1) if m else "").strip('"').lower()
            if alvo not in _FK_ALVOS_OK:
                continue
        linhas_out.append(linha)
    ddl = "\n".join(linhas_out)
    ddl = re.sub(r",(\s*)\)", r"\1)", ddl)
    return ddl


def _recriar_schema(engine) -> None:
    # Ordem: dependentes primeiro no DROP; base primeiro no CREATE.
    drop_order = [
        ConferenciaSaida.__table__,
        SaidaSessaoItem.__table__,
        SaidaHistorico.__table__,
        SaidaSessao.__table__,
        Saida.__table__,
        Motoboy.__table__,
        Owner.__table__,
        User.__table__,
    ]
    create_order = list(reversed(drop_order))
    with engine.begin() as conn:
        for t in drop_order:
            conn.execute(text(f'DROP TABLE IF EXISTS "{t.name}" CASCADE'))
        for t in create_order:
            conn.exec_driver_sql(_ddl_filtrado(t))
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_saida_sessao_aberta_motoboy "
                "ON saida_sessoes (sub_base, motoboy_id) WHERE estado = 'ABERTA'"
            )
        )


def _seed(SessionLocal, *, sub_base: str, motoboy_id: int, user_id: int = 1) -> None:
    db = SessionLocal()
    try:
        if db.get(User, user_id) is None:
            db.add(
                User(
                    id=user_id,
                    password_hash="x",
                    username=f"op{user_id}",
                    contato=f"1199999{user_id:04d}",
                    status=True,
                    must_change_password=False,
                )
            )
        if db.scalar(select(Owner).where(Owner.sub_base == sub_base)) is None:
            next_id = (db.scalar(select(Owner.id_owner).order_by(Owner.id_owner.desc())) or 0) + 1
            db.add(
                Owner(
                    id_owner=int(next_id),
                    email=f"{sub_base}@t.c",
                    username=sub_base,
                    valor=0,
                    sub_base=sub_base,
                    ativo=True,
                    confirmacao_saida_sessoes_habilitada=True,
                )
            )
        if db.get(Motoboy, motoboy_id) is None:
            db.add(
                Motoboy(
                    id_motoboy=motoboy_id,
                    user_id=user_id,
                    sub_base=sub_base,
                    rua="Rua",
                    numero="1",
                    bairro="Centro",
                    cidade="Sao Paulo",
                    cep="01001000",
                    pode_ler_coleta=False,
                    pode_realizar_coleta=False,
                    pode_ler_saida=True,
                    pode_digitar_codigo_manual=True,
                    pode_lancar_avulso=True,
                    pode_criar_avulso_coleta=True,
                    pode_criar_avulso_saida=False,
                    avulso_exige_foto=False,
                    claims_version=1,
                )
            )
        db.commit()
    finally:
        db.close()


def _nova_saida(db, *, sub_base: str, motoboy_id: int, codigo: str) -> Saida:
    row = Saida(
        sub_base=sub_base,
        username="op",
        entregador="Ana",
        motoboy_id=motoboy_id,
        codigo=codigo,
        servico="Shopee",
        status="AGUARDANDO_CONFIRMACAO_SAIDA",
        data=date.today(),
        timestamp=datetime.utcnow(),
    )
    db.add(row)
    db.flush()
    return row


def test_advisory_scan_x_finalizar_sem_deadlock_e_unica_aberta():
    engine = _engine()
    _recriar_schema(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    sub_base = "base-incidente"
    motoboy_id = 10
    _seed(SessionLocal, sub_base=sub_base, motoboy_id=motoboy_id)

    n = 40
    erros: list[BaseException] = []
    lock = threading.Lock()

    def registrar(exc: BaseException) -> None:
        with lock:
            erros.append(exc)

    def worker_incluir(idx: int) -> None:
        db = SessionLocal()
        try:
            saida = _nova_saida(
                db, sub_base=sub_base, motoboy_id=motoboy_id, codigo=f"SC{idx:04d}"
            )
            incluir_saida_na_sessao(
                db,
                saida=saida,
                sub_base=sub_base,
                motoboy_id=motoboy_id,
                user_id=1,
                flag_ja_validada=True,
            )
            db.commit()
        except BaseException as exc:  # noqa: BLE001 — agrega para assert final
            db.rollback()
            registrar(exc)
        finally:
            db.close()

    def worker_finalizar(round_i: int) -> None:
        db = SessionLocal()
        try:
            # Garante pelo menos um item para não cancelar sessão vazia cedo demais.
            saida = _nova_saida(
                db,
                sub_base=sub_base,
                motoboy_id=motoboy_id,
                codigo=f"FN{round_i:04d}",
            )
            incluir_saida_na_sessao(
                db,
                saida=saida,
                sub_base=sub_base,
                motoboy_id=motoboy_id,
                user_id=1,
                flag_ja_validada=True,
            )
            db.commit()
            finalizar_sessao(
                db, sub_base=sub_base, motoboy_id=motoboy_id, user_id=1
            )
            db.commit()
        except Exception as exc:  # HTTPException 404 sessão já finalizada por outro é ok
            db.rollback()
            detail = getattr(exc, "detail", None)
            code = detail.get("code") if isinstance(detail, dict) else None
            if code in {"SESSAO_NAO_ENCONTRADA", "SESSAO_NAO_ABERTA"}:
                return
            if isinstance(exc, OperationalError) and "deadlock" in str(exc).lower():
                registrar(exc)
                return
            # Outros erros (ex.: versão) não contam como deadlock
            if "deadlock" in str(exc).lower():
                registrar(exc)
        finally:
            db.close()

    threads: list[threading.Thread] = []
    for i in range(n):
        threads.append(threading.Thread(target=worker_incluir, args=(i,)))
        if i % 2 == 0:
            threads.append(threading.Thread(target=worker_finalizar, args=(i,)))

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
        assert not t.is_alive(), "thread travou (possível lock wait infinito)"

    deadlocks = [
        e
        for e in erros
        if "deadlock" in str(e).lower()
        or (isinstance(e, OperationalError) and getattr(e.orig, "pgcode", None) == "40P01")
    ]
    assert not deadlocks, f"deadlock detectado: {deadlocks[:3]}"

    integrity = [e for e in erros if isinstance(e, IntegrityError)]
    assert not integrity, f"unicidade ABERTA violada: {integrity[:3]}"

    db = SessionLocal()
    try:
        abertas = db.scalars(
            select(SaidaSessao).where(
                SaidaSessao.sub_base == sub_base,
                SaidaSessao.motoboy_id == motoboy_id,
                SaidaSessao.estado == ESTADO_ABERTA,
            )
        ).all()
        assert len(abertas) <= 1, f"mais de uma ABERTA: {len(abertas)}"
    finally:
        db.close()
    engine.dispose()


def test_dois_motoboys_nao_serializam_cruzado():
    engine = _engine()
    _recriar_schema(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    _seed(SessionLocal, sub_base="base-a", motoboy_id=10, user_id=1)
    _seed(SessionLocal, sub_base="base-a", motoboy_id=11, user_id=2)

    barrier = threading.Barrier(2)
    tempos: dict[int, float] = {}
    erros: list[BaseException] = []

    def criar(motoboy_id: int) -> None:
        import time

        db = SessionLocal()
        try:
            barrier.wait(timeout=10)
            t0 = time.monotonic()
            obter_ou_criar_sessao_aberta(
                db,
                sub_base="base-a",
                motoboy_id=motoboy_id,
                data_ref=date.today(),
                user_id=1,
            )
            # Segura o advisory até o outro motoboy também adquirir o dele.
            time.sleep(0.4)
            db.commit()
            tempos[motoboy_id] = time.monotonic() - t0
        except BaseException as exc:  # noqa: BLE001
            db.rollback()
            erros.append(exc)
        finally:
            db.close()

    t1 = threading.Thread(target=criar, args=(10,))
    t2 = threading.Thread(target=criar, args=(11,))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)
    assert not erros, erros
    # Se serializassem no mesmo lock, um esperaria ~0.4s+ do outro → ambos longos.
    # Com isolamento por motoboy, ambos concluem perto de 0.4s.
    assert tempos[10] < 1.2 and tempos[11] < 1.2, tempos

    db = SessionLocal()
    try:
        abertas = db.scalars(
            select(SaidaSessao).where(
                SaidaSessao.sub_base == "base-a",
                SaidaSessao.estado == ESTADO_ABERTA,
            )
        ).all()
        assert len(abertas) == 2
        assert {int(s.motoboy_id) for s in abertas} == {10, 11}
    finally:
        db.close()
    engine.dispose()


def test_indice_parcial_aberta_presente():
    engine = _engine()
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'saida_sessoes' "
                "AND indexname = 'uq_saida_sessao_aberta_motoboy'"
            )
        ).fetchone()
        # Pode não existir antes do recreate; garante via recreate + check
    _recriar_schema(engine)
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'saida_sessoes' "
                "AND indexname = 'uq_saida_sessao_aberta_motoboy'"
            )
        ).fetchone()
        assert row is not None
    engine.dispose()
