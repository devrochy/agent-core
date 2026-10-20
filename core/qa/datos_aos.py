"""Agent OS en el negocio de prueba del tester (SPEC-30, HU-QA-09, adenda de ADR-0135).

«Preparar mis datos» deja el negocio `qa-<hex>` del tester listo para probar el Agent OS, con
las MISMAS funciones de la operación (sin duplicar su lógica):

1. **Chat web activo** con su clave (`chat_web_conexion.guardar_widget`, igual que
   Configuración → Chat web): reutiliza la clave si ya existe y agrega el origen de
   `LANDING_BASE_URL`, que sirve `/w/<clave>`. Las demás conexiones no se tocan.
2. **Backfills** de conexiones, agentes y releases (`migracion_v3.ejecutar_backfills`).
3. **`migrar_a_v3(construir=True)`**, como `agent-core db migrar-a-v3 --tenant … --construir`.
4. **Agentes del preset v2** (`aplicar_agentes`) con un manifiesto en memoria: las plantillas
   `AGENTES` (Ventas y Agenda, con las keys de sus plantillas). Ningún preset de archivo encaja con
   el negocio de prueba, y uno nuevo en `core/presets/` aparecería en el alta de clientes reales.
   Nunca toca agentes que ya existan (los que creó el tester en el editor, p. ej. Cobranza).
5. **Release activa** en cada conexión conversacional (`construir` + `promover`, que pasa el
   gate): el tester es el dueño de su negocio de prueba y pulsar el botón es su decisión.
6. **`aos_procedures = true` y `aos_routing = 'active'`** con `aos_fijar_banderas` y el rol
   dueño, en la misma transacción que comprueba el slug `qa-` del tester.

Solo con `QA_TESTERS_ENABLED` y `AOS_CONNECTION_ROUTING`, y solo en el negocio de la fila del
tester. Idempotente: cada paso lo es. Si un paso falla, se guarda el motivo
(`qa_tester_datasets.error`) y el negocio queda como esté: antes del paso 3 sigue en legacy;
después ya es un v3 funcional (con el General) y el reintento completa lo que falte. No se vuelve
a legacy: reconstruir en legacy refrescaría el Coordinador desde el v2 y perdería las reglas del
preset, que la lápida de ADR-0139 ya no repone.
"""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from core.config import get_settings
from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

ACTOR = "system:qa-datos"
# Plantillas de `core/presets/plantillas/` que se instalan. Es un fixture de QA, como los
# fragmentos `qa.json`. Cobranza NO: el catálogo de pruebas la crea desde el editor de agentes
# (con la key de la plantilla ya ocupada, el editor respondería 409 `key_taken`).
AGENTES = ("ventas", "agenda")
NOMBRE_ROL = {"core": "Coordinador", "specialist": "Especialista", "backoffice": "Backoffice"}


class FalloAgentOS(Exception):
    """Un paso no se completó; el mensaje es el motivo que ve el tester."""


def _motivo(exc: Exception) -> str:
    if isinstance(exc, FalloAgentOS):
        return str(exc)[:500]
    if isinstance(exc, DBAPIError):
        # Sin los parámetros SQL.
        return f"error de base de datos ({type(exc.orig).__name__})"
    # Sin el texto de la excepción: puede llevar rutas o datos de conexión; el detalle va al log.
    return f"error inesperado ({type(exc).__name__}); vuelve a intentarlo"


def origen_de_la_landing() -> str | None:
    url = get_settings().landing_base_url.strip()
    partes = urlsplit(url)
    if not partes.scheme or not partes.netloc:
        return None
    return f"{partes.scheme}://{partes.netloc}".lower()


def enlace_del_chat(clave: str) -> str | None:
    base = get_settings().landing_base_url.strip().rstrip("/")
    return f"{base}/w/{clave}" if base else None


def _manifiesto() -> Any:
    from core.presets import PresetManifest
    from core.qa.datos import PACKS, PLAN

    return PresetManifest(
        id="qa",
        version=1,
        nombre="Centro de pruebas",
        descripcion="Agentes del negocio de prueba del tester (HU-QA-09). No es un preset de alta.",
        plan_minimo=PLAN,
        packs=list(PACKS),
        agentes=list(AGENTES),
    )


# ------------------------------------------------------------------ pasos
async def _negocio_del_tester(tenant_id: uuid.UUID, slug: str) -> str:
    """El negocio vivo con ese id Y ese slug `qa-`. Devuelve su `aos_runtime`."""
    async with get_session() as session:
        fila = (
            await session.execute(
                text(
                    "SELECT aos_runtime FROM tenants WHERE id = :t AND slug = :s AND slug LIKE 'qa-%' "
                    "AND deleted_at IS NULL"
                ),
                {"t": tenant_id, "s": slug},
            )
        ).first()
    if fila is None:
        raise FalloAgentOS("el negocio no es el negocio de prueba del tester")
    return str(fila[0])


