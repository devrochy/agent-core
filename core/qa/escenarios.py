"""Escenarios de datos de prueba (SPEC-30, HU-QA-13, adenda de ADR-0135).

Botones del centro de pruebas que crean, en el negocio `qa-<hex>` del tester, los datos que cada
módulo del panel necesita para probarse: turnos, escalaciones, calidad, versiones, canary, clientes y
pedidos, suscripciones, avisos, mensajes fallidos y cobranza.

Reglas (adenda de ADR-0135):

- **Solo en test** (`QA_TESTERS_ENABLED`) y **solo en el negocio del tester**: la fila de
  `qa_tester_datasets` en `listo`, el negocio vivo y `slug = qa-<hex del tester>`, revalidado con
  `slug LIKE 'qa-%'` en la misma consulta. Todo corre en su `tenant_scope`.
- **Un candado por negocio** (`qa_escenario:<tenant>`), más el del Agent OS en los que construyen
  releases.
- **Deterministas, sin LLM y aditivos**: el contenido es fijo; cambian los ids y un sufijo corto.
  Repetir crea otro caso y no borra nada.
- **Funciones del core**: los turnos corren dentro del ledger del kernel (`escenarios_aos`); las
  tablas de los packs las escribe cada pack con su gancho `sembrar_escenario`.
- **Sin efectos fuera del negocio de prueba**: ningún cliente lleva teléfono ni conversación de
  WhatsApp. Los avisos `warning`/`critical` (y la escalación) mandan push solo a los dispositivos
  registrados en el negocio `qa-`.

Cada escenario creado deja un `qa_events` (`campo = datos.escenario`) con su resumen: es «lo que
creó la última vez» que muestra la tarjeta.
"""

from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text

from core.db.models import Admin, QaEvent, QaTesterDataset
from core.db.session import get_session
from core.qa.service import QaError
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

CAMPO = "datos.escenario"
ACTOR = "system:qa-escenario"
MONEDA = "COP"
ZONA_HORARIA = "America/Bogota"
MODELO = "escenario-sin-llm"


class FalloEscenario(Exception):
    """El escenario no se pudo crear; el mensaje es el motivo que ve el tester (409)."""


@dataclass
class Contexto:
    """Lo que un escenario necesita del negocio de prueba. Se arma dentro del candado."""

    tenant_id: uuid.UUID
    slug: str
    tester_id: uuid.UUID
    dueno_admin_id: uuid.UUID | None
    dueno_email: str | None
    sufijo: str = field(default_factory=lambda: secrets.token_hex(3).upper())


@dataclass
class Resultado:
    """Lo que creó el escenario: líneas legibles, conteos y la ruta del panel donde se ve."""

    detalle: list[str]
    conteos: dict[str, int]
    ruta: str


@dataclass(frozen=True)
class Escenario:
    slug: str
    titulo: str
    crea: str
    requiere: str
    donde: str
    ruta: str
    agent_os: bool
    releases: bool
    crear: Callable[[Contexto], Awaitable[Resultado]]


# ------------------------------------------------------------------ ayudas comunes
def item_slug(slug: str) -> str:
    return f"datos-escenario-{slug}"


async def conexion_web(tenant_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID | None, uuid.UUID | None]:
    """La conexión del chat web del negocio: `(id, release activa, release en prueba)`."""
    async with get_session() as session:
        fila = (
            await session.execute(
                text(
                    "SELECT id, active_release_id, canary_release_id FROM channel_connections "
                    "WHERE tenant_id = :t AND type_id = 'webchat' AND deleted_at IS NULL ORDER BY created_at LIMIT 1"
                ),
                {"t": tenant_id},
            )
        ).first()
    if fila is None:
        raise FalloEscenario("Tu negocio de prueba no tiene chat web: vuelve a pulsar «Preparar mis datos».")
    return fila[0], fila[1], fila[2]


