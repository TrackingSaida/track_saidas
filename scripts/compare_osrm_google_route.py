#!/usr/bin/env python3
"""
POC Euroville — compara OSRM x Google (tempo vs distância) e testa refreshDetailsRoutes.

Uso (com credenciais ADC / GOOGLE_CLOUD_PROJECT):

  ROUTING_OPTIMIZATION_PROVIDER=google \\
  GOOGLE_CLOUD_PROJECT=seu-projeto \\
  python scripts/compare_osrm_google_route.py --fixture euroville

Não grava rota do motoboy. Não ative Google em produção até validar o trecho 4→5.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Paradas placeholder Euroville/Carapicuíba — substituir por coords reais da rota problemática.
# Índices 3 e 4 (= paradas 4 e 5) devem ser o par que OSRM desvia.
EUROVILLE_FIXTURE = [
    (-23.54150, -46.84500),  # 1
    (-23.54300, -46.84450),  # 2
    (-23.54450, -46.84380),  # 3
    (-23.54520, -46.84200),  # 4  ← problema
    (-23.54580, -46.83950),  # 5  ← problema (passagem condomínio)
    (-23.54700, -46.83800),  # 6
    (-23.54850, -46.83650),  # 7
]


def _points(coords):
    return [(i + 1, lat, lon) for i, (lat, lon) in enumerate(coords)]


def run_osrm(points):
    from geocode_utils import otimizar_ordem_entregas

    t0 = time.monotonic()
    result = otimizar_ordem_entregas(points)
    elapsed = time.monotonic() - t0
    return {
        "ordem": result.get("ordem"),
        "distancia_m": result.get("distancia_total_m"),
        "duracao_s": result.get("duracao_total_s"),
        "modo": result.get("modo"),
        "elapsed_s": round(elapsed, 3),
        "polyline": None,
    }


def run_google(points, cost_objective: str):
    os.environ["ROUTING_GOOGLE_COST_OBJECTIVE"] = cost_objective
    from routing.google_route_optimization import optimize_google, refresh_geometry_google

    t0 = time.monotonic()
    opt = optimize_google(points, populate_polylines=True, cost_objective=cost_objective)
    elapsed = time.monotonic() - t0
    out = {
        "ordem": opt.ordem,
        "distancia_m": opt.distancia_total_m,
        "duracao_s": opt.duracao_total_s,
        "modo": opt.optimization_mode,
        "elapsed_s": round(elapsed, 3),
        "polyline": (opt.polyline_encoded or "")[:80] + "...",
        "cost_objective": cost_objective,
    }

    # Parte B — refreshDetailsRoutes na ordem obtida
    ordered_points = []
    by_id = {sid: (lat, lon) for sid, lat, lon in points}
    for sid in opt.ordem:
        lat, lon = by_id[sid]
        ordered_points.append((sid, lat, lon))
    t1 = time.monotonic()
    geom = refresh_geometry_google(ordered_points)
    out["refresh"] = {
        "ok": geom.ok,
        "elapsed_s": round(time.monotonic() - t1, 3),
        "distancia_m": geom.distancia_total_m,
        "duracao_s": geom.duracao_total_s,
        "error": geom.error_code,
        "polyline_prefix": ((geom.polyline_encoded or "")[:80] + "...") if geom.polyline_encoded else None,
        "billing_note": (
            "Conferir no Cloud Console se refreshDetailsRoutes gerou cobrança "
            "(não assumir custo zero)."
        ),
    }
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", default="euroville")
    parser.add_argument("--skip-google", action="store_true")
    args = parser.parse_args()

    coords = EUROVILLE_FIXTURE
    points = _points(coords)
    report = {"fixture": args.fixture, "n_stops": len(points), "osrm": run_osrm(points)}

    if not args.skip_google:
        if not (os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT")):
            print("Defina GOOGLE_CLOUD_PROJECT para a parte Google.", file=sys.stderr)
            sys.exit(2)
        report["google_traveled_hour"] = run_google(points, "traveled_hour")
        report["google_kilometer"] = run_google(points, "kilometer")

    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(
        "\nCritério Euroville: comparar se a ordem coloca as paradas 4 e 5 "
        "adjacentes E se a polyline Google não dá a volta longa do OSRM.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