async def _chat_web(tenant_id: uuid.UUID) -> None:
    from sqlalchemy import select

    from core.db.models import AppSettings
    from core.gateway import chat_web_conexion

    origen = origen_de_la_landing()
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            config = (
                await session.execute(
                    select(AppSettings.web_chat_config).where(
                        AppSettings.tenant_id == tenant_id, AppSettings.deleted_at.is_(None)
                    )
                )
            ).scalar_one_or_none() or {}
            origenes = [str(o) for o in (config.get("allowed_origins") or []) if o]
            if origen and origen not in [o.rstrip("/").lower() for o in origenes]:
                origenes.append(origen)
            await chat_web_conexion.guardar_widget(session, tenant_id, enabled=True, allowed_origins=origenes)


async def _backfills(tenant_id: uuid.UUID, modulos: list[Any]) -> None:
    from core.releases.migracion_v3 import ejecutar_backfills

    motivo = await ejecutar_backfills(tenant_id, modulos)
    if motivo:
        raise FalloAgentOS(motivo)


async def _a_v3(tenant_id: uuid.UUID, modulos: list[Any]) -> None:
    from core.releases.migracion_v3 import migrar_a_v3

    reporte = await migrar_a_v3(tenant_ids=[tenant_id], dry_run=False, construir=True, modules=modulos)
    negocio = reporte.negocios[0] if reporte.negocios else None
    if negocio is None or negocio.accion not in ("encendido", "ya_estaba"):
        bloqueos = "; ".join(negocio.bloqueos) if negocio else "negocio no encontrado"
        raise FalloAgentOS(f"migrar-a-v3: {bloqueos[:400]}")


async def _preset(tenant_id: uuid.UUID, modulos: list[Any]) -> None:
    from core.presets.agentes import aplicar_agentes

    async with tenant_scope(tenant_id):
        reporte = await aplicar_agentes(tenant_id, _manifiesto(), modules=modulos, aplicar=True)
    if reporte.omitido:
        raise FalloAgentOS(f"preset v2 omitido: {reporte.omitido}")
    if reporte.errores:
        raise FalloAgentOS(f"preset v2: {reporte.errores[0][:300]}")


async def _conexiones(tenant_id: uuid.UUID) -> list[tuple[uuid.UUID, uuid.UUID | None]]:
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            filas = (
                await session.execute(
                    text(
                        "SELECT cc.id, cc.active_release_id FROM channel_connections cc "
                        "JOIN connection_types ct ON ct.id = cc.type_id WHERE cc.tenant_id = :t "
                        "AND cc.deleted_at IS NULL AND ct.produces_conversations ORDER BY cc.id"
                    ),
                    {"t": tenant_id},
                )
            ).all()
    return [(f[0], f[1]) for f in filas]


async def _activar_releases(tenant_id: uuid.UUID, modulos: list[Any]) -> None:
    from core.releases.build import construir
    from core.releases.promocion import promover

    async with tenant_scope(tenant_id):
        for cid, activa in await _conexiones(tenant_id):
            build = await construir(
                tenant_id, cid, actor=ACTOR, modules=modulos, notas="Centro de pruebas: Preparar mis datos (HU-QA-09)."
            )
            if build.release_id != activa:
                await promover(tenant_id, cid, build.release_id, actor=ACTOR, motivo="qa_datos")


