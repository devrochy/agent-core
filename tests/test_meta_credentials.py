"""`credentials_for`: credenciales de Meta por conexión según su `origin` (HU-CONN-01).

Puros: el token store, la config y la consulta de la WABA van falseados. Lo que se
prueba es la regla de resolución -- de dónde sale cada credencial según el origen --
y que nunca se mezclen las de una conexión con las de la plataforma o las de otro negocio.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import core.gateway.meta_credentials as mc
from core.db.models import MetaConnection
from core.gateway.meta_byok import APP_SECRET_KIND, CLOUD_API_ACCESS_TOKEN_KIND
from core.gateway.meta_credentials import CredentialsUnavailable, MetaCredentials, credentials_for
from core.gateway.meta_onboarding import TOKEN_KIND
from core.shared.tenant import tenant_scope

TENANT = uuid.UUID("10000000-0000-0000-0000-0000000000a1")
OTHER_TENANT = uuid.UUID("10000000-0000-0000-0000-0000000000b2")
PLATFORM_APP_ID = "platform-app"
PLATFORM_SECRET = "platform-secret"


def _connection(origin: str, *, business_id: str | None = "biz-1", **overrides) -> MetaConnection:
    values = {
        "id": uuid.uuid4(),
        "tenant_id": TENANT,
        "channel": "whatsapp",
        "origin": origin,
        "meta_app_id": "tenant-app" if origin == "byok" else PLATFORM_APP_ID,
        "business_id": business_id,
        "graph_api_version": "v26.0",
        "deleted_at": None,
    }
    values.update(overrides)
    return MetaConnection(**values)


@pytest.fixture
def store(monkeypatch):
    """Token store en memoria: `{(kind, subject_id): token}`; registra cada lectura."""
    data: dict[tuple[str, str], str] = {}
    reads: list[tuple[str, str]] = []

    async def fake_get_token(connection_id, kind, subject_id, *, tenant_id=None):
        reads.append((kind, subject_id))
        return data.get((kind, subject_id))

    async def fake_waba(_connection):
        return "waba-1"

    monkeypatch.setattr(mc.meta_token_store, "get_token", fake_get_token)
    monkeypatch.setattr(mc, "_waba_id_of", fake_waba)
    monkeypatch.setattr(
        mc, "get_settings",
        lambda: SimpleNamespace(meta_app_id=PLATFORM_APP_ID, meta_app_secret=PLATFORM_SECRET),
    )
    store_ns = SimpleNamespace(data=data, reads=reads)
    return store_ns


async def test_byok_usa_token_y_secreto_propios_nunca_los_de_la_plataforma(store) -> None:
    store.data[(CLOUD_API_ACCESS_TOKEN_KIND, "tenant-app")] = "tok-del-negocio"
    store.data[(APP_SECRET_KIND, "tenant-app")] = "secreto-del-negocio"

    creds = await credentials_for(_connection("byok"))

    assert creds.origin == "byok"
    assert creds.access_token == "tok-del-negocio"
    assert creds.app_secret == "secreto-del-negocio"
    assert creds.app_id == "tenant-app"
    assert creds.app_secret != PLATFORM_SECRET
    assert creds.waba_id == "waba-1"
    assert creds.graph_api_version == "v26.0"


async def test_embedded_signup_usa_token_de_la_conexion_y_secreto_de_la_plataforma(store) -> None:
    store.data[(TOKEN_KIND, "biz-1")] = "tok-system-user"

    creds = await credentials_for(_connection("embedded_signup"))

    assert creds.origin == "embedded_signup"
    assert creds.access_token == "tok-system-user"
    assert creds.app_secret == PLATFORM_SECRET
    assert creds.app_id == PLATFORM_APP_ID
    assert creds.business_id == "biz-1"


async def test_manual_resuelve_igual_que_embedded_signup(store) -> None:
    store.data[(TOKEN_KIND, "biz-1")] = "tok-manual"

    creds = await credentials_for(_connection("manual"))

    assert (creds.origin, creds.access_token, creds.app_secret) == ("manual", "tok-manual", PLATFORM_SECRET)


async def test_un_origen_no_lee_los_tokens_del_otro(store) -> None:
    """BYOK nunca mira el token del system user, ni ES el App Secret guardado del negocio."""
    store.data[(TOKEN_KIND, "biz-1")] = "tok-system-user"
    store.data[(APP_SECRET_KIND, PLATFORM_APP_ID)] = "secreto-que-no-es-de-es"
    await credentials_for(_connection("embedded_signup"))
    assert all(kind != APP_SECRET_KIND for kind, _ in store.reads)

    store.reads.clear()
    store.data[(CLOUD_API_ACCESS_TOKEN_KIND, "tenant-app")] = "tok-byok"
    store.data[(APP_SECRET_KIND, "tenant-app")] = "sec-byok"
    await credentials_for(_connection("byok"))
    assert all(kind != TOKEN_KIND for kind, _ in store.reads)


async def test_embedded_signup_encuentra_el_token_guardado_antes_de_conocer_el_business_id(store) -> None:
    """Si el `business_id` llegó después del canje, el token quedó bajo el id de la conexión."""
    connection = _connection("embedded_signup")
    store.data[(TOKEN_KIND, str(connection.id))] = "tok-guardado-con-el-id"

    creds = await credentials_for(connection)

    assert creds.access_token == "tok-guardado-con-el-id"


@pytest.mark.parametrize(
    ("tokens", "reason"),
    [
        ({}, "missing_access_token"),
        ({(CLOUD_API_ACCESS_TOKEN_KIND, "tenant-app"): "tok"}, "missing_app_secret"),
        ({(APP_SECRET_KIND, "tenant-app"): "sec"}, "missing_access_token"),
    ],
)
async def test_byok_incompleto_falla_con_motivo(store, tokens, reason) -> None:
    store.data.update(tokens)

    with pytest.raises(CredentialsUnavailable) as exc:
        await credentials_for(_connection("byok"))

    assert exc.value.reason == reason


async def test_embedded_signup_sin_token_falla(store) -> None:
    with pytest.raises(CredentialsUnavailable) as exc:
        await credentials_for(_connection("embedded_signup"))
    assert exc.value.reason == "missing_access_token"


async def test_plataforma_sin_app_configurada_falla_para_es_y_manual(store, monkeypatch) -> None:
    monkeypatch.setattr(mc, "get_settings", lambda: SimpleNamespace(meta_app_id="", meta_app_secret=""))
    store.data[(TOKEN_KIND, "biz-1")] = "tok"

    for origin in ("embedded_signup", "manual"):
        with pytest.raises(CredentialsUnavailable) as exc:
            await credentials_for(_connection(origin))
        assert exc.value.reason == "platform_app_not_configured"


async def test_conexion_eliminada_no_presta_credenciales(store) -> None:
    from datetime import UTC, datetime

    store.data[(CLOUD_API_ACCESS_TOKEN_KIND, "tenant-app")] = "tok"
    store.data[(APP_SECRET_KIND, "tenant-app")] = "sec"

    with pytest.raises(CredentialsUnavailable) as exc:
        await credentials_for(_connection("byok", deleted_at=datetime.now(UTC)))

    assert exc.value.reason == "connection_deleted"
    assert store.reads == []  # ni siquiera se tocó el token store


async def test_conexion_de_otro_negocio_se_rechaza_antes_de_leer_nada(store) -> None:
    store.data[(CLOUD_API_ACCESS_TOKEN_KIND, "tenant-app")] = "tok-de-otro-negocio"
    store.data[(APP_SECRET_KIND, "tenant-app")] = "sec-de-otro-negocio"
    ajena = _connection("byok", tenant_id=OTHER_TENANT)

    async with tenant_scope(TENANT):
        with pytest.raises(CredentialsUnavailable) as exc:
            await credentials_for(ajena)

    assert exc.value.reason == "tenant_mismatch"
    assert store.reads == []


async def test_el_tenant_de_la_conexion_es_el_que_se_pasa_al_token_store(monkeypatch, store) -> None:
    """El AAD del token ata la fila al tenant de la conexión: se pasa explícito, no el ambiente."""
    seen: list[uuid.UUID | None] = []

    async def spy(connection_id, kind, subject_id, *, tenant_id=None):
        seen.append(tenant_id)
        return "x"

    monkeypatch.setattr(mc.meta_token_store, "get_token", spy)

    await credentials_for(_connection("byok"))

    assert seen and all(t == TENANT for t in seen)


async def test_origen_invalido_falla(store) -> None:
    with pytest.raises(CredentialsUnavailable) as exc:
        await credentials_for(_connection("otro-origen"))
    assert exc.value.reason == "origin_invalid"


async def test_waba_id_recibido_evita_la_consulta(monkeypatch, store) -> None:
    async def no_deberia_llamarse(_connection):
        raise AssertionError("no debía consultar la WABA")

    monkeypatch.setattr(mc, "_waba_id_of", no_deberia_llamarse)
    store.data[(TOKEN_KIND, "biz-1")] = "tok"

    creds = await credentials_for(_connection("manual"), waba_id="waba-dada")

    assert creds.waba_id == "waba-dada"


def test_repr_y_mensajes_no_filtran_secretos() -> None:
    creds = MetaCredentials(
        connection_id=uuid.uuid4(), tenant_id=TENANT, origin="byok", app_id="app",
        graph_api_version="v26.0", access_token="TOKEN-SECRETO-123", app_secret="APP-SECRET-456",
    )
    text = f"{creds!r} {creds}"
    assert "TOKEN-SECRETO-123" not in text
    assert "APP-SECRET-456" not in text


def test_las_altas_guiadas_fijan_su_origen() -> None:
    """Las tres altas de `MetaConnection` deben guardar su origen explícito; el valor por
    defecto (`manual`) no puede ser la razón por la que una conexión BYOK/ES lo tenga."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    esperado = {
        "core/gateway/meta_byok.py": 'origin="byok"',
        "core/gateway/meta_onboarding.py": 'origin="embedded_signup"',
        "core/api/routers/meta_wizard.py": 'origin="manual"',
    }
    for relative, needle in esperado.items():
        assert needle in (root / relative).read_text(encoding="utf-8"), f"{relative} no fija {needle}"
