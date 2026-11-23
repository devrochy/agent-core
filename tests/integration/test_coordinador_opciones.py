"""HU-AOS-98 · opciones propias del Coordinador (ADR-0211).

Negocio sin especialistas (atiende el Coordinador). Gherkin:

- «Editar voz, horario y escalamiento»: el PUT es parcial, deja `audit_log` y el horario rige en caliente.
- «Reglas del Coordinador»: la release las congela y la vista de recepción las aplica; con la configuración por
  defecto la release y la vista no cambian.
- «Tools y habilidades siguen cerradas»: las rutas del agente Coordinador responden 409 `coordinador_inmutable`.
- «Aislamiento»: lo que edita un negocio no se ve en otro.
- Una regla guardada que ya no pasa el contrato hace fallar el build, no el turno.
"""

from __future__ import annotations

import os

import pytest
from test_coordinador_receptor import (  # noqa: F401  (fixture)
    _alta_con_agent_os,
    _negocio_sin_especialistas,
)
from test_editor_agentes_api import _dueno
from test_habilidades_por_agente_loop import _proceso, _sql  # noqa: F401  (fixture)

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

URL = "/api/v1/admin/coordinator"
REGLA = {"name": "sin_descuentos", "kind": "forbidden_phrase", "phrases": ["descuento"], "action": "escalate"}


async def _release(tid, cid):
    """Construye la release de la conexión y la carga congelada."""
    from core.db.session import get_session
    from core.releases.build import construir
    from core.releases.cargar import cargar_release
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        resultado = await construir(tid, cid, actor="test:hu-aos-98")
        async with get_session() as session:
            release = await cargar_release(session, tid, resultado.release_id)
    assert release is not None
    return resultado, release


