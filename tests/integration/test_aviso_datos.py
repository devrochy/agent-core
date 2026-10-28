"""HU-CRM-16 (ADR-0179 decisión 8): aviso de tratamiento de datos, consentimiento y guardado diferido."""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import api_headers, uniq
from sqlalchemy import func, select, text

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

AVISO = "/api/v1/admin/datos-cliente/aviso"
TEXTO = "Usamos tus datos para atenderte. Responde «acepto» si estás de acuerdo."
PASSWORD = "Colab-Crm16-2026!"


async def _negocio() -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq("crm16")
    tid, _ = await onboard_tenant(
        name=f"N {slug}",
        slug=slug,
        persona={"tone": "cercano"},
        admin_email="admin@local",
        packs=["pack_commerce", "pack_calendar"],
        sembrar_ejemplos=False,
    )
    return tid


async def _activar_aviso(app_client, tid, texto: str = TEXTO) -> dict:
    h = await api_headers(app_client, tid)
    r = await app_client.put(AVISO, headers=h, json={"activo": True, "texto": texto, "confirmo_revision_legal": True})
    assert r.status_code == 200, r.text
    return r.json()


async def _cliente_y_conversacion(tid, *, canal: str = "whatsapp"):
    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid), get_session() as s:
        cliente = Customer(tenant_id=tid, channel=canal, external_id=f"{canal}:{uniq('n')}", name="Ana")
        s.add(cliente)
        await s.flush()
        conv = Conversation(tenant_id=tid, customer_id=cliente.id, channel=canal)
        s.add(conv)
        await s.flush()
        s.expunge_all()
        return cliente, conv


async def _mensaje_del_cliente(tid, conv_id, contenido: str) -> uuid.UUID:
    from core.db.models import Message
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid), get_session() as s:
        m = Message(
            tenant_id=tid, conversation_id=conv_id, direction="inbound", sender_type="customer", content=contenido
        )
        s.add(m)
        await s.flush()
        return m.id


async def _anexar(tid, conv, cliente, behavior):
    """`texto_a_anexar` como lo llama el loop: dentro del alcance del negocio."""
    from core.datos_cliente.aviso_chat import texto_a_anexar
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await texto_a_anexar(tid, conv, cliente, behavior)


async def _filas(tid, cliente_id) -> list[tuple[str, str | None]]:
    from core.db.models import CustomerConsent
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid), get_session() as s:
        filas = (
            await s.execute(
                select(CustomerConsent.estado, CustomerConsent.evidencia)
                .where(CustomerConsent.customer_id == cliente_id, CustomerConsent.proposito == "tratamiento_datos")
                .order_by(CustomerConsent.created_at, CustomerConsent.id)
            )
        ).all()
    return [(f[0], f[1]) for f in filas]


async def _behavior(tid) -> dict | None:
    from core.db.models import AppSettings
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid), get_session() as s:
        return (
            await s.execute(select(AppSettings.behavior_config).where(AppSettings.tenant_id == tid))
        ).scalar_one_or_none()


async def _perfil(tid, cliente_id) -> dict:
    from core.db.models import Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid), get_session() as s:
        return dict((await s.get(Customer, cliente_id)).attributes or {})


# ------------------------------------------------------------------ configuración del aviso


async def test_el_aviso_viene_apagado_con_un_borrador_marcado(app_client) -> None:
    tid = await _negocio()
    h = await api_headers(app_client, tid)
    cuerpo = (await app_client.get(AVISO, headers=h)).json()
    assert cuerpo["activo"] is False and cuerpo["es_borrador"] is True and cuerpo["texto"] and cuerpo["texto_propio"] == ""


async def test_activar_exige_confirmar_la_revision_legal_y_audita(app_client) -> None:
    from core.db.models import AuditLog
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    h = await api_headers(app_client, tid)
    r = await app_client.put(AVISO, headers=h, json={"activo": True, "texto": TEXTO})
    assert r.status_code == 422 and "revisado legalmente" in r.text
    assert (await app_client.get(AVISO, headers=h)).json()["activo"] is False

    # Guardar el texto sin activar no pide la confirmación.
    r = await app_client.put(AVISO, headers=h, json={"activo": False, "texto": TEXTO})
    assert r.status_code == 200 and r.json()["es_borrador"] is False

    salida = await _activar_aviso(app_client, tid)
    assert salida["activo"] is True and salida["texto"] == TEXTO
    async with tenant_scope(tid), get_session() as s:
        n = (
            await s.execute(
                select(func.count()).select_from(AuditLog).where(
                    AuditLog.tenant_id == tid, AuditLog.action == "settings.aviso_datos"
                )
            )
        ).scalar_one()
    assert n == 2  # el texto, y luego la activación; el intento rechazado no deja rastro


