from fechamento_extrato_pure import (
    CODIGO_COLETA_SEM_CODIGO,
    calcular_divergencia,
    montar_linhas_extrato,
    montar_mensagem_divergencia,
    totais_de_itens_fechamento,
)


def test_extrato_so_codigos_bate_com_fechamento():
    saidas = [
        {"data": "2026-09-17", "codigo": "BR1", "servico_tipo": "ml"},
        {"data": "2026-09-17", "codigo": "BR2", "servico_tipo": "ml"},
        {"data": "2026-09-18", "codigo": "SP1", "servico_tipo": "shopee"},
    ]
    volumes = {
        "2026-09-17": {"shopee": 0, "ml": 2, "avulso": 0},
        "2026-09-18": {"shopee": 1, "ml": 0, "avulso": 0},
    }
    linhas, totais, sem_codigo, saldo = montar_linhas_extrato(saidas=saidas, volumes_coleta=volumes)
    assert len(linhas) == 3
    assert all(l["origem"] == "saida" for l in linhas)
    assert totais == {"shopee": 1, "ml": 2, "avulso": 0}
    assert sem_codigo == {"shopee": 0, "ml": 0, "avulso": 0}
    assert saldo == {}
    fech = totais_de_itens_fechamento(
        [
            {"data": "2026-09-17", "shopee": 0, "mercado_livre": 2, "avulso": 0},
            {"data": "2026-09-18", "shopee": 1, "mercado_livre": 0, "avulso": 0},
        ]
    )
    div = calcular_divergencia(fech, totais)
    assert div["divergente"] is False
    assert div["diferenca_total"] == 0


def test_extrato_coleta_sem_codigo_gera_linha_sintetica():
    saidas = [
        {"data": "2026-09-17", "codigo": "BR1", "servico_tipo": "ml"},
    ]
    volumes = {
        "2026-09-17": {"shopee": 0, "ml": 5, "avulso": 0},
    }
    linhas, totais, sem_codigo, saldo = montar_linhas_extrato(saidas=saidas, volumes_coleta=volumes)
    sint = [l for l in linhas if l["origem"] == "coleta_sem_codigo"]
    assert len(sint) == 1
    assert sint[0]["codigo"] == CODIGO_COLETA_SEM_CODIGO
    assert sint[0]["quantidade"] == 4
    assert sint[0]["servico"] == "Mercado Livre"
    assert totais["ml"] == 5
    assert sem_codigo["ml"] == 4
    assert saldo["2026-09-17"]["ml"] == 4


def test_extrato_mais_codigos_que_coleta_marca_divergencia():
    saidas = [
        {"data": "2026-09-17", "codigo": "A", "servico_tipo": "avulso"},
        {"data": "2026-09-17", "codigo": "B", "servico_tipo": "avulso"},
        {"data": "2026-09-17", "codigo": "C", "servico_tipo": "avulso"},
    ]
    volumes = {"2026-09-17": {"shopee": 0, "ml": 0, "avulso": 1}}
    linhas, totais, _, _ = montar_linhas_extrato(saidas=saidas, volumes_coleta=volumes)
    assert len(linhas) == 3
    assert totais["avulso"] == 3
    fech = {"shopee": 0, "ml": 0, "avulso": 1}
    div = calcular_divergencia(fech, totais)
    assert div["divergente"] is True
    assert div["deltas"]["avulso"] == 2
    assert div["diferenca_total"] == 2


def test_divergencia_inclui_cancelados_e_g():
    div = calcular_divergencia(
        {"shopee": 1, "ml": 0, "avulso": 0},
        {"shopee": 1, "ml": 0, "avulso": 0},
        cancelados_fechamento={"shopee": 0, "ml": 2, "avulso": 0},
        cancelados_extrato={"shopee": 0, "ml": 1, "avulso": 0},
        pacotes_g_fechamento=3,
        pacotes_g_extrato=1,
    )
    assert div["divergente"] is True
    assert div["delta_cancelados"] == -1
    assert div["delta_g"] == -2
    msg = montar_mensagem_divergencia(
        totais_sem_codigo={"shopee": 0, "ml": 0, "avulso": 0},
        divergencia=div,
    )
    assert msg is not None
    assert "Cancelados" in msg
    assert "Pacotes G" in msg
