"""Validação e allowlist do batch de telemetria mobile (PRD-018)."""
from mobile_telemetry_policy import (
    ALLOWED_EVENT_NAMES,
    is_allowed_event_name,
    sanitize_props,
    telemetry_sub_base_allowlist,
)


def test_eventos_permitidos_incluem_funil_e_device():
    assert "baixa_confirm_tap" in ALLOWED_EVENT_NAMES
    assert "outbox_sync_stage" in ALLOWED_EVENT_NAMES
    assert "device_snapshot" in ALLOWED_EVENT_NAMES


def test_is_allowed_event_name():
    assert is_allowed_event_name("baixa_confirm_tap")
    assert not is_allowed_event_name("hack_event")


def test_sanitize_props_so_whitelist():
    props = sanitize_props(
        {"kind": "entregue", "secret": "x", "photo_count": 2}
    )
    assert props == {"kind": "entregue", "photo_count": 2}
    assert sanitize_props({"secret": 1}) is None


def test_allowlist_padrao_gg_flex(monkeypatch):
    monkeypatch.delenv("MOBILE_TELEMETRY_SUB_BASES", raising=False)
    assert telemetry_sub_base_allowlist() == {"GG FLEX"}


def test_allowlist_env(monkeypatch):
    monkeypatch.setenv("MOBILE_TELEMETRY_SUB_BASES", "GG FLEX, Outra Base")
    assert telemetry_sub_base_allowlist() == {"GG FLEX", "Outra Base"}
