"""Montagem pura do extrato detalhado de fechamento de bases (PRD-019)."""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

SERVICO_LABEL = {
    "shopee": "Shopee",
    "ml": "Mercado Livre",
    "avulso": "Avulso",
}
SERVICOS = ("shopee", "ml", "avulso")
CODIGO_COLETA_SEM_CODIGO = "Coleta sem código"


def label_servico(tipo: str) -> str:
    return SERVICO_LABEL.get(tipo, "Avulso")


def _bucket() -> Dict[str, int]:
    return {"shopee": 0, "ml": 0, "avulso": 0}


def somar_volumes(
    volumes: Mapping[str, Mapping[str, int]],
) -> Dict[str, int]:
    tot = _bucket()
    for dia_map in volumes.values():
        for tipo in SERVICOS:
            tot[tipo] += int(dia_map.get(tipo) or 0)
    return tot


def montar_linhas_extrato(
    *,
    saidas: Sequence[Mapping[str, Any]],
    volumes_coleta: Mapping[str, Mapping[str, int]],
) -> Tuple[List[dict], Dict[str, int], Dict[str, int], Dict[str, Dict[str, int]]]:
    """
    Gera linhas do extrato (códigos + coleta sem código).

    saidas: itens com data (YYYY-MM-DD), codigo, servico_tipo (shopee|ml|avulso).
    volumes_coleta: {dia: {shopee, ml, avulso}} a partir de Coleta.timestamp.date().

    Retorna (linhas, totais_extrato, totais_sem_codigo, saldo_por_dia).
    """
    contagem_codigo: Dict[str, Dict[str, int]] = {}
    linhas: List[dict] = []

    for s in saidas:
        dia = str(s.get("data") or "")
        tipo = str(s.get("servico_tipo") or "avulso")
        if tipo not in SERVICOS:
            tipo = "avulso"
        if dia not in contagem_codigo:
            contagem_codigo[dia] = _bucket()
        contagem_codigo[dia][tipo] += 1
        linhas.append(
            {
                "data": dia,
                "codigo": str(s.get("codigo") or "").strip() or "—",
                "servico": label_servico(tipo),
                "servico_tipo": tipo,
                "quantidade": 1,
                "origem": "saida",
                "status": s.get("status"),
                "is_grande": bool(s.get("is_grande")),
            }
        )

    saldo_por_dia: Dict[str, Dict[str, int]] = {}
    totais_sem = _bucket()
    dias = set(volumes_coleta.keys()) | set(contagem_codigo.keys())
    for dia in sorted(dias):
        vol = volumes_coleta.get(dia) or _bucket()
        cod = contagem_codigo.get(dia) or _bucket()
        saldo = _bucket()
        for tipo in SERVICOS:
            rem = max(0, int(vol.get(tipo) or 0) - int(cod.get(tipo) or 0))
            saldo[tipo] = rem
            if rem:
                totais_sem[tipo] += rem
                linhas.append(
                    {
                        "data": dia,
                        "codigo": CODIGO_COLETA_SEM_CODIGO,
                        "servico": label_servico(tipo),
                        "servico_tipo": tipo,
                        "quantidade": rem,
                        "origem": "coleta_sem_codigo",
                        "status": None,
                        "is_grande": False,
                    }
                )
        if any(saldo[t] for t in SERVICOS):
            saldo_por_dia[dia] = saldo

    def _ord_key(row: dict) -> Tuple[str, int, str, str]:
        origem_rank = 0 if row.get("origem") == "saida" else 1
        return (
            str(row.get("data") or ""),
            origem_rank,
            str(row.get("servico_tipo") or ""),
            str(row.get("codigo") or ""),
        )

    linhas.sort(key=_ord_key)

    totais = _bucket()
    for row in linhas:
        tipo = str(row.get("servico_tipo") or "avulso")
        if tipo not in SERVICOS:
            tipo = "avulso"
        totais[tipo] += int(row.get("quantidade") or 0)

    return linhas, totais, totais_sem, saldo_por_dia