async def crear_cliente(
    tenant_id: uuid.UUID,
    nombre: str,
    *,
    sufijo: str,
    canal: str = "web",
    connection_id: uuid.UUID | None = None,
    estado: str = "open",
    bot_paused: bool = False,
    conversacion: bool = True,
    external_id: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID | None]:
    """Un cliente ficticio sin teléfono y, si se pide, su conversación en el chat web. El canal del
    cliente puede ser `whatsapp` (así lo pide la ficha), pero su conversación siempre es `web`: ningún
    aviso al cliente sale por WhatsApp."""
    cliente, conv = uuid.uuid4(), uuid.uuid4() if conversacion else None
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO customers (id, tenant_id, channel, external_id, name, bot_paused) "
                "VALUES (:id, :t, :canal, :ext, :nombre, :pausa)"
            ),
            {
                "id": cliente,
                "t": tenant_id,
                "canal": canal,
                "ext": external_id or f"{canal}:qa-{sufijo.lower()}-{cliente.hex[:8]}",
                "nombre": nombre,
                "pausa": bot_paused,
            },
        )
        if conv is not None:
            await session.execute(
                text(
                    "INSERT INTO conversations (id, tenant_id, customer_id, channel, connection_id, status) "
                    "VALUES (:id, :t, :c, 'web', :cx, :estado)"
                ),
                {"id": conv, "t": tenant_id, "c": cliente, "cx": connection_id, "estado": estado},
            )
    return cliente, conv


def _pack(pack_id: str) -> Any:
    from core.plugin_manager import plugin_manager

    modulo = plugin_manager.get(pack_id)
    if modulo is None:
        raise FalloEscenario(f"La habilidad {pack_id} no está instalada en este entorno.")
    return modulo


async def sembrar_en_pack(pack_id: str, escenario: str, entrada: Any) -> Any:
    from core.qa.contrato_datos import EscenarioNoDisponible

    try:
        return await _pack(pack_id).sembrar_escenario(entrada.tenant_id, escenario, entrada.entrada)
    except EscenarioNoDisponible as exc:
        raise FalloEscenario(str(exc)[:300]) from exc


@dataclass(frozen=True)
class _Llamada:
    tenant_id: uuid.UUID
    entrada: Any


def entrada(ctx: Contexto, **campos: Any) -> _Llamada:
    from core.qa.contrato_datos import EntradaDeEscenario

    return _Llamada(
        ctx.tenant_id,
        EntradaDeEscenario(
            sufijo=ctx.sufijo, moneda=MONEDA, zona_horaria=ZONA_HORARIA, autor_admin_id=ctx.dueno_admin_id, **campos
        ),
    )


# ------------------------------------------------------------------ clientes y pedidos
CLIENTES: tuple[dict[str, Any], ...] = (
    {"nombre": "Laura Gómez", "canal": "whatsapp", "email": "laura.gomez@ejemplo.test", "municipio": "Manizales",
     "clasificacion": "frecuente", "nota": "Prefiere entregas en la tarde."},
    {"nombre": "Andrés Rivera", "canal": "web", "email": "andres.rivera@ejemplo.test", "municipio": "Pereira",
     "clasificacion": "nuevo", "nota": "Preguntó por café para oficina."},
    {"nombre": "Camila Torres", "canal": "whatsapp", "email": "camila.torres@ejemplo.test", "municipio": "Armenia",
     "clasificacion": "fiel", "nota": "Compra cada mes la prensa y filtros."},
    {"nombre": "Julián Herrera", "canal": "web", "email": "julian.herrera@ejemplo.test", "municipio": "Manizales",
     "clasificacion": "ocasional", "nota": "Pidió factura a nombre de su empresa."},
    {"nombre": "Valentina Ruiz", "canal": "web", "email": "valentina.ruiz@ejemplo.test", "municipio": "Chinchiná",
     "clasificacion": "inactivo", "nota": "No responde desde hace dos meses."},
    {"nombre": "Santiago Mejía", "canal": "whatsapp", "email": "santiago.mejia@ejemplo.test", "municipio": "Villamaría",
     "clasificacion": "frecuente", "nota": "Quiere el plan anual de asesoría."},
)


