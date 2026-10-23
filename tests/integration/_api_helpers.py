"""Helpers compartidos de los tests de integración de la API nueva.

Login por `POST /api/v1/auth/login` (no por el panel SSR en retiro) y headers
`Authorization: Bearer` + `X-Tenant-Id`, el mismo contrato del panel web.
"""

from __future__ import annotations

import os
import uuid

ADMIN_EMAIL = "admin@local"


def uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


def admin_password() -> str:
    return os.environ.get("TEST_ADMIN_PASSWORD", "admin123")


async def api_headers(
    app_client,
    tenant_id: uuid.UUID,
    *,
    email: str | None = None,
    password: str | None = None,
) -> dict[str, str]:
    r = await app_client.post(
        "/api/v1/auth/login",
        json={"identifier": email or ADMIN_EMAIL, "password": password or admin_password()},
    )
    assert r.status_code == 200, f"login API falló para {email or ADMIN_EMAIL}: {r.text}"
    return {
        "Authorization": f"Bearer {r.json()['token']}",
        "X-Tenant-Id": str(tenant_id),
    }


def stub_meta_pair_ok(monkeypatch) -> None:
    """Graph confirma que el número es de la WABA y que el token la ve (ADR-0095).

    Las altas (BYOK, manual, Embedded Signup) lo verifican antes de guardar; los tests
    que ya falsean Graph para el resto del alta lo falsean también aquí."""
    import core.gateway.meta_asset_claims as claims_mod
    import core.gateway.meta_byok as byok_mod

    async def _pair_ok(**_kwargs):
        return {}

    monkeypatch.setattr(claims_mod, "verify_waba_and_phone", _pair_ok)
    monkeypatch.setattr(byok_mod, "verify_waba_and_phone", _pair_ok)


async def banderas_como_dueno(tenant_id: uuid.UUID, **columnas: object) -> None:
    """Fija banderas del Agent OS (`aos_runtime`, `aos_procedures`, `aos_routing`) con el rol
    dueño: desde 0097 un UPDATE de `app_runtime` sobre ellas lo rechaza el trigger
    `tenants_aos_banderas_guard` (el escritor real es `aos_fijar_banderas`)."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from core.config import get_settings

    permitidas = {"aos_runtime", "aos_procedures", "aos_routing"}
    assert columnas and set(columnas) <= permitidas, columnas
    settings = get_settings()
    engine = create_async_engine(settings.migrations_database_url or settings.database_url)
    try:
        async with engine.begin() as conn:
            asignaciones = ", ".join(f"{c} = :{c}" for c in columnas)
            await conn.execute(text(f"update tenants set {asignaciones} where id = :t"), {"t": tenant_id, **columnas})
    finally:
        await engine.dispose()


APP_ID_PLATAFORMA = "platform-app-test"


class MetaSimulado:
    """Meta simulado para las altas de Messenger: las Páginas del usuario y qué responde cada suscripción."""

    def __init__(
        self,
        paginas: list[dict],
        *,
        suscripcion_falla: set[str] | None = None,
        campos_previos: dict[str, list[str]] | None = None,
    ):
        self.paginas = paginas
        self.suscripcion_falla = suscripcion_falla or set()
        self.llamadas: list[tuple[str, str, str]] = []
        # Con `campos_previos` la suscripción tiene estado (como Meta): el POST REEMPLAZA la lista de la Página.
        self.suscritos: dict[str, list[str]] | None = None if campos_previos is None else dict(campos_previos)
        self.pedidos: list[str] = []

    async def graph_fetch(self, path, *, token, app_secret, method="GET", body=None, query=None, **_kw):
        from core.gateway.meta_errors import GraphError

        self.llamadas.append((method, path, token))
        if path == "/me/accounts":
            return {"data": self.paginas}
        page_id = path.split("/")[1]
        if path.endswith("/subscribed_apps"):
            if page_id in self.suscripcion_falla:
                raise GraphError("(#200) Permissions error", category="PERMANENT", code=200)
            if method == "POST":
                assert "subscribed_fields" in (query or {})  # el nombre correcto del parámetro
                self.pedidos.append(query["subscribed_fields"])
                if self.suscritos is not None:
                    self.suscritos[page_id] = query["subscribed_fields"].split(",")
                return {"success": True}
            if self.suscritos is not None:
                campos = self.suscritos.get(page_id)
                return {"data": [{"id": APP_ID_PLATAFORMA, "subscribed_fields": campos}] if campos else []}
            return {"data": [{"id": APP_ID_PLATAFORMA, "subscribed_fields": ["messages", "messaging_postbacks"]}]}
        raise AssertionError(f"llamada inesperada a Graph: {method} {path}")


def pagina_de_meta(page_id: str, *, tasks=("MESSAGING", "MANAGE"), token: str | None = None) -> dict:
    """Una Página como la devuelve `GET /me/accounts`."""
    return {
        "id": page_id, "name": f"Página {page_id}", "category": "Tienda",
        "tasks": list(tasks), "access_token": token or f"tok-{page_id}",
    }


def pagina_con_instagram(
    page_id: str, ig_user_id: str, *, username: str | None = None, tasks=("MESSAGING", "MANAGE"), token: str | None = None
) -> dict:
    """Una Página con su cuenta de Instagram vinculada, como la devuelve `GET /me/accounts`."""
    pagina = pagina_de_meta(page_id, tasks=tasks, token=token)
    pagina["instagram_business_account"] = {"id": ig_user_id, "username": username or f"cuenta_{ig_user_id}"}
    return pagina
