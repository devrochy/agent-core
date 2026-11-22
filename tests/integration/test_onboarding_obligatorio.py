"""HU-SH-10 / HU-AOS-57 · Onboarding obligatorio pero saltable, contra el stack real.

Negocio NUEVO con el General y el Coordinador del alta (SPEC-39) y `AOS_CONNECTION_ROUTING` encendida.
Gherkin:

- «Solo el propietario es abierto»: un negocio creado desde el corte queda `pendiente` y el panel debe
  abrirlo (`abrir`) solo al propietario; el master lo ve sin bloqueo. Uno anterior al corte `no_aplica`.
- «Omitir deja el aviso»: omitir guarda quién y cuándo, `avisar` queda encendido y `abrir` apagado;
  reanudar o avanzar lo retoma.
- «Sin agente propio ajeno»: el Coordinador del alta (`kind='core'`) no cuenta, y el agente que creó la
  bienvenida (`agente_id` de `dejar-listo`) tampoco: el aviso sigue de la estación 2 a la 5. En cuanto el
  negocio crea otro agente, `avisar` y `abrir` se apagan aunque el recorrido siga en curso o pendiente.
- «Terminar lo valida el servidor»: `finalizar` responde 409 `faltan_pasos` con lo que falta.
- «Dejar listo»: crea el chat web CERRADO, publica el agente del guion y activa su versión, sin duplicar
  nada al repetir; con un chat web que ya atiende clientes, 409 `canal_en_vivo`.
- «Probar el agente nuevo»: el playground acepta `connection_id` (solo del negocio) y la conversación de
  prueba queda atada a esa conexión; esa primera prueba habilita `finalizar`.
Sin LLM ni red.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from _api_helpers import api_headers, uniq
from sqlalchemy import text

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PASSWORD = "clave-segura-123"


def _modules() -> list[Any]:
    from core.plugin_manager import plugin_manager

    return plugin_manager.scan()


async def _sql(tid: uuid.UUID | None, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            resultado = await session.execute(text(sql), params)
            return list(resultado.all()) if getattr(resultado, "returns_rows", False) else []


@pytest.fixture(scope="module", autouse=True)
async def _proceso() -> None:
    from core.plugin_manager import plugin_manager
    from core.releases.packs import sync_packs

    reporte = await sync_packs(_modules())
    assert not reporte.fallo, reporte
    await plugin_manager.sync_registry()
    await plugin_manager.activate_all_configured()


@pytest.fixture(scope="module", autouse=True)
def _alta_con_agent_os():
    import _alta_agent_os

    _alta_agent_os.ACTIVO = True
    yield
    _alta_agent_os.ACTIVO = False


@pytest.fixture(autouse=True)
def _ruteo_y_corte(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "onboarding_desde", date(2020, 1, 1))


async def _negocio() -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq("hush10")
    tid, _ = await onboard_tenant(name=f"HUSH10 {slug}", slug=slug, sembrar_ejemplos=False, plan="premium")
    return tid


async def _persona(app_client, tid: uuid.UUID, rol: str) -> dict[str, str]:
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    email = f"{uniq('onb')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role=rol))
    return await api_headers(app_client, tid, email=email, password=PASSWORD)


async def _recorrido(app_client, h: dict[str, str]) -> dict[str, Any]:
    r = await app_client.get("/api/v1/admin/onboarding", headers=h)
    assert r.status_code == 200, r.text
    return r.json()["recorrido"]


async def test_abre_solo_al_propietario_y_los_negocios_anteriores_al_corte_no_aplican(
    app_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = await _negocio()
    dueno = await _persona(app_client, tid, "owner")
    colaborador = await _persona(app_client, tid, "colaborador")
    master = await api_headers(app_client, tid)

    r = await _recorrido(app_client, dueno)
    assert (r["estado"], r["aplica"], r["abrir"], r["avisar"], r["total_pasos"]) == ("pendiente", True, True, False, 5)
    # El master ve el estado, pero no lo bloquea; el colaborador tampoco es redirigido.
    assert (await _recorrido(app_client, master))["abrir"] is False
    assert (await _recorrido(app_client, colaborador))["abrir"] is False

    from core.config import get_settings

    # El corte se compara con la fecha UTC de `created_at`: el mañana también va en UTC (con
    # `date.today()` el test fallaba entre las 00:00 y el fin del día local del runner).
    monkeypatch.setattr(get_settings(), "onboarding_desde", datetime.now(UTC).date() + timedelta(days=1))
    r = await _recorrido(app_client, dueno)
    assert (r["estado"], r["aplica"], r["abrir"]) == ("no_aplica", False, False)
    # Y sin recorrido no se puede mover.
    r = await app_client.post("/api/v1/admin/onboarding/omitir", headers=dueno)
    assert (r.status_code, r.json()["error"]["code"]) == (409, "transicion_invalida")


async def test_avanzar_omitir_y_reanudar_dejan_rastro_y_solo_los_mueve_el_propietario(app_client) -> None:
    tid = await _negocio()
    dueno = await _persona(app_client, tid, "owner")
    colaborador = await _persona(app_client, tid, "colaborador")

    r = await app_client.post("/api/v1/admin/onboarding/avanzar", headers=dueno, json={"paso": 3, "pasos": 5})
    assert r.status_code == 200, r.text
    assert (r.json()["estado"], r.json()["paso"], r.json()["avisar"], r.json()["abrir"]) == ("en_curso", 3, True, False)

    r = await app_client.post("/api/v1/admin/onboarding/omitir", headers=dueno)
    assert r.status_code == 200, r.text
    omitido = r.json()
    assert (omitido["estado"], omitido["paso"], omitido["avisar"], omitido["abrir"]) == ("omitido", 3, True, False)
    assert omitido["omitido_en"] is not None
    # Persistido en el negocio (no en el navegador) y auditado.
    assert (await _recorrido(app_client, dueno))["estado"] == "omitido"
    auditoria = await _sql(tid, "select input->>'accion' from audit_log where action = 'onboarding_recorrido' order by created_at")
    assert [a for (a,) in auditoria] == ["avanzar", "omitir"]

    r = await app_client.post("/api/v1/admin/onboarding/reanudar", headers=dueno)
    assert (r.status_code, r.json()["estado"], r.json()["paso"]) == (200, "en_curso", 3)
    r = await app_client.post("/api/v1/admin/onboarding/reanudar", headers=dueno)
    assert (r.status_code, r.json()["error"]["code"]) == (409, "transicion_invalida")

    # Fuera de rango: más de 9 o una escala que no existe lo frena el esquema; fuera de la escala de 5 (o de 4),
    # 409 sin guardar nada.
    for cuerpo in ({"paso": 10, "pasos": 9}, {"paso": 10}, {"paso": 2, "pasos": 6}):
        r = await app_client.post("/api/v1/admin/onboarding/avanzar", headers=dueno, json=cuerpo)
        assert r.status_code == 422, cuerpo
    for cuerpo, tope in (({"paso": 6, "pasos": 5}, 5), ({"paso": 5, "pasos": 4}, 4)):
        r = await app_client.post("/api/v1/admin/onboarding/avanzar", headers=dueno, json=cuerpo)
        assert (r.status_code, r.json()["error"]["code"]) == (409, "transicion_invalida"), r.text
        assert r.json()["error"]["message"] == f"el paso debe estar entre 1 y {tope}"
    [(guardado,)] = await _sql(tid, "select settings->'onboarding' from tenants where id = :t", t=tid)
    assert (guardado["estado"], guardado["paso"], guardado["pasos"]) == ("en_curso", 3, 5)

    # Un colaborador con permiso de escribir no mueve el recorrido del negocio.
    r = await app_client.post("/api/v1/admin/onboarding/omitir", headers=colaborador)
    assert r.status_code == 403, r.text


async def _guardar(tid: uuid.UUID, onboarding: str) -> None:
    await _sql(
        tid,
        "update tenants set settings = jsonb_set(coalesce(settings, '{}'::jsonb), '{onboarding}', "
        "cast(:o as jsonb)) where id = :t",
        o=onboarding,
        t=tid,
    )


async def test_cinco_pasos_se_guardan_tal_cual(app_client) -> None:
    """HU-AOS-84 «Cinco pasos»: `{paso: 4, pasos: 5}` se guarda así y `GET` lo devuelve con `total_pasos: 5`."""
    tid = await _negocio()
    dueno = await _persona(app_client, tid, "owner")
    await _guardar(tid, '{"estado": "en_curso", "paso": 2, "pasos": 5}')
    r = await app_client.post("/api/v1/admin/onboarding/avanzar", headers=dueno, json={"paso": 4, "pasos": 5})
    assert (r.status_code, r.json()["paso"], r.json()["total_pasos"]) == (200, 4, 5), r.text
    recorrido = await _recorrido(app_client, dueno)
    assert (recorrido["estado"], recorrido["paso"], recorrido["total_pasos"]) == ("en_curso", 4, 5)
    [(guardado,)] = await _sql(tid, "select settings->'onboarding' from tenants where id = :t", t=tid)
    assert (guardado["paso"], guardado["pasos"]) == (4, 5)


async def test_un_recorrido_de_4_6_o_9_pasos_se_retoma_en_su_equivalente_de_5(app_client) -> None:
    """HU-AOS-84: la bienvenida es de 5 pasos. Un recorrido guardado con 4, 6 (o 9) se lee en su paso de 5 y,
    al escribirse otra vez, queda guardado ya con los 5 sin perder el paso."""
    tid = await _negocio()
    dueno = await _persona(app_client, tid, "owner")
    # Guardado por la creación rápida de 4: «Datos para probar» era el 3 (hoy el 4 de 5).
    await _guardar(tid, '{"estado": "en_curso", "paso": 3, "pasos": 4}')
    recorrido = await _recorrido(app_client, dueno)
    assert (recorrido["estado"], recorrido["paso"], recorrido["total_pasos"]) == ("en_curso", 4, 5)

    # Guardado por la bienvenida de 6 pasos: «Carga sus datos» era el 5 (hoy «Datos para probar», el 4).
    await _guardar(tid, '{"estado": "en_curso", "paso": 5}')
    assert (await _recorrido(app_client, dueno))["paso"] == 4

    # Omitir no trae paso: se escribe el ya traducido y queda marcado con los 5.
    r = await app_client.post("/api/v1/admin/onboarding/omitir", headers=dueno)
    assert (r.status_code, r.json()["paso"]) == (200, 4), r.text
    [(guardado,)] = await _sql(tid, "select settings->'onboarding' from tenants where id = :t", t=tid)
    assert (guardado["paso"], guardado["pasos"]) == (4, 5)

    # La APK (sin `pasos`) avanza con su numeración de 6 y no recibe 422: «Probar» (6) es el 5.
    r = await app_client.post("/api/v1/admin/onboarding/avanzar", headers=dueno, json={"paso": 6})
    assert (r.status_code, r.json()["paso"]) == (200, 5), r.text
    # Un panel en caché con 9: «Habilidades» (5) es el 2. El panel de 4 (en test hasta HU-UX-25) manda
    # «Datos para probar» como 3 y se guarda como 4 de 5.
    r = await app_client.post("/api/v1/admin/onboarding/avanzar", headers=dueno, json={"paso": 5, "pasos": 9})
    assert (r.status_code, r.json()["paso"]) == (200, 2), r.text
    r = await app_client.post("/api/v1/admin/onboarding/avanzar", headers=dueno, json={"paso": 3, "pasos": 4})
    assert (r.status_code, r.json()["paso"]) == (200, 4), r.text
    [(guardado,)] = await _sql(tid, "select settings->'onboarding' from tenants where id = :t", t=tid)
    assert (guardado["paso"], guardado["pasos"]) == (4, 5)


async def test_el_paso_5_de_4_que_dejo_dejar_listo_se_retoma_en_habilidades(app_client) -> None:
    """Lo que escribió el `dejar_listo` anterior a HU-AOS-84 (`{paso: 5, pasos: 4}`, el 5 de 9) es el 2 de 5."""
    tid = await _negocio()
    dueno = await _persona(app_client, tid, "owner")
    await _guardar(tid, '{"estado": "en_curso", "paso": 5, "pasos": 4}')
    recorrido = await _recorrido(app_client, dueno)
    assert (recorrido["paso"], recorrido["total_pasos"]) == (2, 5)


async def test_el_aviso_y_la_apertura_solo_insisten_sin_agente_propio(app_client) -> None:
    """Un negocio recién creado solo tiene el Coordinador (`kind='core'`), que no cuenta como agente."""
    tid = await _negocio()
    h = await _persona(app_client, tid, "owner")

    r = await app_client.post("/api/v1/admin/onboarding/avanzar", headers=h, json={"paso": 2, "pasos": 5})
    assert r.status_code == 200, r.text
    assert (r.json()["avisar"], r.json()["abrir"]) == (True, False)

    # Al crear un especialista el recorrido deja de insistir aunque siga en curso.
    r = await app_client.post(
        "/api/v1/admin/agents/from-script", headers=h, json={"template_key": "ventas", "answers": {}}
    )
    assert r.status_code == 201, r.text
    actual = await _recorrido(app_client, h)
    assert (actual["estado"], actual["avisar"], actual["abrir"]) == ("en_curso", False, False)

    # Tampoco se abre solo si el recorrido nunca se tocó y el negocio ya tiene agente.
    tid_nuevo = await _negocio()
    h_nuevo = await _persona(app_client, tid_nuevo, "owner")
    r = await app_client.post(
        "/api/v1/admin/agents/from-script",
        headers=h_nuevo,
        json={"template_key": "ventas", "answers": {}},
    )
    assert r.status_code == 201, r.text
    pendiente = await _recorrido(app_client, h_nuevo)
    assert (pendiente["estado"], pendiente["avisar"], pendiente["abrir"]) == (
        "pendiente",
        False,
        False,
    )


async def _agente(app_client, h: dict[str, str], plantilla: str = "ventas") -> str:
    r = await app_client.post(
        "/api/v1/admin/agents/from-script", headers=h, json={"template_key": plantilla, "answers": {}}
    )
    assert r.status_code == 201, r.text
    return r.json()["agent"]["id"]


async def test_el_agente_de_la_bienvenida_no_apaga_el_aviso_y_uno_ajeno_si(app_client) -> None:
    """SPEC-49 (decisión del 2026-11-20): la bienvenida crea su agente al salir de la estación 1 y el aviso
    sigue hasta terminarla; un agente creado por «Nuevo agente» lo apaga aunque el recorrido esté omitido."""
    tid = await _negocio()
    h = await _persona(app_client, tid, "owner")
    de_la_bienvenida = await _agente(app_client, h)

    # `dejar-listo` guarda el agente y su RETURNING ya lo excluye: avisa en la misma respuesta.
    r = await app_client.post(
        "/api/v1/admin/onboarding/dejar-listo", headers=h, json={"agent_id": de_la_bienvenida}
    )
    assert r.status_code == 200, r.text
    listo = r.json()["recorrido"]
    assert (listo["estado"], listo["paso"], listo["agente_id"]) == ("en_curso", 2, de_la_bienvenida)
    assert (listo["avisar"], listo["abrir"]) == (True, False)
    actual = await _recorrido(app_client, h)
    assert (actual["estado"], actual["avisar"], actual["abrir"]) == ("en_curso", True, False)

    # «Seguir después» en la estación 3: el aviso sigue y cuenta desde donde quedó.
    r = await app_client.post("/api/v1/admin/onboarding/avanzar", headers=h, json={"paso": 3, "pasos": 5})
    assert r.status_code == 200, r.text
    r = await app_client.post("/api/v1/admin/onboarding/omitir", headers=h)
    assert r.status_code == 200, r.text
    assert (r.json()["estado"], r.json()["paso"], r.json()["avisar"]) == ("omitido", 3, True)

    # Ya omitido, el dueño publica otro agente por «Nuevo agente»: el recorrido deja de insistir, pero
    # sigue omitido y se puede retomar a mano.
    await _agente(app_client, h, "agenda")
    actual = await _recorrido(app_client, h)
    assert (actual["estado"], actual["aplica"], actual["avisar"], actual["abrir"]) == (
        "omitido",
        True,
        False,
        False,
    )

    # Si el agente ajeno se da de baja (`deleted_at`) y queda solo el de la bienvenida, vuelve el aviso.
    await _sql(
        tid,
        "update agents set deleted_at = now() where tenant_id = :t and kind <> 'core' and id <> :a",
        t=tid,
        a=uuid.UUID(de_la_bienvenida),
    )
    assert (await _recorrido(app_client, h))["avisar"] is True


async def test_el_agente_de_la_bienvenida_de_otro_negocio_no_cuenta(app_client) -> None:
    """Aislamiento: el `agente_id` guardado solo excluye agentes del propio negocio, y los agentes de otro
    negocio no apagan el aviso de este."""
    tid_a = await _negocio()
    h_a = await _persona(app_client, tid_a, "owner")
    tid_b = await _negocio()
    h_b = await _persona(app_client, tid_b, "owner")
    agente_b = await _agente(app_client, h_b)

    # Sin `agente_id` que lo excluya, el agente de B no apaga el aviso de A: se cuenta solo dentro del negocio.
    await _guardar(tid_a, '{"estado": "omitido", "paso": 2, "pasos": 5}')
    assert (await _recorrido(app_client, h_a))["avisar"] is True

    # A guarda (mal) el agente de B como el de su bienvenida: no lo vuelve propio ni ajeno; A no tiene
    # agentes y sigue avisando.
    await _guardar(tid_a, f'{{"estado": "omitido", "paso": 2, "pasos": 5, "agente_id": "{agente_b}"}}')
    assert (await _recorrido(app_client, h_a))["avisar"] is True

    # B, con un agente que no es el de su bienvenida, no avisa: que A haya guardado ese id no lo cambia.
    await _guardar(tid_b, '{"estado": "omitido", "paso": 2, "pasos": 5}')
    assert (await _recorrido(app_client, h_b))["avisar"] is False

    # Un `agente_id` mal formado no rompe la lectura.
    await _guardar(tid_a, '{"estado": "en_curso", "paso": 2, "pasos": 5, "agente_id": "no-es-uuid"}')
    assert (await _recorrido(app_client, h_a))["avisar"] is True


async def test_finalizar_lo_valida_el_servidor_y_dejar_listo_publica_y_activa_sin_duplicar(
    app_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = await _negocio()
    h = await _persona(app_client, tid, "owner")

    r = await app_client.post("/api/v1/admin/onboarding/finalizar", headers=h)
    assert (r.status_code, r.json()["error"]["code"]) == (409, "faltan_pasos"), r.text
    assert r.json()["error"]["details"]["faltan"] == ["datos_negocio", "agente", "agente_publicado", "prueba"]

    # El negocio se describe (paso 1 del asistente: lo calcula el checklist de siempre).
    r = await app_client.put(
        "/api/v1/admin/settings/identity",
        headers=h,
        json={"assistant_name": "Ana", "business_description": "Vendo ropa deportiva en Manizales."},
    )
    assert r.status_code == 200, r.text

    r = await app_client.post(
        "/api/v1/admin/agents/from-script",
        headers=h,
        json={"template_key": "ventas", "answers": {"que_vendes": "productos", "largo_respuesta": "normal"}},
    )
    assert r.status_code == 201, r.text
    agente = r.json()["agent"]["id"]

    r = await app_client.post("/api/v1/admin/onboarding/dejar-listo", headers=h, json={"agent_id": agente})
    assert r.status_code == 200, r.text
    listo = r.json()
    assert listo["listo"] is True and listo["release_id"]
    assert [(p["clave"], p["estado"]) for p in listo["pasos"]] == [
        ("conexion", "hecho"),
        ("publicar", "hecho"),
        # El primer especialista de una conexión que atendía solo el Coordinador ya queda activo al publicarlo
        # (adenda de ADR-0205 §3): «activar» lo encuentra hecho.
        ("activar", "ya_estaba"),
    ]
    assert (listo["recorrido"]["agente_id"], listo["recorrido"]["conexion_id"]) == (agente, listo["conexion_id"])
    # Desde `pendiente` queda en curso en «Habilidades» (2 de 5), no en «Revisar y publicar».
    assert (listo["recorrido"]["estado"], listo["recorrido"]["paso"], listo["recorrido"]["total_pasos"]) == (
        "en_curso",
        2,
        5,
    )
    [(guardado,)] = await _sql(tid, "select settings->'onboarding' from tenants where id = :t", t=tid)
    assert (guardado["paso"], guardado["pasos"]) == (2, 5)

    # El chat web nació cerrado al público y con la release del agente activa.
    conexion = await _sql(
        tid,
        "select active, active_release_id::text, type_id from channel_connections where id = :c",
        c=uuid.UUID(listo["conexion_id"]),
    )
    assert conexion == [(False, listo["release_id"], "webchat")]
    publicado = await _sql(
        tid,
        "select count(*) from agent_instances where connection_id = :c and agent_id = :a and deleted_at is null",
        c=uuid.UUID(listo["conexion_id"]),
        a=uuid.UUID(agente),
    )
    assert publicado == [(1,)]

    # Repetir no duplica el agente en la conexión ni crea otra conexión, y no retrocede un recorrido que ya
    # va más adelante.
    r = await app_client.post("/api/v1/admin/onboarding/avanzar", headers=h, json={"paso": 4, "pasos": 5})
    assert r.status_code == 200, r.text
    r = await app_client.post("/api/v1/admin/onboarding/dejar-listo", headers=h, json={"agent_id": agente})
    assert r.status_code == 200, r.text
    assert r.json()["recorrido"]["paso"] == 4
    assert r.json()["conexion_id"] == listo["conexion_id"]
    # La segunda vez nada se crea: conexión y publicación ya estaban, y la versión ya es la activa.
    assert {p["estado"] for p in r.json()["pasos"]} == {"ya_estaba"}
    assert next(p["clave"] for p in r.json()["pasos"]) == "conexion"
    assert await _sql(tid, "select count(*) from channel_connections where type_id = 'webchat' and deleted_at is null") == [(1,)]
    assert await _sql(
        tid, "select count(*) from agent_instances where agent_id = :a and deleted_at is null", a=uuid.UUID(agente)
    ) == [(1,)]

    # Aún falta la prueba con ese agente.
    r = await app_client.post("/api/v1/admin/onboarding/finalizar", headers=h)
    assert r.status_code == 409 and r.json()["error"]["details"]["faltan"] == ["prueba"], r.text

    # Probar por una conexión ajena o inexistente se rechaza; por la del agente, el turno corre atado a ella.
    ajena = await _sql(None, "select id::text from channel_connections where tenant_id <> :t limit 1", t=tid)
    for invalida in (str(uuid.uuid4()), *(a for (a,) in ajena)):
        r = await app_client.post(
            "/api/v1/admin/playground/message", headers=h, json={"text": "Hola", "connection_id": invalida}
        )
        assert (r.status_code, r.json()["error"]["code"]) == (422, "invalid_connection"), r.text

    vistas: list[uuid.UUID | None] = []

    async def _turno(conversation_id: uuid.UUID, text_: str) -> Any:
        import types

        fila = await _sql(tid, "select connection_id from conversations where id = :c", c=conversation_id)
        vistas.append(fila[0][0])
        return types.SimpleNamespace(text=f"Respuesta a: {text_}", image_url=None, escalated=False)

    async def _turno_escalado(conversation_id: uuid.UUID, text_: str) -> Any:
        import types

        return types.SimpleNamespace(text="Tuve un inconveniente", image_url=None, escalated=True)

    # Un turno que escaló (p. ej. falló el modelo) no prueba nada: no habilita terminar.
    monkeypatch.setattr("core.orchestrator.loop.process_conversation", _turno_escalado)
    r = await app_client.post(
        "/api/v1/admin/playground/message",
        headers=h,
        json={"text": "¿Qué venden?", "connection_id": listo["conexion_id"]},
    )
    assert r.status_code == 200, r.text
    assert (await _recorrido(app_client, h))["probado_en"] is None

    monkeypatch.setattr("core.orchestrator.loop.process_conversation", _turno)
    r = await app_client.post(
        "/api/v1/admin/playground/message",
        headers=h,
        json={"text": "¿Qué venden?", "connection_id": listo["conexion_id"]},
    )
    assert r.status_code == 200, r.text
    assert vistas == [uuid.UUID(listo["conexion_id"])]
    assert (await _recorrido(app_client, h))["probado_en"] is not None

    # La prueba sin conexión elegida va por la conexión real con release (ADR-0191), no por la interna.
    r = await app_client.post("/api/v1/admin/playground/message", headers=h, json={"text": "Hola"})
    assert r.status_code == 200, r.text
    assert vistas[-1] == uuid.UUID(listo["conexion_id"])

    r = await app_client.post("/api/v1/admin/onboarding/finalizar", headers=h)
    assert r.status_code == 200, r.text
    assert (r.json()["estado"], r.json()["avisar"], r.json()["completado_en"] is not None) == ("completo", False, True)
    assert (r.json()["paso"], r.json()["total_pasos"]) == (5, 5)
    # Ya completo, no se vuelve a mover.
    r = await app_client.post("/api/v1/admin/onboarding/omitir", headers=h)
    assert (r.status_code, r.json()["error"]["code"]) == (409, "transicion_invalida")


async def test_un_chat_web_que_ya_atiende_clientes_no_se_toca_desde_el_primer_uso(app_client) -> None:
    tid = await _negocio()
    h = await _persona(app_client, tid, "owner")
    r = await app_client.put("/api/v1/admin/settings/web-chat", headers=h, json={"enabled": True, "allowed_origins": []})
    assert r.status_code == 200, r.text
    r = await app_client.post(
        "/api/v1/admin/agents/from-script", headers=h, json={"template_key": "ventas", "answers": {}}
    )
    assert r.status_code == 201, r.text

    r = await app_client.post("/api/v1/admin/onboarding/dejar-listo", headers=h, json={"agent_id": r.json()["agent"]["id"]})
    assert (r.status_code, r.json()["error"]["code"]) == (409, "canal_en_vivo"), r.text
