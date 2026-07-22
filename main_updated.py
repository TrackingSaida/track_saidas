from __future__ import annotations

import os
import time
import logging
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.exceptions import HTTPException as FastAPIHTTPException

logger = logging.getLogger("main")

# ──────────────────────────────────────────────────────────────────
# Config
API_PREFIX = os.getenv("API_PREFIX", "/api")

# ALLOWED_ORIGINS pode vir por ENV (lista separada por vírgula) ou usar a default abaixo
_env_origins = os.getenv("ALLOWED_ORIGINS")
if _env_origins:
    ALLOWED_ORIGINS = [o.strip() for o in _env_origins.split(",") if o.strip()]
    # Garantir localhost:3000 para desenvolvimento/testes mesmo quando ENV está definida
    for origin in ("http://localhost:3000", "http://127.0.0.1:3000"):
        if origin not in ALLOWED_ORIGINS:
            ALLOWED_ORIGINS.append(origin)
else:
    ALLOWED_ORIGINS = [
        "https://admirable-sprinkles-d10196.netlify.app",
        "https://tracking-saidas.com.br",
        "https://www.tracking-saidas.com.br",
        "https://track-saidas-html.onrender.com",
        "http://localhost:5500", "http://127.0.0.1:5500",
        "http://localhost:8000", "http://127.0.0.1:8000",
        "http://localhost:3000", "http://172.30.33.97:3000",
        "http://account.sandbox.test-stable.shopee.com",
    ]

# ──────────────────────────────────────────────────────────────────
# App
app = FastAPI(
    title="API Saídas",
    version="1.5.0",
    openapi_url=f"{API_PREFIX}/openapi.json",
    docs_url=f"{API_PREFIX}/docs",
    redoc_url=f"{API_PREFIX}/redoc",
)


@app.api_route("/", methods=["GET", "HEAD"], tags=["Root"])
def root():
    """Responde na raiz para o health check do Render (GET ou HEAD) e ao acessar a URL do serviço."""
    return {
        "message": "API Track Saídas",
        "docs": f"{API_PREFIX}/docs",
        "health": f"{API_PREFIX}/health",
    }


# ──────────────────────────────────────────────────────────────────
# CORS fallback (primeiro middleware = último na volta): garante que
# toda resposta, inclusive de erro/500, tenha CORS quando houver Origin.
from starlette.middleware.base import BaseHTTPMiddleware


class CORSFallbackMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except RuntimeError as exc:
            if str(exc) == "No response returned." and await request.is_disconnected():
                logger.warning(
                    "client_disconnected method=%s path=%s",
                    request.method,
                    request.url.path,
                )
                return Response(status_code=499)
            raise
        except Exception as exc:
            logger.exception(
                "cors_fallback_exception method=%s path=%s",
                request.method,
                request.url.path,
            )
            origin = request.headers.get("origin")
            headers = {}
            if origin:
                headers["Access-Control-Allow-Origin"] = origin
                headers["Access-Control-Allow-Credentials"] = "true"
            return JSONResponse(status_code=500, content={"detail": str(exc)}, headers=headers)

        end = time.perf_counter()
        response.headers["X-Backend-Process-Time"] = f"{(end - start) * 1000:.3f}"

        origin = request.headers.get("origin")
        if origin and "access-control-allow-origin" not in [k.lower() for k in response.headers.keys()]:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
        return response

app.add_middleware(CORSFallbackMiddleware)


# ──────────────────────────────────────────────────────────────────
# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Content-Type",
        "Authorization",
        "X-Requested-With",
        "Accept",
        "Accept-Language",
        "Cache-Control", "Pragma",
    ],
    max_age=86400,                           # cache do preflight
    expose_headers=["X-Backend-Process-Time"],  # frontend lê para logs de performance
)

# ──────────────────────────────────────────────────────────────────
# Routers em uso
from users_routes_updated import router as users_router
from entregador_routes import router as entregadores_router
from entregador_fechamento_routes import router as fechamento_router
from auth import router as auth_router
from saidas_routes import router as saidas_router, pedidos_router
from owner_routes import router as owners_router
from base import router as base_router
from coletas import router as coletas_router
from base_fechamento_routes import router as base_fechamento_router
from routes_ui import router as ui_router
from ml_int_routes import router as ml_int_router
from signup_routes import router as signup_router
from shopee_routes import router as shopee_router
from logs import router as logs_router
from contabilidade_routes import router as contabilidade_router
from etiquetas_routes import router as etiquetas_router
from dashboard_routes import router as dashboard_router
from mobile_entregas_routes import router as mobile_entregas_router
from upload_routes import router as upload_router
from acompanhamento_routes import router as acompanhamento_router
from cep_routes import router as cep_router
from config_campos_obrigatorios_routes import router as config_campos_obrigatorios_router