async def _perfil_y_nota(ctx: Contexto, customer_id: uuid.UUID, datos: dict[str, Any]) -> None:
    from core.db.models import CustomerNote
    from core.memory import facts

    nombre, apellido = datos["nombre"].split(" ", 1)
    async with get_session() as session:
        await facts.escribir_hechos(
            session,
            tenant_id=ctx.tenant_id,
            customer_id=customer_id,
            namespace=facts.PERFIL,
            valores={
                "name": nombre,
                "apellido": apellido,
                "email": datos["email"],
                "municipio": datos["municipio"],
                "departamento": "Caldas",
                "pais": "Colombia",
                "clasificacion": datos["clasificacion"],
            },
            source="human",
        )
        session.add(
            CustomerNote(
                tenant_id=ctx.tenant_id,
                customer_id=customer_id,
                text=datos["nota"],
                author_admin_id=ctx.dueno_admin_id,
                author_label=ctx.dueno_email or "Dueño de prueba",
            )
        )


async def _clientes_y_pedidos(ctx: Contexto) -> Resultado:
    from core.qa.contrato_datos import ClienteDeEscenario

    cid, _activa, _canary = await conexion_web(ctx.tenant_id)
    clientes: list[ClienteDeEscenario] = []
    for datos in CLIENTES:
        nombre = f"{datos['nombre']} · {ctx.sufijo}"
        cliente, conv = await crear_cliente(
            ctx.tenant_id, nombre, sufijo=ctx.sufijo, canal=datos["canal"], connection_id=cid, estado="closed"
        )
        assert conv is not None
        await _perfil_y_nota(ctx, cliente, datos)
        clientes.append(ClienteDeEscenario(customer_id=cliente, conversation_id=conv, nombre=nombre))
    resumen = await sembrar_en_pack("pack_commerce", "clientes-y-pedidos", entrada(ctx, clientes=tuple(clientes)))
    whatsapp = sum(1 for d in CLIENTES if d["canal"] == "whatsapp")
    return Resultado(
        detalle=[
            f"{len(clientes)} clientes con perfil y una nota del equipo ({whatsapp} de WhatsApp, sin teléfono)",
            *resumen.detalle,
        ],
        conteos={"clientes": len(clientes), **resumen.conteos},
        ruta="/leads",
    )


# ------------------------------------------------------------------ suscripciones
async def _activar_suscripciones(ctx: Contexto) -> bool:
    """Activa `pack_subscriptions` en el negocio (lo mismo que `PUT /admin/plugins/{id}/config`).
    Devuelve True si lo activó ahora."""
    from core.admin.views import configure_plugin_for_tenant
    from core.agents.spec import enabled_packs, load_active_spec
    from core.billing.enforcement import QuotaExceeded

    if "pack_subscriptions" in enabled_packs(await load_active_spec(ctx.tenant_id)):
        return False
    try:
        await configure_plugin_for_tenant("pack_subscriptions", {})
    except QuotaExceeded as exc:
        raise FalloEscenario(f"El plan del negocio de prueba no admite otra habilidad: {exc}") from exc
    return True


async def _suscripciones(ctx: Contexto) -> Resultado:
    from core.qa.contrato_datos import ClienteDeEscenario

    _pack("pack_subscriptions")
    activada = await _activar_suscripciones(ctx)
    planes = await sembrar_en_pack("pack_subscriptions", "planes-de-suscripcion", entrada(ctx))
    cid, _activa, _canary = await conexion_web(ctx.tenant_id)
    clientes: list[ClienteDeEscenario] = []
    for nombre in ("Diana Castaño", "Felipe Ocampo", "Mariana Londoño", "Tomás Arango"):
        etiqueta = f"{nombre} · {ctx.sufijo}"
        cliente, conv = await crear_cliente(ctx.tenant_id, etiqueta, sufijo=ctx.sufijo, connection_id=cid, estado="closed")
        assert conv is not None
        clientes.append(ClienteDeEscenario(customer_id=cliente, conversation_id=conv, nombre=etiqueta))
    resumen = await sembrar_en_pack(
        "pack_subscriptions",
        "suscripciones",
        entrada(ctx, clientes=tuple(clientes), planes=tuple(planes.planes)),
    )
    detalle = [*planes.detalle, *resumen.detalle]
    if activada:
        detalle.insert(0, "Activó la habilidad Suscripciones en tu negocio de prueba")
    return Resultado(detalle=detalle, conteos={**planes.conteos, **resumen.conteos}, ruta="/suscripciones")


