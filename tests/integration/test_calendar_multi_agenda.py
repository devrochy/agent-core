"""Varias agendas por negocio (HU-CAL-08, ADR-0170): el cliente elige con cuál quiere la cita.

Cubre la elección de agenda del asistente (con 1 agenda nada cambia, con 2 el cliente elige y
la bandera `multi_agenda_modo` lo apaga), el solape POR agenda, el CRUD de agendas del panel y
las cuentas de Google que no mezclan sus tokens.
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import uniq
from test_calendar_interno import FECHA, _configurar, _contexto, _seed_email, _tenant_calendario

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _segunda_agenda(tenant_id: uuid.UUID, nombre: str = "Dra. Ana", **extra) -> uuid.UUID:
    from pack_calendar import agendas
    from pack_calendar.models import Calendar

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        await agendas.ensure_default()
        async with get_session() as session:
            fila = Calendar(tenant_id=tenant_id, name=nombre, color="cat-2", sort_order=1, **extra)
            session.add(fila)
            await session.flush()
            nueva = fila.id
        agendas.invalidar(tenant_id)
    return nueva


async def _preparar(correo: str = "laura@ejemplo.com"):
    from pack_calendar.skills import CalendarSkill

    tenant_id = await _tenant_calendario()
    await _configurar(tenant_id)
    ctx = _contexto()
    await _seed_email(tenant_id, ctx.conversation_id, correo)
    return tenant_id, CalendarSkill(), ctx


def _cita(hora: str, **extra) -> dict:
    return {
        "title": "Cita",
        "start_datetime": f"{FECHA}T{hora}:00",
        "attendee_email": "laura@ejemplo.com",
        **extra,
    }


async def test_con_una_agenda_nada_cambia() -> None:
    from core.shared.tenant import tenant_scope

    tenant_id, skill, ctx = await _preparar()
    async with tenant_scope(tenant_id):
        assert not await skill._predicado_multi(set())  # list_calendars no se ofrece
        avail = await skill.check_availability({"date": FECHA}, ctx)
        assert "calendars" not in avail and "multi_agenda" not in avail
        assert "10:00" in avail["free_slots"]
        creada = await skill.create_event(_cita("10:00"), ctx)
        assert creada["status"] == "created"  # sin `calendar`: no se pide


async def test_dos_agendas_el_cliente_elige_y_el_solape_es_por_agenda() -> None:
    from core.shared.tenant import tenant_scope

    tenant_id, skill, ctx = await _preparar()
    await _segunda_agenda(tenant_id, aliases=["doctora", "sede norte"])
    async with tenant_scope(tenant_id):
        assert await skill._predicado_multi(set())
        listadas = await skill.list_calendars({}, ctx)
        assert [c["nombre"] for c in listadas["calendars"]] == ["Agenda principal", "Dra. Ana"]
        assert not any(c["agenda_anterior"] for c in listadas["calendars"])

        # Sin elegir agenda no se crea nada y se le pasan las opciones al modelo.
        pendiente = await skill.create_event(_cita("10:00"), ctx)
        assert pendiente["status"] == "needs_data" and pendiente["missing"] == ["calendar"]
        assert len(pendiente["calendars"]) == 2

        # Por alias, y la misma hora en la OTRA agenda también se puede.
        assert (await skill.create_event(_cita("10:00", calendar="doctora"), ctx))["status"] == "created"
        otra = await skill.create_event(_cita("10:00", calendar="Agenda principal"), ctx)
        assert otra["status"] == "created"
        # Pero no dos veces en la misma.
        repetida = await skill.create_event(_cita("10:00", calendar="Dra. Ana"), ctx)
        assert repetida["status"] == "rejected" and repetida["reason"] == "occupied"

        # Agenda inexistente: no inventa, ofrece las opciones.
        mala = await skill.create_event(_cita("11:00", calendar="Dr. Nadie"), ctx)
        assert mala["status"] == "needs_data" and mala["missing"] == ["calendar"]

        # Con la historia del cliente, la última agenda usada se marca como anterior.
        listadas = await skill.list_calendars({}, ctx)
        assert [c["agenda_anterior"] for c in listadas["calendars"]] == [True, False] or [
            c["agenda_anterior"] for c in listadas["calendars"]
        ] == [False, True]


async def test_disponibilidad_por_agenda_y_hora_deseada() -> None:
    from core.shared.tenant import tenant_scope

    tenant_id, skill, ctx = await _preparar()
    await _segunda_agenda(tenant_id, aliases=["doctora"])
    async with tenant_scope(tenant_id):
        assert (await skill.create_event(_cita("10:00", calendar="Dra. Ana"), ctx))["status"] == "created"

        todas = await skill.check_availability({"date": FECHA, "desired_time": "10:00"}, ctx)
        assert todas["multi_agenda"] is True
        por_nombre = {c["nombre"]: c["free_slots"] for c in todas["calendars"]}
        assert "10:00" not in por_nombre["Dra. Ana"] and "10:00" in por_nombre["Agenda principal"]
        assert todas["disponibles_a_esa_hora"] == ["Agenda principal"]
        assert todas["libres_por_hora"]["10:00"] == ["Agenda principal"]
        assert todas["alternativas"] == [{"nombre": "Dra. Ana", "mas_cercano": "09:00"}] or todas[
            "alternativas"
        ] == [{"nombre": "Dra. Ana", "mas_cercano": "11:00"}]

        una = await skill.check_availability({"date": FECHA, "calendar": "doctora"}, ctx)
        assert "10:00" not in una["free_slots"] and una["calendar"]["nombre"] == "Dra. Ana"

        proximos = await skill.find_next_available({"days": 3, "desired_time": "10:00"}, ctx)
        assert proximos["status"] == "ok" and proximos["multi_agenda"] is True
        assert proximos["days"][0]["calendars"]


async def test_bandera_desactivado_vuelve_a_una_sola_agenda() -> None:
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    tenant_id, skill, ctx = await _preparar()
    await _segunda_agenda(tenant_id)
    async with tenant_scope(tenant_id):
        cfg = (await plugin_manager.load_config("pack_calendar")).model_dump(mode="json")
        cfg["client_secret"] = ""
        cfg["multi_agenda_modo"] = "desactivado"
        await plugin_manager.save_config("pack_calendar", cfg)
        assert not await skill._predicado_multi(set())
        avail = await skill.check_availability({"date": FECHA}, ctx)
        assert "calendars" not in avail
        creada = await skill.create_event(_cita("10:00"), ctx)
        assert creada["status"] == "created"  # va a la agenda principal sin preguntar


async def test_crud_de_agendas_en_la_api(app_client) -> None:
    from _api_helpers import api_headers

    tenant_id = await _tenant_calendario()
    await _configurar(tenant_id)
    headers = await api_headers(app_client, tenant_id)
    base = "/api/v1/pack/calendar/calendars"

    inicial = (await app_client.get(base, headers=headers)).json()
    assert [c["name"] for c in inicial["calendars"]] == ["Agenda principal"]
    assert inicial["multi_activo"] is False
    principal_id = inicial["calendars"][0]["id"]

    creada = await app_client.post(
        base, headers=headers, json={"name": "  Dr.  Luis ", "aliases": ["doctor", "Doctor"], "color": "cat-3"}
    )
    assert creada.status_code == 201, creada.text
    luis = creada.json()
    assert luis["name"] == "Dr. Luis" and luis["aliases"] == ["doctor"] and luis["source"] == "interno"

    # Nombre repetido (sin acentos ni mayúsculas) y color fuera de la paleta.
    assert (await app_client.post(base, headers=headers, json={"name": "dr. luis"})).status_code == 400
    assert (await app_client.post(base, headers=headers, json={"name": "X", "color": "rojo"})).status_code == 400

    despues = (await app_client.get(base, headers=headers)).json()
    assert despues["multi_activo"] is True

    # La principal no se elimina ni se desactiva; la otra sí, y las citas viajan con `move_to`.
    assert (await app_client.post(f"{base}/{principal_id}/delete", headers=headers)).status_code == 400
    assert (
        await app_client.put(f"{base}/{principal_id}", headers=headers, json={"active": False})
    ).status_code == 400
    renombrada = await app_client.put(f"{base}/{luis['id']}", headers=headers, json={"name": "Dr. Luis Gómez"})
    assert renombrada.status_code == 200 and renombrada.json()["name"] == "Dr. Luis Gómez"
    assert (await app_client.post(f"{base}/{luis['id']}/delete", headers=headers)).status_code == 204
    assert [c["name"] for c in (await app_client.get(base, headers=headers)).json()["calendars"]] == [
        "Agenda principal"
    ]


async def test_cita_del_panel_exige_agenda_si_hay_varias(app_client) -> None:
    from _api_helpers import api_headers

    tenant_id = await _tenant_calendario()
    await _configurar(tenant_id)
    ana = await _segunda_agenda(tenant_id)
    headers = await api_headers(app_client, tenant_id)
    cita = {"title": "Visita", "start_at": f"{FECHA}T15:00:00+00:00", "end_at": f"{FECHA}T16:00:00+00:00"}

    sin_agenda = await app_client.post("/api/v1/pack/calendar/appointments", headers=headers, json=cita)
    assert sin_agenda.status_code == 400 and sin_agenda.json()["error"]["code"] == "agenda_requerida"
    con_agenda = await app_client.post(
        "/api/v1/pack/calendar/appointments", headers=headers, json={**cita, "calendar_id": str(ana)}
    )
    assert con_agenda.status_code == 201, con_agenda.text
    assert con_agenda.json()["calendar_id"] == str(ana)

    eventos = (
        await app_client.get(
            "/api/v1/pack/calendar/events",
            headers=headers,
            params={"start": f"{FECHA}T00:00:00+00:00", "end": f"{FECHA}T23:59:59+00:00"},
        )
    ).json()
    assert eventos["multi_activo"] is True and len(eventos["calendars"]) == 2
    [evento] = [e for e in eventos["events"] if e["kind"] == "appointment"]
    assert evento["calendar_id"] == str(ana) and evento["calendar_color"] == "cat-2"
    # El filtro por agenda deja fuera la cita de otra agenda.
    solo_principal = eventos["calendars"][0]["id"]
    filtrado = (
        await app_client.get(
            "/api/v1/pack/calendar/events",
            headers=headers,
            params={
                "start": f"{FECHA}T00:00:00+00:00",
                "end": f"{FECHA}T23:59:59+00:00",
                "calendar_ids": solo_principal,
            },
        )
    ).json()
    assert [e for e in filtrado["events"] if e["kind"] == "appointment"] == []


async def test_dos_cuentas_de_google_no_mezclan_sus_tokens(monkeypatch) -> None:
    """Una cuenta nueva sin refresh_token NO hereda el de otra cuenta (HU-CAL-08)."""
    from pack_calendar import oauth

    from core.gateway.connections import credentials_of, get_connection
    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_calendario()
    correos = {"tok-ana": "ana@x.com", "tok-luis": "luis@x.com"}

    async def _email(token: str) -> str:
        return correos[token]

    monkeypatch.setattr(oauth, "_account_email", _email)
    monkeypatch.setattr(oauth, "_escribir_copia_vieja", lambda _t: _noop())

    async with tenant_scope(tenant_id):
        ana_id = await oauth.save_tokens(
            {"access_token": "tok-ana", "refresh_token": "refresh-de-ana", "expires_at": 9999999999}
        )
        luis_id = await oauth.save_tokens({"access_token": "tok-luis", "expires_at": 9999999999})
        assert ana_id and luis_id and ana_id != luis_id
        luis = credentials_of(await get_connection(luis_id))
        assert luis["account_email"] == "luis@x.com"
        assert luis.get("refresh_token", "") == ""  # nunca el de Ana
        assert credentials_of(await get_connection(ana_id))["refresh_token"] == "refresh-de-ana"

        # Reconectar a Luis con la cuenta de Ana no pisa nada.
        with pytest.raises(oauth.OtraCuentaError):
            await oauth.save_tokens({"access_token": "tok-ana", "expires_at": 1}, connection_id=luis_id)
        assert credentials_of(await get_connection(luis_id))["account_email"] == "luis@x.com"

        # Reconectar a Ana con su misma cuenta conserva su refresh_token.
        await oauth.save_tokens({"access_token": "tok-ana", "expires_at": 9999999999}, connection_id=ana_id)
        assert credentials_of(await get_connection(ana_id))["refresh_token"] == "refresh-de-ana"


async def _noop() -> None:
    return None


async def test_vincular_conexion_crea_una_agenda_por_cuenta_sin_chocar_por_nombre() -> None:
    from pack_calendar import agendas
    from pack_calendar.config import CalendarConfig

    from core.gateway.connections import save_connection
    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_calendario()
    async with tenant_scope(tenant_id):
        config = CalendarConfig()  # modo interno: la principal queda interna
        ids = []
        for correo in ("ana.perez@x.com", "ana.perez@otro.com", "luis@x.com"):
            conexion, _ = await save_connection(
                "Google Calendar",
                {"access_token": "a", "refresh_token": "r", "expires_at": 1, "account_email": correo},
                type_id="google_calendar",
            )
            agenda, creada = await agendas.vincular_conexion(conexion.id, correo, config)
            assert creada and agenda.es_google
            ids.append(agenda)
        # Idempotente: reconectar la misma cuenta no crea otra agenda.
        _, otra_vez = await agendas.vincular_conexion(ids[0].connection_id, "ana.perez@x.com", config)
        assert not otra_vez
        nombres = [a.name for a in await agendas.listar(config)]
        assert nombres == ["Agenda principal", "Ana Perez", "Ana Perez 2", "Luis"]
        assert len({a.color for a in ids}) >= 2  # la paleta rota
    _ = uniq


async def test_cita_sin_agenda_cae_en_la_principal_y_choca_con_ella() -> None:
    """El trigger evita que `NULL` y el id de la principal sean «agendas distintas» (pods viejos)."""
    from datetime import UTC, datetime, timedelta

    from pack_calendar import agendas
    from pack_calendar.models import Appointment
    from sqlalchemy.exc import IntegrityError

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_calendario()
    inicio = datetime(2027, 4, 5, 15, tzinfo=UTC)

    def _fila(**extra) -> Appointment:
        return Appointment(
            tenant_id=tenant_id, title="x", start_at=inicio, end_at=inicio + timedelta(hours=1), **extra
        )

    async with tenant_scope(tenant_id):
        principal = await agendas.ensure_default()
        async with get_session() as session:
            vieja = _fila()  # un pod anterior a 0006 escribe sin calendar_id
            session.add(vieja)
            await session.flush()
            await session.refresh(vieja)  # el trigger la rellena en la base
            assert vieja.calendar_id == principal.id
        with pytest.raises(IntegrityError):
            async with get_session() as session:
                session.add(_fila(calendar_id=principal.id))
                await session.flush()
