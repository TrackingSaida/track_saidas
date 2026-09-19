"""Testes unitários Avulso Identificável (campos + labels + gate seleção)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from avulso_campos_service import (
    build_label_amigavel,
    normalize_contexto_avulso,
    normalize_tipo_campo,
    origem_amigavel,
    validate_campos_payload,
    _slug_chave,
)


def test_slug_chave():
    assert _slug_chave("Número do Pedido") == "numero_do_pedido"
    assert _slug_chave("  Destinatário ") == "destinatario"


def test_normalize_contexto_ok():
    assert normalize_contexto_avulso("coleta_avulso") == "COLETA_AVULSO"


def test_normalize_contexto_invalido():
    with pytest.raises(HTTPException) as exc:
        normalize_contexto_avulso("ENTREGUE")
    assert exc.value.status_code == 422


def test_validate_obrigatorio_faltando():
    cfg = [
        SimpleNamespace(
            chave="pedido",
            label="Pedido",
            tipo="texto",
            obrigatorio=True,
            opcoes_json=None,
        )
    ]
    with pytest.raises(HTTPException) as exc:
        validate_campos_payload(cfg, {})
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "CAMPOS_AVULSO_OBRIGATORIOS"


def test_validate_sem_config_aceita_legado():
    out = validate_campos_payload([], {}, identificacao_legado="Cliente X")
    assert out["identificacao"] == "Cliente X"


def test_validate_ok():
    cfg = [
        SimpleNamespace(
            chave="pedido",
            label="Pedido",
            tipo="texto",
            obrigatorio=True,
            opcoes_json=None,
        )
    ]
    out = validate_campos_payload(cfg, {"pedido": "99821"})
    assert out == {"pedido": "99821"}


def test_label_amigavel_com_identificacao():
    cfg = [
        SimpleNamespace(
            chave="pedido",
            label="Pedido",
            usar_na_identificacao=True,
            exibir_na_selecao=True,
        ),
        SimpleNamespace(
            chave="dest",
            label="Dest",
            usar_na_identificacao=True,
            exibir_na_selecao=True,
        ),
    ]
    label = build_label_amigavel(
        "AVULSO-000184",
        campos_cfg=cfg,
        valores={"pedido": "99821", "dest": "Maria"},
    )
    assert "AVULSO-000184" in label
    assert "99821" in label
    assert "Maria" in label


def test_tipo_lista_invalido():
    cfg = [
        SimpleNamespace(
            chave="tipo",
            label="Tipo",
            tipo="lista",
            obrigatorio=False,
            opcoes_json='["A","B"]',
        )
    ]
    with pytest.raises(HTTPException):
        validate_campos_payload(cfg, {"tipo": "C"})


def test_normalize_tipo():
    assert normalize_tipo_campo("Telefone") == "telefone"


def test_owner_exige_selecao_coleta_on():
    from avulso_campos_service import owner_exige_selecao_avulso

    class FakeScalar:
        def __init__(self, owner):
            self.owner = owner

        def scalar(self, *_a, **_k):
            return self.owner

    owner = SimpleNamespace(ignorar_coleta=False, entrada_obrigatoria_habilitada=False)
    assert owner_exige_selecao_avulso(FakeScalar(owner), "base1") is True


def test_owner_exige_selecao_ambos_off():
    from avulso_campos_service import owner_exige_selecao_avulso

    class FakeScalar:
        def __init__(self, owner):
            self.owner = owner

        def scalar(self, *_a, **_k):
            return self.owner

    owner = SimpleNamespace(ignorar_coleta=True, entrada_obrigatoria_habilitada=False)
    assert owner_exige_selecao_avulso(FakeScalar(owner), "base1") is False


def test_owner_exige_selecao_entrada_on():
    from avulso_campos_service import owner_exige_selecao_avulso

    class FakeScalar:
        def __init__(self, owner):
            self.owner = owner

        def scalar(self, *_a, **_k):
            return self.owner

    owner = SimpleNamespace(ignorar_coleta=True, entrada_obrigatoria_habilitada=True)
    assert owner_exige_selecao_avulso(FakeScalar(owner), "base1") is True


def test_label_legado_sem_eav():
    label = build_label_amigavel("AVULSO-000001", base_legado="Cliente X", campos_cfg=[], valores={})
    assert "AVULSO-000001" in label
    assert "Cliente X" in label


def test_label_legado_usa_base():
    label = build_label_amigavel(
        "AVULSO-000001",
        base_legado="Cliente X",
        campos_cfg=[],
        valores={},
    )
    assert "AVULSO-000001" in label
    assert "Cliente X" in label


def test_label_ignora_campo_sem_flag():
    cfg = [
        SimpleNamespace(
            chave="interno",
            label="Interno",
            usar_na_identificacao=False,
            exibir_na_selecao=False,
        )
    ]
    label = build_label_amigavel("AVULSO-1", campos_cfg=cfg, valores={"interno": "segredo"})
    assert "segredo" not in label


class _FakeDb:
    def __init__(self, owner):
        self._owner = owner

    def scalar(self, _q):
        return self._owner


def test_owner_exige_selecao_coleta_on():
    from avulso_campos_service import owner_exige_selecao_avulso

    db = _FakeDb(SimpleNamespace(ignorar_coleta=False, entrada_obrigatoria_habilitada=False))
    assert owner_exige_selecao_avulso(db, "base-a") is True


def test_owner_exige_selecao_ambos_off():
    from avulso_campos_service import owner_exige_selecao_avulso

    db = _FakeDb(SimpleNamespace(ignorar_coleta=True, entrada_obrigatoria_habilitada=False))
    assert owner_exige_selecao_avulso(db, "base-a") is False


def test_owner_exige_selecao_entrada_on():
    from avulso_campos_service import owner_exige_selecao_avulso

    db = _FakeDb(SimpleNamespace(ignorar_coleta=True, entrada_obrigatoria_habilitada=True))
    assert owner_exige_selecao_avulso(db, "base-a") is True


def test_owner_exige_selecao_sem_owner():
    from avulso_campos_service import owner_exige_selecao_avulso

    assert owner_exige_selecao_avulso(_FakeDb(None), "base-a") is False


def test_origem_amigavel():
    assert origem_amigavel("coleta") == "Coleta"
    assert origem_amigavel("entrada") == "Entrada"
    assert origem_amigavel(None, excepcional=True) == "Cadastro excepcional na saída"
    assert origem_amigavel("saida_excecao") == "Cadastro excepcional na saída"


def test_tipos_novos_e_labels():
    from avulso_campos_service import (
        TIPOS_META,
        format_valor_tipo,
        normalize_tipo_campo,
        status_avulso_label,
        tipo_meta,
        contexto_meta,
    )

    ids = {t["id"] for t in TIPOS_META}
    assert "primeiro_nome" not in ids
    assert "segundo_nome" not in ids
    assert "foto" not in ids
    assert "texto" in ids
    assert "lista" in ids
    assert normalize_tipo_campo("foto") == "foto"
    assert tipo_meta("foto")["label"] == "Foto"
    assert normalize_tipo_campo("CEP") == "cep"
    assert normalize_tipo_campo("primeiro_nome") == "primeiro_nome"
    assert tipo_meta("cep")["label"] == "CEP"
    assert tipo_meta("primeiro_nome")["label"] == "Texto"
    assert contexto_meta("COLETA_AVULSO")["label"] == "Coleta"
    assert "Coleta" in contexto_meta("TODOS_AVULSO")["badges"]
    assert format_valor_tipo("cep", "01310100", "CEP") == "01310-100"
    assert format_valor_tipo("primeiro_nome", "maria", "Primeiro nome") == "Maria"
    assert format_valor_tipo("segundo_nome", "silva", "Segundo nome") == "Silva"
    assert format_valor_tipo("telefone", "11988887777", "Telefone") == "(11) 98888-7777"
    assert status_avulso_label("NA_BASE") == "Na base"
    assert status_avulso_label("coletado") == "Coletado"
    with pytest.raises(HTTPException):
        format_valor_tipo("cep", "123", "CEP")
    with pytest.raises(HTTPException):
        format_valor_tipo("primeiro_nome", "Maria2", "Primeiro nome")
