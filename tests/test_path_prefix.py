"""Soporte de prefijo de ruta (ADR-0073): prefija URLs salientes."""

from __future__ import annotations

import httpx
import pytest

from core.admin.path_prefix import PathPrefixMiddleware, rewrite_html


def test_rewrite_html_prefija_urls_de_raiz_y_respeta_externas():
    html = (
        '<a href="/admin">x</a>'
        '<link href="/static/a.css">'
        '<form action="/admin/login">'
        '<script>fetch("/api/x"); f.action="/admin/y"; '
        'window.location.href="/admin/z";</script>'
        '<a href="//cdn.ejemplo.com">proto-rel</a>'
        '<a href="https://externo.com">ext</a>'
    )
    out = rewrite_html(html, "/agent-core")
    assert 'href="/agent-core/admin"' in out
    assert 'href="/agent-core/static/a.css"' in out
    assert 'action="/agent-core/admin/login"' in out
    assert 'fetch("/agent-core/api/x")' in out
    assert '.action="/agent-core/admin/y"' in out
    assert 'location.href="/agent-core/admin/z"' in out
    assert 'href="//cdn.ejemplo.com"' in out  # protocolo-relativo intacto
    assert 'href="https://externo.com"' in out  # externo intacto


def test_rewrite_html_sin_prefijo_no_cambia_nada():
    html = '<a href="/admin">x</a>'
    assert rewrite_html(html, "") == html


@pytest.mark.asyncio
async def test_middleware_prefija_location_y_html():
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse, RedirectResponse
    from starlette.routing import Route

    async def page(request):  # type: ignore[no-untyped-def]
        return HTMLResponse('<a href="/admin">x</a>')

    async def go(request):  # type: ignore[no-untyped-def]
        return RedirectResponse("/admin")

    app = Starlette(routes=[Route("/page", page), Route("/go", go)])
    app.add_middleware(PathPrefixMiddleware, prefix="/agent-core")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/page")
        assert 'href="/agent-core/admin"' in r.text

        r = await client.get("/go", follow_redirects=False)
        assert r.headers["location"] == "/agent-core/admin"
