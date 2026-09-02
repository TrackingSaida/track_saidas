import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from ml_int_service import (
    invalidate_ml_conexao,
    ml_conexao_status,
    ml_token_precisa_renovar,
    refresh_all_ml_int_tokens,
    refresh_ml_int_token,
)


def _ml_conexao(**kwargs):
    defaults = {
        "user_id_ml": 123,
        "sub_base": "TEST",
        "refresh_token": "rt-abc",
        "access_token": "at-abc",
        "expires_at": datetime.utcnow() + timedelta(hours=2),
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_ml_nao_renova_token_ainda_valido():
    conexao = _ml_conexao(expires_at=datetime.utcnow() + timedelta(hours=2))
    assert ml_token_precisa_renovar(conexao) is False


def test_ml_renova_token_expirado():
    conexao = _ml_conexao(expires_at=datetime.utcnow() - timedelta(minutes=1))
    assert ml_token_precisa_renovar(conexao) is True


def test_ml_renova_dentro_do_buffer():
    conexao = _ml_conexao(expires_at=datetime.utcnow() + timedelta(minutes=2))
    assert ml_token_precisa_renovar(conexao, buffer_seconds=300) is True


def test_ml_sem_refresh_token_nao_renova():
    conexao = _ml_conexao(refresh_token="", expires_at=datetime.utcnow() - timedelta(hours=1))
    assert ml_token_precisa_renovar(conexao) is False


def test_ml_status_conectado_com_refresh_expirado():
    conexao = _ml_conexao(expires_at=datetime.utcnow() - timedelta(hours=1))
    assert ml_conexao_status(conexao) == "conectado"


def test_ml_status_requer_reautorizacao():
    conexao = _ml_conexao(refresh_token="")
    assert ml_conexao_status(conexao) == "requer_reautorizacao"


@patch("ml_int_service.refresh_ml_int_token")
def test_refresh_all_pula_tokens_validos(mock_refresh):
    valida = _ml_conexao()
    expirada = _ml_conexao(expires_at=datetime.utcnow() - timedelta(minutes=10))

    db = MagicMock()
    db.query.return_value.all.return_value = [valida, expirada]
    mock_refresh.return_value = expirada

    stats = refresh_all_ml_int_tokens(db)

    assert stats["total"] == 2
    assert stats["refreshed"] == 1
    assert stats["skipped_valid"] == 1
    assert stats["failed"] == 0
    mock_refresh.assert_called_once_with(db, expirada)


@patch("ml_int_service.refresh_ml_int_token")
def test_refresh_all_pula_conexoes_invalidas(mock_refresh):
    invalida = _ml_conexao(refresh_token="", expires_at=datetime.utcnow() - timedelta(hours=1))
    db = MagicMock()
    db.query.return_value.all.return_value = [invalida]

    stats = refresh_all_ml_int_tokens(db)

    assert stats["skipped_invalid"] == 1
    assert stats["refreshed"] == 0
    mock_refresh.assert_not_called()


@patch("ml_int_service.requests.post")
def test_refresh_ml_int_token_pula_se_ainda_valido(mock_post):
    conexao = _ml_conexao(expires_at=datetime.utcnow() + timedelta(hours=3))
    db = MagicMock()

    result = refresh_ml_int_token(db, conexao)

    assert result is conexao
    mock_post.assert_not_called()


@patch("ml_int_service.requests.post")
def test_refresh_invalid_grant_invalida_conexao(mock_post):
    conexao = _ml_conexao(expires_at=datetime.utcnow() - timedelta(minutes=1))
    db = MagicMock()
    resp = MagicMock()
    resp.status_code = 400
    resp.text = '{"error":"invalid_grant"}'
    resp.json.return_value = {
        "error": "invalid_grant",
        "error_description": "Error validating grant. Your authorization code or refresh token may be expired or it was already used",
    }
    mock_post.return_value = resp

    with patch.dict(os.environ, {
        "ML_CLIENT_ID": "id",
        "ML_CLIENT_SECRET": "secret",
        "ML_REDIRECT_URI": "https://example.com/callback",
    }):
        result = refresh_ml_int_token(db, conexao)

    assert result is None
    assert conexao.refresh_token == ""
    assert ml_conexao_status(conexao) == "requer_reautorizacao"
    db.commit.assert_called()


def test_invalidate_ml_conexao_limpa_refresh():
    conexao = _ml_conexao()
    db = MagicMock()
    invalidate_ml_conexao(db, conexao, reason="test")
    assert conexao.refresh_token == ""
    assert ml_conexao_status(conexao) == "requer_reautorizacao"
