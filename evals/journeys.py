"""Runner de journeys: conversaciones completas con herramientas, pagos y reloj (HU-SUB-12).

El golden set de `evals/runner.py` mira la PRIMERA respuesta del modelo, sin ejecutar
herramientas: no ve si una compra termina, si un pago activa la suscripción o si la
mora avisa. Un journey corre el **loop real** (`process_conversation`) turno a turno,
con un **proveedor de pagos simulado** (sin red), eventos (`payment.confirmed`),
**saltos de reloj** y jobs de los packs, y verifica herramientas, estados y mensajes.

Uso:
  python evals/journeys.py                          # set `suscripciones` completo
  python evals/journeys.py 03,05                    # solo los que matcheen
  python evals/journeys.py --set comercio           # otro set de evals/journeys/<set>/
  python evals/journeys.py --umbral 0.95 --reporte evals/reportes/journeys.json
  python evals/journeys.py --tenant solvershub      # set de un negocio: TENANTS_ROOT/<slug>/evals/journeys/
  python evals/journeys.py 01,05 --grabacion replay # desde las grabaciones, sin red ni LLM_API_KEY
  python evals/journeys.py 05 --grabacion record    # regraba (LLM real: OK del dueño)

Requiere la base migrada y, sin `--grabacion replay`, LLM_API_KEY (crea un negocio de prueba por corrida). El
formato de los journeys está en `evals/journeys/README.md`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from core.llm import cassette

EVALS_DIR = Path(__file__).resolve().parent
JOURNEYS_DIR = EVALS_DIR / "journeys"
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
_PLANTILLA = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_.]*)\}")
_SALTO = re.compile(r"^\+(\d+)([dhm])$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class FallaJourney(AssertionError):
    """Un paso no cumplió lo esperado (el journey se corta ahí)."""


# ------------------------------------------------------------------ pagos simulados
@dataclass
class PagoSimulado:
    """Pasarela simulada: reemplaza al PROVEEDOR de `pack_payments` (la pieza que habla
    con Wompi), así corre el camino real del pack (capacidad y transacciones) sin red.

    Respeta el mismo mínimo que la pasarela real (`monto_minimo`; Wompi: 150.000 COP).
    """

    monto_minimo: float = 0
    links: list[dict[str, Any]] = field(default_factory=list)

    def instalar(self) -> None:
        try:
            import pack_payments.payments as capacidad
        except ImportError:  # sin pack de pagos instalado no hay nada que simular
            return
        capacidad.build_provider = self._proveedor  # type: ignore[assignment]

    def _proveedor(self, config: Any) -> Any:
        # Misma firma que `pack_payments.proveedor.build_provider` (#324).
        from pack_payments.providers.base import (
            BasePaymentProvider,
            PaymentLinkResult,
            PaymentRequestRejected,
        )

        pago = self

        class _Proveedor(BasePaymentProvider):
            provider_name = "simulado"

            async def create_payment_link(self, *, reference, amount, currency, expires_in_hours):  # type: ignore[no-untyped-def, override]
                if float(amount) < pago.monto_minimo:
                    raise PaymentRequestRejected(f"El monto {amount} es menor al mínimo de la pasarela ({pago.monto_minimo}).")
                pago.links.append({"reference": reference, "amount": float(amount)})
                return PaymentLinkResult(
                    provider_reference=f"sim-{reference}", payment_link_url=f"https://pagos.simulados/{reference}"
                )

            def verify_webhook_signature(self, payload):  # type: ignore[no-untyped-def, override]
                return True

            def parse_webhook_event(self, payload):  # type: ignore[no-untyped-def, override]
                # Los journeys confirman pagos con el evento del bus, no con webhooks.
                raise NotImplementedError("la pasarela simulada no recibe webhooks")

        return _Proveedor()


# ------------------------------------------------------------------ contexto
def datos_unicos(customer_id: uuid.UUID) -> dict[str, str]:
    """Documento y celular propios de cada intento, derivados del id del cliente: las reglas del
    alta (un Free por persona, intentos por persona; ADR-0125) cuentan por documento y teléfono, y
    los k intentos comparten el negocio de la corrida. Tienen forma válida (10 dígitos; el celular
    empieza por 3) y salen de bits distintos del id."""
    n = customer_id.int
    return {
        "documento": str(1_000_000_000 + n % 9_000_000_000),
        "celular": f"3{(n // 10**10) % 10**9:09d}",
    }


@dataclass
class Contexto:
    tenant_id: uuid.UUID
    customer_id: uuid.UUID
    conversation_id: uuid.UUID
    pagos: PagoSimulado
    desfase: timedelta = timedelta(0)
    variables: dict[str, Any] = field(default_factory=dict)
    semilla: int = 0
    # HU-ARCH-28: evidencia del intento para pass^k.
    aserciones: list[dict[str, Any]] = field(default_factory=list)
    herramientas: list[str] = field(default_factory=list)
    simulador: dict[str, Any] | None = None

    @property
    def ahora(self) -> datetime:
        return datetime.now(UTC) + self.desfase

    def valor(self, ruta: str) -> Any:
        partes = ruta.split(".")
        actual: Any = {
            **self.variables,
            "negocio": {"id": str(self.tenant_id)},
            "cliente": {"id": str(self.customer_id), **datos_unicos(self.customer_id)},
            "ahora": self.ahora.isoformat(),
            "pagos": {"ultimo": self.pagos.links[-1] if self.pagos.links else {}},
            "correos": {"ultimo": CORREOS.enviados[-1] if CORREOS.enviados else {}},
        }
        for parte in partes:
            if isinstance(actual, dict) and parte in actual:
                actual = actual[parte]
            elif isinstance(actual, list) and parte.isdigit() and int(parte) < len(actual):
                actual = actual[int(parte)]
            else:
                raise FallaJourney(f"variable sin valor: {{{ruta}}}")
        return actual

    def render(self, dato: Any) -> Any:
        """Reemplaza `{ruta}` en strings (recursivo). Un string que es solo `{ruta}` toma el valor crudo."""
        if isinstance(dato, str):
            unico = _PLANTILLA.fullmatch(dato)
            if unico:
                return self.valor(unico.group(1))
            return _PLANTILLA.sub(lambda m: str(self.valor(m.group(1))), dato)
        if isinstance(dato, list):
            return [self.render(d) for d in dato]
        if isinstance(dato, dict):
            return {k: self.render(v) for k, v in dato.items()}
        return dato


@dataclass
class CorreosSimulados:
    """Reemplaza el correo de sistema (invitaciones del alta, avisos): nada sale del proceso
    y el journey puede leer lo enviado como `{correos.ultimo.para}`."""

    enviados: list[dict[str, Any]] = field(default_factory=list)

    def instalar(self) -> None:
        import core.gateway.system_email as correo

        async def enviar(to: str, subject: str, body: str) -> str:
            self.enviados.append({"para": to, "asunto": subject, "cuerpo": body})
            return f"<journeys-{uuid.uuid4().hex}@example.com>"

        correo.send_system_email = enviar  # type: ignore[assignment]
        # Quien lo importó a nivel de módulo guarda su propia referencia.
        import core.admin.password_reset_service as reseteo

        reseteo.send_system_email = enviar  # type: ignore[assignment]


CORREOS = CorreosSimulados()


# ------------------------------------------------------------------ réplica de un negocio
def _sin_copiar(directorio: str, nombres: list[str]) -> set[str]:
    """`evals/`, respaldos y symlinks: un symlink podría traer archivos de otro negocio."""
    fuera = set(shutil.ignore_patterns("evals", "*.tgz")(directorio, nombres))
    return fuera | {n for n in nombres if (Path(directorio) / n).is_symlink()}


def preparar_replica(origen: Path | None) -> tuple[str, Path]:
    """`TENANTS_ROOT` temporal de la corrida: los artefactos que escriben el negocio de prueba
    y las altas automáticas nunca quedan en el real. Con `origen` (HU-SH-08), además copia
    `TENANTS_ROOT/<slug>/` (sin `evals/` ni symlinks) con el slug del negocio de la corrida:
    el negocio real y su directorio no se tocan y los overrides por slug (ADR-0026) leen la
    copia. Devuelve el slug de la corrida y la raíz temporal."""
    from core.config import get_settings

    slug = f"journeys-{uuid.uuid4().hex[:8]}"
    raiz = Path(tempfile.mkdtemp(prefix="journeys-tenants-"))
    try:
        if origen is not None:
            shutil.copytree(origen, raiz / slug, ignore=_sin_copiar)
    except Exception:
        shutil.rmtree(raiz, ignore_errors=True)
        raise
    os.environ["TENANTS_ROOT"] = str(raiz)
    get_settings.cache_clear()
    return slug, raiz


def _como_vendedor(tenant_id: uuid.UUID) -> None:
    """El negocio de la corrida vende negocios (alta automática, ADR-0121) SOLO en este
    proceso: la allowlist real (`PROVISIONING_SELLER_TENANTS`) no se lee ni se toca."""
    import core.provisioning.vendedores as vendedores

    vendedores.vendedores_configurados = lambda: frozenset({tenant_id})  # type: ignore[assignment]


# ------------------------------------------------------------------ negocio y cliente
async def preparar_negocio(
    definicion: dict[str, Any],
    pagos: PagoSimulado,
    *,
    slug: str | None = None,
    replica: bool = False,
    motor: bool | None = None,
) -> uuid.UUID:
    """Negocio de prueba de la corrida: preset, plan, catálogo y config de packs. Con
    `replica`, además los artefactos del negocio copiados a su directorio (catálogo, seed de
    packs y voz de marca, como `agent-core tenant seed --voz-de-marca`) y sin ejemplos.
    `motor` (de `modo_de_runtime`) manda sobre `motor_de_procedimientos` del set."""
    from core.admin.views import set_pack_for_tenant
    from core.alta_negocio import onboard_tenant
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    slug = slug or f"journeys-{uuid.uuid4().hex[:8]}"
    tenant_id, _ = await onboard_tenant(
        name=definicion.get("nombre", "Negocio de journeys"),
        slug=slug,
        business_description=definicion.get("descripcion", "Negocio de prueba de los journeys."),
        packs=list(definicion.get("packs", [])),
        preset=definicion.get("preset"),
        plan=definicion.get("plan", "premium"),
        sembrar_ejemplos=not definicion.get("catalogo") and not definicion.get("planes") and not replica,
        agentes_base=False,  # el journey monta su propio runtime (`encender_runtime_v3`), no el del alta
    )
    async with tenant_scope(tenant_id):
        for pack_id, config in (definicion.get("config") or {}).items():
            await plugin_manager.save_config(pack_id, config)
            error = await set_pack_for_tenant(pack_id, True)
            if error:
                raise RuntimeError(f"No se pudo habilitar {pack_id}: {error}")
    if replica:
        from core.onboarding import sembrar_artefactos

        await sembrar_artefactos(tenant_id, slug, voz_de_marca=True)
    if definicion.get("catalogo"):
        from pack_commerce.seed import seed_catalog  # el set declara su catálogo de commerce

        await seed_catalog(tenant_id, definicion["catalogo"])
    if definicion.get("planes"):
        from pack_subscriptions.seed import (
            seed_planes,  # el set declara sus planes de suscripciones (SPEC-43)
        )

        await seed_planes(tenant_id, definicion["planes"])
    if definicion.get("vendedor"):
        _como_vendedor(tenant_id)
    await publicar_documentos_legales(tenant_id, definicion.get("documentos_legales") or [])
    if definicion.get("motor_de_procedimientos") if motor is None else motor:
        await encender_motor_de_procedimientos(tenant_id)
    pagos.instalar()
    return tenant_id


def modo_de_runtime(negocio: dict[str, Any], runtime: str | None) -> tuple[bool | None, bool]:
    """(motor, releases) de la corrida según `--runtime`.

    - Sin `--runtime`: lo de siempre (`motor_de_procedimientos` del set, sin releases).
    - Set con motor: `legacy` es la línea base de un agente, sin el motor; `v3` enciende el
      motor (`aos_runtime = v3` y `aos_procedures`, HU-AOS-07). Así se mide pass^k con y sin
      procedimientos sobre el mismo set (ADR-0112). No monta conexión ni release: el motor no
      las necesita.
    - Set sin motor: `v3` monta la release 1 por conexión (HU-AOS-15); `legacy`, nada."""
    if runtime is None:
        return None, False
    if negocio.get("motor_de_procedimientos"):
        return runtime == "v3", False
    return False, runtime == "v3"


def _exigir_base_local(*urls: str | None, que: str) -> None:
    """Las escrituras de banderas y conexiones van por la URL del dueño Y por la de la app:
    las dos tienen que ser locales (si no, la corrida escribiría en una base real)."""
    from sqlalchemy.engine import make_url

    for url in urls:
        if not url:
            continue
        host = (make_url(url).host or "").lower()
        if host not in ("localhost", "127.0.0.1", "::1"):
            raise SystemExit(f"{que} solo se enciende(n) en una base local, no en {host!r}.")


async def encender_runtime_v3(tenant_id: uuid.UUID) -> uuid.UUID:
    """Runtime por releases (HU-AOS-15) para el negocio de la corrida: una conexión
    conversacional, su release 1 (`backfill_releases`, desde el AgentSpec v2) y
    `aos_runtime = 'v3'` sin `aos_procedures`. Exige `AOS_CONNECTION_ROUTING=true` en el entorno.
    Devuelve la conexión: `nuevo_cliente` liga a ella las conversaciones de la corrida. Sirve para
    medir pass^k legacy vs v3 con el mismo set (`--runtime v3`)."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from core.config import get_settings
    from core.db.session import get_session
    from core.plugin_manager import plugin_manager
    from core.releases.packs import sync_packs
    from core.releases.release_uno import backfill_releases
    from core.shared.tenant import tenant_scope

    settings = get_settings()
    if not settings.aos_connection_routing:
        raise SystemExit("--runtime v3 exige AOS_CONNECTION_ROUTING=true (orden de banderas, ADR-0112).")
    url = settings.migrations_database_url or settings.database_url
    _exigir_base_local(url, settings.database_url, que="El runtime v3 de los journeys")
    conexion = uuid.uuid4()
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            await session.execute(
                text(
                    "insert into channel_connections (id, tenant_id, channel, provider, external_id, type_id) "
                    "values (:id, :t, 'instagram', 'meta', :ext, 'instagram_meta')"
                ),
                {"id": conexion, "t": tenant_id, "ext": f"journeys-{conexion.hex[:8]}"},
            )
    modulos = plugin_manager.scan()
    reporte_packs = await sync_packs(modulos)
    if reporte_packs.fallo:
        raise SystemExit(f"sync-packs falló: {reporte_packs.errores}")
    reporte = await backfill_releases(dry_run=False, tenant_id=tenant_id, modules=modulos)
    if reporte.fallo:
        raise SystemExit(f"No se pudo construir la release 1 del negocio de la corrida: {reporte.errores}")
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("update tenants set aos_runtime = 'v3' where id = :t"), {"t": tenant_id})
    finally:
        await engine.dispose()
    _CONEXION_V3[tenant_id] = conexion
    return conexion


