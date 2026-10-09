"""Primeros pasos calculados y embudo de activación (HU-SH-06, ADR-0124) contra la BD real.

Gherkin:
- «Primeros pasos calculados»: el checklist sale del estado real y cada paso marca su avance solo.
- «Packs pendientes»: una habilidad que el preset dejó pendiente de configurar aparece como paso
  con su explicación; el tipo de negocio que espera un plan más alto, también.
- «Medición»: cada paso cumplido registra una sola vez el tiempo desde el alta.
- Aislamiento: el checklist y los hitos de un negocio no ven datos de otro.
"""

from __future__ import annotations

import asyncio
import os
import random
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from _api_helpers import api_headers, uniq
from sqlalchemy import func, select, update

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

ONBOARDING = "/api/v1/admin/onboarding"
CLAVE = "clave-de-prueba-123"
NUMERO_OK = {"number_registered": {"ok": True, "detail": "status=CONNECTED"}}


# ------------------------------------------------------------------ ayudantes
async def _negocio(**alta) -> tuple[uuid.UUID, str]:  # type: ignore[no-untyped-def]
    """Negocio nuevo con su dueño (contraseña conocida). Devuelve (tenant, correo del dueño)."""
    from core.alta_negocio import onboard_tenant

    slug = uniq("pasos")
    email = f"{slug}@correo-prueba.co"
    tenant_id, _ = await onboard_tenant(
        name=f"Negocio {slug}", slug=slug, admin_email=email, admin_password=CLAVE, **alta
    )
    return tenant_id, email