async def test_cambiar_el_texto_cambia_la_version(app_client) -> None:
    tid = await _negocio()
    v1 = (await _activar_aviso(app_client, tid))["version"]
    v2 = (await _activar_aviso(app_client, tid, TEXTO + " Gracias."))["version"]
    assert v1 != v2


async def test_solo_quien_puede_escribir_en_configuracion_guarda_el_aviso(app_client) -> None:
    from core.admin.auth import hash_password
    from core.db.models import Admin, AdminPermission, TenantUser
    from core.db.session import get_session

    tid = await _negocio()
    email = f"{uniq('colab')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="colaborador"))
        session.add(AdminPermission(admin_id=admin.id, section="settings", access="read", tenant_id=tid))
    h = await api_headers(app_client, tid, email=email, password=PASSWORD)
    assert (await app_client.get(AVISO, headers=h)).status_code == 200
    r = await app_client.put(AVISO, headers=h, json={"activo": False, "texto": "x"})
    assert r.status_code == 403


# ------------------------------------------------------------------ el aviso en el chat


async def test_el_aviso_se_anexa_una_sola_vez_por_version_y_no_en_el_canal_de_prueba(app_client) -> None:
    tid = await _negocio()
    behavior_apagado = await _behavior(tid)
    cliente, conv = await _cliente_y_conversacion(tid)
    assert await _anexar(tid, conv, cliente, behavior_apagado) is None  # apagado: nada, ni fila
    assert await _filas(tid, cliente.id) == []

    version = (await _activar_aviso(app_client, tid))["version"]
    behavior = await _behavior(tid)
    assert await _anexar(tid, conv, cliente, behavior) == TEXTO
    assert await _anexar(tid, conv, cliente, behavior) is None  # ya informado de esta versión
    filas = await _filas(tid, cliente.id)
    assert [f[0] for f in filas] == ["informado"] and filas[0][1] == f"conversacion:{conv.id}"

    # Una versión nueva del texto se vuelve a mostrar.
    await _activar_aviso(app_client, tid, TEXTO + " Gracias.")
    assert await _anexar(tid, conv, cliente, await _behavior(tid)) is not None
    assert version != (await _behavior(tid))["datos_cliente"]["aviso"]  # sanidad: el texto nuevo quedó guardado

    # El canal de prueba (el simulador del panel) no registra nada.
    from core.shared.test_mode import TEST_CHANNEL

    prueba, conv_prueba = await _cliente_y_conversacion(tid, canal=TEST_CHANNEL)
    assert await _anexar(tid, conv_prueba, prueba, behavior) is None
    assert await _filas(tid, prueba.id) == []


async def test_dos_mensajes_en_rafaga_no_avisan_dos_veces(app_client) -> None:
    import asyncio

    tid = await _negocio()
    await _activar_aviso(app_client, tid)
    behavior = await _behavior(tid)
    cliente, conv = await _cliente_y_conversacion(tid)
    resultados = await asyncio.gather(*(_anexar(tid, conv, cliente, behavior) for _ in range(4)))
    assert [r for r in resultados if r] == [TEXTO]
    assert len(await _filas(tid, cliente.id)) == 1


# ------------------------------------------------------------------ la respuesta del cliente