# Conexión del negocio de la corrida con `--runtime v3` (las conversaciones nuevas se ligan a ella).
_CONEXION_V3: dict[uuid.UUID, uuid.UUID] = {}


async def encender_motor_de_procedimientos(tenant_id: uuid.UUID) -> None:
    """Motor de procedimientos (HU-AOS-07) para el negocio de la corrida: `aos_runtime = v3` y
    `aos_procedures`, como lo haría el master. Lo pide un set con `"motor_de_procedimientos": true`
    en su `negocio.json`; el negocio es de la corrida, nunca uno real. Las banderas solo las
    escribe el rol dueño (el mismo que migra)."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from core.config import get_settings

    settings = get_settings()
    url = settings.migrations_database_url or settings.database_url
    _exigir_base_local(url, settings.database_url, que="Las banderas del motor")
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("update tenants set aos_runtime = 'v3', aos_procedures = true where id = :t"), {"t": tenant_id}
            )
    finally:
        await engine.dispose()


async def publicar_documentos_legales(tenant_id: uuid.UUID, documentos: list[dict[str, Any]]) -> None:
    """Términos, privacidad y DPA de prueba (ADR-0125), publicados por la misma vía que usa
    Plataforma (`legal.publicar`, con su validación de tipo, versión y enlace): con los tres, el
    pedido de un plan pide aceptarlos antes de crearse, como en el vendedor real."""
    from core.provisioning import legal

    for documento in documentos:
        await legal.publicar(
            tenant_id,
            tipo=str(documento["tipo"]),
            version=str(documento["version"]),
            url=str(documento["url"]),
            por="journeys",
        )


async def nuevo_cliente(tenant_id: uuid.UUID, nombre: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Cliente y conversación propios del journey (canal `test`: sin envíos ni métricas)."""
    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope
    from core.shared.test_mode import TEST_CHANNEL

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            cliente = Customer(
                tenant_id=tenant_id, channel=TEST_CHANNEL, external_id=f"journey:{uuid.uuid4().hex}", name=nombre
            )
            session.add(cliente)
            await session.flush()
            conversacion = Conversation(
                tenant_id=tenant_id, customer_id=cliente.id, channel=TEST_CHANNEL, status="open", state={"is_test": True},
                # Con `--runtime v3` la conversación es de la conexión con release (HU-AOS-15).
                connection_id=_CONEXION_V3.get(tenant_id),
            )
            session.add(conversacion)
            await session.flush()
            return cliente.id, conversacion.id