app.include_router(cep_router, prefix=API_PREFIX)
app.include_router(ml_int_router, prefix=API_PREFIX)
app.include_router(etiquetas_router, prefix=API_PREFIX)
app.include_router(contabilidade_router, prefix=API_PREFIX)
app.include_router(dashboard_router, prefix=API_PREFIX)
app.include_router(ui_router, prefix=API_PREFIX)
app.include_router(coletas_router, prefix=API_PREFIX)
app.include_router(base_fechamento_router, prefix=f"{API_PREFIX}/coletas")
app.include_router(users_router,        prefix=API_PREFIX)
app.include_router(entregadores_router, prefix=API_PREFIX)
app.include_router(fechamento_router, prefix=f"{API_PREFIX}/entregadores")
app.include_router(auth_router,         prefix=API_PREFIX)
app.include_router(saidas_router,       prefix=API_PREFIX)
app.include_router(pedidos_router,      prefix=API_PREFIX)
app.include_router(acompanhamento_router, prefix=API_PREFIX)
app.include_router(mobile_entregas_router, prefix=API_PREFIX)
app.include_router(upload_router, prefix=API_PREFIX)
app.include_router(owners_router, prefix=API_PREFIX)
app.include_router(base_router, prefix=API_PREFIX)
app.include_router(signup_router, prefix=API_PREFIX)
app.include_router(shopee_router, prefix=API_PREFIX)
app.include_router(logs_router, prefix=API_PREFIX)
app.include_router(config_campos_obrigatorios_router, prefix=API_PREFIX)


def _cors_headers_for_request(request: Request):
    """Headers CORS na resposta de erro para a origem da requisição (evita CORS missing em 500)."""
    origin = request.headers.get("origin")
    if not origin:
        return {}
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Credentials": "true",
    }