async def test_responder_solo_procede_con_un_aviso_pendiente_y_una_respuesta_clara(app_client) -> None:
    from core.datos_cliente.aviso_chat import responder

    tid = await _negocio()
    await _activar_aviso(app_client, tid)
    cliente, conv = await _cliente_y_conversacion(tid)
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        # Sin aviso mostrado todavía: el modelo no puede registrar aceptación por su cuenta.
        await _mensaje_del_cliente(tid, conv.id, "acepto")
        assert "error" in await responder(tid, conv.id, cliente.id, "acepto")
        assert await _filas(tid, cliente.id) == []

        await _anexar(tid, conv, cliente, await _behavior(tid))
        # «ok» no es una respuesta clara: no cambia nada.
        await _mensaje_del_cliente(tid, conv.id, "ok dale")
        assert (await responder(tid, conv.id, cliente.id, "acepto"))["status"] == "sin_respuesta_clara"
        assert [f[0] for f in await _filas(tid, cliente.id)] == ["informado"]

        # El modelo dice «acepto» pero el cliente escribió «no»: gana lo que escribió el cliente.
        await _mensaje_del_cliente(tid, conv.id, "No")
        assert (await responder(tid, conv.id, cliente.id, "acepto"))["status"] == "sin_respuesta_clara"

        # Otra conversación del mismo cliente no puede cerrar este aviso.
        _, otra = await _cliente_y_conversacion(tid)
        await _mensaje_del_cliente(tid, otra.id, "acepto")
        assert "error" in await responder(tid, otra.id, cliente.id, "acepto")

        await _mensaje_del_cliente(tid, conv.id, "Sí, acepto")
        assert (await responder(tid, conv.id, cliente.id, "acepto")) == {"status": "registrado", "estado": "otorgado"}
    filas = await _filas(tid, cliente.id)
    assert [f[0] for f in filas] == ["informado", "otorgado"]
    assert filas[1][1].startswith("mensaje:") and "acepto" in filas[1][1]


async def test_si_el_cliente_dice_que_no_queda_revocado_y_no_se_le_insiste(app_client) -> None:
    from core.datos_cliente.aviso_chat import responder
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    await _activar_aviso(app_client, tid)
    cliente, conv = await _cliente_y_conversacion(tid)
    async with tenant_scope(tid):
        await _anexar(tid, conv, cliente, await _behavior(tid))
        await _mensaje_del_cliente(tid, conv.id, "no acepto")
        assert (await responder(tid, conv.id, cliente.id, "rechazo"))["estado"] == "revocado"
    # Con otra versión del texto tampoco se le vuelve a mostrar.
    await _activar_aviso(app_client, tid, TEXTO + " Nueva versión.")
    assert await _anexar(tid, conv, cliente, await _behavior(tid)) is None


# ------------------------------------------------------------------ guardado diferido


async def test_con_el_aviso_activo_lo_no_operativo_espera_la_aceptacion(app_client) -> None:
    from core.datos_cliente.guardar import guardar
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    cliente, conv = await _cliente_y_conversacion(tid)

    # Aviso apagado: se guarda todo, como antes.
    async with tenant_scope(tid):
        g = await guardar(cliente.id, {"email": "a@example.com"}, accion="ventas.pedido_retiro")
    assert g.guardados == ["email"] and not g.requiere_aceptacion

    await _activar_aviso(app_client, tid)
    async with tenant_scope(tid):
        g = await guardar(
            cliente.id,
            {"name": "Ana", "address": "Calle 1", "gusto": "pizza"},
            accion="ventas.pedido_retiro",
            operativos=("gusto",),
        )
    # name es requerido en la acción y `gusto` lo declara el módulo como operativo; la dirección (no se pide para retirar) espera.
    assert set(g.guardados) == {"name", "gusto"} and g.diferidos == ["address"]
    assert g.requiere_aceptacion and g.estado_aviso == "sin_aviso"
    perfil = await _perfil(tid, cliente.id)
    assert "address" not in perfil and perfil.get("gusto") == "pizza"

    # Tras aceptar, se guarda todo.
    from core.datos_cliente.aviso_chat import responder

    async with tenant_scope(tid):
        await _anexar(tid, conv, cliente, await _behavior(tid))
        await _mensaje_del_cliente(tid, conv.id, "acepto")
        await responder(tid, conv.id, cliente.id, "acepto")
        g = await guardar(cliente.id, {"address": "Calle 1"}, accion="ventas.pedido_retiro")
    assert g.guardados == ["address"] and not g.requiere_aceptacion
    assert (await _perfil(tid, cliente.id))["address"] == "Calle 1"


