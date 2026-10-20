"""Tipos del MCP Host interno: el contrato entre plugins y core.

Conceptos (ver docs/PLUGIN-CONTRACT.md):

- MCPTool: una herramienta ejecutable, descrita con JSON Schema (estándar MCP).
  El core la traduce al formato de function calling del LLM activo.

- BaseSkill: agrupación SEMÁNTICA de tools. El SkillRouter inyecta en el
  system prompt solo las skills relevantes al contexto del turno.

- BaseModule: el plugin. Declara skills, guardrails, suscripciones a eventos
  y su schema de configuración (Pydantic -> formulario auto-generado en el admin).

- GuardrailResult: veredicto de una validación post-LLM.

Todo corre EN EL MISMO PROCESO. Ejecutar una tool es una llamada de función
Python, no una petición de red (docs/MONOLITH-MODEL.md).
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel

from core.shared.logger import get_logger

if TYPE_CHECKING:
    import httpx

    from core.mcp.host import MCPHost
    from core.primeros_pasos.contrato import ContenidoCargado
    from core.qa.contrato_datos import ContextoDeNegocio, FormaDeDatos, RegistroSembrado

log = get_logger(__name__)

# handler(arguments, context) -> resultado JSON-serializable
ToolHandler = Callable[[dict[str, Any], "ToolContext"], Awaitable[dict[str, Any]]]
GuardrailFn = Callable[[str, list[dict[str, Any]]], Awaitable["GuardrailResult"]]
AfirmacionesFn = Callable[[], Awaitable[list["Afirmacion"]]]
# fuente(conversation_id, customer_id) -> referencias vigentes de la conversación (ADR-0130)
ReferenciasFn = Callable[[uuid.UUID, uuid.UUID], Awaitable[list["Referencia"]]]
# resolver de un dato de procedimiento (ADR-0113 §1, HU-AOS-05): (valor, contexto, datos) -> veredicto
ValidarDatoFn = Callable[[Any, "ToolContext", dict[str, Any]], Awaitable["ResolverResult"]]
# candidatos de un dato (`candidates.source: resolver:`): (contexto, datos) -> ids que el cliente elige
CandidatosFn = Callable[["ToolContext", dict[str, Any]], Awaitable[list[str]]]
# resumen de confirmación de una acción de un pack (HU-AOS-07, adenda HU-AOS-09 de ADR-0113):
# (args firmados, contexto) -> datos que el cliente confirma, o None si no hay un resumen fiel
ResumenFn = Callable[[dict[str, Any], "ToolContext"], Awaitable["list[DatoDeResumen] | None"]]


@dataclass
class ToolContext:
    """Contexto inyectado por el core al ejecutar una tool.

    El LLM NUNCA ve estos valores: el orchestrator los inyecta
    (mismo principio que agent-sale: conversation_id/idempotency_key son del core).

    `http` (ADR-0052) es la única salida a red que una tool puede usar: acotada a
    una allowlist de dominios. Nunca se entrega el pool de Postgres ni `environ`.
    """

    conversation_id: uuid.UUID
    customer_id: uuid.UUID
    idempotency_key: str
    channel: str
    http: ScopedHttp | None = None
    # `memory.writeNamespaces` del agente v3 que corre la tool (ADR-0114 §3, HU-AOS-36).
    # None = runtime legacy: no se chequea (sin cambio de conducta con las banderas apagadas).
    write_namespaces: frozenset[str] | None = None
    # Identidad del agente v3 en foco y su bloque `knowledge` congelado (ADR-0145 §5, enmienda de
    # ADR-0118): el core los inyecta desde la release del turno; el LLM nunca los elige. None en el
    # runtime legacy o sin release. Un pack los lee para acotar lo suyo al agente que responde.
    agent_id: uuid.UUID | None = None
    agent_version_id: uuid.UUID | None = None
    knowledge: Mapping[str, Any] | None = None


# Sufijos que nunca son un destino público (DNS interno, mDNS, metadata de cloud).
_INTERNAL_SUFFIXES = ("localhost", "internal", "local", "localdomain", "home.arpa", "lan")
_LABEL_RE = re.compile(r"^(?!-)[a-z0-9_-]{1,63}(?<!-)$")
# Última etiqueta numérica (decimal, octal o hex): glibc la resuelve como IPv4
# (`127.1`, `0x7f.1`, `0177.0.0.1`), igual que el "ends in a number" del WHATWG URL.
_NUMERIC_LABEL_RE = re.compile(r"^(0x[0-9a-f]*|[0-9]+)$")
# TLD de verdad: letras o IDN en punycode (ni `_` ni dígitos: `db.red_default`, `127.1`).
_TLD_RE = re.compile(r"^([a-z]{2,63}|xn--[a-z0-9-]{1,59})$")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
# Direcciones a probar si la conexión a la primera falla (p. ej. IPv6 sin ruta), y
# tope del connect de cada intento para no gastar todo el timeout en una sola IP.
_MAX_ADDRS = 2
_CONNECT_TIMEOUT = 5.0

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def _blocked_ip(ip: IPAddress) -> bool:
    """True si la IP no es un destino público: privada, loopback, link-local,
    reservada, multicast, sin especificar, CGNAT o una IPv4 embebida en IPv6
    (mapeada, Teredo, 6to4 hacia una IPv4 no pública; NAT64 cae en `is_reserved`)."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None or ip.teredo is not None or ip.is_site_local:
            return True
        if ip.sixtofour is not None and _blocked_ip(ip.sixtofour):
            return True
    return (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or ip in _CGNAT
    )