# ------------------------------------------------------------------ cobranza
async def _cobranza(ctx: Contexto) -> Resultado:
    resumen = await sembrar_en_pack("pack_cobranza", "cobranza-completa", entrada(ctx))
    return Resultado(detalle=resumen.detalle, conteos=resumen.conteos, ruta="/cobranza")


# ------------------------------------------------------------------ notificaciones
# (categoría, severidad, título, cuerpo, sección, entidad). Textos fijos de infraestructura, como
# los de `core/notifications/catalog.py`.
AVISOS: tuple[tuple[str, str, str, str, str | None, str], ...] = (
    ("conversaciones", "warning", "Un cliente espera respuesta",
     "Escenario de prueba: un cliente del chat web lleva un rato sin respuesta.", "conversations", "conversacion"),
    ("ventas", "success", "Pedido nuevo",
     "Escenario de prueba: entró un pedido por el chat web.", None, "pedido"),
    ("cobros", "info", "Pago recibido",
     "Escenario de prueba: se registró un pago de una suscripción.", None, "pago"),
    ("agenda", "info", "Cita agendada",
     "Escenario de prueba: un cliente agendó una asesoría para mañana.", None, "cita"),
    ("sistema", "critical", "Un mensaje no se pudo procesar",
     "Escenario de prueba: un mensaje agotó sus reintentos y está en Mensajes fallidos.", None, "mensaje_fallido"),
)


async def _notificaciones(ctx: Contexto) -> Resultado:
    from core.notifications.service import notify_standalone
    from core.notifications.spec import NotificationSpec

    cid, _activa, _canary = await conexion_web(ctx.tenant_id)
    _cliente, conv = await crear_cliente(
        ctx.tenant_id, f"Cliente de los avisos · {ctx.sufijo}", sufijo=ctx.sufijo, connection_id=cid
    )
    creados = 0
    for n, (categoria, severidad, titulo, cuerpo, seccion, entidad) in enumerate(AVISOS, start=1):
        objetivo = str(conv) if entidad == "conversacion" else uuid.uuid4().hex
        spec = NotificationSpec(
            event_key=f"qa.escenario.{categoria}",
            category=categoria,
            severity=severidad,
            title=titulo,
            body=cuerpo,
            dedupe_key=f"qa-escenario:{ctx.sufijo}:{n}",
            section=seccion,
            data={"escenario": "notificaciones"},
            target={"entity": entidad, "id": objetivo, "section": seccion} if entidad == "conversacion" else None,
        )
        if await notify_standalone(ctx.tenant_id, spec) is not None:
            creados += 1
    if creados == 0:
        raise FalloEscenario("No se pudo crear ningún aviso.")
    return Resultado(
        detalle=[f"{creados} avisos, uno por categoría: conversaciones, ventas, cobros, agenda y sistema"],
        conteos={"avisos": creados},
        ruta="/dashboard",
    )