# ------------------------------------------------------------------ pasos
# Borradores que un guardrail o el verificador bloquearon en el turno (solo evals). En producción
# el texto no se registra (puede traer datos del cliente); en un journey es de un cliente simulado,
# y sin él un intento que escala no dice QUÉ frase lo bloqueó.
_BLOQUEADOS: list[tuple[str, str]] = []


def registrar_bloqueos() -> None:
    """Envuelve `run_guardrails` del loop (una sola vez) para anotar el borrador y el motivo de
    cada bloqueo. No cambia el resultado: solo observa."""
    from core.orchestrator import loop

    original = loop.run_guardrails
    if getattr(original, "_registra_bloqueos", False):
        return

    async def observado(borrador: str, *args: Any, **kwargs: Any) -> Any:
        veredicto = await original(borrador, *args, **kwargs)
        if not veredicto.passed:
            _BLOQUEADOS.append((borrador, str(veredicto.reason)))
        return veredicto

    observado._registra_bloqueos = True  # type: ignore[attr-defined]
    loop.run_guardrails = observado


def tomar_bloqueados() -> list[str]:
    """Líneas del registro con los bloqueos del último turno; vacía la lista."""
    lineas = [f"    borrador bloqueado ({motivo.split(':', 1)[0][:60]}): {texto!r}" for texto, motivo in _BLOQUEADOS]
    _BLOQUEADOS.clear()
    return lineas


async def _turno(ctx: Contexto, mensaje: str) -> Any:
    """Un turno del cliente como en producción: el consumidor guarda el mensaje entrante, corre
    el loop y el envío guarda la respuesta. Sin esos mensajes en la base, lo que depende de ellos
    (la aceptación de los términos del alta, ADR-0125) nunca pasaría en un journey."""
    from core.orchestrator.loop import process_conversation
    from core.shared.tenant import tenant_scope

    async with tenant_scope(ctx.tenant_id):
        await _guardar_mensaje(ctx, "inbound", mensaje)
        resultado = await process_conversation(ctx.conversation_id, mensaje)
        if resultado is not None and resultado.text:
            await _guardar_mensaje(ctx, "outbound", resultado.text)
        return resultado


async def _guardar_mensaje(ctx: Contexto, direccion: str, texto: str) -> None:
    from core.db.models import Message
    from core.db.session import get_session

    async with get_session() as session:
        session.add(
            Message(
                tenant_id=ctx.tenant_id,
                conversation_id=ctx.conversation_id,
                direction=direccion,
                sender_type="customer" if direccion == "inbound" else "agent",
                content=texto,
            )
        )


async def _herramientas_desde(ctx: Contexto, desde: datetime) -> list[dict[str, Any]]:
    """Herramientas ejecutadas en la conversación desde `desde` (fuente: `audit_log`)."""
    from sqlalchemy import select

    from core.db.models import AuditLog
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(ctx.tenant_id):
        async with get_session() as session:
            filas = (
                await session.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.tenant_id == ctx.tenant_id,
                        AuditLog.conversation_id == ctx.conversation_id,
                        AuditLog.actor.like("tool:%"),
                        AuditLog.created_at >= desde,
                    )
                    .order_by(AuditLog.created_at)
                )
            ).scalars().all()
    return [{"nombre": f.actor.removeprefix("tool:"), "entrada": f.input or {}, "salida": f.output or {}} for f in filas]


