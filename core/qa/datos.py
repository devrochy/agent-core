"""Datos de prueba propios de cada tester (SPEC-30, HU-QA-07, D-SPEC-30-6).

«Preparar mis datos» crea, solo en test (`QA_TESTERS_ENABLED`), un negocio de prueba por
tester con Comercio, Agenda y Cobranza, sus datos de ejemplo (fragmentos
`packages/pack-*/presets/qa.json`) y tres cuentas en el dominio reservado `.test`:

- **dueño** (owner del negocio): panel y APK como propietario;
- **gestor** (colaborador con perfil de gestor y la cartera asignada): app de campo;
- **colaborador** (permisos de Conversaciones, Leads y Escalaciones): panel limitado.

Las contraseñas se generan al azar y se devuelven **una sola vez** (al preparar o al pedir
contraseñas nuevas); no se guardan. Es idempotente: una fila por tester en
`qa_tester_datasets` reclama el trabajo con un INSERT condicional, y si una corrida falla
a medias, la siguiente reutiliza el negocio y las cuentas que ya creó.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert

from core.admin.auth import hash_password
from core.admin.roles import testers_habilitados
from core.db.models import Admin, AdminPermission, AdminSession, QaTesterDataset, Tenant, TenantUser
from core.db.session import get_session
from core.qa.service import QaError
from core.shared.logger import get_logger

log = get_logger(__name__)

PACKS = ("pack_commerce", "pack_calendar", "pack_cobranza")
PLAN = "premium"
VERTICAL = "qa"
DOMINIO = "pruebas.solvershub.test"
# Una corrida que quedó en «creando» más de esto se considera caída y se puede retomar.
CORRIDA_VENCIDA = timedelta(minutes=10)

CUENTAS: tuple[dict[str, str], ...] = (
    {
        "rol": "dueno",
        "columna": "dueno_admin_id",
        "titulo": "Dueño del negocio",
        "uso": "Panel y APK como propietario del negocio de prueba.",
    },
    {
        "rol": "gestor",
        "columna": "gestor_admin_id",
        "titulo": "Gestor de cobranza",
        "uso": "App de campo: ve solo la Cartera Centro.",
    },
    {
        "rol": "colaborador",
        "columna": "colaborador_admin_id",
        "titulo": "Colaborador",
        "uso": "Panel con Resumen, Conversaciones, Leads y Escalaciones.",
    },
)
# Permisos del colaborador en su negocio (secciones del core, ADR-0047).
PERMISOS_COLABORADOR = (("dashboard", "read"), ("conversations", "write"), ("leads", "write"), ("escalations", "write"))


def _corto(admin_id: uuid.UUID) -> str:
    return admin_id.hex[:8]


def slug_de(admin_id: uuid.UUID) -> str:
    return f"qa-{_corto(admin_id)}"


def correo_de(rol: str, admin_id: uuid.UUID) -> str:
    return f"{rol}.{_corto(admin_id)}@{DOMINIO}"


def _contrasena() -> str:
    return secrets.token_urlsafe(12)


def _exigir_habilitado() -> None:
    if not testers_habilitados():
        raise QaError("conflicto", "Los datos de prueba solo existen en el entorno de test (QA_TESTERS_ENABLED).")


# ------------------------------------------------------------------ contenido
def _fragmento(pack: str) -> dict[str, Any]:
    from core.presets import load_fragment

    return dict(load_fragment(pack, VERTICAL).seed)


def _estado_cliente(prestamo: dict[str, Any] | None) -> str:
    if not prestamo:
        return "sin préstamo"
    hace = int(prestamo.get("hace_dias") or 0)
    dias = max(1, int(prestamo.get("dias_entre_cuotas") or 1))
    pagadas = int(prestamo.get("cuotas_pagadas") or 0)
    total = int(prestamo.get("num_cuotas") or 1)
    vencidas = sum(1 for i in range(pagadas + 1, total + 1) if i * dias < hace)
    if vencidas:
        return f"en mora ({vencidas} cuotas atrasadas)"
    return "al día"


def contenido() -> list[dict[str, Any]]:
    """Qué trae el negocio de prueba, leído de los mismos fragmentos que se siembran."""
    comercio = _fragmento("pack_commerce")
    agenda = _fragmento("pack_calendar")
    cobranza = _fragmento("pack_cobranza")
    hoy = date.today()
    salida: list[dict[str, Any]] = []
    productos = comercio.get("products") or []
    salida.append(
        {
            "habilidad": "Comercio",
            "detalle": [
                f"{p['name']} · {', '.join(v['sku'] + (' (agotado)' if not v.get('stock') else '') for v in p.get('variants') or [])}"
                for p in productos
            ],
        }
    )
    citas = [
        f"{c.get('title')} · {(hoy + timedelta(days=int(c.get('en_dias') or 0))).isoformat()} {c.get('hora')}"
        for c in agenda.get("citas") or []
    ]
    salida.append(
        {
            "habilidad": "Agenda",
            "detalle": [f"Tipo de cita {t['name']} ({t['duration_minutes']} min)" for t in agenda.get("tipos") or []]
            + citas
            + [f"Bloqueo: {b.get('reason')} · de {b.get('desde')} a {b.get('hasta')}" for b in agenda.get("bloqueos") or []],
        }
    )
    for cartera in cobranza.get("carteras") or []:
        salida.append(
            {
                "habilidad": "Cobranza",
                "detalle": [f"{cartera['nombre']}, asignada al gestor de prueba"]
                + [
                    f"{c['nombre']} {c.get('apellido', '')}".strip() + f" · {_estado_cliente(c.get('prestamo'))}"
                    for c in cartera.get("clientes") or []
                ],
            }
        )
    return salida


# ------------------------------------------------------------------ lectura
async def _tenant_vivo(session, tenant_id: uuid.UUID) -> Tenant | None:  # type: ignore[no-untyped-def]
    return (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id, Tenant.deleted_at.is_(None)))
    ).scalar_one_or_none()


async def _a_dict(session, ds: QaTesterDataset) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    tenant = await _tenant_vivo(session, ds.tenant_id)
    estado = ds.estado
    if estado == "listo" and tenant is None:
        estado = "purgado"
    cuentas = []
    for c in CUENTAS:
        cuentas.append(
            {
                "rol": c["rol"],
                "titulo": c["titulo"],
                "uso": c["uso"],
                "email": correo_de(c["rol"], ds.admin_id),
                "creada": getattr(ds, c["columna"]) is not None,
            }
        )
    return {
        "estado": estado,
        "error": ds.error if estado == "error" else None,
        "negocio": {
            "id": str(ds.tenant_id),
            "slug": ds.slug,
            "nombre": tenant.name if tenant else None,
        },
        "cuentas": cuentas,
        "contenido": contenido(),
        "creado_en": ds.creado_en.isoformat() if ds.creado_en else None,
        "actualizado_en": ds.actualizado_en.isoformat() if ds.actualizado_en else None,
    }


async def mis_datos(admin: Admin) -> dict[str, Any]:
    """Estado de los datos del tester. Sin contraseñas."""
    disponible = testers_habilitados()
    async with get_session() as session:
        ds = (
            await session.execute(select(QaTesterDataset).where(QaTesterDataset.admin_id == admin.id))
        ).scalar_one_or_none()
        datos = await _a_dict(session, ds) if ds is not None else None
    return {"disponible": disponible, "datos": datos, "contenido": contenido()}


# ------------------------------------------------------------------ preparar
async def _reclamar(admin: Admin) -> QaTesterDataset:
    """Toma la corrida: crea la fila o retoma una fallida, vencida o con el negocio purgado.
    Si ya está lista (y el negocio existe) o hay otra corrida en curso, responde 409."""
    ahora = datetime.now(UTC)
    async with get_session() as session:
        nuevo = await session.execute(
            insert(QaTesterDataset)
            .values(
                id=uuid.uuid4(),
                admin_id=admin.id,
                tenant_id=uuid.uuid4(),
                slug=slug_de(admin.id),
                estado="creando",
                creado_en=ahora,
                actualizado_en=ahora,
            )
            .on_conflict_do_nothing(index_elements=["admin_id"])
            .returning(QaTesterDataset.id)
        )
        recien = nuevo.scalar_one_or_none() is not None
        await session.commit()
    async with get_session() as session:
        ds = (
            await session.execute(
                select(QaTesterDataset).where(QaTesterDataset.admin_id == admin.id).with_for_update()
            )
        ).scalar_one()
        if recien:
            return ds
        if ds.estado == "listo":
            if await _tenant_vivo(session, ds.tenant_id) is not None:
                raise QaError("conflicto", "Ya tienes datos de prueba. Puedes pedir contraseñas nuevas.")
            # El master purgó el negocio: se vuelve a crear con otro id y las mismas cuentas.
            ds.tenant_id = uuid.uuid4()
        elif ds.estado == "creando" and ds.actualizado_en > ahora - CORRIDA_VENCIDA:
            raise QaError("conflicto", "Tus datos de prueba se están preparando. Espera un momento.")
        ds.estado = "creando"
        ds.error = None
        ds.actualizado_en = ahora
        await session.commit()
        return ds


async def _cuenta(ds_id: uuid.UUID, columna: str, email: str, password: str) -> uuid.UUID:
    """Crea la cuenta (o reutiliza la que esta misma fila ya creó) con una contraseña nueva."""
    async with get_session() as session:
        ds = await session.get(QaTesterDataset, ds_id)
        assert ds is not None
        propia = getattr(ds, columna)
        existente = (await session.execute(select(Admin).where(Admin.email == email))).scalar_one_or_none()
        if existente is not None and existente.id != propia:
            # Nunca se toma una cuenta que no creó esta fila.
            raise QaError("conflicto", f"El correo {email} ya pertenece a otra cuenta.")
        if existente is None:
            existente = Admin(email=email, password_hash=hash_password(password), role="colaborador", active=True)
            session.add(existente)
            await session.flush()
            setattr(ds, columna, existente.id)
        else:
            existente.password_hash = hash_password(password)
            existente.active = True
        return existente.id


async def _vincular(tenant_id: uuid.UUID, admin_id: uuid.UUID, rol: str) -> None:
    async with get_session() as session:
        link = (
            await session.execute(
                select(TenantUser).where(TenantUser.tenant_id == tenant_id, TenantUser.admin_id == admin_id)
            )
        ).scalar_one_or_none()
        if link is None:
            session.add(TenantUser(tenant_id=tenant_id, admin_id=admin_id, role=rol))


async def _permisos_colaborador(tenant_id: uuid.UUID, admin_id: uuid.UUID) -> None:
    async with get_session() as session:
        actuales = {
            p.section
            for p in (
                await session.execute(
                    select(AdminPermission).where(
                        AdminPermission.admin_id == admin_id, AdminPermission.tenant_id == tenant_id
                    )
                )
            ).scalars()
        }
        for seccion, acceso in PERMISOS_COLABORADOR:
            if seccion not in actuales:
                session.add(AdminPermission(admin_id=admin_id, section=seccion, access=acceso, tenant_id=tenant_id))


async def _sembrar(tenant_id: uuid.UUID, gestor_email: str) -> list[str]:
    from core.plugin_manager import plugin_manager

    pasos: list[str] = []
    for pack in PACKS:
        module = plugin_manager.get(pack)
        if module is None:
            pasos.append(f"{pack} no está instalado")
            continue
        data = _fragmento(pack)
        if pack == "pack_cobranza":
            for cartera in data.get("carteras") or []:
                cartera["gestor"] = {**(cartera.get("gestor") or {}), "correo": gestor_email}
        pasos.extend(await module.seed_data(tenant_id, data))
    return pasos


async def preparar(admin: Admin) -> dict[str, Any]:
    """Crea el negocio de prueba del tester con sus datos y cuentas. Devuelve las
    contraseñas en claro **una sola vez**."""
    from core.admin.collaborators_service import apply_pack_roles
    from core.alta_negocio import onboard_tenant

    _exigir_habilitado()
    ds = await _reclamar(admin)
    try:
        async with get_session() as session:
            otro = (
                await session.execute(select(Tenant.id).where(Tenant.slug == ds.slug, Tenant.id != ds.tenant_id))
            ).scalar_one_or_none()
        if otro is not None:
            raise QaError("conflicto", f"Ya existe otro negocio con el slug {ds.slug}.")

        contrasenas: dict[str, str] = {}
        ids: dict[str, uuid.UUID] = {}
        for c in CUENTAS:
            contrasenas[c["rol"]] = _contrasena()
            ids[c["rol"]] = await _cuenta(ds.id, c["columna"], correo_de(c["rol"], admin.id), contrasenas[c["rol"]])

        etiqueta = admin.username or _corto(admin.id)
        await onboard_tenant(
            name=f"QA · {etiqueta}",
            slug=ds.slug,
            business_description=(
                "Negocio de prueba del centro de pruebas: tienda de café con agenda de asesorías "
                "y una cartera de créditos."
            ),
            packs=list(PACKS),
            admin_email=correo_de("dueno", admin.id),
            tenant_id=ds.tenant_id,
            plan=PLAN,
        )
        await _vincular(ds.tenant_id, ids["gestor"], "colaborador")
        await apply_pack_roles(["pack_cobranza:gestor"], ds.tenant_id, ids["gestor"])
        await _vincular(ds.tenant_id, ids["colaborador"], "colaborador")
        await _permisos_colaborador(ds.tenant_id, ids["colaborador"])
        pasos = await _sembrar(ds.tenant_id, correo_de("gestor", admin.id))
    except Exception as exc:
        mensaje = exc.mensaje if isinstance(exc, QaError) else "Falló la preparación; vuelve a intentarlo."
        async with get_session() as session:
            await session.execute(
                update(QaTesterDataset)
                .where(QaTesterDataset.id == ds.id)
                .values(estado="error", error=mensaje, actualizado_en=datetime.now(UTC))
            )
        log.warning("qa_datos_fallo", admin_id=str(admin.id), slug=ds.slug, exc_info=not isinstance(exc, QaError))
        if isinstance(exc, QaError):
            raise
        raise QaError("conflicto", mensaje) from exc

    async with get_session() as session:
        fila = await session.get(QaTesterDataset, ds.id)
        assert fila is not None
        fila.estado = "listo"
        fila.error = None
        fila.actualizado_en = datetime.now(UTC)
        await session.flush()
        salida = await _a_dict(session, fila)
    log.info("qa_datos_preparados", admin_id=str(admin.id), slug=ds.slug, pasos=len(pasos))
    salida["contrasenas"] = contrasenas
    return salida


async def contrasenas_nuevas(admin: Admin) -> dict[str, Any]:
    """Contraseñas nuevas para las tres cuentas; cierra sus sesiones del panel."""
    _exigir_habilitado()
    contrasenas: dict[str, str] = {}
    async with get_session() as session:
        ds = (
            await session.execute(
                select(QaTesterDataset).where(QaTesterDataset.admin_id == admin.id).with_for_update()
            )
        ).scalar_one_or_none()
        if ds is None or ds.estado != "listo":
            raise QaError("no_encontrado", "Todavía no tienes datos de prueba listos.")
        for c in CUENTAS:
            cuenta_id = getattr(ds, c["columna"])
            if cuenta_id is None:
                continue
            cuenta = await session.get(Admin, cuenta_id)
            if cuenta is None:
                continue
            contrasenas[c["rol"]] = _contrasena()
            cuenta.password_hash = hash_password(contrasenas[c["rol"]])
            await session.execute(delete(AdminSession).where(AdminSession.admin_id == cuenta_id))
        ds.actualizado_en = datetime.now(UTC)
        await session.flush()
        salida = await _a_dict(session, ds)
    log.info("qa_datos_contrasenas_nuevas", admin_id=str(admin.id))
    salida["contrasenas"] = contrasenas
    return salida