def totais_de_itens_fechamento(itens: Iterable[Any]) -> Dict[str, int]:
    """Soma volumes brutos do snapshot (shopee / ml / avulso)."""
    tot = _bucket()
    for it in itens:
        get = it.get if isinstance(it, Mapping) else (lambda k, d=0: getattr(it, k, d))
        tot["shopee"] += int(get("shopee") or 0)
        tot["ml"] += int(get("mercado_livre") or get("ml") or 0)
        tot["avulso"] += int(get("avulso") or 0)
    return tot


def totais_cancelados_itens(itens: Iterable[Any]) -> Dict[str, int]:
    tot = _bucket()
    for it in itens:
        get = it.get if isinstance(it, Mapping) else (lambda k, d=0: getattr(it, k, d))
        tot["shopee"] += int(get("cancelados_shopee") or 0)
        tot["ml"] += int(get("cancelados_ml") or 0)
        tot["avulso"] += int(get("cancelados_avulso") or 0)
    return tot


def _total_mapa(mapa: Mapping[str, int] | None) -> int:
    if not mapa:
        return 0
    return sum(int(mapa.get(t) or 0) for t in SERVICOS)


def calcular_divergencia(
    totais_fechamento: Mapping[str, int],
    totais_extrato: Mapping[str, int],
    *,
    cancelados_fechamento: Mapping[str, int] | None = None,
    cancelados_extrato: Mapping[str, int] | None = None,
    pacotes_g_fechamento: int | None = None,
    pacotes_g_extrato: int | None = None,
) -> Dict[str, Any]:
    deltas: Dict[str, int] = {}
    divergente = False
    for tipo in SERVICOS:
        d = int(totais_extrato.get(tipo) or 0) - int(totais_fechamento.get(tipo) or 0)
        deltas[tipo] = d
        if d:
            divergente = True
    total_fech = _total_mapa(totais_fechamento)
    total_ext = _total_mapa(totais_extrato)
    canc_fech = _total_mapa(cancelados_fechamento)
    canc_ext = _total_mapa(cancelados_extrato)
    delta_cancelados = canc_ext - canc_fech
    if delta_cancelados:
        divergente = True
    g_fech = int(pacotes_g_fechamento or 0)
    g_ext = int(pacotes_g_extrato or 0)
    delta_g = g_ext - g_fech
    if pacotes_g_fechamento is not None and pacotes_g_extrato is not None and delta_g:
        divergente = True
    return {
        "divergente": divergente,
        "deltas": deltas,
        "total_fechamento": total_fech,
        "total_extrato": total_ext,
        "diferenca_total": total_ext - total_fech,
        "delta_cancelados": delta_cancelados,
        "delta_g": delta_g,
    }


def montar_mensagem_divergencia(
    *,
    totais_sem_codigo: Mapping[str, int],
    divergencia: Mapping[str, Any],
) -> str | None:
    partes: List[str] = []
    sem = _total_mapa(totais_sem_codigo)
    if sem > 0:
        partes.append(f"Inclui {sem} pacote(s) lançados em coleta sem código bipado.")
    if divergencia.get("diferenca_total"):
        partes.append(
            f"Este extrato lista {divergencia.get('total_extrato', 0)} pacotes. "
            f"O fechamento cobra {divergencia.get('total_fechamento', 0)}. "
            f"Diferença: {int(divergencia.get('diferenca_total') or 0):+d}."
        )
    delta_canc = int(divergencia.get("delta_cancelados") or 0)
    if delta_canc:
        partes.append(f"Cancelados do extrato diferem do fechamento ({delta_canc:+d}).")
    delta_g = int(divergencia.get("delta_g") or 0)
    if delta_g:
        partes.append(f"Pacotes G do extrato diferem do fechamento ({delta_g:+d}).")
    if not partes and divergencia.get("divergente"):
        partes.append("Há divergência entre o extrato e o snapshot do fechamento.")
    return " ".join(partes) if partes else None