# ------------------------------------------------------------------ mensajes fallidos
async def _mensajes_fallidos(ctx: Contexto) -> Resultado:
    """Una entrada de la DLQ de entrada (con el payload que deja el consumer) y un evento de dominio
    fallido con un topic sin handlers. El cliente del mensaje existe antes con el agente en pausa:
    «Reintentar» lo procesa de verdad, sin turno ni envío."""
    from core.db.models import DomainEvent
    from core.memory.working import get_redis
    from core.orchestrator.consumer import DEAD_LETTER_STREAM, MARCA_ESCENARIO

    cid, _activa, _canary = await conexion_web(ctx.tenant_id)
    async with get_session() as session:
        clave = (
            await session.execute(
                text("SELECT external_id FROM channel_connections WHERE tenant_id = :t AND id = :c"),
                {"t": ctx.tenant_id, "c": cid},
            )
        ).scalar_one()
    visitante = f"web:qa-dlq-{ctx.sufijo.lower()}"
    await crear_cliente(
        ctx.tenant_id,
        f"Visitante del mensaje fallido · {ctx.sufijo}",
        sufijo=ctx.sufijo,
        bot_paused=True,
        conversacion=False,
        external_id=visitante,
    )
    ahora = datetime.now(UTC)
    entrada_dlq = {
        "tenant_id": str(ctx.tenant_id),
        "provider": "web",
        "channel": "web",
        "external_message_id": f"web:qa-dlq-{uuid.uuid4()}",
        "customer_external_id": visitante,
        "customer_name": "",
        "content": "Hola, ¿tienen café molido para cafetera de goteo? (mensaje de prueba)",
        "connection_external_id": clave,
        "media": "[]",
        "media_context": "",
        "error": "RuntimeError: escenario de prueba (el turno falló tres veces)",
        "deliveries": "3",
        "failed_at": ahora.isoformat(),
        # No cuenta para la alerta al on-call ni para el chequeo previo a producción (`dlq_pendientes`).
        MARCA_ESCENARIO: "mensajes-fallidos",
    }
    entry_id = await get_redis().xadd(DEAD_LETTER_STREAM, entrada_dlq)
    async with get_session() as session:
        session.add(
            DomainEvent(
                tenant_id=ctx.tenant_id,
                topic="qa.escenario_de_prueba",
                payload={"tenant_id": str(ctx.tenant_id), "escenario": "mensajes-fallidos"},
                status="fallido",
                attempts=5,
                next_attempt_at=ahora,
                dedupe_key=f"qa-escenario:{ctx.sufijo}",
                last_error="Escenario de prueba: el evento agotó sus 5 intentos.",
            )
        )
    return Resultado(
        detalle=[
            f"Un mensaje del chat web en la cola de fallidos (entrada {entry_id})",
            "Un evento de dominio fallido (qa.escenario_de_prueba) con 5 intentos",
        ],
        conteos={"mensajes": 1, "eventos": 1},
        ruta="/dlq",
    )


# ------------------------------------------------------------------ registro
def _registro() -> dict[str, Escenario]:
    from core.qa import escenarios_aos as aos

    lista = (
        Escenario(
            "conversacion-con-turnos", "Conversación con turnos",
            "Un cliente ficticio y una conversación en el chat web con 3 turnos: uno normal, uno con una acción "
            "con datos personales ficticios y una consulta entre agentes, y uno con traspaso de Ventas a Agenda; "
            "comandos, consumo y un rechazo del verificador.",
            "Tus datos preparados con el Agent OS listo.", "Conversaciones \u203a «Ver turno»", "/conversaciones",
            True, False, aos.conversacion_con_turnos,
        ),
        Escenario(
            "escalacion-en-curso", "Escalación en curso",
            "Una conversación escalada y pendiente con dos trámites: «Contratar un plan» en «elegir plan» con "
            "nombre, correo y una referencia de pago, y un pedido que se cerró al pasar a una persona.",
            "Tus datos preparados con el Agent OS listo.", "Conversaciones \u203a tarjeta «En curso» y Escalaciones",
            "/conversaciones", True, False, aos.escalacion_en_curso,
        ),
        Escenario(
            "calidad-con-muestra", "Calidad con muestra",
            "Unos 50 turnos de Ventas y Agenda, por encima de la muestra mínima, con escalaciones, recorridos "
            "completos e incompletos, rechazos del verificador y un veredicto de consistencia.",
            "Tus datos preparados con el Agent OS listo.", "Agentes \u203a Calidad", "/agentes/calidad",
            True, False, aos.calidad_con_muestra,
        ),
        Escenario(
            "version-candidata", "Versión candidata",
            "Una versión candidata en el chat web: cambia el nombre del asistente. Si vuelves a pulsar «Preparar "
            "mis datos», esa candidata se publica.",
            "Tus datos preparados con el Agent OS listo y sin una prueba en curso.", "Conexiones \u203a Versiones",
            "/conexiones", True, True, aos.version_candidata,
        ),
        Escenario(
            "canary-en-curso", "Prueba con una parte",
            "Una candidata en prueba con el 20 % de las conversaciones nuevas y una muestra parcial en las dos "
            "versiones (por debajo de las 200 conversaciones). Si vuelves a pulsar «Preparar mis datos», la versión "
            "en prueba se publica.",
            "Tus datos preparados con el Agent OS listo y sin otra prueba en curso.",
            "Conexiones \u203a Versiones, bloque de la prueba", "/conexiones", True, True, aos.canary_en_curso,
        ),
        Escenario(
            "clientes-y-pedidos", "Clientes y pedidos",
            "6 clientes con perfil, interés y notas (3 de WhatsApp, sin teléfono); pedidos pendiente, pagado y "
            "cancelado, y una promoción.",
            "Tus datos preparados.", "Clientes, Ventas \u203a Pedidos y Promociones", "/leads",
            False, False, _clientes_y_pedidos,
        ),
        Escenario(
            "suscripciones", "Suscripciones",
            "Activa Suscripciones y crea 2 planes y suscripciones activa, por vencer y vencida, con cargos y "
            "pagos.",
            "Tus datos preparados.", "Suscripciones", "/suscripciones", False, False, _suscripciones,
        ),
        Escenario(
            "notificaciones", "Notificaciones",
            "Un aviso por categoría: conversaciones, ventas, cobros, agenda y sistema.",
            "Tus datos preparados. Para verlos, entra con tu dueño de prueba.", "Campana", "/dashboard",
            False, False, _notificaciones,
        ),
        Escenario(
            "mensajes-fallidos", "Mensajes fallidos",
            "Un mensaje del chat web y un evento de dominio fallidos, listos para «Reintentar».",
            "Tus datos preparados.", "Configuración \u203a Mensajes fallidos", "/dlq", False, False, _mensajes_fallidos,
        ),
        Escenario(
            "cobranza-completa", "Cobranza completa",
            "Sobre una cartera nueva: promesas, cobro al propietario, alertas de acceso (una denegada), un "
            "dispositivo por aprobar, reglas de negociación y un cierre de caja con su liquidación.",
            "Tus datos preparados.", "Cobranza", "/cobranza", False, False, _cobranza,
        ),
    )
    return {e.slug: e for e in lista}


