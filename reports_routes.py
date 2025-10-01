# reports_routes.py
from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import StreamingResponse
from playwright.async_api import async_playwright
import io
import os

router = APIRouter(prefix="/relatorios", tags=["Relatórios"])

# Front hospedado que será impresso
FRONT_BASE = os.getenv("FRONT_BASE", "https://track-saidas-html.onrender.com")

# Só permitimos páginas desta whitelist
PAGES = {
    "dashboard_saidas": "/dashboard-tracking-saidas.html",
}

@router.get("/pdf")
async def gerar_pdf(
    page: str = Query(..., description="ex.: dashboard_saidas"),
    qs: str | None = Query(None, description="querystring opcional, ex.: data_ini=2025-09-17&data_fim=2025-09-30"),
    token: str | None = Query(None, description="JWT opcional se o front fizer fetch com Authorization"),
    wait_selector: str | None = Query(None, description="CSS opcional indicando 'pronto', ex.: #pdf-ready"),
):
    path = PAGES.get(page)
    if not path:
        raise HTTPException(status_code=400, detail="Página não permitida")

    url = f"{FRONT_BASE}{path}"
    if qs:
        url += ("?" + qs)

    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox"])
        context = await browser.new_context()

        # Se seu front faz fetch com Authorization: Bearer <token>
        if token:
            await context.set_extra_http_headers({"Authorization": f"Bearer {token}"})

        pg = await context.new_page()
        resp = await pg.goto(url, wait_until="networkidle")
        if not resp or not resp.ok:
            await browser.close()
            raise HTTPException(status_code=502, detail=f"Falha ao carregar {url}")

        # Se o seu JS inserir um marcador quando tudo estiver carregado:
        # document.body.insertAdjacentHTML('beforeend','<div id="pdf-ready" hidden></div>');
        if wait_selector:
            await pg.wait_for_selector(wait_selector, state="attached", timeout=30000)

        pdf_bytes = await pg.pdf(
            format="A4",
            print_background=True,
            margin={"top": "18mm", "right": "14mm", "bottom": "20mm", "left": "14mm"},
        )
        await browser.close()

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="relatorio_dashboard_saidas.pdf"'}
    )
