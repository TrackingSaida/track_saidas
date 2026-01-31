from __future__ import annotations

import os
import time
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


# ──────────────────────────────────────────────────────────────────
# Config
API_PREFIX = os.getenv("API_PREFIX", "/api")

# ALLOWED_ORIGINS pode vir por ENV (lista separada por vírgula) ou usar a default abaixo
_env_origins = os.getenv("ALLOWED_ORIGINS")
if _env_origins:
    ALLOWED_ORIGINS = [o.strip() for o in _env_origins.split(",") if o.strip()]
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
    # expose_headers=["X-Backend-Process-Time"],  # só se quiser debugar via browser
)

# ──────────────────────────────────────────────────────────────────
# Routers em uso
from users_routes_updated import router as users_router
from entregador_routes import router as entregadores_router
from auth import router as auth_router
from saidas_routes import router as saidas_router
from owner_routes import router as owners_router
from base import router as base_router
from coletas import router as coletas_router
from routes_ui import router as ui_router
from ml_routes import router as ml_router
from entregador_entregas_routes import router as entregador_entregas_router
from signup_routes import router as signup_router
from shopee_routes import router as shopee_router
from logs import router as logs_router

app.include_router(ml_router, prefix=API_PREFIX)
app.include_router(ui_router, prefix=API_PREFIX)
app.include_router(coletas_router, prefix=API_PREFIX)
app.include_router(users_router,        prefix=API_PREFIX)
app.include_router(entregadores_router, prefix=API_PREFIX)
app.include_router(auth_router,         prefix=API_PREFIX)
app.include_router(saidas_router,       prefix=API_PREFIX)
app.include_router(owners_router, prefix=API_PREFIX)
app.include_router(base_router, prefix=API_PREFIX)
app.include_router(entregador_entregas_router, prefix=API_PREFIX)
app.include_router(signup_router, prefix=API_PREFIX)
app.include_router(shopee_router, prefix=API_PREFIX)
app.include_router(logs_router, prefix=API_PREFIX)

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
        "main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=os.getenv("RELOAD", "true").lower() == "true",
    )
