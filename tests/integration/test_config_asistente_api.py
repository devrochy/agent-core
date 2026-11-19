"""Asistente de configuración por la API: vista previa sin escribir, aplicar todo o nada."""

from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

BASE = "/api/v1/admin/settings"


async def _negocio(app_client) -> tuple[str, str]:
    """Un negocio nuevo y la cookie de un admin que actúa sobre él."""
    from core.alta_negocio import onboard_tenant

    slug = f"asis-{os.urandom(3).hex()}"
    tid, _ = await onboard_tenant(
        name=f"Asistente {slug}",
        slug=slug,
        business_description="Test.",
        persona={"tone": "cercano"},
        admin_email="admin@local",
    )
    r = await app_client.post("/admin/login", data={"email": "admin@local", "password": "admin123"})
    assert r.status_code == 303, r.text
    session = r.headers["set-cookie"].split(";")[0]
    r2 = await app_client.post("/admin/switch-tenant", headers={"Cookie": session}, data={"tenant_id": str(tid)})
    tenant_cookie = next(c.split(";")[0] for c in r2.headers.get_list("set-cookie") if "ac_tenant" in c)
    return str(tid), f"{session}; {tenant_cookie}"


async def _identidad(app_client, cookie: str) -> dict:
    r = await app_client.get(BASE, headers={"Cookie": cookie})
    assert r.status_code == 200, r.text
    return r.json()["identity"]


async def test_el_catalogo_trae_el_guion_del_asistente(app_client) -> None:
    _, cookie = await _negocio(app_client)
    r = await app_client.get(f"{BASE}/catalog", headers={"Cookie": cookie})
    assert r.status_code == 200, r.text
    secciones = {g["seccion"]: [q["id"] for q in g["preguntas"]] for g in r.json()["asistente"]}
    # La voz sale de la release (ADR-0191), así que solo queda lo que no es persona.
    assert secciones["voz"] == ["largo"]
    assert "horario" in secciones["atencion"]
    assert "aos_runtime" not in r.json()


async def test_vista_previa_no_escribe_y_aplicar_guarda_ambos_almacenes(app_client) -> None:
    _, cookie = await _negocio(app_client)
    antes = await _identidad(app_client, cookie)
    respuestas = {"tono": "directo", "largo": "detalladas"}

    voz = await app_client.post(
        f"{BASE}/asistente/voz/vista-previa", headers={"Cookie": cookie}, json={"respuestas": respuestas}
    )
    atencion = await app_client.post(
        f"{BASE}/asistente/atencion/vista-previa", headers={"Cookie": cookie}, json={"respuestas": respuestas}
    )
    assert voz.status_code == 200, voz.text
    assert atencion.status_code == 422  # «tono» no es una pregunta de esa sección
    cambios = {c["clave"]: c["propuesto"] for c in voz.json()["cambios"]}
    assert cambios == {"split_style": "pocos_cortos"}
    # Configuración ya no edita agentes (ADR-0191 §4): lo de la persona se omite y se dice por qué.
    assert {o["clave"] for o in voz.json()["omitidos"]} == {"persona_tone", "persona_warmth", "persona_max_length"}
    assert await _identidad(app_client, cookie) == antes  # la vista previa no escribió nada

    aplicado = await app_client.post(
        f"{BASE}/asistente/voz/aplicar",
        headers={"Cookie": cookie},
        json={"respuestas": respuestas, "huella": voz.json()["huella"]},
    )
    assert aplicado.status_code == 200, aplicado.text
    despues = await _identidad(app_client, cookie)
    assert despues["persona_tone"] == antes["persona_tone"]  # la persona no se toca en v3
    assert despues["persona_max_length"] == antes["persona_max_length"]
    assert despues["split_style"] == "pocos_cortos"  # behavior_config
    assert despues["assistant_name"] == antes["assistant_name"]  # lo que el asistente no toca se conserva


async def test_aplicar_con_huella_vieja_responde_409_y_no_escribe(app_client) -> None:
    _, cookie = await _negocio(app_client)
    antes = await _identidad(app_client, cookie)
    r = await app_client.post(
        f"{BASE}/asistente/voz/aplicar",
        headers={"Cookie": cookie},
        json={"respuestas": {"tono": "directo"}, "huella": "no-es-la-que-se-vio"},
    )
    assert r.status_code == 409, r.text
    assert await _identidad(app_client, cookie) == antes


async def test_atencion_horario_y_escalamiento_conservan_palabras_propias(app_client) -> None:
    _, cookie = await _negocio(app_client)
    actual = await _identidad(app_client, cookie)
    actual["escalation_keywords"] = ["mi frase"]
    r = await app_client.put(f"{BASE}/identity", headers={"Cookie": cookie}, json=actual)
    assert r.status_code == 200, r.text

    respuestas = {"horario": "semana", "fuera_de_horario": "responde", "escalar_motivos": ["pago"]}
    previa = await app_client.post(
        f"{BASE}/asistente/atencion/vista-previa", headers={"Cookie": cookie}, json={"respuestas": respuestas}
    )
    assert previa.status_code == 200, previa.text
    r2 = await app_client.post(
        f"{BASE}/asistente/atencion/aplicar",
        headers={"Cookie": cookie},
        json={"respuestas": respuestas, "huella": previa.json()["huella"]},
    )
    assert r2.status_code == 200, r2.text
    despues = await _identidad(app_client, cookie)
    assert [f["day"] for f in despues["business_ranges"]] == [0, 1, 2, 3, 4]
    assert despues["business_hours_mode"] == "informativo"
    assert despues["escalation_keywords"] == ["mi frase", "me cobraron", "cobro doble", "pago no llegó", "reembolso"]


async def test_seccion_desconocida_responde_404(app_client) -> None:
    _, cookie = await _negocio(app_client)
    r = await app_client.post(f"{BASE}/asistente/cobros/vista-previa", headers={"Cookie": cookie}, json={"respuestas": {}})
    assert r.status_code == 404, r.text