async def runtime_efectivo(tenant_id: uuid.UUID) -> dict[str, Any]:
    """Banderas del negocio de la corrida tal como quedaron en la base (prueba del runtime)."""
    from sqlalchemy import text

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            fila = (
                await session.execute(
                    text("select aos_runtime, aos_procedures from tenants where id = :t"), {"t": tenant_id}
                )
            ).first()
    if fila is None:
        return {"aos_runtime": None, "aos_procedures": None}
    return {"aos_runtime": fila[0], "aos_procedures": bool(fila[1])}


def pidio_relevo(herramientas: list[dict[str, Any]]) -> bool:
    """¿El turno pasó a un humano con `escalate_to_human`? En el canal `test` la tool no pausa el
    bot (`escalate` no aplica ahí), así que el loop no marca el turno como escalado: sin esto la
    conversación seguía hasta el tope de turnos (pass^4 de los procedimientos, 2026-10-18)."""
    return any(
        h.get("nombre") == "escalate_to_human" and not (h.get("salida") or {}).get("error") for h in herramientas
    )


async def motivo_del_relevo(ctx: Contexto, desde: datetime) -> str:
    """Por qué escaló el turno, desde `audit_log` (solo códigos): el guardrail que agotó los
    reintentos (`core:guardrail`) y los motivos del verificador (`core:verificador`). En el
    canal `test` la escalación no deja fila en `escalations`."""
    from sqlalchemy import select

    from core.db.models import AuditLog
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(ctx.tenant_id):
        async with get_session() as session:
            filas = (
                await session.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.tenant_id == ctx.tenant_id,
                        AuditLog.conversation_id == ctx.conversation_id,
                        AuditLog.actor.in_(("core:guardrail", "core:verificador", "tool:escalate_to_human")),
                        AuditLog.created_at >= desde,
                    )
                    .order_by(AuditLog.created_at)
                )
            ).scalars().all()
    partes: list[str] = []
    for fila in filas:
        datos = fila.input or {}
        if fila.action == "guardrail_escalated":
            afirmacion = datos.get("afirmacion")
            partes.append(f"guardrail={datos.get('guardrail')}" + (f" ({afirmacion})" if afirmacion else ""))
        elif fila.actor == "tool:escalate_to_human":
            partes.append("tool=escalate_to_human")  # sin el motivo: lo escribe el modelo
        elif fila.action == "verificacion" and datos.get("resultado") == "fallo":
            partes.append(f"verificador={','.join(map(str, datos.get('motivos') or []))}")
    return "; ".join(partes)


def _revisar_turno(texto: str, herramientas: list[str], escalo: bool, espera: dict[str, Any]) -> list[str]:
    fallas: list[str] = []
    bajo = texto.lower()
    usadas = set(herramientas)
    for nombre in espera.get("herramientas", []):
        if nombre not in usadas:
            fallas.append(f"no llamó {nombre} (llamó {herramientas})")
    alguna = espera.get("alguna_herramienta", [])
    if alguna and not usadas & set(alguna):
        fallas.append(f"no llamó ninguna de {alguna} (llamó {herramientas})")
    for nombre in espera.get("sin_herramientas", []):
        if nombre in usadas:
            fallas.append(f"llamó {nombre}, que no debía")
    for frase in espera.get("texto_contiene", []):
        if frase.lower() not in bajo:
            fallas.append(f"la respuesta no dice «{frase}»")
    alguna_frase = espera.get("texto_contiene_alguno", [])
    if alguna_frase and not any(f.lower() in bajo for f in alguna_frase):
        fallas.append(f"la respuesta no dice ninguna de {alguna_frase}")
    for frase in espera.get("texto_no_contiene", []):
        if frase.lower() in bajo:
            fallas.append(f"la respuesta dice «{frase}», que no debía")
    maximo = espera.get("max_caracteres")
    if maximo and len(texto) > maximo:
        fallas.append(f"respuesta de {len(texto)} caracteres (máx {maximo})")
    if "escala" in espera and bool(espera["escala"]) != escalo:
        fallas.append("debía escalar" if espera["escala"] else "escaló sin motivo")
    return fallas


async def paso_usuario(ctx: Contexto, paso: dict[str, Any], registro: list[str]) -> None:
    """Uno o más turnos del cliente. Con `hasta_herramienta`, insiste con `insistir`
    (hasta `max_turnos`) mientras el asistente no haya llamado esa herramienta."""
    espera = paso.get("espera", {})
    objetivo = paso.get("hasta_herramienta")
    objetivos = set([objetivo] if isinstance(objetivo, str) else (objetivo or []))
    mensaje = ctx.render(paso["usuario"])
    inicio = datetime.now(UTC) - timedelta(seconds=1)
    textos: list[str] = []
    escalo = False
    for turno in range(int(paso.get("max_turnos", 1 if not objetivo else 3))):
        resultado = await _turno(ctx, mensaje)
        if resultado is None:
            raise FallaJourney("el loop no devolvió respuesta (conversación pausada o escalada)")
        textos.append(resultado.text)
        escalo = escalo or resultado.escalated
        registro.append(f"    cliente: {mensaje}\n    asistente: {resultado.text}")
        if not objetivo:
            break
        usadas = {h["nombre"] for h in await _herramientas_desde(ctx, inicio)}
        if usadas & objetivos:
            break
        mensaje = ctx.render(paso.get("insistir", "Sí, confirmo. Adelante."))
        _ = turno
    herramientas = await _herramientas_desde(ctx, inicio)
    for h in herramientas:  # disponibles para los pasos siguientes: {herramientas.crear_pedido.order_id}
        ctx.variables.setdefault("herramientas", {})[h["nombre"]] = h["salida"]
    nombres = [h["nombre"] for h in herramientas]
    ctx.herramientas.extend(nombres)
    registro.append(f"    herramientas: {nombres}")
    for h in herramientas:
        salida = json.dumps(h["salida"], ensure_ascii=False, default=str)
        registro.append(f"      {h['nombre']}({json.dumps(h['entrada'], ensure_ascii=False, default=str)[:200]}) → {salida[:400]}")
    fallas = _revisar_turno(textos[-1], nombres, escalo, espera)
    if objetivos and not objetivos & set(nombres):
        fallas.insert(0, f"tras {len(textos)} turnos no llamó {' ni '.join(sorted(objetivos))}")
    if fallas:
        raise FallaJourney("; ".join(fallas))


