"""Piso de versão do app: 1.11.0 para todos; 1.12.0 para root, admin e operador."""
from mobile_app_version import (
    is_update_required,
    load_policy,
    parse_role_minimums,
    public_policy_payload,
    required_version,
    version_lt,
)


def test_comparacao_semver():
    assert version_lt("1.10.0", "1.11.0")
    assert version_lt("1.11.0", "1.12.0")
    assert not version_lt("1.11.0", "1.11.0")
    assert not version_lt("1.12.0", "1.11.0")
    assert not version_lt("1.12", "1.12.0")
    assert not version_lt("lixo", "1.11.0")


def test_piso_padrao_por_perfil(monkeypatch):
    monkeypatch.delenv("MOBILE_MIN_VERSION", raising=False)
    monkeypatch.delenv("MOBILE_MIN_VERSION_ROLES", raising=False)
    policy = load_policy()

    assert required_version(policy, None) == "1.11.0"
    assert required_version(policy, 4) == "1.11.0"
    assert required_version(policy, 3) == "1.11.0"
    assert required_version(policy, 0) == "1.12.0"
    assert required_version(policy, 1) == "1.12.0"
    assert required_version(policy, 2) == "1.12.0"

    assert is_update_required("1.10.0", policy, 4)
    assert not is_update_required("1.11.0", policy, 4)
    assert not is_update_required("1.12.0", policy, 4)
    assert is_update_required("1.11.0", policy, 2)
    assert not is_update_required("1.12.0", policy, 1)
    assert not is_update_required("", policy, 1)


def test_antes_do_login_nao_aplica_piso_de_admin(monkeypatch):
    monkeypatch.delenv("MOBILE_MIN_VERSION", raising=False)
    monkeypatch.delenv("MOBILE_MIN_VERSION_ROLES", raising=False)
    policy = load_policy()
    assert not is_update_required("1.11.0", policy, None)
    assert is_update_required("1.11.0", policy, 1)


def test_env_substitui_pisos(monkeypatch):
    monkeypatch.setenv("MOBILE_MIN_VERSION", "1.13.0")
    monkeypatch.setenv("MOBILE_MIN_VERSION_ROLES", "motoboy:1.14.0, admin:1.15.0")
    policy = load_policy()
    assert required_version(policy, 4) == "1.14.0"
    assert required_version(policy, 1) == "1.15.0"
    assert required_version(policy, 2) == "1.13.0"


def test_roles_vazio_remove_pisos_por_perfil(monkeypatch):
    monkeypatch.setenv("MOBILE_MIN_VERSION_ROLES", "")
    policy = load_policy()
    assert policy.min_version_by_role == {}
    assert required_version(policy, 1) == policy.min_version


def test_parse_ignora_entrada_invalida():
    parsed = parse_role_minimums("9:1.2.0, operador:abc, 2:1.12.0")
    assert parsed == {9: "1.2.0", 2: "1.12.0"}


def test_payload_publico_nao_expoe_tenant(monkeypatch):
    monkeypatch.delenv("MOBILE_MIN_VERSION", raising=False)
    monkeypatch.delenv("MOBILE_MIN_VERSION_ROLES", raising=False)
    payload = public_policy_payload()
    assert payload["min_version"] == "1.11.0"
    assert "3" not in payload["min_version_by_role"]
    assert "4" not in payload["min_version_by_role"]
    assert payload["min_version_by_role"]["1"] == "1.12.0"
    assert "owner" not in payload
    assert "sub_base" not in payload
    assert payload["store_url"].endswith("br.com.trackingsaidas.mobile")