def _ip_literal(host: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _public_hostname(host: str) -> bool:
    """True si `host` (minúsculas, ASCII) es un nombre DNS con forma de dominio
    público: con punto, etiquetas LDH, sin TLD numérico ni sufijo interno."""
    if not host.isascii() or len(host) > 253:
        return False
    labels = host.split(".")
    if len(labels) < 2 or not all(_LABEL_RE.match(label) for label in labels):
        return False
    if _NUMERIC_LABEL_RE.match(labels[-1]) or not _TLD_RE.match(labels[-1]):
        return False
    return not any(host == s or host.endswith("." + s) for s in _INTERNAL_SUFFIXES)


def is_public_host(host: str) -> bool:
    """Chequeo sin DNS de un host de URL (ADR-0052): una IP pública canónica o un
    nombre con forma de dominio público. Rechaza hosts de una etiqueta (`redis`,
    `postgres`), formas numéricas no canónicas (`2130706433`, `127.1`, `0x7f.1`) y
    sufijos internos (`localhost`, `metadata.google.internal`). Que el nombre
    resuelva a una IP pública lo comprueba `ScopedHttp.request` al conectar."""
    host = host.lower().removesuffix(".")
    ip = _ip_literal(host)
    if ip is not None:
        return not _blocked_ip(ip)
    return _public_hostname(host)


def public_fqdn(domain: str) -> str | None:
    """`domain` normalizado si es un FQDN público (sin IPs literales), si no None.

    Lo usan las tools declarativas para validar su `allowed_domains` al guardar."""
    host = str(domain).strip().lower().removesuffix(".")
    if _ip_literal(host) is not None or not _public_hostname(host):
        return None
    return host


async def _resolve_host(host: str, port: int, timeout: float) -> list[IPAddress]:
    """IPs (sin repetir, en el orden del resolver) a las que resuelve `host`."""
    loop = asyncio.get_running_loop()
    infos = await asyncio.wait_for(
        loop.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_ADDRCONFIG),
        timeout,
    )
    ips: list[IPAddress] = []
    for *_, sockaddr in infos:
        ip = ipaddress.ip_address(str(sockaddr[0]))
        if ip not in ips:
            ips.append(ip)
    return ips


# Métodos que puede usar un pack con su allowlist del manifest (APIs REST como
# Google Calendar necesitan editar y borrar). Las tools declarativas del tenant
# siguen limitadas a GET/POST (default de `ScopedHttp`).
PLUGIN_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