async def paso_evento(ctx: Contexto, paso: dict[str, Any], registro: list[str]) -> None:
    """Evento del bus con su payload (p. ej. `payment.confirmed` de la pasarela)."""
    from core.shared.events import event_bus
    from core.shared.tenant import tenant_scope

    datos = ctx.render(paso.get("datos", {}))
    registro.append(f"    evento {paso['evento']}: {datos}")
    async with tenant_scope(ctx.tenant_id):
        await event_bus.emit_strict(paso["evento"], **datos)


def paso_reloj(ctx: Contexto, paso: dict[str, Any], registro: list[str]) -> None:
    if str(paso["reloj"]) == "hábil":
        # Adelanta hasta el horario legal de cobro (HU-SUB-18, zona de Bogotá): sin esto el
        # resultado dependería de la hora del runner. 30 min de margen para no quedar en el
        # borde del cierre mientras corre el journey.
        from pack_subscriptions.horario_legal import proximo_inicio_de_cobro

        objetivo = proximo_inicio_de_cobro(ctx.ahora + timedelta(minutes=30), "America/Bogota")
        if objetivo is None:
            raise FallaJourney("reloj hábil: la ventana legal no abre en 15 días")
        ctx.desfase += objetivo - ctx.ahora
        registro.append(f"    reloj → {ctx.ahora:%Y-%m-%d %H:%M} UTC (horario legal de cobro)")
        return
    salto = _SALTO.match(str(paso["reloj"]))
    if not salto:
        raise FallaJourney(f"reloj inválido: {paso['reloj']!r} (formato +30d, +6h, +15m o hábil)")
    cantidad, unidad = int(salto.group(1)), salto.group(2)
    ctx.desfase += {"d": timedelta(days=cantidad), "h": timedelta(hours=cantidad), "m": timedelta(minutes=cantidad)}[unidad]
    registro.append(f"    reloj → {ctx.ahora:%Y-%m-%d %H:%M} UTC")


async def paso_job(ctx: Contexto, paso: dict[str, Any], registro: list[str]) -> None:
    """Corre un job de pack con el reloj del journey: `<pack>.jobs._<nombre>(now)`."""
    import importlib

    pack, _, nombre = str(paso["job"]).partition(".")
    if not (pack.startswith("pack_") and _IDENT.match(pack) and _IDENT.match(nombre)):
        raise FallaJourney(f"job inválido: {paso['job']!r} (formato pack_x.nombre)")
    modulo = importlib.import_module(f"{pack}.jobs")
    funcion = getattr(modulo, f"_{nombre}", None)
    if funcion is None:
        raise FallaJourney(f"{pack}.jobs no tiene _{nombre}(now)")
    # El job recorre los negocios de `_tenant_ids()`: acá SOLO el del journey, para no
    # tocar otros negocios con el reloj adelantado y la pasarela simulada (auditoría).
    original = getattr(modulo, "_tenant_ids", None)

    async def _solo_este_negocio() -> list[uuid.UUID]:
        return [ctx.tenant_id]

    if original is not None:
        modulo._tenant_ids = _solo_este_negocio  # type: ignore[attr-defined]
    try:
        resultado = await funcion(ctx.ahora)
    finally:
        if original is not None:
            modulo._tenant_ids = original  # type: ignore[attr-defined]
    registro.append(f"    job {paso['job']} @ {ctx.ahora:%Y-%m-%d}: {resultado}")


async def paso_outbox(ctx: Contexto, paso: dict[str, Any], registro: list[str]) -> None:
    """Entrega los eventos del outbox (ADR-0107) pendientes del negocio del journey, como el
    despachador del worker (`entregar_evento`), hasta `rondas` vueltas: un evento puede
    generar otro (`payment.confirmed` → activación → `subscription.activated` → alta). Los
    lee con RLS y `tenant_id`: nunca entrega eventos de otros negocios de la base."""
    from sqlalchemy import select

    from core.db.models import DomainEvent
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope
    from core.tasks.domain_events_dispatch import entregar_evento

    async def pendientes() -> list[DomainEvent]:
        async with tenant_scope(ctx.tenant_id):
            async with get_session() as session:
                return list(
                    (
                        await session.execute(
                            select(DomainEvent)
                            .where(DomainEvent.tenant_id == ctx.tenant_id, DomainEvent.status == "pendiente")
                            .order_by(DomainEvent.created_at)
                        )
                    )
                    .scalars()
                    .all()
                )

    entregados: list[str] = []
    for _ronda in range(max(1, int(paso.get("rondas", 5)))):
        eventos = [e for e in await pendientes() if e.attempts == 0]
        if not eventos:
            break
        for evento in eventos:
            if await entregar_evento(evento.id, ctx.tenant_id, datetime.now(UTC)):
                entregados.append(evento.topic)
    registro.append(f"    outbox: {entregados or 'sin eventos'}")
    con_error = [e for e in await pendientes() if e.attempts > 0]
    if con_error:
        detalle = "; ".join(f"{e.topic}: {(e.last_error or '')[:200]}" for e in con_error)
        raise FallaJourney(f"eventos del outbox con error: {detalle}")


def _json(valor: Any) -> Any:
    """Valor de la base serializable para el reporte (Decimal, fechas, UUID → str)."""
    if valor is None or isinstance(valor, (bool, int, float, str, list, dict)):
        return valor
    return str(valor)


async def paso_simulado(ctx: Contexto, paso: dict[str, Any], registro: list[str]) -> None:
    """Cliente simulado (HU-ARCH-28): conversa con el loop real hasta cumplir su objetivo,
    pedir relevo humano o llegar al tope de turnos. La conversación queda en la traza."""
    from simulador import CUMPLIDO, RINDE, ClienteSimulado

    from core.llm import resolve_provider
    from core.shared.tenant import tenant_scope

    datos = ctx.render(paso["simulado"])
    cliente = ClienteSimulado(
        objetivo=str(datos["objetivo"]),
        datos=dict(datos.get("datos") or {}),
        estilo=str(datos.get("estilo") or "cordial y breve"),
        semilla=ctx.semilla,
    )
    tope = int(datos.get("max_turnos", 8))
    async with tenant_scope(ctx.tenant_id):
        provider = resolve_provider()
    inicio = datetime.now(UTC) - timedelta(seconds=1)
    fin = "tope"
    for _ in range(tope):
        mensaje = await cliente.siguiente(provider)
        if mensaje == CUMPLIDO:
            fin = "objetivo"
            break
        if mensaje == RINDE:
            fin = "se_rindio"
            break
        cliente.anotar("cliente", mensaje)
        inicio_turno = datetime.now(UTC) - timedelta(seconds=1)
        resultado = await _turno(ctx, mensaje)
        if resultado is None:
            fin = "relevo"
            break
        cliente.anotar("asistente", resultado.text)
        registro.append(f"    cliente: {mensaje}\n    asistente: {resultado.text}")
        registro.extend(tomar_bloqueados())
        if resultado.escalated or pidio_relevo(await _herramientas_desde(ctx, inicio_turno)):
            fin = "relevo"
            break
    herramientas = await _herramientas_desde(ctx, inicio)
    for h in herramientas:
        ctx.variables.setdefault("herramientas", {})[h["nombre"]] = h["salida"]
    nombres = [h["nombre"] for h in herramientas]
    ctx.herramientas.extend(nombres)
    ctx.simulador = cliente.traza(fin)
    registro.append(f"    simulador: fin={fin}, turnos={ctx.simulador['turnos']}, herramientas={nombres}")
    esperados = paso.get("espera_fin")
    if esperados and fin not in esperados:
        motivo = ""
        if fin == "relevo":
            motivo = await motivo_del_relevo(ctx, inicio)
            registro.append(f"    motivo del relevo: {motivo or 'sin rastro'}")
        raise FallaJourney(
            f"la conversación terminó por «{fin}», se esperaba {esperados}" + (f" [{motivo}]" if motivo else "")
        )


