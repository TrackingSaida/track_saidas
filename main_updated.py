from __future__ import annotations

import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

API_PREFIX = os.getenv("API_PREFIX", "/api")

app = FastAPI(title="API Saídas", version="0.2.0")

ALLOWED_ORIGINS = [
    "https://track-saidas-html.onrender.com",
    "http://localhost:5500", "http://127.0.0.1:5500",
    "http://localhost:8000", "http://127.0.0.1:8000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS"],
    allow_headers=["*"],
)

# Rotas externas (como já estavam)
from users_routes_updated import router as users_router          # noqa: E402
from entregador_routes import router as entregadores_router      # noqa: E402
from owner_routes import router as owners_router             # noqa: E402
from auth import router as auth_router                           # noqa: E402

# NOVO: rota de saídas separada
from saidas_routes import router as saidas_router                # noqa: E402

app.include_router(users_router,        prefix=API_PREFIX)
app.include_router(entregadores_router, prefix=API_PREFIX)
app.include_router(owners_router,     prefix=API_PREFIX)
app.include_router(auth_router,         prefix=API_PREFIX)
app.include_router(saidas_router,       prefix=API_PREFIX)

@app.get(f"{API_PREFIX}/health")
def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=os.getenv("RELOAD", "true").lower() == "true",
    )