class ScopedHttp:
    """Cliente HTTP acotado a una allowlist de dominios (ADR-0052).

    Por defecto solo permite `POST`/`GET` a hosts de la allowlist; `methods`
    amplía el set (packs, ver `plugin_http`). Sin allowlist, toda salida se
    rechaza (fail-closed). Nunca expone `httpx` crudo ni el entorno.

    Anti-SSRF: el host debe pasar `is_public_host`, se resuelve por DNS y se
    rechaza si ALGUNA IP no es pública; luego se conecta a la IP ya validada
    (sin segunda resolución: evita DNS rebinding) con `Host` y SNI del nombre
    original, así el certificado se verifica contra el dominio. Sin redirects
    ni proxies del entorno.
    """

    def __init__(
        self,
        allowed_domains: list[str] | None,
        timeout: float = 15.0,
        methods: tuple[str, ...] = ("GET", "POST"),
    ) -> None:
        self._allowed = {
            d.strip().lower().removesuffix(".") for d in (allowed_domains or []) if d.strip()
        }
        self._timeout = timeout
        self._methods = {m.upper() for m in methods}

    def _allowed_host(self, url: httpx.URL) -> str | None:
        """Host normalizado si la URL apunta a la allowlist y a un host público."""
        if url.scheme not in ("http", "https"):
            return None
        try:
            host = url.raw_host.decode("ascii").lower().removesuffix(".")
        except UnicodeDecodeError:
            return None
        # Nunca direcciones internas/link-local/loopback ni metadata de cloud
        # (SSRF), aunque estuvieran en la allowlist (ADR-0052).
        if not host or not is_public_host(host):
            return None
        if any(host == d or host.endswith("." + d) for d in self._allowed):
            return host
        return None

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """`data` va form-encoded (p. ej. el token endpoint de OAuth2); `timeout`
        pisa el del cliente para esta llamada."""
        import httpx

        if method.upper() not in self._methods:
            return {"error": f"Método no permitido: {method}"}
        denied = {"error": f"Dominio no autorizado: {url}"}
        try:
            target = httpx.URL(url)
        except Exception:
            return denied
        # Se valida y se conecta con el MISMO parseo (sin diferencias de parser).
        host = self._allowed_host(target)
        if host is None:
            return denied
        effective_timeout = timeout or self._timeout
        port = target.port or (443 if target.scheme == "https" else 80)
        try:
            ips = await _resolve_host(host, port, effective_timeout)
        except Exception as exc:
            # Mismo error que una IP interna: distinguirlos permitiría enumerar
            # nombres internos (Docker/Coolify) desde una tool del tenant.
            log.warning("scoped_http_dns_fallo", host=host, error=type(exc).__name__)
            return denied
        if not ips or any(_blocked_ip(ip) for ip in ips):
            log.warning("scoped_http_ip_no_publica", host=host, ips=[str(ip) for ip in ips])
            return denied

        send_headers = {k: v for k, v in (headers or {}).items() if k.lower() != "host"}
        send_headers["Host"] = target.netloc.decode("ascii")
        extensions = {"sni_hostname": host} if target.scheme == "https" else {}
        per_try = httpx.Timeout(effective_timeout, connect=min(effective_timeout, _CONNECT_TIMEOUT))
        last_exc: Exception | None = None
        try:
            async with httpx.AsyncClient(
                timeout=effective_timeout, follow_redirects=False, trust_env=False
            ) as client:
                # IPv4 primero: sin happy-eyeballs, una IPv6 sin salida no debe
                # costar la llamada.
                for ip in sorted(ips, key=lambda ip: ip.version)[:_MAX_ADDRS]:
                    try:
                        resp = await client.request(
                            method.upper(),
                            target.copy_with(host=str(ip)),
                            params=params,
                            json=json_body,
                            data=data,
                            headers=send_headers,
                            extensions=extensions,
                            timeout=per_try,
                        )
                        break
                    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                        last_exc = exc
                else:
                    raise last_exc or httpx.ConnectError("sin direcciones")
        except Exception as exc:
            return {"error": f"Fallo de red: {type(exc).__name__}"}
        try:
            body = resp.json()
        except Exception:
            body = resp.text[:2000]
        return {"status": resp.status_code, "data": body}