ESCENARIOS: dict[str, Escenario] = {}


def escenarios() -> dict[str, Escenario]:
    if not ESCENARIOS:
        ESCENARIOS.update(_registro())
    return ESCENARIOS


# ------------------------------------------------------------------ ejecución
def _exigir_habilitado() -> None:
    from core.admin.roles import testers_habilitados

    if not testers_habilitados():
        raise QaError("no_encontrado", "Los escenarios de datos de prueba solo existen en el entorno de test.")


async def _negocio_del_tester(admin: Admin) -> Contexto:
    """El negocio de prueba vivo del tester, con su slug `qa-` revalidado en la misma consulta."""
    from core.qa.datos import slug_de

    async with get_session() as session:
        fila = (
            await session.execute(
                text(
                    "SELECT d.tenant_id, d.slug, d.dueno_admin_id, a.email FROM qa_tester_datasets d "
                    "JOIN tenants t ON t.id = d.tenant_id AND t.slug = d.slug AND t.slug LIKE 'qa-%' "
                    "AND t.deleted_at IS NULL AND t.status = 'active' "
                    "LEFT JOIN admins a ON a.id = d.dueno_admin_id "
                    "WHERE d.admin_id = :a AND d.estado = 'listo' AND d.slug = :s"
                ),
                {"a": admin.id, "s": slug_de(admin.id)},
            )
        ).first()
    if fila is None:
        raise QaError("conflicto", "Prepara tus datos primero: pulsa «Preparar mis datos».")
    return Contexto(
        tenant_id=fila[0], slug=fila[1], tester_id=admin.id, dueno_admin_id=fila[2], dueno_email=fila[3]
    )


async def _exigir_agent_os(tenant_id: uuid.UUID) -> None:
    from core.config import get_settings

    if not get_settings().kernel_ledger_enabled:
        raise FalloEscenario("El ledger del kernel está apagado en este entorno (KERNEL_LEDGER_ENABLED).")
    async with get_session() as session:
        runtime = (
            await session.execute(text("SELECT aos_runtime FROM tenants WHERE id = :t"), {"t": tenant_id})
        ).scalar_one()
    if runtime != "v3":
        raise FalloEscenario(
            "Tu negocio de prueba no tiene el Agent OS listo: vuelve a pulsar «Preparar mis datos»."
        )