def _coincide(valor: Any, esperado: Any) -> bool:
    if isinstance(esperado, dict):
        if "$no_nulo" in esperado:
            return (valor is not None) == bool(esperado["$no_nulo"])
        if "$en" in esperado:
            return any(_coincide(valor, e) for e in esperado["$en"])
        if "$contiene" in esperado:
            return str(esperado["$contiene"]).lower() in str(valor or "").lower()
    if valor is None or esperado is None:
        return valor is esperado
    try:
        return float(valor) == float(esperado)
    except (TypeError, ValueError):
        return str(valor) == str(esperado)


async def paso_verifica(ctx: Contexto, paso: dict[str, Any], registro: list[str]) -> None:
    """Consulta filas del negocio (RLS + `tenant_id` explícito) y compara campos.

    `donde` filtra por igualdad (o `campo->>clave` para JSONB); `espera` compara la
    primera fila; `filas` exige una cantidad; `guardar_como` deja la fila para `{x.campo}`.
    """
    from sqlalchemy import text

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    crudo = paso["verifica"]
    # `tabla` y `orden` salen del JSON versionado, nunca de una plantilla (una salida de
    # tool no puede elegir la tabla).
    datos = {**ctx.render({k: v for k, v in crudo.items() if k not in ("tabla", "orden")}), "tabla": crudo["tabla"]}
    if "orden" in crudo:
        datos["orden"] = crudo["orden"]
    tabla = datos["tabla"]
    if not _IDENT.match(tabla):
        raise FallaJourney(f"tabla inválida: {tabla!r}")
    condiciones = ["tenant_id = :_tenant"]
    valores: dict[str, Any] = {"_tenant": str(ctx.tenant_id)}
    for i, (campo, valor) in enumerate((datos.get("donde") or {}).items()):
        columna, _, clave = campo.partition("->>")
        if not _IDENT.match(columna) or (clave and not _IDENT.match(clave)):
            raise FallaJourney(f"campo inválido: {campo!r}")
        expresion = f"{columna}->>'{clave}'" if clave else f"{columna}::text"
        condiciones.append(f"{expresion} = :v{i}")
        valores[f"v{i}"] = str(valor)
    orden = datos.get("orden", "created_at desc")
    if not re.fullmatch(r"[a-z_]+( (asc|desc))?", orden):
        raise FallaJourney(f"orden inválido: {orden!r}")
    consulta = text(f"select * from {tabla} where {' and '.join(condiciones)} order by {orden}")
    async with tenant_scope(ctx.tenant_id):
        async with get_session() as session:
            filas = [dict(r._mapping) for r in (await session.execute(consulta, valores)).all()]
    registro.append(f"    verifica {tabla}: {len(filas)} fila(s)")
    donde = datos.get("donde") or {}
    # HU-ARCH-28: cada aserción queda con esperado y real (eval_results.state_assertions).
    if "filas" in datos:
        ok = len(filas) == int(datos["filas"])
        ctx.aserciones.append({"tabla": tabla, "donde": donde, "campo": "filas", "esperado": int(datos["filas"]), "real": len(filas), "ok": ok})
        if not ok:
            raise FallaJourney(f"{tabla}: {len(filas)} filas, se esperaban {datos['filas']}")
    esperado = datos.get("espera") or {}
    if esperado and not filas:
        ctx.aserciones.append({"tabla": tabla, "donde": donde, "campo": "*", "esperado": esperado, "real": None, "ok": False})
        raise FallaJourney(f"{tabla}: sin filas para {donde}")
    fallas = []
    for campo, valor in esperado.items():
        real = filas[0].get(campo)
        ok = _coincide(real, valor)
        ctx.aserciones.append({"tabla": tabla, "donde": donde, "campo": campo, "esperado": valor, "real": _json(real), "ok": ok})
        if not ok:
            fallas.append(f"{tabla}.{campo} = {real!r}, se esperaba {valor!r}")
    if fallas:
        raise FallaJourney("; ".join(fallas))
    if datos.get("guardar_como") and filas:
        ctx.variables[datos["guardar_como"]] = {k: (str(v) if v is not None else None) for k, v in filas[0].items()}


async def paso_accion(ctx: Contexto, paso: dict[str, Any], registro: list[str]) -> None:
    """Acciones del contrato del core, sin conversación (preparan estados largos)."""
    accion = paso["accion"]
    datos = ctx.render(paso.get("datos", {}))
    if accion == "activar_suscripcion":
        from pack_subscriptions.capability import crear_suscripcion_para
        from pack_subscriptions.seed import seed_planes

        from core.shared.tenant import tenant_scope
        from core.subscriptions import capability_del_negocio

        plan = datos["plan"]
        codigo = str(plan.get("plan_code") or "")
        async with tenant_scope(ctx.tenant_id):
            cap = await capability_del_negocio(ctx.tenant_id)
            if cap is None:
                raise FallaJourney("el negocio no tiene proveedor de suscripciones")
            # El plan sale del catálogo de suscripciones (SPEC-43); si el set no lo trae, se crea
            # con el snapshot del paso (el mismo `plan` que antes se pasaba a `activate`).
            if await cap.plan_por_codigo(codigo) is None:
                await seed_planes(ctx.tenant_id, {"planes": [{
                    "codigo": codigo, "nombre": plan.get("nombre") or codigo, "precio": plan.get("precio", 0),
                    "moneda": plan.get("moneda") or "COP", "periodicidad": plan.get("periodicidad") or "mensual",
                    "trial_dias": plan.get("trial_dias") or 0,
                }]})
            elegido = await cap.plan_por_codigo(codigo)
            if elegido is None:
                raise FallaJourney(f"no se pudo crear el plan {codigo!r}")
            sub = await crear_suscripcion_para(customer_id=ctx.customer_id, plan_id=uuid.UUID(str(elegido["plan_id"])))
            vista = await cap.get(sub.id)
        ctx.variables["suscripcion"] = dict(vista or {})
        registro.append(f"    acción activar_suscripcion: {(vista or {}).get('estado')}")
        return
    raise FallaJourney(f"acción desconocida: {accion!r}")


