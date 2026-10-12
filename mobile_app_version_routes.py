"""Versão mínima do app mobile. Público: o bloqueio precisa valer antes do login."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from mobile_app_version import public_policy_payload

router = APIRouter(prefix="/mobile", tags=["Mobile App"])


@router.get("/app-version")
def get_mobile_app_version():
    return JSONResponse(
        content=public_policy_payload(),
        headers={"Cache-Control": "no-store"},
    )