async def _ultimos(admin_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    async with get_session() as session:
        filas = (
            await session.execute(
                select(QaEvent.item_slug, QaEvent.despues, QaEvent.en)
                .where(QaEvent.admin_id == admin_id, QaEvent.campo == CAMPO)
                .order_by(QaEvent.item_slug, QaEvent.en.desc())
                .distinct(QaEvent.item_slug)
            )
        ).all()
    salida: dict[str, dict[str, Any]] = {}
    for slug, despues, en in filas:
        try:
            datos = json.loads(despues or "{}")
        except ValueError:
            continue
        salida[str(slug)] = {**datos, "creado_en": en.isoformat() if en else None}
    return salida


def _a_dict(e: Escenario, ultimo: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "slug": e.slug,
        "titulo": e.titulo,
        "crea": e.crea,
        "requiere": e.requiere,
        "donde": e.donde,
        "ruta": e.ruta,
        "agent_os": e.agent_os,
        "ultimo": ultimo,
    }


async def listar(admin: Admin) -> dict[str, Any]:
    """Los escenarios y lo que creó la última vez cada uno para este tester."""
    _exigir_habilitado()
    async with get_session() as session:
        ds = (
            await session.execute(select(QaTesterDataset.estado).where(QaTesterDataset.admin_id == admin.id))
        ).scalar_one_or_none()
    ultimos = await _ultimos(admin.id)
    return {
        "datos_listos": ds == "listo",
        "escenarios": [_a_dict(e, ultimos.get(item_slug(e.slug))) for e in escenarios().values()],
    }


async def crear(admin: Admin, slug: str) -> dict[str, Any]:
    """Crea un caso nuevo del escenario en el negocio de prueba del tester. 404 si no existe el
    escenario (o fuera de test), 409 si no se puede (sin datos, otra corrida en curso, un motivo)."""
    from core.kernel.banderas import CANDADO_SQL
    from core.releases.promocion import ConflictoDeRelease

    _exigir_habilitado()
    escenario = escenarios().get(slug)
    if escenario is None:
        raise QaError("no_encontrado", "Ese escenario no existe.")
    ctx = await _negocio_del_tester(admin)
    async with get_session() as candado:
        tomado = (
            await candado.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended('qa_escenario:' || :t, 0))"),
                {"t": str(ctx.tenant_id)},
            )
        ).scalar_one()
        if tomado and escenario.releases:
            tomado = (await candado.execute(text(CANDADO_SQL), {"t": str(ctx.tenant_id)})).scalar_one()
        if not tomado:
            raise QaError("conflicto", "Ya se está creando un escenario en tu negocio de prueba. Espera un momento.")
        # Con el candado tomado se vuelve a comprobar el negocio (pudo suspenderse o purgarse en el medio).
        if (await _negocio_del_tester(admin)).tenant_id != ctx.tenant_id:
            raise QaError("conflicto", "Tu negocio de prueba cambió: vuelve a intentarlo.")
        try:
            async with tenant_scope(ctx.tenant_id):
                if escenario.agent_os:
                    await _exigir_agent_os(ctx.tenant_id)
                resultado = await escenario.crear(ctx)
        except FalloEscenario as exc:
            raise QaError("conflicto", str(exc)) from exc
        except ConflictoDeRelease as exc:
            raise QaError("conflicto", str(exc)) from exc
        except PermissionError as exc:  # un pack fuera del negocio del escenario: nunca debería pasar
            log.error("qa_escenario_fuera_del_negocio", escenario=slug)
            raise QaError("conflicto", "El escenario no se pudo crear en tu negocio de prueba.") from exc
    resumen = {"sufijo": ctx.sufijo, "detalle": resultado.detalle, "conteos": resultado.conteos, "ruta": resultado.ruta}
    async with get_session() as session:
        evento = QaEvent(
            item_slug=item_slug(slug), campo=CAMPO, despues=json.dumps(resumen, ensure_ascii=False), admin_id=admin.id
        )
        session.add(evento)
        await session.flush()
        en = evento.en
    log.info("qa_escenario_creado", escenario=slug, tenant_id=str(ctx.tenant_id), conteos=resultado.conteos)
    return {
        **_a_dict(escenario, None),
        "negocio_id": ctx.tenant_id,
        "creado": {**resumen, "creado_en": en.isoformat() if en else None},
    }


__all__ = ["ESCENARIOS", "Contexto", "Escenario", "FalloEscenario", "Resultado", "crear", "escenarios", "listar"]