def plugin_http(allowed_domains: Iterable[str] | None) -> ScopedHttp:
    """`ScopedHttp` de un pack: su allowlist del manifest y los métodos REST."""
    return ScopedHttp(list(allowed_domains or ()), methods=PLUGIN_HTTP_METHODS)


@dataclass(frozen=True)
class RegistroCreado:
    """Lo que una tool crea en el negocio (HU-AOS-47, ADR-0140): el core guarda su procedencia
    (qué agente, conexión y turno lo crearon) en `agent_record_origins` sin conocer las tablas
    ni el vocabulario del pack.

    - `kind`: tipo de registro (`pedido`, `lead`, `cita`, `promesa`…), texto corto del pack.
    - `id_key`: clave del resultado con el id del registro.
    - `label_key`: clave opcional con una referencia PÚBLICA (número de pedido); nunca datos
      personales del cliente.
    - `es_nuevo`: el pack decide si el resultado es un registro recién creado (y no uno que ya
      existía o un duplicado por reintento). El core solo descarta antes lo que trae `error` u
      `ok: false`.
    """

    kind: str
    id_key: str
    es_nuevo: Callable[[dict[str, Any]], bool]
    label_key: str | None = None


@dataclass
class MCPTool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema: {"type": "object", "properties": {...}, "required": [...]}
    handler: ToolHandler
    plugin_id: str = ""  # lo completa el MCPHost al registrar
    scopes: list[str] = field(default_factory=list)  # scopes que requiere (ADR-0019)
    # Tipos de conexión activos que la tool necesita para ofrecerse/ejecutarse
    # (ADR-0072; ej. ["google_calendar"]). Ortogonal a packs/salud/scopes.
    requires_connection_types: list[str] = field(default_factory=list)
    # Gating condicional (ADR-0084): si se define, decide POR REQUEST si la tool
    # puede ofrecerse, recibiendo los tipos de conexión activos del tenant. Se usa
    # cuando el requisito depende de la config del pack (p. ej. pack-calendar en
    # modo `interno` no necesita Google; en modo `google` sí). Tiene prioridad
    # sobre `requires_connection_types`.
    connection_predicate: Callable[[set[str]], Awaitable[bool]] | None = None
    # Tools del core que se ofrecen aunque ninguna sección del flujo las nombre
    # (memoria, perfil, escalamiento, correo, suscripciones; HU-FWK-01, ADR-0109).
    # Siguen sujetas a `requires_connection_types`/`connection_predicate`.
    always_available: bool = False
    # HU-AOS-47 (ADR-0140): si la tool crea un registro del negocio, cómo reconocerlo para
    # guardar su procedencia. No entra al manifiesto de la release (`core/releases/packs.py`).
    crea_registro: RegistroCreado | None = None
    # Qué se guarda del resultado en `audit_log` (ADR-0145 §4): por defecto, el resultado
    # completo. Una tool que devuelve contenido que se puede borrar (fragmentos de documentos)
    # guarda solo ids y puntajes. El LLM sigue recibiendo el resultado completo.
    resumen_de_auditoria: Callable[[dict[str, Any]], dict[str, Any]] | None = None


@dataclass
class MCPResource:
    """Recurso de solo lectura que un plugin expone (estándar MCP)."""

    uri: str
    name: str
    description: str
    reader: Callable[[], Awaitable[str]]
    plugin_id: str = ""


@dataclass
class GuardrailResult:
    passed: bool
    reason: str = ""
    retry_instruction: str = ""  # instrucción correctiva para el reintento del LLM