async def test_la_tool_del_perfil_avisa_que_falta_la_aceptacion(app_client) -> None:
    from core.mcp.types import ToolContext
    from core.shared.tenant import tenant_scope
    from core.tools.core_skill import CoreSkill

    tid = await _negocio()
    await _activar_aviso(app_client, tid)
    cliente, conv = await _cliente_y_conversacion(tid)
    ctx = ToolContext(conv.id, cliente.id, uniq("k"), "whatsapp")
    async with tenant_scope(tid):
        r = await CoreSkill()._update_customer_profile({"attributes": {"hobby": "ajedrez"}}, ctx)
    assert r["status"] == "requiere_aceptacion" and r["no_guardado"] == ["hobby"] and r["updated"] == []
    assert "hobby" not in await _perfil(tid, cliente.id)


# ------------------------------------------------------------------ ficha: estado y revocación


async def test_la_ficha_muestra_el_estado_y_el_dueno_puede_revocar(app_client) -> None:
    from core.db.models import AuditLog
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    await _activar_aviso(app_client, tid)
    cliente, conv = await _cliente_y_conversacion(tid)
    async with tenant_scope(tid):
        from core.datos_cliente.aviso_chat import responder
        from core.datos_cliente.guardar import guardar

        await _anexar(tid, conv, cliente, await _behavior(tid))
        await _mensaje_del_cliente(tid, conv.id, "acepto")
        await responder(tid, conv.id, cliente.id, "acepto")
        await guardar(cliente.id, {"email": "a@example.com", "hobby": "ajedrez"})
    h = await api_headers(app_client, tid)
    base = f"/api/v1/admin/leads/{cliente.id}/consentimiento-datos"
    cuerpo = (await app_client.get(base, headers=h)).json()
    assert cuerpo["aviso_activo"] is True and cuerpo["estado"] == "otorgado"
    assert [i["estado"] for i in cuerpo["historial"]] == ["otorgado", "informado"]

    r = await app_client.post(f"{base}/revocar", headers=h)
    assert r.status_code == 200, r.text
    salida = r.json()
    assert salida["estado"] == "revocado" and set(salida["datos_retirados"]) >= {"email", "hobby"}
    assert salida["supresion_en_modulos_pendiente"] is True
    assert "email" not in await _perfil(tid, cliente.id)

    # Revocar otra vez no repite nada ni deja otra fila.
    r = await app_client.post(f"{base}/revocar", headers=h)
    assert r.status_code == 200 and r.json()["datos_retirados"] == []
    assert [f[0] for f in await _filas(tid, cliente.id)] == ["informado", "otorgado", "revocado"]
    async with tenant_scope(tid), get_session() as s:
        n = (
            await s.execute(
                select(func.count()).select_from(AuditLog).where(
                    AuditLog.tenant_id == tid, AuditLog.action == "consentimiento_datos.revocado"
                )
            )
        ).scalar_one()
    assert n == 1


async def test_el_consentimiento_de_un_cliente_de_otro_negocio_no_se_ve(app_client) -> None:
    a, b = await _negocio(), await _negocio()
    cliente_b, _ = await _cliente_y_conversacion(b)
    ha = await api_headers(app_client, a)
    base = f"/api/v1/admin/leads/{cliente_b.id}/consentimiento-datos"
    assert (await app_client.get(base, headers=ha)).status_code == 404
    assert (await app_client.post(f"{base}/revocar", headers=ha)).status_code == 404


async def test_la_tabla_sigue_siendo_solo_de_insercion_y_valida_sus_estados() -> None:
    from sqlalchemy.exc import DBAPIError

    from core.db.models import CustomerConsent
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    cliente, _ = await _cliente_y_conversacion(tid)

    async def _insertar(**kw):
        async with tenant_scope(tid), get_session() as s:
            s.add(
                CustomerConsent(
                    tenant_id=tid, customer_id=cliente.id, origen="x", actor_label="", evidencia="", **kw
                )
            )

    # `informado` solo existe para tratamiento_datos, y este exige la versión del aviso.
    with pytest.raises(DBAPIError):
        await _insertar(proposito="promociones", estado="informado", version_aviso=None)
    with pytest.raises(DBAPIError):
        await _insertar(proposito="tratamiento_datos", estado="otorgado", version_aviso=None)
    await _insertar(proposito="tratamiento_datos", estado="informado", version_aviso="abc")
    with pytest.raises(DBAPIError):
        async with tenant_scope(tid), get_session() as s:
            await s.execute(text("update customer_consents set estado = 'otorgado' where customer_id = :c"), {"c": cliente.id})