@app.exception_handler(FastAPIHTTPException)
async def http_exception_handler(request: Request, exc: FastAPIHTTPException):
    """Garante que respostas HTTPException (401, 404, etc.) tenham CORS para o browser não bloquear."""
    detail = exc.detail
    if isinstance(detail, dict):
        detail = detail.get("message", detail.get("detail", str(detail)))
    body = {"detail": str(detail) if detail else "Erro"}
    headers = dict(_cors_headers_for_request(request))
    return JSONResponse(status_code=exc.status_code, content=body, headers=headers)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Garante que respostas de erro (500) incluam headers CORS para o browser não bloquear."""
    status = 500
    detail = str(exc) or "Erro interno do servidor"
    try:
        if hasattr(exc, "status_code"):
            status = getattr(exc, "status_code", 500)
        if hasattr(exc, "detail"):
            d = getattr(exc, "detail", None)
            if d is not None:
                detail = d if isinstance(d, str) else str(d.get("message", d.get("detail", d)))
    except Exception:
        pass
    logger.exception(
        "unhandled_exception method=%s path=%s status=%s detail=%s",
        request.method,
        request.url.path,
        status,
        detail,
    )
    body = {"detail": detail}
    headers = dict(_cors_headers_for_request(request))
    return JSONResponse(status_code=status, content=body, headers=headers)


# ──────────────────────────────────────────────────────────────────
# Rotina de startup — renova tokens ML Int e Shopee ao inicializar a API
from db import SessionLocal
from ml_int_service import refresh_all_ml_int_tokens
from shopee_routes import refresh_all_shopee_tokens
from cleanup_service import run_history_cleanup, estimate_old_volume, _CleanupContext

@app.on_event("startup")
def startup_event():
    """Executa ao subir a API: renova todos os tokens ML Int e Shopee (agendamento)."""
    db = SessionLocal()
    try:
        try:
            refresh_all_ml_int_tokens(db)
        except Exception as e:
            print(f"[ML Int] Erro durante renovação inicial: {e}")
        try:
            refresh_all_shopee_tokens(db)
        except Exception as e:
            print(f"[Shopee] Erro durante renovação inicial: {e}")
    finally:
        db.close()

# ──────────────────────────────────────────────────────────────────
# Healthcheck
@app.get(f"{API_PREFIX}/health", tags=["Health"])
def health():
    return {"status": "ok", "version": app.version}

# ──────────────────────────────────────────────────────────────────
# Endpoint interno: refresh de tokens (Cron Render — a cada ~5h)
@app.post(f"{API_PREFIX}/internal/refresh-tokens", tags=["Internal"])
def internal_refresh_tokens(request: Request):
    """
    Renova tokens ML Int e Shopee. Protegido por header X-Cron-Secret (CRON_REFRESH_SECRET).
    Uso: Cron Job no Render a cada 5h.
    """
    secret = os.getenv("CRON_REFRESH_SECRET")
    if not secret:
        return JSONResponse(status_code=500, content={"detail": "CRON_REFRESH_SECRET não configurado"})
    received = request.headers.get("X-Cron-Secret") or request.query_params.get("key")
    if received != secret:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    db = SessionLocal()
    try:
        ml_count = refresh_all_ml_int_tokens(db)
        shopee_count = refresh_all_shopee_tokens(db)
        return {"status": "ok", "ml_refreshed": ml_count, "shopee_refreshed": shopee_count}
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})
    finally:
        db.close()


@app.post(f"{API_PREFIX}/internal/cleanup-history", tags=["Internal"])
def internal_cleanup_history(request: Request):
    """
    Limpa histórico > D-60 (v2): saidas, filhas, logs, rotas, caches e purge B2.
    Protegido por header X-Cron-Secret (CRON_CLEANUP_SECRET, fallback CRON_REFRESH_SECRET).
    """
    secret = os.getenv("CRON_CLEANUP_SECRET") or os.getenv("CRON_REFRESH_SECRET")
    if not secret:
        return JSONResponse(status_code=500, content={"detail": "CRON_CLEANUP_SECRET não configurado"})

    received = request.headers.get("X-Cron-Secret")
    if received != secret:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

    batch_size = int(os.getenv("HISTORY_CLEANUP_BATCH_SIZE", "3000"))
    max_runtime_seconds = int(os.getenv("HISTORY_CLEANUP_MAX_RUNTIME_SECONDS", "540"))
    retention_days = int(os.getenv("HISTORY_RETENTION_DAYS", "60"))

    db = SessionLocal()
    ctx = _CleanupContext()
    try:
        before = estimate_old_volume(db, retention_days=retention_days, ctx=ctx)
        result = run_history_cleanup(
            db,
            retention_days=retention_days,
            batch_size=batch_size,
            max_runtime_seconds=max_runtime_seconds,
            ctx=ctx,
        )
        after = estimate_old_volume(db, retention_days=result.retention_days, ctx=ctx)
        payload = {
            "status": result.status,
            "partial": result.partial,
            "retention_days": result.retention_days,
            "cutoff_utc": result.cutoff.isoformat(),
            "deleted": {
                "saida_historico": result.rows_historico,
                "saidas_detail": result.rows_detail,
                "owner_cobranca_itens": result.rows_cobranca,
                "logs_leitura": result.rows_logs_leitura,
                "saidas": result.rows_saidas,
                "coletas": result.rows_coletas,
                "rotas_motoboy": result.rows_rotas,
                "address_telemetry": result.rows_address_telemetry,
                "geocode_cache": result.rows_geocode_cache,
                "suggestion_cache": result.rows_suggestion_cache,
                "enderecos_conhecidos": result.rows_enderecos_conhecidos,
                "b2_objects": {
                    "deleted": result.b2_objects_deleted,
                    "failed": result.b2_objects_failed,
                },
            },
            "skipped_tables": ctx.skipped_list(),
            "processed_saida_ids": result.processed_saida_ids,
            "last_saida_id_checkpoint": result.last_saida_id,
            "duration_ms": result.duration_ms,
            "remaining_estimate": {
                "before": before,
                "after": after,
            },
        }
        if result.error:
            payload["error"] = result.error
        status_code = 200 if result.status != "error" else 500
        return JSONResponse(status_code=status_code, content=payload)
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})
    finally:
        db.close()

@app.post(f"{API_PREFIX}/internal/encerrar-pendentes-quinzena", tags=["Internal"])
def internal_encerrar_pendentes_quinzena(request: Request):
    """
    Encerra pendentes (SAIU/EM_ROTA) com data operacional anterior à janela de 2 quinzenas.
    Protegido por X-Cron-Secret (CRON_ENCERRAMENTO_SECRET ou CRON_CLEANUP/REFRESH).
    Query: dry_run=true|false, batch_size=500, sub_base=opcional, data=YYYY-MM-DD (ref).
    """
    from encerramento_quinzena_service import run_encerrar_pendentes_quinzena
    from datetime import date as date_cls

    secret = (
        os.getenv("CRON_ENCERRAMENTO_SECRET")
        or os.getenv("CRON_CLEANUP_SECRET")
        or os.getenv("CRON_REFRESH_SECRET")
    )
    if not secret:
        return JSONResponse(status_code=500, content={"detail": "CRON_ENCERRAMENTO_SECRET não configurado"})
    received = request.headers.get("X-Cron-Secret")
    if received != secret:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

    dry_raw = (request.query_params.get("dry_run") or "true").strip().lower()
    dry_run = dry_raw in ("1", "true", "yes", "sim")
    try:
        batch_size = int(request.query_params.get("batch_size") or "500")
    except ValueError:
        batch_size = 500
    batch_size = max(50, min(batch_size, 2000))
    sub_base = (request.query_params.get("sub_base") or "").strip() or None
    ref = None
    data_raw = (request.query_params.get("data") or "").strip()
    if data_raw:
        try:
            ref = date_cls.fromisoformat(data_raw)
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "data inválida (YYYY-MM-DD)"})

    db = SessionLocal()
    try:
        result = run_encerrar_pendentes_quinzena(
            db,
            ref=ref,
            dry_run=dry_run,
            batch_size=batch_size,
            sub_base=sub_base,
        )
        return {
            "status": "ok",
            "dry_run": result.dry_run,
            "ref_date": result.ref_date.isoformat(),
            "inicio_vivo": result.inicio_vivo.isoformat(),
            "candidatos": result.candidatos,
            "elegiveis": result.elegiveis,
            "atualizados": result.atualizados,
            "por_sub_base": result.por_sub_base,
            "sample_ids": result.sample_ids,
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})
    finally:
        db.close()


# ──────────────────────────────────────────────────────────────────
# Execução local
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main_updated:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=os.getenv("RELOAD", "true").lower() == "true",
    )
