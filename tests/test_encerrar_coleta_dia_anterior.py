"""Dia passado não permanece Em coleta."""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://pytest:pytest@localhost/pytest")

from datetime import date, datetime, timedelta

from coleta_operacional_service import encerrar_coletas_dias_anteriores


HOJE = date(2026, 10, 26)
ONTEM = HOJE - timedelta(days=1)


class _Db:
    def __init__(self):
        self.deleted = []
        self.commits = 0

    def delete(self, obj):
        self.deleted.append(obj)

    def flush(self):
        return None

    def expire(self, obj, _attrs=None):
        obj.participantes = [item for item in obj.participantes if item not in self.deleted]

    def commit(self):
        self.commits += 1


def _participante(**overrides):
    base = {
        "id_participante": 1,
        "status": "em_coleta",
        "shopee": 0,
        "mercado_livre": 0,
        "avulso": 0,
        "sem_volume": False,
        "versao": 1,
        "atualizado_em": datetime(2026, 10, 2, 9, 0),
    }
    base.update(overrides)
    return type("P", (), base)()


def _execucao(*participantes, dia=ONTEM, status="em_coleta"):
    return type(
        "E",
        (),
        {
            "id_execucao": 10,
            "data_operacao": dia,
            "status": status,
            "participantes": list(participantes),
            "atualizado_em": datetime(2026, 10, 2, 9, 0),
        },
    )()


def test_dia_passado_com_volume_vira_coletada():
    participante = _participante(shopee=3)
    execucao = _execucao(participante)
    db = _Db()

    restantes = encerrar_coletas_dias_anteriores(db, [execucao], hoje=HOJE)

    assert restantes == [execucao]
    assert participante.status == "finalizado"
    assert execucao.status == "coletado"
    assert db.commits == 1
    assert db.deleted == []


def test_dia_passado_sem_volume_marcado_permanece_sem_volume():
    participante = _participante(sem_volume=True)
    execucao = _execucao(participante)
    db = _Db()

    encerrar_coletas_dias_anteriores(db, [execucao], hoje=HOJE)

    assert participante.status == "finalizado"
    assert execucao.status == "sem_volume"


def test_dia_passado_aberto_sem_lancamento_volta_a_pendente():
    participante = _participante()
    execucao = _execucao(participante)
    db = _Db()

    restantes = encerrar_coletas_dias_anteriores(db, [execucao], hoje=HOJE)

    assert restantes == []
    assert participante in db.deleted
    assert execucao in db.deleted
    assert db.commits == 1


def test_dia_de_hoje_nao_muda():
    participante = _participante(shopee=2, atualizado_em=datetime(2026, 10, 26, 8, 0))
    execucao = _execucao(participante, dia=HOJE)
    db = _Db()

    restantes = encerrar_coletas_dias_anteriores(db, [execucao], hoje=HOJE)

    assert restantes == [execucao]
    assert participante.status == "em_coleta"
    assert execucao.status == "em_coleta"
    assert db.commits == 0


def test_lancamento_retroativo_de_hoje_permanece_em_coleta():
    participante = _participante(shopee=1, atualizado_em=datetime(2026, 10, 26, 11, 30))
    execucao = _execucao(participante)
    db = _Db()

    encerrar_coletas_dias_anteriores(db, [execucao], hoje=HOJE)

    assert participante.status == "em_coleta"
    assert execucao.status == "em_coleta"
    assert db.commits == 0


def test_status_gravado_em_coleta_sem_participacao_aberta_recalcula():
    participante = _participante(status="finalizado", shopee=4)
    execucao = _execucao(participante, status="em_coleta")
    db = _Db()

    encerrar_coletas_dias_anteriores(db, [execucao], hoje=HOJE)

    assert execucao.status == "coletado"
    assert db.commits == 1
