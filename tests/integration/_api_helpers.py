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
