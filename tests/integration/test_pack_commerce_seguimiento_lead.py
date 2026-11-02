"""HU-CRM-21 · Próxima acción y fecha de seguimiento del lead.

Con la BD real: una persona programa, mueve, quita y completa el seguimiento desde la ficha; la lista
lo marca («Vencido»), lo filtra, lo cuenta y lo ordena; todo lo que cuenta como «hoy» es la zona del
negocio; el cierre queda en la línea de tiempo con su nota; programar no mueve la actividad del lead;
nada cruza negocios.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from _api_helpers import api_headers, uniq

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

URL = "/api/v1/admin/leads"
PACK = "pack_commerce"


async def _negocio(zona: str | None = None) -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq("seg")
    tid, _ = await onboard_tenant(
        name=f"Seguimiento {slug}", slug=slug, persona={"tone": "cercano"}, admin_email="admin@local",
        packs=["pack_commerce"], sembrar_ejemplos=False,
    )
    if zona:
        from sqlalchemy import select

        from core.db.models import AppSettings
        from core.db.session import get_session
        from core.shared.tenant import tenant_scope

        async with tenant_scope(tid):
            async with get_session() as session:
                fila = (await session.execute(select(AppSettings).where(AppSettings.tenant_id == tid))).scalar_one()
                fila.timezone = zona
    return tid


async def _cliente_con_lead(tid: uuid.UUID, nombre: str = "Ana", **campos) -> uuid.UUID:
    from pack_commerce.models import Lead

    from core.db.models import Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            cliente = Customer(tenant_id=tid, channel="whatsapp", external_id=f"whatsapp:{uniq('n')}", name=nombre)
            session.add(cliente)
            await session.flush()
            session.add(Lead(tenant_id=tid, customer_id=cliente.id, **campos))
            return cliente.id


async def _lead(tid: uuid.UUID, cliente: uuid.UUID):
    from pack_commerce.models import Lead
    from sqlalchemy import select

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return (await session.execute(select(Lead).where(Lead.customer_id == cliente))).scalar_one()


async def _historia(tid: uuid.UUID, cliente: uuid.UUID) -> list[tuple[str, str | None]]:
    from pack_commerce.models import LeadFollowup
    from sqlalchemy import select

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            filas = (
                await session.execute(
                    select(LeadFollowup.outcome, LeadFollowup.note)
                    .where(LeadFollowup.customer_id == cliente)
                    .order_by(LeadFollowup.closed_at, LeadFollowup.id)
                )
            ).all()
    return [(f.outcome, f.note) for f in filas]


def _patch(app_client, cliente, headers, **valores):
    return app_client.patch(f"{URL}/{cliente}/pack/{PACK}", json={"valores": valores}, headers=headers)


def _fila(r, cliente) -> dict:
    return next(i for i in r.json()["items"] if i["id"] == str(cliente))


# ---------------------------------------------------------------- Gherkin
async def test_programar_se_ve_en_la_ficha_y_en_la_lista(app_client) -> None:
    tid = await _negocio()
    cliente = await _cliente_con_lead(tid)
    headers = await api_headers(app_client, tid)

    r = await _patch(app_client, cliente, headers, seguimiento_nota="Enviar cotización", seguimiento_en="2031-05-04T09:00")
    assert r.status_code == 200, r.text

    # La ficha lo devuelve en hora del negocio (Bogotá por defecto), listo para el selector.
    editables = {e["key"]: e for e in r.json()["editables"] if e["plugin_id"] == PACK}
    assert editables["seguimiento_en"]["tipo"] == "fecha"
    assert editables["seguimiento_en"]["value"] == "2031-05-04T09:00"
    assert editables["seguimiento_nota"]["value"] == "Enviar cotización"
    assert r.json()["extras"]["seguimiento_en"] == "2031-05-04T14:00:00+00:00"  # UTC

    lista = await app_client.get(URL, headers=headers)
    fila = _fila(lista, cliente)
    assert fila["extras"]["seguimiento_nota"] == "Enviar cotización"
    assert fila["extras"]["seguimiento_estado"] == "mas_adelante"
    assert any(c["key"] == "seguimiento_nota" for c in lista.json()["columns"])
    assert any(f["key"] == "seguimiento" for f in lista.json()["filters"])


async def test_programar_no_mueve_la_actividad_del_lead(app_client) -> None:
    tid = await _negocio()
    antes = datetime.now(UTC) - timedelta(days=3)
    cliente = await _cliente_con_lead(tid, last_activity_at=antes)
    headers = await api_headers(app_client, tid)

    r = await _patch(app_client, cliente, headers, seguimiento_nota="Llamar", seguimiento_en="2031-05-04T09:00")
    assert r.status_code == 200, r.text
    assert (await _lead(tid, cliente)).last_activity_at == antes  # ADR-0149


async def test_filtros_pestañas_y_orden(app_client) -> None:
    from pack_commerce import seguimiento as seg

    tid = await _negocio()
    headers = await api_headers(app_client, tid)
    zona = ZoneInfo("America/Bogota")
    lim = seg.limites(zona)
    ahora = lim.ahora
    cuando = {
        "vencidos": ahora - timedelta(days=2),
        "hoy": ahora + (lim.fin_de_hoy - ahora) / 2,
        "proximos_7_dias": lim.fin_de_hoy + timedelta(hours=3),
        "mas_adelante": lim.fin_de_proximos + timedelta(hours=3),
        "sin_seguimiento": None,
    }
    ids = {}
    for clave, en in cuando.items():
        ids[clave] = await _cliente_con_lead(tid, nombre=clave, next_action_at=en, next_action_note=clave if en else None)

    for clave, cliente in ids.items():
        r = await app_client.get(URL, params={"filtro": f"seguimiento:{clave}"}, headers=headers)
        assert r.status_code == 200, r.text
        assert [i["id"] for i in r.json()["items"]] == [str(cliente)], clave

    r = await app_client.get(URL, params={"facetas": "seguimiento"}, headers=headers)
    faceta = next(f for f in r.json()["facetas"] if f["key"] == "seguimiento")
    assert faceta["counts"] == {clave: 1 for clave in cuando}  # una sola opción por cliente: suman el total
    assert faceta["total"] == 5

    # Más atrasados primero; sin seguimiento al final.
    r = await app_client.get(URL, params={"orden": "seguimiento"}, headers=headers)
    assert [i["cliente"] for i in r.json()["items"]] == [
        "vencidos", "hoy", "proximos_7_dias", "mas_adelante", "sin_seguimiento"
    ]
    assert _fila(r, ids["vencidos"])["extras"]["seguimiento_estado"] == "vencidos"
    assert _fila(r, ids["hoy"])["extras"]["seguimiento_estado"] == "hoy"


async def test_hoy_es_el_dia_del_negocio_no_el_de_utc(app_client) -> None:
    from pack_commerce import seguimiento as seg

    # Un negocio en Auckland (UTC+12/13): su «hoy» no coincide con el día UTC la mayor parte del día.
    tid = await _negocio("Pacific/Auckland")
    headers = await api_headers(app_client, tid)
    zona = ZoneInfo("Pacific/Auckland")
    lim = seg.limites(zona)
    cliente = await _cliente_con_lead(tid, next_action_at=lim.fin_de_hoy - timedelta(minutes=1))
    otro = await _cliente_con_lead(tid, nombre="Mañana", next_action_at=lim.fin_de_hoy + timedelta(minutes=1))

    r = await app_client.get(URL, params={"filtro": "seguimiento:hoy"}, headers=headers)
    assert [i["id"] for i in r.json()["items"]] == [str(cliente)]
    r = await app_client.get(URL, params={"filtro": "seguimiento:proximos_7_dias"}, headers=headers)
    assert [i["id"] for i in r.json()["items"]] == [str(otro)]

    # Lo que se escribe sin zona es hora del negocio.
    r = await _patch(app_client, cliente, headers, seguimiento_nota="x", seguimiento_en="2031-05-04T09:00")
    assert r.status_code == 200, r.text
    assert (await _lead(tid, cliente)).next_action_at == datetime(2031, 5, 3, 21, 0, tzinfo=UTC)  # 09:00 NZST = UTC+12


async def test_la_nota_tiene_tope_y_requiere_fecha(app_client) -> None:
    tid = await _negocio()
    cliente = await _cliente_con_lead(tid)
    headers = await api_headers(app_client, tid)

    r = await _patch(app_client, cliente, headers, seguimiento_nota="x" * 201, seguimiento_en="2031-05-04T09:00")
    assert r.status_code == 400 and "seguimiento_nota" in r.json()["error"]["fields"], r.text
    r = await _patch(app_client, cliente, headers, seguimiento_nota="x" * 200, seguimiento_en="2031-05-04T09:00")
    assert r.status_code == 200, r.text

    r = await _patch(app_client, cliente, headers, seguimiento_en="no es fecha")
    assert r.status_code == 400 and "seguimiento_en" in r.json()["error"]["fields"], r.text

    sin_fecha = await _cliente_con_lead(tid, nombre="Sin fecha")
    r = await _patch(app_client, sin_fecha, headers, seguimiento_nota="Solo una nota")
    assert r.status_code == 400, r.text
    assert (await _lead(tid, sin_fecha)).next_action_note is None


async def test_mover_o_quitar_deja_historia_y_se_ve_en_la_linea_de_tiempo(app_client) -> None:
    tid = await _negocio()
    cliente = await _cliente_con_lead(tid)
    headers = await api_headers(app_client, tid)

    await _patch(app_client, cliente, headers, seguimiento_nota="Primera", seguimiento_en="2031-05-04T09:00")
    assert await _historia(tid, cliente) == []  # programar por primera vez no cierra nada
    await _patch(app_client, cliente, headers, seguimiento_nota="Primera", seguimiento_en="2031-05-04T09:00")
    await _patch(app_client, cliente, headers, seguimiento_nota="Primera, con otra nota")
    assert await _historia(tid, cliente) == []  # misma fecha: solo cambia la nota
    await _patch(app_client, cliente, headers, seguimiento_nota="Segunda", seguimiento_en="2031-05-06T10:00")
    await _patch(app_client, cliente, headers, seguimiento_en="")  # quitar
    lead = await _lead(tid, cliente)
    assert lead.next_action_at is None and lead.next_action_note is None
    assert await _historia(tid, cliente) == [
        ("reprogramado", "Primera, con otra nota"),
        ("cancelado", "Segunda"),
    ]

    eventos = (await app_client.get(f"{URL}/{cliente}/timeline", headers=headers)).json()["items"]
    segs = [e for e in eventos if e["kind"] == "seguimiento"]
    assert [e["title"] for e in segs] == [
        "Seguimiento cancelado: Segunda",
        "Seguimiento reprogramado: Primera, con otra nota",
    ]
    assert segs[1]["detail"] == "Estaba para el 04/05/2031 09:00 · Lo hizo una persona"


async def test_completar_queda_en_la_historia_y_programa_el_siguiente(app_client) -> None:
    tid = await _negocio()
    cliente = await _cliente_con_lead(tid)
    headers = await api_headers(app_client, tid)
    hecho = f"/api/v1/pack/commerce/leads/{cliente}/seguimiento/hecho"

    r = await app_client.post(hecho, json={}, headers=headers)
    assert r.status_code == 400, r.text  # no hay nada que completar

    await _patch(app_client, cliente, headers, seguimiento_nota="Enviar cotización", seguimiento_en="2031-05-04T09:00")
    r = await app_client.post(
        hecho, json={"siguiente_en": "2031-05-08T15:00", "siguiente_nota": "Confirmar pago"}, headers=headers
    )
    assert r.status_code == 204, r.text
    lead = await _lead(tid, cliente)
    assert (lead.next_action_at, lead.next_action_note) == (datetime(2031, 5, 8, 20, 0, tzinfo=UTC), "Confirmar pago")
    assert await _historia(tid, cliente) == [("hecho", "Enviar cotización")]

    r = await app_client.post(hecho, json={}, headers=headers)  # sin siguiente: queda sin seguimiento
    assert r.status_code == 204, r.text
    assert (await _lead(tid, cliente)).next_action_at is None
    assert await _historia(tid, cliente) == [("hecho", "Enviar cotización"), ("hecho", "Confirmar pago")]

    r = await app_client.post(hecho, json={"siguiente_nota": "sin fecha"}, headers=headers)
    assert r.status_code == 400, r.text


async def test_sin_lead_programar_lo_crea_y_completar_no(app_client) -> None:
    from core.db.models import Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    headers = await api_headers(app_client, tid)
    async with tenant_scope(tid):
        async with get_session() as session:
            c = Customer(tenant_id=tid, channel="whatsapp", external_id=f"whatsapp:{uniq('n')}", name="Sin lead")
            session.add(c)
            await session.flush()
            cliente = c.id

    r = await app_client.post(
        f"/api/v1/pack/commerce/leads/{cliente}/seguimiento/hecho", json={}, headers=headers
    )
    assert r.status_code == 404, r.text

    r = await _patch(app_client, cliente, headers, seguimiento_nota="Primer contacto", seguimiento_en="2031-05-04T09:00")
    assert r.status_code == 200, r.text  # «Crear lead» en el mismo gesto
    assert (await _lead(tid, cliente)).next_action_note == "Primer contacto"


async def test_el_agente_no_tiene_como_cambiar_el_seguimiento() -> None:
    from pack_commerce.skills import LeadSkill

    sus_args = {"seguimiento_en", "seguimiento_nota", "next_action_at", "next_action_note"}
    for nombre in ("actualizar_lead", "calificar_lead", "crear_lead"):
        esquema = getattr(LeadSkill, nombre).__doc__ or ""
        assert not any(a in esquema for a in sus_args)
    import inspect

    assert "next_action" not in inspect.getsource(LeadSkill)


async def test_cada_negocio_ve_solo_sus_seguimientos(app_client) -> None:
    vencido = datetime.now(UTC) - timedelta(days=1)
    mio, ajeno = await _negocio(), await _negocio()
    suyo = await _cliente_con_lead(mio, nombre="Mío", next_action_at=vencido, next_action_note="mío")
    await _cliente_con_lead(ajeno, nombre="Ajeno", next_action_at=vencido, next_action_note="ajeno")
    headers = await api_headers(app_client, mio)

    r = await app_client.get(URL, params={"filtro": "seguimiento:vencidos", "facetas": "seguimiento"}, headers=headers)
    assert [i["id"] for i in r.json()["items"]] == [str(suyo)]
    assert r.json()["facetas"][0]["counts"]["vencidos"] == 1
    r = await app_client.get(URL, params={"orden": "seguimiento"}, headers=headers)
    assert [i["cliente"] for i in r.json()["items"]] == ["Mío"]

    # «Hecho» y la línea de tiempo tampoco cruzan negocios: un cliente ajeno es un 404 y no se toca.
    ajeno_id = await _cliente_con_lead(ajeno, nombre="Ajeno 2", next_action_at=vencido, next_action_note="ajeno 2")
    r = await app_client.post(f"/api/v1/pack/commerce/leads/{ajeno_id}/seguimiento/hecho", json={}, headers=headers)
    assert r.status_code == 404, r.text
    assert (await _lead(ajeno, ajeno_id)).next_action_note == "ajeno 2"
    r = await app_client.get(f"{URL}/{ajeno_id}/timeline", headers=headers)
    assert r.status_code == 404, r.text


async def test_un_orden_desconocido_se_rechaza(app_client) -> None:
    tid = await _negocio()
    headers = await api_headers(app_client, tid)
    r = await app_client.get(URL, params={"orden": "cualquiera"}, headers=headers)
    assert r.status_code == 422, r.text