# ------------------------------------------------------------------ journey
@dataclass
class Resultado:
    nombre: str
    ok: bool
    detalle: str = ""
    segundos: float = 0.0
    registro: list[str] = field(default_factory=list)
    ctx: Contexto | None = None

    def intento(self):  # type: ignore[no-untyped-def]
        """El intento para pass^k (HU-ARCH-28)."""
        from consistencia import Intento

        return Intento(
            ok=self.ok,
            detalle=self.detalle,
            herramientas=list(self.ctx.herramientas) if self.ctx else [],
            aserciones=list(self.ctx.aserciones) if self.ctx else [],
            simulador=self.ctx.simulador if self.ctx else None,
            segundos=round(self.segundos, 1),
        )


async def correr_journey(ruta: Path, tenant_id: uuid.UUID, pagos: PagoSimulado, semilla: int = 0) -> Resultado:
    journey = json.loads(ruta.read_text(encoding="utf-8"))
    nombre = journey.get("nombre", ruta.stem)
    inicio = time.monotonic()
    registro: list[str] = []
    customer_id, conversation_id = await nuevo_cliente(tenant_id, journey.get("cliente", "Cliente de prueba"))
    ctx = Contexto(tenant_id, customer_id, conversation_id, pagos, semilla=int(journey.get("semilla", 0)) + semilla)
    with cassette.intento(ctx.semilla):  # la semilla del intento es parte de la clave de la grabación
        return await _correr_pasos(journey, nombre, ctx, inicio, registro)


async def _correr_pasos(journey: dict[str, Any], nombre: str, ctx: Contexto, inicio: float, registro: list[str]) -> Resultado:
    for numero, paso in enumerate(journey["pasos"], start=1):
        registro.append(f"  paso {numero}: {paso.get('nota', next(iter(paso)))}")
        try:
            if "usuario" in paso:
                await paso_usuario(ctx, paso, registro)
            elif "evento" in paso:
                await paso_evento(ctx, paso, registro)
            elif "reloj" in paso:
                paso_reloj(ctx, paso, registro)
            elif "job" in paso:
                await paso_job(ctx, paso, registro)
            elif "verifica" in paso:
                await paso_verifica(ctx, paso, registro)
            elif "accion" in paso:
                await paso_accion(ctx, paso, registro)
            elif "simulado" in paso:
                await paso_simulado(ctx, paso, registro)
            elif "outbox" in paso:
                await paso_outbox(ctx, paso, registro)
            else:
                raise FallaJourney(f"paso sin tipo conocido: {sorted(paso)}")
        except cassette.ErrorGrabacion:
            raise  # la grabación falta o cambió: corta la corrida, no es un fallo del journey
        except FallaJourney as exc:
            return Resultado(nombre, False, f"paso {numero}: {exc}", time.monotonic() - inicio, registro, ctx)
        except Exception as exc:  # un error del sistema bajo prueba también es una falla del journey
            import traceback

            registro.append("    " + "".join(traceback.format_exc(limit=-6)).replace("\n", "\n    "))
            return Resultado(nombre, False, f"paso {numero}: {type(exc).__name__}: {exc}", time.monotonic() - inicio, registro, ctx)
    return Resultado(nombre, True, "", time.monotonic() - inicio, registro, ctx)


def carpeta_del_negocio(slug: str) -> Path:
    """Set de journeys de un negocio: `TENANTS_ROOT/<slug>/evals/journeys/`. Los escenarios
    de un cliente viven con sus artefactos, nunca en el framework (regla de separación)."""
    from core.onboarding import tenants_root

    if not _SLUG.match(slug):
        raise SystemExit(f"Slug inválido: {slug!r}")
    return tenants_root() / slug / "evals" / "journeys"


def rutas_del_set(set_id: str, patrones: list[str], carpeta: Path | None = None) -> tuple[dict[str, Any], list[Path]]:
    if carpeta is None:
        if not _IDENT.match(set_id.replace("-", "_")):
            raise SystemExit(f"Set inválido: {set_id!r}")
        carpeta = JOURNEYS_DIR / set_id
    if not (carpeta / "negocio.json").exists():
        raise SystemExit(f"No hay negocio.json en {carpeta}")
    negocio = json.loads((carpeta / "negocio.json").read_text(encoding="utf-8"))
    rutas = sorted(p for p in carpeta.glob("*.json") if p.name != "negocio.json")
    if patrones != [""]:
        rutas = [p for p in rutas if any(pat in p.stem for pat in patrones)]
    return negocio, rutas


async def _preparar_proceso() -> None:
    """Igual que el runner del golden set: packs activos y tools del core registradas."""
    from core.events.core_handlers import ensure_core_handlers
    from core.mcp.host import mcp_host
    from core.plugin_manager import plugin_manager
    from core.tools.core_skill import register_core_skills

    plugin_manager.scan()
    await plugin_manager.sync_registry()
    await plugin_manager.activate_all_configured()
    await plugin_manager.ensure_process_activation()
    register_core_skills(mcp_host)
    # Handlers del core del outbox (alta automática, cobro de plataforma): el paso
    # `outbox` los necesita como el despachador del worker.
    ensure_core_handlers()
    registrar_bloqueos()


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Runner de journeys (HU-SUB-12) con pass^k (HU-ARCH-28)")
    parser.add_argument("patron", nargs="?", default="", help="Subconjunto por nombre (ej: 03,05)")
    parser.add_argument("--set", default="suscripciones", help="Carpeta de evals/journeys/")
    parser.add_argument(
        "--tenant",
        default="",
        help="Slug de un negocio: corre TENANTS_ROOT/<slug>/evals/journeys/ contra una réplica de sus artefactos",
    )
    parser.add_argument("--k", type=int, default=1, help="Intentos por journey (4 en el nightly, 8 antes de una release)")
    parser.add_argument("--umbral", type=float, default=1.0, help="pass@1 mínimo (0..1) para salir con 0")
    parser.add_argument("--reporte", default="", help="Ruta del reporte JSON (opcional)")
    parser.add_argument("--detalle", action="store_true", help="Imprimir la conversación de cada intento")
    parser.add_argument(
        "--runtime",
        choices=("legacy", "v3"),
        default=None,
        help="Sin la opción, como siempre. En un set sin motor: legacy o v3 (el negocio de la corrida corre su "
        "release 1, HU-AOS-15; exige AOS_CONNECTION_ROUTING=true). En un set con motor de procedimientos: "
        "legacy es la línea base de un agente (sin el motor) y v3 enciende el motor (ADR-0112). Sirve para "
        "comparar pass^k entre runtimes.",
    )
    sys.path.insert(0, str(EVALS_DIR))
    import grabaciones

    grabaciones.agregar_argumentos(parser)
    from core.shared.logger import configure_console_logging

    configure_console_logging()  # tracebacks sin variables locales: nunca imprimen la config
    args = parser.parse_args(argv)
    try:
        grabaciones.activar(args.grabacion)
    except cassette.ErrorGrabacion as exc:
        print(f"\n{exc}")
        return grabaciones.SALIDA_GRABACION

    patrones = [p.strip() for p in args.patron.split(",") if p.strip()] or [""]
    carpeta = carpeta_del_negocio(args.tenant) if args.tenant else None
    set_id = f"tenant:{args.tenant}" if args.tenant else args.set
    negocio, rutas = rutas_del_set(args.set, patrones, carpeta)
    if not rutas:
        print(f"No hay journeys que matcheen '{args.patron}' en {carpeta or JOURNEYS_DIR / args.set}")
        return 1
    k = max(1, args.k)
    # Antes de la réplica: `preparar_replica` cambia TENANTS_ROOT a un temporal que se borra.
    args.raiz_grabaciones = grabaciones.raiz(args.grabaciones, carpeta.parent if carpeta else None) / "journeys"
    if not carpeta:
        args.raiz_grabaciones = args.raiz_grabaciones / args.set

    _solo_bases_de_prueba()
    slug, raiz = preparar_replica(carpeta.parent.parent if carpeta else None)
    try:
        await _preparar_proceso()
        pagos = PagoSimulado(monto_minimo=float((negocio.get("pagos_simulados") or {}).get("monto_minimo", 0)))
        CORREOS.instalar()
        motor, releases = modo_de_runtime(negocio, args.runtime)
        tenant_id = await preparar_negocio(negocio, pagos, slug=slug, replica=carpeta is not None, motor=motor)
        if releases:
            await encender_runtime_v3(tenant_id)
        if args.runtime:
            set_id = f"{set_id}@{args.runtime}"
        return await _correr_set(args, set_id, rutas, k, tenant_id, pagos)
    except cassette.ErrorGrabacion as exc:
        print(f"\n{exc}")
        return grabaciones.SALIDA_GRABACION
    finally:
        shutil.rmtree(raiz, ignore_errors=True)


