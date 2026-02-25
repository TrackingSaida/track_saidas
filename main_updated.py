from __future__ import annotations

import os
import time
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import HTTPException as FastAPIHTTPException


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
    version="0.2.1",  # bump leve (opcional)
    openapi_url=f"{API_PREFIX}/openapi.json",
    docs_url=f"{API_PREFIX}/docs",
    redoc_url=f"{API_PREFIX}/redoc",
)

# ──────────────────────────────────────────────────────────────────
# CORS fallback (primeiro middleware = último na volta): garante que
# toda resposta, inclusive de erro/500, tenha CORS quando houver Origin.
from starlette.middleware.base import BaseHTTPMiddleware


class CORSFallbackMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        try:
            response = await call_next(request)
        except Exception as exc:
            origin = request.headers.get("origin")
            headers = {}
            if origin:
                headers["Access-Control-Allow-Origin"] = origin
                headers["Access-Control-Allow-Credentials"] = "true"
            return JSONResponse(status_code=500, content={"detail": str(exc)}, headers=headers)
        origin = request.headers.get("origin")
        if origin and "access-control-allow-origin" not in [k.lower() for k in response.headers.keys()]:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
        return response

app.add_middleware(CORSFallbackMiddleware)

# ──────────────────────────────────────────────────────────────────
# 🔥 Middleware — tempo real de processamento do BACKEND
@app.middleware("http")
async def backend_timing_middleware(request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    end = time.perf_counter()

    # tempo em ms, apenas processamento interno do backend
    response.headers["X-Backend-Process-Time"] = f"{(end - start) * 1000:.3f}"
    return response


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
from saidas_routes import router as saidas_router
from owner_routes import router as owners_router
from base import router as base_router
from coletas import router as coletas_router
from base_fechamento_routes import router as base_fechamento_router
from routes_ui import router as ui_router
from ml_routes import router as ml_router
from signup_routes import router as signup_router
from shopee_routes import router as shopee_router
from logs import router as logs_router
from contabilidade_routes import router as contabilidade_router
from etiquetas_routes import router as etiquetas_router
from dashboard_routes import router as dashboard_router
from mobile_entregas_routes import router as mobile_entregas_router
from upload_routes import router as upload_router

app.include_router(ml_router, prefix=API_PREFIX)
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
app.include_router(mobile_entregas_router, prefix=API_PREFIX)
app.include_router(upload_router, prefix=API_PREFIX)
app.include_router(owners_router, prefix=API_PREFIX)
app.include_router(base_router, prefix=API_PREFIX)
app.include_router(signup_router, prefix=API_PREFIX)
app.include_router(shopee_router, prefix=API_PREFIX)
app.include_router(logs_router, prefix=API_PREFIX)


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
    body = {"detail": detail}
    headers = dict(_cors_headers_for_request(request))
    return JSONResponse(status_code=status, content=body, headers=headers)


# ──────────────────────────────────────────────────────────────────
# Rotina de startup — renova tokens ML ao inicializar a API
from db import SessionLocal
from ml_token_service import refresh_all_ml_tokens

@app.on_event("startup")
def startup_event():
    """Executa ao subir a API: renova tokens vencidos do Mercado Livre."""
    db = SessionLocal()
    try:
        refresh_all_ml_tokens(db)
    except Exception as e:
        print(f"[ML] Erro durante renovação inicial: {e}")
    finally:
        db.close()

# ──────────────────────────────────────────────────────────────────
# Healthcheck
@app.get(f"{API_PREFIX}/health", tags=["Health"])
def health():
    return {"status": "ok"}

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
