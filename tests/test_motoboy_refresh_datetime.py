"""Normalização de datetime no refresh do motoboy."""

from datetime import datetime, timezone, timedelta

from auth import _as_naive_utc


def test_as_naive_utc_mantem_naive():
    dt = datetime(2026, 9, 21, 12, 0, 0)
    assert _as_naive_utc(dt) == dt


def test_as_naive_utc_converte_aware_para_naive_utc():
    aware = datetime(2026, 9, 21, 15, 0, 0, tzinfo=timezone(timedelta(hours=-3)))
    naive = _as_naive_utc(aware)
    assert naive.tzinfo is None
    assert naive == datetime(2026, 9, 21, 18, 0, 0)


def test_as_naive_utc_none():
    assert _as_naive_utc(None) is None


def test_comparacao_expires_at_aware_com_utcnow_nao_quebra():
    now = datetime.utcnow()
    expires_aware = datetime.now(timezone.utc) + timedelta(days=1)
    expires = _as_naive_utc(expires_aware)
    assert expires is not None
    assert expires > now