async def _correr_set(args: argparse.Namespace, set_id: str, rutas: list[Path], k: int, tenant_id: uuid.UUID, pagos: PagoSimulado) -> int:
    sys.path.insert(0, str(EVALS_DIR))
    from consistencia import (
        ResultadoEscenario,
        escribir_reporte,
        linea,
        reporte,
        resumir,
        texto_resumen,
    )

    efectivo = await runtime_efectivo(tenant_id)
    print(
        f"Corriendo {len(rutas)} journeys del set {set_id} con k={k} (negocio {tenant_id}, "
        f"aos_runtime={efectivo['aos_runtime']}, aos_procedures={efectivo['aos_procedures']})...\n"
    )

    import grabaciones

    resultados: list[ResultadoEscenario] = []
    extra = f"--tenant {args.tenant}" if args.tenant else ("" if args.set == "suscripciones" else f"--set {args.set}")
    if getattr(args, "runtime", None):
        extra = f"{extra} --runtime {args.runtime}".strip()
    raiz_grabaciones: Path = getattr(args, "raiz_grabaciones", grabaciones.CASSETTES_DIR / "journeys" / args.set)
    for ruta in rutas:
        escenario = ResultadoEscenario(nombre=ruta.stem)
        regrabar = grabaciones.comando_regrabar("evals/journeys.py", ruta.stem, f"{extra} --k {k}".strip())
        with cassette.escena(raiz_grabaciones / f"{ruta.stem}.json", regrabar=regrabar):
            for intento in range(k):
                resultado = await correr_journey(ruta, tenant_id, pagos, semilla=intento)
                escenario.nombre = resultado.nombre
                escenario.intentos.append(resultado.intento())
                if args.detalle or not resultado.ok:
                    print(f"  — intento {intento + 1}/{k} {'ok' if resultado.ok else 'falló'} ({resultado.segundos:.0f}s)")
                    print("\n".join(resultado.registro))
        resultados.append(escenario)
        print(linea(escenario))

    resumen = resumir(resultados, k)
    print("\n" + texto_resumen(f"Journeys {set_id}", resumen, args.umbral))
    simulador = {"k": k, "semillas": list(range(k))}
    # En replay se repite lo grabado: no mide la consistencia del modelo y no se guarda (ADR-0119).
    if cassette.modo_activo() != "replay":
        await _guardar(tenant_id, set_id, resultados, k, simulador)
    if args.reporte:
        datos = reporte(set_id, resultados, k, simulador)
        # Lo que de verdad corrió el negocio (no solo lo pedido con `--runtime`).
        datos["runtime"] = {"pedido": getattr(args, "runtime", None), **await runtime_efectivo(tenant_id)}
        escribir_reporte(args.reporte, datos)
    # El gate del 0A no cambia (ADR-0119): se mide sobre pass@1; pass^k se reporta.
    return 0 if resumen.pass_1 >= args.umbral else 1


def _solo_bases_de_prueba() -> None:
    """Los journeys crean negocios y reemplazan la pasarela en el proceso: nunca contra
    producción. Se corren en bases efímeras (CI, nightly, stack local de prueba)."""
    from core.config import get_settings

    entorno = (get_settings().app_env or "").lower()
    if entorno not in ("development", "ci", "test"):
        raise SystemExit(f"Los journeys no corren con APP_ENV={entorno!r}: solo en bases de prueba.")
    # Crean negocios (también por alta automática) y entregan eventos con parches del proceso:
    # solo contra una base local (stack efímero o servicio del CI). Una base compartida tiene
    # un worker que tomaría esos eventos sin los parches (correos y allowlist reales).
    from sqlalchemy.engine import make_url

    host = (make_url(get_settings().database_url).host or "").lower()
    if host not in ("localhost", "127.0.0.1", "::1"):
        raise SystemExit(f"Los journeys solo corren contra una base local, no contra {host!r}.")


async def _guardar(tenant_id: uuid.UUID, set_id: str, resultados: list, k: int, simulador: dict) -> None:  # type: ignore[type-arg]
    """`eval_runs` del negocio de la corrida, atado a la versión de su AgentSpec."""
    try:
        from consistencia import persistir

        from core.agents.spec import load_active_spec
        from core.shared.tenant import tenant_scope

        sys.path.insert(0, str(EVALS_DIR))
        from runner import _git_ref  # type: ignore[import-not-found]

        async with tenant_scope(tenant_id):
            spec = await load_active_spec()
        await persistir(tenant_id, f"journeys:{set_id}@{spec.version}", _git_ref(), resultados, k, simulador)
    except Exception as exc:  # no rompe la corrida: el resultado ya se imprimió
        print(f"(no se guardó la corrida: {type(exc).__name__}: {exc})")


async def _correr() -> int:
    try:
        return await main()
    finally:  # cerrar conexiones antes de que asyncio cierre el loop (sin ruido al salir)
        from core.db.session import get_engine
        from core.memory import working

        await get_engine().dispose()
        if getattr(working, "_client", None) is not None:
            await working._client.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_correr()))
