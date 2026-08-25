from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import coletas


def _user(**overrides):
    values = {
        "sub_base": "SB-01",
        "username": "operador",
        "role": 2,
        "ignorar_coleta": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_lancar_avulso_coleta_reutiliza_lote_e_retorna_codigos(monkeypatch):
    generated = iter(["AVULSO-CLIENTE-000001", "AVULSO-CLIENTE-000002"])
    monkeypatch.setattr(coletas, "_gerar_codigo_avulso", lambda _db, _label: next(generated))

    captured = {}

    def fake_lote(payload, db, current_user):
        captured["payload"] = payload
        return coletas.LoteResponse(
            coleta=coletas.ColetaOut(
                id_coleta=10,
                timestamp="2026-08-14T10:00:00",
                base=payload.base,
                sub_base=current_user.sub_base,
                username_entregador=current_user.username,
                shopee=0,
                mercado_livre=0,
                avulso=2,
                valor_total="20.00",
            ),
            resumo=coletas.ResumoLote(
                inseridos=2,
                duplicados=0,
                codigos_duplicados=[],
                contagem={"shopee": 0, "mercado_livre": 0, "avulso": 2},
                precos={"shopee": "0.00", "ml": "0.00", "avulso": "10.00"},
                total="20.00",
            ),
            saidas_criadas=[
                coletas.SaidaCriadaLote(codigo="AVULSO-CLIENTE-000001", id_saida=101),
                coletas.SaidaCriadaLote(codigo="AVULSO-CLIENTE-000002", id_saida=102),
            ],
        )

    monkeypatch.setattr(coletas, "registrar_coleta_em_lote", fake_lote)

    result = coletas.lancar_avulso_coleta(
        coletas.ColetaLancarAvulsoIn(base=" BASE-01 ", identificacao="Cliente", quantidade=2),
        db=object(),
        current_user=_user(),
    )

    assert result.quantidade_criada == 2
    assert result.codigos == ["AVULSO-CLIENTE-000001", "AVULSO-CLIENTE-000002"]
    assert result.coleta.sub_base == "SB-01"
    assert result.saidas[0].status == "coletado"
    assert captured["payload"].base == "BASE-01"
    assert [item.servico for item in captured["payload"].itens] == ["Avulso", "Avulso"]


@pytest.mark.parametrize(
    ("user", "status_code"),
    [
        (_user(sub_base=None), 422),
        (_user(role=4), 403),
        (_user(ignorar_coleta=True), 403),
    ],
)
def test_lancar_avulso_coleta_bloqueia_contexto_invalido(user, status_code):
    with pytest.raises(HTTPException) as exc:
        coletas.lancar_avulso_coleta(
            coletas.ColetaLancarAvulsoIn(base="BASE-01", quantidade=1),
            db=object(),
            current_user=user,
        )

    assert exc.value.status_code == status_code
