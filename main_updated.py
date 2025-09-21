from __future__ import annotations

import os
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
        
    ]

# ──────────────────────────────────────────────────────────────────
# App
app = FastAPI(
    title="API Saídas",
    version="0.2.0",
    openapi_url=f"{API_PREFIX}/openapi.json",
    docs_url=f"{API_PREFIX}/docs",
    redoc_url=f"{API_PREFIX}/redoc",
)

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
    max_age=86400,                           # (opcional) cache do preflight
    # expose_headers=["Set-Cookie"],         # opcional, só p/ depurar
)

# ──────────────────────────────────────────────────────────────────
# Routers em uso
from users_routes_updated import router as users_router          # noqa: E402
from entregador_routes import router as entregadores_router      # noqa: E402
from auth import router as auth_router                           # noqa: E402
from saidas_routes import router as saidas_router                # noqa: E402
from owner_routes import router as owners_router
from base import router as base_router
from coletas import router as coletas_router

app.include_router(coletas_router, prefix=API_PREFIX)
app.include_router(users_router,        prefix=API_PREFIX)
app.include_router(entregadores_router, prefix=API_PREFIX)
app.include_router(auth_router,         prefix=API_PREFIX)
app.include_router(saidas_router,       prefix=API_PREFIX)
app.include_router(owners_router, prefix=API_PREFIX)
app.include_router(base_router, prefix=API_PREFIX)

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