@dataclass
class Guardrail:
    name: str
    fn: GuardrailFn
    priority: int = 10  # menor = se evalúa primero
    plugin_id: str = ""


@dataclass(frozen=True)
class Afirmacion:
    """Afirmación de cierre que declara un pack para el verificador del core
    (ADR-0119, HU-ARCH-29): frases que dan por hecha una acción y las tools que
    la respaldan.

    - `patron`: lo compila el pack. Las frases que configura un negocio
      (`completion_claims_custom`, ADR-0025) llegan siempre escapadas, nunca
      como regex crudo.
    - `tools`: alguna tiene que haber terminado bien en el turno actual.
    - `basta_url`: también la respalda cualquier tool del turno que haya
      devuelto una URL (un link de pago lo puede dar el pedido, la suscripción
      o pagos en línea; así el pack no lista tools de otros packs).
    - `datos`: con el motor de procedimientos activo, también la respalda que el
      motor haya fijado en este turno alguno de esos datos de la tarea en foco
      (`actualizar_tarea` aceptado: validado y, si es personal, dicho por el
      cliente). "Anoté tu dirección" es verdad aunque no corra `crear_lead`.

    El verificador descarta solo las preguntas, las negaciones y los
    condicionales ("para generar el link…"): eso es español genérico y vive en
    el core, no en cada pack.
    """

    label: str
    patron: re.Pattern[str]
    tools: frozenset[str] = frozenset()
    basta_url: bool = False
    datos: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Referencia:
    """Referencia vigente de la conversación que un pack aporta al prompt (ADR-0130):
    el id real de lo que el cliente está negociando, leído del estado actual de las
    tablas del pack (p. ej. la cotización en curso y su total).

    - `tipo` y las claves de `datos`: minúsculas y `_`.
    - Valores: UUID, número o un texto corto de letras, dígitos, `_`, `-` y `.` (un
      estado, un número público). Nada de texto libre del cliente ni del negocio (invariante 7).

    El core sanea y arma la línea del prompt (`core/orchestrator/referencias.py`); lo
    que no cumple se descarta. Cómo usarla lo dice el playbook del pack.
    """

    tipo: str
    datos: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True)
class ResolverResult:
    """Veredicto de un *resolver* de datos (ADR-0113 §1, HU-AOS-05).

    - `ok=True`: el dato es válido; `value` es el valor normalizado que se guarda (p. ej. el
      id de la variante para "el premium"). `None` = se guarda el valor tal como llegó.
    - `ok=False`: `error` es un mensaje en lenguaje claro para el modelo (el dato no se guarda).
      Lo escribe la habilidad, nunca el cliente.
    """

    ok: bool
    value: Any = None
    error: str = ""


@dataclass(frozen=True)
class DatoDeResumen:
    """Una línea del resumen que el cliente confirma (HU-AOS-07). Lo arma el pack desde SU
    estado (p. ej. la cotización vigente), nunca desde el texto del modelo. El core muestra
    `nombre` como rótulo y sanea `valor` (`core/kernel/confirmacion.py`); `es_monto` va con el
    formato de dinero y nunca se recorta.

    `documento_aceptado` (adenda 2026-10-13 de ADR-0113 y de ADR-0125): `valor` es el enlace https
    de un documento que el cliente ACEPTA al confirmar (términos, privacidad, DPA). El kernel lo
    muestra bajo "Al confirmar aceptas:", sin recortar el enlace; si no es un enlace https ASCII,
    el resumen no sale (`sin_resumen`). Úsalo solo para eso: el rótulo dice que se acepta."""

    nombre: str
    valor: Any
    es_monto: bool = False
    documento_aceptado: bool = False


@dataclass(frozen=True)
class Resolver:
    """Funciones del servidor de una habilidad que validan o proponen un dato de un
    procedimiento. Reciben solo `ToolContext` (invariante 6) y no tienen efectos."""

    name: str
    validar: ValidarDatoFn | None = None
    candidatos: CandidatosFn | None = None
    plugin_id: str = ""