async def test_el_dueno_edita_voz_horario_nivel_y_reglas_y_la_release_las_congela(app_client) -> None:
    from core.agents.recepcion import vista_de_recepcion
    from core.agents.turno import agente_del_turno  # noqa: F401
    from core.llm import niveles

    tid, cid = await _negocio_sin_especialistas()
    h = await _dueno(app_client, tid)
    base, release_base = await _release(tid, cid)
    assert release_base.recepcion == {}

    r = await app_client.get(URL, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["guardrails"] == [] and r.json()["nivel_ia"] == "estandar"

    # PUT parcial: solo lo enviado cambia.
    r = await app_client.put(
        URL,
        headers=h,
        json={
            "assistant_name": "Lía",
            "brand_voice": "cercana y breve",
            "business_ranges": [{"day": 1, "from": "08:00", "to": "17:00"}],
            "out_of_hours_message": "Te respondemos mañana.",
        },
    )
    assert r.status_code == 200, r.text
    voz = r.json()
    assert (voz["assistant_name"], voz["brand_voice"], voz["out_of_hours_message"]) == (
        "Lía", "cercana y breve", "Te respondemos mañana.",
    )
    assert voz["business_ranges"] == [{"day": 1, "from": "08:00", "to": "17:00"}]
    assert voz["guardrails"] == []

    r = await app_client.put(URL, headers=h, json={"guardrails": [REGLA], "nivel_ia": "avanzado"})
    assert r.status_code == 200, r.text
    assert r.json()["assistant_name"] == "Lía"  # lo que no vino no se tocó
    assert [g["name"] for g in r.json()["guardrails"]] == ["sin_descuentos"]
    assert r.json()["nivel_ia"] == "avanzado"

    auditoria = await _sql(tid, "select count(*) from audit_log where tenant_id = :t and action = 'settings.coordinator'", t=tid)
    assert auditoria == [(2,)]

    # La release congela las reglas y el nivel; la vista de recepción los aplica.
    nueva, release = await _release(tid, cid)
    assert nueva.manifest_hash != base.manifest_hash
    assert [g["name"] for g in release.recepcion["guardrails"]] == ["sin_descuentos"]
    assert release.recepcion["nivel_ia"] == "avanzado"
    coordinador = next(c for c in release.componentes if c.role == "core")
    vista = vista_de_recepcion(coordinador, recepcion=release.recepcion)
    assert [g["name"] for g in vista.content["guardrails"]] == ["sin_descuentos"]
    assert vista.content["model"]["primary"] == niveles.modelos_de("avanzado").primary

    # Volver a la configuración por defecto devuelve el mismo hash que la release inicial.
    r = await app_client.put(URL, headers=h, json={"guardrails": [], "nivel_ia": "estandar"})
    assert r.status_code == 200, r.text
    # (La voz y el horario sí cambiaron, así que el hash difiere: se comprueba que la clave ya no viaja.)
    _, vuelta = await _release(tid, cid)
    assert vuelta.recepcion == {}


async def test_reglas_invalidas_dan_422_por_campo_y_no_guardan(app_client) -> None:
    tid, _cid = await _negocio_sin_especialistas()
    h = await _dueno(app_client, tid)
    r = await app_client.put(
        URL,
        headers=h,
        json={"guardrails": [{"name": "x", "kind": "required_tool", "phrases": ["envío"], "tool": "cotizar"}]},
    )
    assert r.status_code == 422, r.text
    assert "guardrails[0].tool" in r.json()["error"]["fields"]
    assert (await app_client.get(URL, headers=h)).json()["guardrails"] == []


async def test_una_regla_guardada_que_ya_no_vale_hace_fallar_el_build(app_client) -> None:
    from core.releases.build import ErrorDelBuild

    tid, cid = await _negocio_sin_especialistas()
    await _sql(
        tid,
        "update app_settings set behavior_config = coalesce(behavior_config, '{}'::jsonb) || "
        "'{\"recepcion_guardrails\": [{\"name\": \"x\", \"kind\": \"inventada\"}]}'::jsonb where tenant_id = :t",
        t=tid,
    )
    with pytest.raises(ErrorDelBuild) as exc:
        await _release(tid, cid)
    assert exc.value.codigo == "reglas_recepcion_invalidas"


async def test_las_rutas_del_agente_coordinador_siguen_cerradas(app_client) -> None:
    tid, _cid = await _negocio_sin_especialistas()
    h = await _dueno(app_client, tid)
    [(coordinador,)] = await _sql(tid, "select id from agents where key = 'coordinador'")
    for ruta, cuerpo in (
        ("guardrails", {"guardrails": [], "lock_version": 1}),
        ("declarative-tools", {"tools": [], "secrets": {}, "lock_version": 1}),
    ):
        r = await app_client.put(f"/api/v1/admin/agents/{coordinador}/{ruta}", headers=h, json=cuerpo)
        assert (r.status_code, r.json()["error"]["code"]) == (409, "coordinador_inmutable"), (ruta, r.text)


async def test_aislamiento_entre_negocios(app_client) -> None:
    tid_a, _ = await _negocio_sin_especialistas()
    tid_b, _cid_b = await _negocio_sin_especialistas()
    ha, hb = await _dueno(app_client, tid_a), await _dueno(app_client, tid_b)
    r = await app_client.put(URL, headers=ha, json={"assistant_name": "Solo A", "guardrails": [REGLA]})
    assert r.status_code == 200, r.text
    b = (await app_client.get(URL, headers=hb)).json()
    assert b["assistant_name"] != "Solo A" and b["guardrails"] == []

    # La release de B no hereda lo de A, y B no deja rastro en la auditoría de A.
    _, release_b = await _release(tid_b, _cid_b)
    assert release_b.recepcion == {}
    r = await app_client.put(URL, headers=hb, json={"brand_voice": "voz de B"})
    assert r.status_code == 200, r.text
    assert (await app_client.get(URL, headers=ha)).json()["brand_voice"] != "voz de B"
    filas = await _sql(tid_a, "select count(*) from audit_log where tenant_id = :t and action = 'settings.coordinator'", t=tid_a)
    assert filas == [(1,)]
    # Un dueño de A no puede operar con el negocio de B.
    cruzado = {**ha, "X-Tenant-Id": str(tid_b)}
    assert (await app_client.get(URL, headers=cruzado)).status_code == 403
    assert (await app_client.put(URL, headers=cruzado, json={"brand_voice": "x"})).status_code == 403


async def test_horario_y_zona_invalidos_dan_422_y_no_borran_el_horario(app_client) -> None:
    tid, _cid = await _negocio_sin_especialistas()
    h = await _dueno(app_client, tid)
    ok = await app_client.put(URL, headers=h, json={"business_ranges": [{"day": 1, "from": "08:00", "to": "17:00"}]})
    assert ok.status_code == 200, ok.text
    for cuerpo in (
        {"business_ranges": [{"day": 1, "from": "99:99", "to": "17:00"}]},
        {"business_ranges": [{"day": 1, "from": "18:00", "to": "09:00"}]},
        {"business_tz": "Marte/Olimpo"},
    ):
        r = await app_client.put(URL, headers=h, json=cuerpo)
        assert r.status_code == 422, (cuerpo, r.text)
    asi = (await app_client.get(URL, headers=h)).json()
    assert asi["business_ranges"] == [{"day": 1, "from": "08:00", "to": "17:00"}]
    # La respuesta serializa la clave «from» (alias), no «from_».
    assert "from_" not in asi["business_ranges"][0]
