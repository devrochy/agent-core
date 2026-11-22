"""HU-BILL-25 · historial por plan y exportación de conversaciones contra Postgres real (D-79, SPEC-51).

Decisión del dueño (2026-11-22): el historial **solo se oculta en el panel**; nada se borra ni se archiva.

- Basic (90 días): la bandeja, el buscador y las recientes del resumen no muestran una conversación sin
  actividad en 90 días; pedirla por id da 409 `plan_feature_not_included` (`feature = historial`); de una
  conversación larga solo salen los mensajes de la ventana.
- Al subir a Empresarial se ve todo al instante, y nada se borró.
- La memoria del turno (`_load_history`) sigue leyendo los mensajes viejos.
- Exportar: Premium recibe un CSV solo con sus datos (otro negocio Premium con datos no se cuela);
  Basic recibe 409 `plan_feature_not_included` con `{feature: exportar, plan: basic}`.

Sin LLM ni red.
"""

from __future__ import annotations

import csv
import io
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from _api_helpers import api_headers, uniq
from sqlalchemy import text

pytestmark = [
    pytest.mark.aislamiento,  # historial y exportación por negocio
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

URL = "/api/v1/admin/conversations"


async def _sql(tid: uuid.UUID | None, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            resultado = await session.execute(text(sql), params)
            return list(resultado.all()) if getattr(resultado, "returns_rows", False) else []


async def _negocio(plan: str) -> dict[str, Any]:
    """Negocio en `plan` con tres conversaciones:

    - `vieja`: empezó y habló hace 200 días.
    - `larga`: empezó hace 200 días, con un mensaje de entonces y otro de ayer.
    - `nueva`: de hace una hora.
    """
    from _habilidades import onboard_tenant

    from core.db.models import Conversation, Customer, Message
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug = uniq("bill25")
    marca = uuid.uuid4().hex[:8]
    tid, _ = await onboard_tenant(name=f"BILL25 {slug}", slug=slug, sembrar_ejemplos=False, plan=plan)
    ahora = datetime.now(UTC)
    hace = ahora - timedelta(days=200)
    async with tenant_scope(tid):
        async with get_session() as session:
            def cliente(nombre: str) -> Customer:
                return Customer(tenant_id=tid, channel="whatsapp", external_id=f"whatsapp:{uniq('n')}", name=f"{nombre} {marca}")

            ana, beto, caro = cliente("Ana"), cliente("Beto"), cliente("Caro")
            session.add_all([ana, beto, caro])
            await session.flush()
            vieja = Conversation(tenant_id=tid, customer_id=ana.id, started_at=hace)
            larga = Conversation(tenant_id=tid, customer_id=beto.id, started_at=hace)
            nueva = Conversation(tenant_id=tid, customer_id=caro.id, started_at=ahora - timedelta(hours=1))
            session.add_all([vieja, larga, nueva])
            await session.flush()

            def msg(conv: Conversation, contenido: str, cuando: datetime) -> Message:
                return Message(
                    tenant_id=tid, conversation_id=conv.id, direction="inbound", sender_type="customer",
                    content=f"{contenido} {marca}", created_at=cuando,
                )

            session.add_all(
                [
                    msg(vieja, "viejo", hace),
                    msg(larga, "larga-de-antes", hace + timedelta(minutes=1)),
                    msg(larga, "larga-de-ayer", ahora - timedelta(days=1)),
                    msg(nueva, "nuevo", ahora - timedelta(minutes=30)),
                ]
            )
    return {"tid": tid, "marca": marca, "vieja": vieja.id, "larga": larga.id, "nueva": nueva.id}


async def _cambiar_plan(tid: uuid.UUID, plan: str) -> None:
    await _sql(tid, "update tenant_subscriptions set plan_id = :p where tenant_id = :t and deleted_at is null", p=plan, t=tid)


def _ids(r) -> set[str]:
    assert r.status_code == 200, r.text
    return {i["id"] for i in r.json()["items"]}


# ------------------------------------------------------------------ historial
async def test_basic_oculta_lo_viejo_y_al_subir_de_plan_vuelve(app_client) -> None:
    n = await _negocio("basic")
    tid = n["tid"]
    h = await api_headers(app_client, tid)

    # Bandeja: la vieja no sale; la larga sí (tuvo actividad ayer).
    assert _ids(await app_client.get(URL, headers=h)) == {str(n["larga"]), str(n["nueva"])}

    # Pedida por id: aviso claro, no 404.
    for ruta in (f"{URL}/{n['vieja']}", f"{URL}/{n['vieja']}/messages"):
        r = await app_client.get(ruta, headers=h)
        assert r.status_code == 409, r.text
        error = r.json()["error"]
        assert error["code"] == "plan_feature_not_included"
        assert error["details"] == {"feature": "historial", "plan": "basic", "historial_dias": 90}

    # De la larga solo salen los mensajes de la ventana.
    r = await app_client.get(f"{URL}/{n['larga']}", headers=h)
    assert r.status_code == 200, r.text
    assert [m["content"] for m in r.json()["messages"]] == [f"larga-de-ayer {n['marca']}"]
    r = await app_client.get(f"{URL}/{n['larga']}/messages", headers=h)
    assert [m["content"] for m in r.json()["items"]] == [f"larga-de-ayer {n['marca']}"]

    # Buscador y recientes del resumen tampoco la muestran.
    r = await app_client.get("/api/v1/search", params={"q": f"Ana {n['marca']}"}, headers=h)
    assert r.status_code == 200, r.text
    conversaciones = [s for s in r.json()["resultados"] if s["seccion"] == "conversations"]
    assert conversaciones and conversaciones[0]["items"] == []
    r = await app_client.get("/api/v1/admin/dashboard", headers=h)
    assert r.status_code == 200, r.text
    assert str(n["vieja"]) not in {c["id"] for c in r.json()["recent"]}

    # Sube a Empresarial: se ve todo al instante y nada se borró.
    await _cambiar_plan(tid, "empresarial")
    assert _ids(await app_client.get(URL, headers=h)) == {str(n["vieja"]), str(n["larga"]), str(n["nueva"])}
    r = await app_client.get(f"{URL}/{n['vieja']}", headers=h)
    assert r.status_code == 200, r.text
    r = await app_client.get(f"{URL}/{n['larga']}", headers=h)
    assert len(r.json()["messages"]) == 2
    r = await app_client.get("/api/v1/search", params={"q": f"Ana {n['marca']}"}, headers=h)
    conversaciones = [s for s in r.json()["resultados"] if s["seccion"] == "conversations"]
    assert [i["id"] for i in conversaciones[0]["items"]] == [str(n["vieja"])]


async def test_la_memoria_del_turno_no_se_filtra() -> None:
    """El turno lee la historia completa aunque el panel la oculte (Free, 30 días)."""
    from core.orchestrator.loop import _load_history
    from core.shared.tenant import tenant_scope

    n = await _negocio("free")  # conversación recién escrita: sin memoria de trabajo, cae a Postgres
    async with tenant_scope(n["tid"]):
        historia = await _load_history(n["vieja"])
    assert [m.content for m in historia] == [f"viejo {n['marca']}"]
    filas = await _sql(n["tid"], "select count(*) from messages where tenant_id = :t and deleted_at is null", t=n["tid"])
    assert int(filas[0][0]) == 4  # nada se borró ni se archivó


async def test_la_bandeja_de_otro_negocio_no_cambia_el_corte() -> None:
    """El corte es del plan de cada negocio: Basic ve 90 días y Empresarial todo, a la vez."""
    from core.api.historial import historial_del_panel

    basico, empresa = await _negocio("basic"), await _negocio("empresarial")
    assert (await historial_del_panel(basico["tid"])).dias == 90
    assert (await historial_del_panel(empresa["tid"])).desde is None


# ------------------------------------------------------------------ exportar
def _csv(r) -> list[dict[str, str]]:
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    cuerpo = r.text.lstrip("﻿")
    return list(csv.DictReader(io.StringIO(cuerpo)))


async def test_exportar_premium_trae_solo_lo_suyo(app_client) -> None:
    propio, ajeno = await _negocio("premium"), await _negocio("premium")
    h = await api_headers(app_client, propio["tid"])

    r = await app_client.get(f"{URL}/export.csv", headers=h)
    filas = _csv(r)
    assert r.headers["x-export-truncated"] == "false"
    # Premium ve 365 días: los 4 mensajes, una fila por mensaje.
    assert len(filas) == 4
    assert {f["Conversación"] for f in filas} == {str(propio["vieja"]), str(propio["larga"]), str(propio["nueva"])}
    assert all(propio["marca"] in f["Contenido"] for f in filas)
    texto = r.text
    assert ajeno["marca"] not in texto
    for conv in ("vieja", "larga", "nueva"):
        assert str(ajeno[conv]) not in texto
    assert list(filas[0]) == [
        "Conversación", "Cliente", "Identificador", "Canal", "Conexión", "Estado de la conversación", "Inicio",
        "Cierre", "Mensaje", "Fecha del mensaje", "Dirección", "Remitente", "Estado del mensaje", "Contenido",
        "Tipo de archivo", "Transcripción",
    ]


async def test_exportar_respeta_el_historial(app_client) -> None:
    """Con un plan que exporta pero recorta (Premium, 365 días), lo de antes del corte no sale."""
    n = await _negocio("premium")
    await _sql(
        n["tid"],
        "update messages set created_at = now() - interval '400 days' where tenant_id = :t and conversation_id = :c",
        t=n["tid"], c=n["vieja"],
    )
    h = await api_headers(app_client, n["tid"])
    filas = _csv(await app_client.get(f"{URL}/export.csv", headers=h))
    assert str(n["vieja"]) not in {f["Conversación"] for f in filas}
    assert len(filas) == 3


async def test_exportar_en_basic_es_409(app_client) -> None:
    n = await _negocio("basic")
    h = await api_headers(app_client, n["tid"])
    r = await app_client.get(f"{URL}/export.csv", headers=h)
    assert r.status_code == 409, r.text
    error = r.json()["error"]
    assert error["code"] == "plan_feature_not_included"
    assert error["details"] == {"feature": "exportar", "plan": "basic"}