class BaseSkill(ABC):
    """Agrupación semántica de tools. Ej: LeadSkill agrupa crear_lead + calificar_lead."""

    name: str = "skill"
    description: str = ""
    # Palabras clave que el SkillRouter usa para decidir relevancia por turno.
    # Lista vacía = la skill siempre está activa.
    keywords: list[str] = []

    @abstractmethod
    def get_tools(self) -> list[MCPTool]: ...


class BasePluginConfig(BaseModel):
    """Base del schema de configuración de un plugin.

    El FormGenerator del admin convierte este modelo en un formulario HTML.
    Los campos SecretStr se cifran con Fernet antes de persistir.
    """


class BaseModule(ABC):
    """Contrato del plugin. Ciclo de vida: Install -> Detect -> Configure -> Activate -> Run."""

    id: str = ""             # "mod_crm_engine" — debe coincidir con el entry_point
    name: str = ""           # "CRM Engine"
    version: str = "0.1.0"
    description: str = ""
    requires_config: bool = True  # False = se activa sin formulario (ej. plugins internos)
    config_schema: type[BasePluginConfig] | None = None

    # --- Catálogo de habilidades (HU-FWK-04, SPEC-16) ---
    # Nombre visible al cliente (glosario: "habilidad", no "plugin"/"pack"). Si
    # falta, el catálogo cae a `name`.
    display_name: str = ""
    # Categoría del catálogo (p. ej. "Ventas", "Cobranza", "Agenda").
    category: str = ""
    # Conexiones/tipos que la habilidad necesita para funcionar (ADR-0072), ej.
    # ["whatsapp_meta", "google_calendar"]. Se muestran como requisitos.
    required_connections: ClassVar[list[str]] = []
    # Allowlist de dominios a los que el pack puede salir (ADR-0019/0052). Es la
    # verdad en runtime; debe coincidir con `network` de `[tool.agent_core.plugin]`.
    # El core arma con ella el `context.http` de sus tools (`plugin_manager.scoped_http`).
    network_allowlist: ClassVar[tuple[str, ...]] = ()
    # Plan mínimo en el que está disponible (código de `core/billing/plans.py`),
    # ej. "free" | "basic" | "premium" | "empresarial".
    min_plan: str = "free"
    # Resumen de lo que hace, en una frase por acción (para la tarjeta del catálogo).
    action_summary: ClassVar[list[str]] = []
    # Guía de configuración breve (markdown corto). La guía extensa vive en
    # `TENANTS_ROOT`/playbooks (HU-UX-05).
    setup_guide: str = ""

    # Rutas del panel admin que el plugin aporta (aparecen en el menú lateral al
    # activarse). Claves opcionales (ADR-0061):
    #   - `section` (str): etiqueta de agrupación dentro del grupo del pack
    #     ("General", "Monitoreo", "Seguridad"); se renderiza preservando el orden.
    #   - `master_only` (bool): el item solo lo ve un admin `master`.
    admin_menu: list[dict[str, Any]] = []
    # [{"label": "Leads", "path": "/crm/leads", "section": "General"}]

    # Sub-secciones de permiso que el pack aporta SIN item de menú (p. ej.
    # `pack_calendar:notificaciones`): se pueden otorgar a colaboradores como
    # cualquier sección pero no renderizan una pantalla. Formato: ["<key>", ...].
    section_keys: ClassVar[list[str]] = []

    # Roles de colaborador que el pack aporta al alta de cuentas del panel
    # (ADR-0066). Se ofrecen en `/admin/colaboradores` solo si el pack está
    # activo. El pack recibe el aviso vía `on_collaborator_role` para crear su
    # perfil propio. Formato: [{"id": "gestor", "label": "Gestor de cobranza"}].
    collaborator_roles: list[dict[str, Any]] = []

    async def on_collaborator_role(self, tenant_id: uuid.UUID, admin_id: uuid.UUID, role_id: str) -> None:
        """Hook opcional (ADR-0066): el admin `admin_id` recibió el rol de pack
        `role_id` en `tenant_id`. El pack crea/reconcilia el perfil que lo marca
        como su usuario (ej. perfil `Gestor`). Default: no-op.
        """
        return None

    async def seed_data(self, tenant_id: uuid.UUID, data: dict[str, Any]) -> list[str]:
        """Hook opcional de seed (ADR-0083, issue #154): importa `data` a la BD
        del tenant de forma idempotente. El CLI/onboarding lo invoca para CADA
        pack habilitado que tenga un archivo `TENANTS_ROOT/<slug>/<plugin_id>.json`.
        Default: no-op.
        """
        return []

    async def contenido_cargado(self, tenant_id: uuid.UUID) -> ContenidoCargado | None:
        """Hook opcional (HU-SH-06, ADR-0124): cuánto contenido propio cargó el negocio en
        esta habilidad (catálogo, servicios, cartera), para el paso «catálogo o servicios» de
        los primeros pasos. El core lo llama solo si la habilidad está habilitada, bajo el
        `tenant_scope` del negocio; el pack filtra igual por `tenant_id` y no escribe nada.
        Default: None (la habilidad no aporta contenido a ese paso).
        """
        return None

    def formas_de_datos_de_prueba(self) -> list[FormaDeDatos]:
        """Hook opcional (HU-QA-10, ADR-0146): los juegos de datos de prueba que este pack sabe
        sembrar para un agente (esquema para validar lo que genera el LLM y datos de respaldo).
        Default: ninguno (el botón del agente queda deshabilitado)."""
        return []

    async def sembrar_datos_de_prueba(
        self, tenant_id: uuid.UUID, modo: str, datos: BaseModel, contexto: ContextoDeNegocio
    ) -> list[RegistroSembrado]:
        """Siembra `datos` (ya validados con el esquema de la forma `modo`) y devuelve los
        registros que creó. Lo que ya existía y se reutilizó no se devuelve: no es de la prueba."""
        raise NotImplementedError

    async def retirar_datos_de_prueba(self, tenant_id: uuid.UUID, registros: list[RegistroSembrado]) -> int:
        """Retira lo sembrado (borrado lógico o desactivación, nunca físico). Devuelve cuántos."""
        raise NotImplementedError

    def public_routes(self) -> list[Any]:
        """Routers PÚBLICOS del pack (sin sesión de admin), ej. la app de campo.

        El prefijo DEBE empezar por `/api/pack/` (ADR-0057): el core lo añade a la
        allowlist del middleware y el pack autentica cada request con su propia
        identidad. Default: ninguno.
        """
        return []

    def admin_api_routes(self) -> list[Any]:
        """Routers de API ADMIN del pack (ADR-0079), montados en `/api/v1`.

        El prefijo DEBE empezar por `/api/v1/pack/<plugin_id>`. El pack usa las
        dependencias del core (`core.api.deps.require`) para heredar la identidad
        de admin, el `X-Tenant-Id` validado y el RBAC por sección; el core solo
        monta. Default: ninguno.
        """
        return []

    def extra_admin_routers(self) -> list[Any]:
        """Routers admin adicionales del pack (ej. pestañas de Configuración).

        Además del `admin_router` principal, un pack puede aportar routers
        propios que el core incluye al arrancar (ADR-0049). El prefijo DEBE
        empezar por `/admin` para quedar bajo el gate del panel; el core ignora
        (con warning) cualquiera que no lo cumpla. Default: ninguno.
        """
        return []

    def __init__(self) -> None:
        self.config: BasePluginConfig | None = None

    @abstractmethod
    def on_activate(self, host: MCPHost) -> None:
        """Registra skills, guardrails y suscripciones. Llamado al activar el plugin."""

    def on_deactivate(self) -> None:
        """Limpieza opcional al desactivar. El MCPHost retira los registros automáticamente."""


@dataclass
class SkillInfo:
    """Metadata de una skill registrada (para el panel admin y el SDD)."""

    name: str
    description: str
    plugin_id: str
    tools: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