async def _pasos(app_client, tenant_id: uuid.UUID, email: str) -> dict:  # type: ignore[no-untyped-def]
    headers = await api_headers(app_client, tenant_id, email=email, password=CLAVE)
    r = await app_client.get(ONBOARDING, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _por_clave(respuesta: dict) -> dict[str, dict]:
    return {p["clave"]: p for p in respuesta["pasos"]}


async def _hitos(tenant_id: uuid.UUID) -> dict[str, object]:
    from core.db.models import OnboardingHito
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as s:
            filas = (
                await s.execute(select(OnboardingHito).where(OnboardingHito.tenant_id == tenant_id))
            ).scalars().all()
    return {f.paso: f for f in filas}


async def _auditados(tenant_id: uuid.UUID) -> int:
    from core.db.models import AuditLog
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as s:
            return (
                await s.execute(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(AuditLog.tenant_id == tenant_id, AuditLog.action == "primer_paso_completado")
                )
            ).scalar_one()


async def _describir(tenant_id: uuid.UUID) -> None:
    from core.db.models import AppSettings
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as s:
            await s.execute(
                update(AppSettings)
                .where(AppSettings.tenant_id == tenant_id)
                .values(business_description="Panadería de barrio: pan, tortas y domicilios en Cali")
            )


async def _conectar_whatsapp(tenant_id: uuid.UUID, *, status: str = "connected", health: dict | None = None) -> uuid.UUID:
    from core.db.models import MetaConnection, utcnow
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as s:
            conexion = MetaConnection(
                tenant_id=tenant_id,
                channel="whatsapp",
                environment="production",
                origin="byok",
                meta_app_id=uniq("app"),
                status=status,
                health=NUMERO_OK if health is None else health,
                connected_at=utcnow(),
            )
            s.add(conexion)
            await s.flush()
            return conexion.id


async def _producto(tenant_id: uuid.UUID, *, activo: bool = True) -> None:
    from pack_commerce.models import Product, ProductVariant

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as s:
            producto = Product(tenant_id=tenant_id, name="Pan francés")
            s.add(producto)
            await s.flush()
            s.add(ProductVariant(tenant_id=tenant_id, product_id=producto.id, sku=uniq("SKU"), price=1500, active=activo))


async def _cuenta(email: str | None = None) -> tuple[uuid.UUID, str]:
    from core.admin.auth import hash_password
    from core.db.models import Admin
    from core.db.session import get_session

    email = email or f"{uniq('colab')}@correo-prueba.co"
    async with get_session() as s:
        admin = Admin(email=email, password_hash=hash_password(CLAVE), role="colaborador")
        s.add(admin)
        await s.flush()
        return admin.id, email


async def _settings(tenant_id: uuid.UUID, **cambios) -> None:  # type: ignore[no-untyped-def]
    from core.db.models import Tenant
    from core.db.session import get_session

    async with get_session() as s:
        negocio = await s.get(Tenant, tenant_id)
        negocio.settings = {**(negocio.settings or {}), **cambios}


# ------------------------------------------------------------------ primeros pasos calculados
async def test_un_negocio_nuevo_arranca_con_los_cinco_pasos_pendientes(app_client) -> None:
    tenant_id, email = await _negocio()

    respuesta = await _pasos(app_client, tenant_id, email)

    assert [p["clave"] for p in respuesta["pasos"]] == [
        "datos_negocio",
        "whatsapp",
        "catalogo",
        "probar_asistente",
        "equipo",
    ]
    assert (respuesta["completos"], respuesta["total"], respuesta["origen"]) == (0, 5, "manual")
    pasos = _por_clave(respuesta)
    assert {clave: (p["estado"], p["motivo"]) for clave, p in pasos.items()} == {
        "datos_negocio": ("pendiente", "sin_descripcion"),
        "whatsapp": ("pendiente", "sin_conexion"),
        "catalogo": ("pendiente", "sin_habilidad"),
        "probar_asistente": ("pendiente", "sin_pruebas"),
        "equipo": ("pendiente", "sin_invitaciones"),
    }
    # El dueño puede actuar en todos: el panel le muestra cada acción.
    assert all(p["puede_actuar"] for p in pasos.values())
    assert all(p["completado_en"] is None for p in pasos.values())


async def test_cada_paso_marca_su_avance_solo(app_client) -> None:
    """Datos del negocio, WhatsApp, catálogo, probar el asistente (por el playground real) y
    equipo: cada cambio del estado real cambia el paso, sin marcar nada a mano."""
    tenant_id, email = await _negocio(packs=["pack_commerce"])
    headers = await api_headers(app_client, tenant_id, email=email, password=CLAVE)

    catalogo = _por_clave(await _pasos(app_client, tenant_id, email))["catalogo"]
    assert (catalogo["estado"], catalogo["motivo"], catalogo["pack"], catalogo["tipo"]) == (
        "pendiente",
        "sin_contenido",
        "pack_commerce",
        "catalogo",
    )

    await _describir(tenant_id)
    await _conectar_whatsapp(tenant_id)
    await _producto(tenant_id, activo=False)  # sin variante a la venta: no cuenta
    await _producto(tenant_id)
    turno = await app_client.post("/api/v1/admin/playground/message", json={"text": "hola"}, headers=headers)
    assert turno.status_code == 200, turno.text
    _, otro = await _cuenta()
    invitado = await app_client.post("/api/v1/admin/invitations", json={"email": otro}, headers=headers)
    assert invitado.status_code == 204, invitado.text

    respuesta = await _pasos(app_client, tenant_id, email)
    pasos = _por_clave(respuesta)
    assert {clave: p["estado"] for clave, p in pasos.items()} == dict.fromkeys(pasos, "completo")
    assert (respuesta["completos"], respuesta["total"]) == (5, 5)
    assert pasos["catalogo"]["cantidad"] == 1
    assert all(p["completado_en"] and p["segundos_desde_alta"] >= 0 for p in pasos.values())


async def test_un_whatsapp_sin_verificar_o_con_problemas_queda_en_curso(app_client) -> None:
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tenant_id, email = await _negocio()
    conexion = await _conectar_whatsapp(tenant_id, health={"token_valid": {"ok": True}})
    whatsapp = _por_clave(await _pasos(app_client, tenant_id, email))["whatsapp"]
    assert (whatsapp["estado"], whatsapp["motivo"]) == ("en_curso", "sin_verificar")

    async with tenant_scope(tenant_id):
        async with get_session() as s:
            await s.execute(
                update(MetaConnection)
                .where(MetaConnection.id == conexion, MetaConnection.tenant_id == tenant_id)
                .values(health=NUMERO_OK)
            )
    assert _por_clave(await _pasos(app_client, tenant_id, email))["whatsapp"]["estado"] == "completo"
    hito = (await _hitos(tenant_id))["whatsapp"]

    # Se cae después: el paso vuelve atrás, pero el hito (la primera vez) queda.
    async with tenant_scope(tenant_id):
        async with get_session() as s:
            await s.execute(
                update(MetaConnection)
                .where(MetaConnection.id == conexion, MetaConnection.tenant_id == tenant_id)
                .values(status="error")
            )
    whatsapp = _por_clave(await _pasos(app_client, tenant_id, email))["whatsapp"]
    assert (whatsapp["estado"], whatsapp["motivo"]) == ("en_curso", "con_problemas")
    assert whatsapp["completado_en"] is not None
    assert (await _hitos(tenant_id))["whatsapp"].completado_en == hito.completado_en


# ------------------------------------------------------------------ packs pendientes
async def test_una_habilidad_pendiente_de_configurar_aparece_como_paso(app_client) -> None:
    """El preset `comercio` en Basic deja `pack_payments` pendiente (faltan las llaves de Wompi,
    ADR-0110): aparece como paso con la guía de la habilidad y se cumple al habilitarla."""
    from core.agents.spec import load_active_spec, save_spec_if_changed
    from core.shared.tenant import tenant_scope

    tenant_id, email = await _negocio(plan="basic", preset="comercio", sembrar_ejemplos=False)

    respuesta = await _pasos(app_client, tenant_id, email)
    pasos = _por_clave(respuesta)
    pagos = pasos["configurar_pack_payments"]
    assert (pagos["estado"], pagos["motivo"], pagos["pack"], pagos["titulo"]) == (
        "pendiente",
        "faltan_datos",
        "pack_payments",
        "Pagos en línea",
    )
    assert "Wompi" in pagos["explicacion"]
    assert pagos["puede_actuar"] is True
    assert respuesta["total"] == 6
    # El preset habilitó comercio: el catálogo ya apunta a cargar productos.
    assert (pasos["catalogo"]["motivo"], pasos["catalogo"]["pack"]) == ("sin_contenido", "pack_commerce")

    # El dueño carga sus llaves y la habilidad queda habilitada: el paso se marca solo.
    async with tenant_scope(tenant_id):
        spec = await load_active_spec()
        datos = spec.model_dump(mode="json")
        datos["packs"] = sorted({*datos["packs"], "pack_payments"})
        await save_spec_if_changed(spec, datos, tenant_id)
    pagos = _por_clave(await _pasos(app_client, tenant_id, email))["configurar_pack_payments"]
    assert (pagos["estado"], pagos["motivo"]) == ("completo", None)


async def test_el_tipo_de_negocio_que_espera_un_plan_aparece_como_paso(app_client) -> None:
    """Un Free del alta automática con el vertical `comercio`: el preset quedó pendiente porque
    pide Basic (ADR-0121 § 3.7). Aparece como paso que lleva al plan."""
    from datetime import UTC, datetime

    tenant_id, email = await _negocio()
    await _settings(
        tenant_id,
        alta={
            "origen": "provisioning",
            "vertical": "comercio",
            "preset": {"id": "comercio", "estado": "pendiente", "detalle": "necesita un plan más alto"},
        },
    )

    respuesta = await _pasos(app_client, tenant_id, email)
    assert respuesta["origen"] == "aprovisionamiento"
    preset = _por_clave(respuesta)["activar_preset"]
    assert (preset["estado"], preset["motivo"], preset["titulo"], preset["plan_requerido"]) == (
        "pendiente",
        "plan_insuficiente",
        "Comercio",
        "Basic",
    )
    assert preset["puede_actuar"] is True

    await _settings(tenant_id, preset={"id": "comercio", "version": 1, "aplicado_en": datetime.now(UTC).isoformat(), "pendientes": []})
    assert _por_clave(await _pasos(app_client, tenant_id, email))["activar_preset"]["estado"] == "completo"


# ------------------------------------------------------------------ medición
async def test_cada_hito_se_registra_una_vez_con_el_tiempo_desde_el_alta(app_client) -> None:
    from core.db.models import Tenant
    from core.db.session import get_session

    tenant_id, email = await _negocio(packs=["pack_commerce"])
    # El alta fue hace dos horas.
    async with get_session() as s:
        await s.execute(
            update(Tenant).where(Tenant.id == tenant_id).values(created_at=datetime.now(UTC) - timedelta(hours=2))
        )
    await _producto(tenant_id)

    await _pasos(app_client, tenant_id, email)
    hitos = await _hitos(tenant_id)
    # El dueño quedó vinculado en el alta; el catálogo, ahora (la hora del primer producto).
    assert {"invitacion_aceptada", "catalogo"} <= set(hitos)
    catalogo = hitos["catalogo"]
    assert catalogo.fuente == "dato"
    assert 2 * 3600 - 60 <= catalogo.segundos_desde_alta <= 2 * 3600 + 120
    auditados = await _auditados(tenant_id)
    assert auditados == len(hitos)

    # Volver a evaluar (y dos evaluaciones a la vez) no duplica hitos ni auditoría.
    from core.primeros_pasos.hitos import evaluar

    await asyncio.gather(evaluar(tenant_id), evaluar(tenant_id), _pasos(app_client, tenant_id, email))
    assert set(await _hitos(tenant_id)) == set(hitos)
    assert await _auditados(tenant_id) == auditados


async def test_el_primer_mensaje_de_un_cliente_real_cierra_el_embudo(app_client) -> None:
    from core.db.models import Conversation, Customer, Message
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope
    from core.shared.test_mode import TEST_CHANNEL

    tenant_id, email = await _negocio()
    async with tenant_scope(tenant_id):
        async with get_session() as s:
            for canal in (TEST_CHANNEL, "whatsapp"):
                cliente = Customer(tenant_id=tenant_id, channel=canal, external_id=uniq("cli"), name="Cliente")
                s.add(cliente)
                await s.flush()
                conversacion = Conversation(tenant_id=tenant_id, customer_id=cliente.id, channel=canal)
                s.add(conversacion)
                await s.flush()
                s.add(
                    Message(
                        tenant_id=tenant_id,
                        conversation_id=conversacion.id,
                        direction="inbound",
                        sender_type="customer",
                        content="hola",
                    )
                )
    await _pasos(app_client, tenant_id, email)
    assert "primer_mensaje" in await _hitos(tenant_id)


async def test_el_embudo_agrega_los_hitos_de_las_altas_de_la_ventana() -> None:
    """`agent-core tenant embudo`: barre los negocios dados de alta en la ventana y agrega."""
    from core.db.models import Tenant
    from core.db.session import get_session
    from core.primeros_pasos.hitos import embudo

    # Una ventana propia en el pasado, para no mezclar con negocios de otros tests.
    alta = datetime(2001, 1, 1, tzinfo=UTC) + timedelta(seconds=random.randint(0, 10_000_000))
    con_catalogo, _ = await _negocio(packs=["pack_commerce"])
    sin_nada, _ = await _negocio()
    for tenant_id in (con_catalogo, sin_nada):
        async with get_session() as s:
            await s.execute(update(Tenant).where(Tenant.id == tenant_id).values(created_at=alta))
        await _settings(tenant_id, alta={"origen": "provisioning"})
    await _producto(con_catalogo)

    resultado = await embudo(alta - timedelta(seconds=1), alta + timedelta(seconds=1), solo_automaticas=True)

    assert resultado.altas == 2
    filas = {f.paso: f for f in resultado.filas}
    assert filas["invitacion_aceptada"].negocios == 2
    assert (filas["catalogo"].negocios, filas["catalogo"].porcentaje) == (1, 50.0)
    assert filas["catalogo"].en_24_horas == 0  # el alta es de 2001
    assert resultado.filas[0].paso == "invitacion_aceptada"


# ------------------------------------------------------------------ aislamiento y permisos
@pytest.mark.aislamiento
async def test_el_checklist_de_un_negocio_no_ve_datos_de_otro(app_client) -> None:
    """B tiene todo cargado; A, nada. El checklist de A sigue pendiente, sus hitos no traen los
    de B y la RLS no deja leer los hitos de B desde A."""
    from core.db.models import OnboardingHito
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    a, email_a = await _negocio(packs=["pack_commerce"])
    b, email_b = await _negocio(packs=["pack_commerce"])
    await _describir(b)
    await _conectar_whatsapp(b)
    await _producto(b)
    headers_b = await api_headers(app_client, b, email=email_b, password=CLAVE)
    assert (await app_client.post("/api/v1/admin/playground/message", json={"text": "hola"}, headers=headers_b)).status_code == 200
    _, otro = await _cuenta()
    assert (await app_client.post("/api/v1/admin/invitations", json={"email": otro}, headers=headers_b)).status_code == 204

    assert (await _pasos(app_client, b, email_b))["completos"] == 5
    respuesta_a = await _pasos(app_client, a, email_a)
    assert respuesta_a["completos"] == 0
    assert _por_clave(respuesta_a)["catalogo"]["cantidad"] == 0
    assert set(await _hitos(a)) == {"invitacion_aceptada"}

    async with tenant_scope(a):
        async with get_session() as s:
            de_b = (
                await s.execute(select(OnboardingHito).where(OnboardingHito.tenant_id == b))
            ).scalars().all()
    assert de_b == []  # RLS: desde A no se ven los hitos de B

    # El dueño de A no puede pedir el checklist de B.
    headers_a = await api_headers(app_client, a, email=email_a, password=CLAVE)
    r = await app_client.get(ONBOARDING, headers={**headers_a, "X-Tenant-Id": str(b)})
    assert r.status_code == 403


async def test_un_colaborador_ve_los_pasos_sin_acciones(app_client) -> None:
    """Nunca un 403 por diseño: un colaborador (lectura) ve el avance, pero el panel no le
    muestra acciones que no puede usar."""
    from core.db.models import TenantUser
    from core.db.session import get_session

    tenant_id, _ = await _negocio()
    admin_id, email = await _cuenta()
    async with get_session() as s:
        s.add(TenantUser(tenant_id=tenant_id, admin_id=admin_id, role="colaborador"))

    respuesta = await _pasos(app_client, tenant_id, email)
    assert respuesta["total"] == 5
    assert not any(p["puede_actuar"] for p in respuesta["pasos"])


async def test_el_contenido_de_cada_habilidad_es_solo_del_negocio() -> None:
    """Los hooks de los packs filtran por negocio: los tipos de cita y clientes de otro no
    cuentan."""
    from pack_calendar.models import AppointmentType

    from core.db.session import get_session
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    plugin_manager.scan()
    a, _ = await _negocio()
    b, _ = await _negocio()
    async with tenant_scope(b):
        async with get_session() as s:
            s.add(AppointmentType(tenant_id=b, name="Corte", duration_minutes=30))
            s.add(AppointmentType(tenant_id=b, name="Retirado", duration_minutes=30, active=False))
    agenda = plugin_manager.get("pack_calendar")
    cobranza = plugin_manager.get("pack_cobranza")
    async with tenant_scope(a):
        en_a = await agenda.contenido_cargado(a)
        cartera_a = await cobranza.contenido_cargado(a)
    async with tenant_scope(b):
        en_b = await agenda.contenido_cargado(b)
    assert (en_a.cantidad, en_a.tipo, en_a.seccion) == (0, "servicios", "pack_calendar:tipos")
    assert (en_b.cantidad, en_b.primero_en is not None) == (1, True)
    assert (cartera_a.cantidad, cartera_a.tipo) == (0, "cartera")


# ------------------------------------------------------------------ invitaciones al entrar
async def test_me_cuenta_las_invitaciones_pendientes_que_se_pueden_mostrar(app_client) -> None:
    """Una cuenta que ya existía recibe la invitación en la app (ADR-0121): `/me` dice cuántas
    hay, con los mismos filtros que la lista (vigentes y de negocios activos)."""
    from core.admin.invitations_service import crear_invitacion
    from core.db.models import Tenant
    from core.db.session import get_session

    activo, _ = await _negocio()
    suspendido, _ = await _negocio()
    propio, email = await _negocio()
    cuenta = await _admin_id(email)
    assert await crear_invitacion(tenant_id=activo, admin_id=cuenta, invited_by=None) is None
    assert await crear_invitacion(tenant_id=suspendido, admin_id=cuenta, invited_by=None) is None
    async with get_session() as s:
        await s.execute(update(Tenant).where(Tenant.id == suspendido).values(status="suspended"))

    headers = await api_headers(app_client, propio, email=email, password=CLAVE)
    me = await app_client.get("/api/v1/admin/me", headers=headers)
    assert me.status_code == 200, me.text
    assert me.json()["invitaciones_pendientes"] == 1
    lista = await app_client.get("/api/v1/admin/invitations/pending", headers=headers)
    assert lista.json()["total"] == 1


async def _admin_id(email: str) -> uuid.UUID:
    from core.db.models import Admin
    from core.db.session import get_session

    async with get_session() as s:
        return (await s.execute(select(Admin.id).where(Admin.email == email))).scalar_one()