async def _banderas(tenant_id: uuid.UUID, slug: str) -> None:
    """`aos_procedures` y `aos_routing = 'active'` con el rol dueño (`aos_fijar_banderas` sin
    admin solo acepta esa sesión, migración 0097). En la misma transacción se bloquea la fila y se
    exige el slug `qa-` del tester y `v3`."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from core.kernel.banderas import BanderasAOS, BanderasRechazadas, fijar_banderas

    url = get_settings().migrations_database_url
    if not url:
        raise FalloAgentOS("falta MIGRATIONS_DATABASE_URL (rol dueño) para escribir las banderas")
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            fila = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM tenants WHERE id = :t AND slug = :s AND slug LIKE 'qa-%' "
                        "AND deleted_at IS NULL AND aos_runtime = 'v3' FOR UPDATE"
                    ),
                    {"t": tenant_id, "s": slug},
                )
            ).first()
            if fila is None:
                raise FalloAgentOS("no se pudieron encender los procedimientos y el enrutamiento")
            try:
                await fijar_banderas(
                    conn, tenant_id, BanderasAOS(runtime="v3", procedimientos=True, ruteo="active"), actor=ACTOR
                )
            except BanderasRechazadas as exc:
                raise FalloAgentOS(f"no se pudieron encender los procedimientos y el enrutamiento: {exc}") from None
    finally:
        await engine.dispose()


# ------------------------------------------------------------------ entrada
async def preparar(tenant_id: uuid.UUID, slug: str) -> str | None:
    """Deja el negocio de prueba listo para el Agent OS. Devuelve None si quedó listo o el motivo
    si no (nunca lanza: los datos y las cuentas del tester sirven igual). 409 si ya hay una
    corrida en curso para ese negocio (`QaError`)."""
    from core.admin.roles import testers_habilitados
    from core.plugin_manager import plugin_manager
    from core.qa.service import QaError

    if not testers_habilitados():
        return "Los datos de prueba solo existen en el entorno de test (QA_TESTERS_ENABLED)."
    if not get_settings().aos_connection_routing:
        return "AOS_CONNECTION_ROUTING está apagada en este entorno: el negocio sigue en el motor anterior."
    async with get_session() as candado:
        from core.kernel.banderas import CANDADO_SQL

        # El mismo candado que el panel del master (HU-AOS-15): no se intercalan backfills ni builds.
        tomado = (await candado.execute(text(CANDADO_SQL), {"t": str(tenant_id)})).scalar_one()
        if not tomado:
            raise QaError("conflicto", "Tu negocio de prueba se está preparando para el Agent OS. Espera un momento.")
        try:
            await _negocio_del_tester(tenant_id, slug)
            modulos = list(plugin_manager.list_modules())
            await _chat_web(tenant_id)
            await _backfills(tenant_id, modulos)
            # Otra vez justo antes de escribir `aos_runtime` con el rol dueño (migrar_a_v3 filtra
            # solo por id): el slug no cambió durante la corrida.
            await _negocio_del_tester(tenant_id, slug)
            await _a_v3(tenant_id, modulos)
            await _preset(tenant_id, modulos)
            await _activar_releases(tenant_id, modulos)
            await _banderas(tenant_id, slug)
        except Exception as exc:  # frontera: el motivo queda para el tester y el reintento
            motivo = _motivo(exc)
            log.warning(
                "qa_datos_agent_os_fallo",
                tenant_id=str(tenant_id),
                error=type(exc).__name__,
                motivo=motivo[:300],
                exc_info=not isinstance(exc, FalloAgentOS | DBAPIError),
            )
            return f"No se terminó de preparar el Agent OS: {motivo}"
    log.info("qa_datos_agent_os_listo", tenant_id=str(tenant_id))
    return None


async def resumen(tenant_id: uuid.UUID, motivo: str | None) -> dict[str, Any]:
    """Lo que ve el tester: estado, motivo, chat web (clave y enlace) y agentes del chat web."""
    from core.gateway import chat_web_conexion

    async with get_session() as session:
        fila = (
            await session.execute(
                text("SELECT aos_runtime, aos_procedures, aos_routing FROM tenants WHERE id = :t AND deleted_at IS NULL"),
                {"t": tenant_id},
            )
        ).first()
    chat: dict[str, Any] | None = None
    agentes: list[dict[str, Any]] = []
    if fila is not None:
        async with tenant_scope(tenant_id):
            async with get_session() as session:
                conexion = await chat_web_conexion.conexion_del_negocio(session, tenant_id)
                if conexion is not None:
                    chat = {
                        "clave": conexion.external_id,
                        "enlace": enlace_del_chat(conexion.external_id),
                        "activo": bool(conexion.active),
                    }
                    filas = (
                        await session.execute(
                            text(
                                "SELECT a.key, a.name, a.kind, i.is_default FROM agent_instances i "
                                "JOIN agents a ON a.tenant_id = i.tenant_id AND a.id = i.agent_id "
                                "WHERE i.tenant_id = :t AND i.connection_id = :c AND i.deleted_at IS NULL "
                                "AND a.deleted_at IS NULL AND a.status = 'active' "
                                "ORDER BY a.kind = 'core' DESC, i.is_default DESC, a.key"
                            ),
                            {"t": tenant_id, "c": conexion.id},
                        )
                    ).all()
                    agentes = [
                        {"key": f[0], "nombre": f[1], "rol": NOMBRE_ROL.get(str(f[2]), str(f[2])), "por_defecto": bool(f[3])}
                        for f in filas
                    ]
    listo = (
        fila is not None
        and (fila[0], bool(fila[1]), fila[2]) == ("v3", True, "active")
        and chat is not None
        and chat["activo"]
    )
    return {
        "estado": "listo" if listo and not motivo else ("error" if motivo else "pendiente"),
        "motivo": motivo,
        "chat_web": chat,
        "agentes": agentes,
    }


def como_contenido(agent_os: dict[str, Any]) -> dict[str, Any]:
    """La misma información como una entrada de `contenido`, que la pestaña Datos ya muestra."""
    detalle: list[str] = []
    chat = agent_os.get("chat_web")
    if chat:
        detalle.append(f"Chat web: {chat['enlace'] or chat['clave']}")
    if agent_os["agentes"]:
        detalle.append(
            "Agentes: "
            + ", ".join(a["nombre"] + (" (por defecto)" if a["por_defecto"] else "") for a in agent_os["agentes"])
        )
    if agent_os["estado"] == "listo":
        detalle.append("Enrutamiento activo: «quiero agendar una demo» lo atiende Agenda")
    elif agent_os["motivo"]:
        detalle.append(agent_os["motivo"])
    else:
        detalle.append("Pendiente: pulsa «Preparar mis datos» para activarlo")
    return {"habilidad": "Agent OS", "detalle": detalle}


__all__ = ["AGENTES", "como_contenido", "enlace_del_chat", "preparar", "resumen"]
