"""Lector tolerante AgentSpec v2 → `agentspec/3` + `kernelspec/1` (ADR-0113 §5, HU-AOS-12).

El AgentSpec v2 activo de un negocio se carga como el agente **General** de cada conexión
conversacional, y hasta la release 1 de la conexión (ADR-0117 §2) el KernelSpec del
Coordinador se deriva en memoria de la `persona` v2 y de los valores por defecto.

- `convertir_v2(spec, ...)`: puro. Aplica la tabla campo por campo de ADR-0113 y devuelve el
  General, el KernelSpec, los procedimientos abiertos del flujo, los secretos literales que
  hay que mover a la configuración cifrada y los avisos de lo que se normalizó.
- `cargar_como_v3(tenant_id)`: arma las entradas como el loop (acciones de los packs
  habilitados, declarativas, flujo compuesto) y lista las conexiones conversacionales.
  No persiste nada y no devuelve secretos.

Garantías:
- **Sin cambio de conducta:** las acciones que hoy ofrece el loop (las del flujo, las
  `always_available` y las declarativas que nombra una sección `tenant`; sin flujo, todas las
  candidatas) quedan consentidas en `tools`, con los scopes que exigen (hoy corren sin binding,
  ADR-0019). Los bindings v2 se conservan, también los deshabilitados.
- **La salida pasa `revisar_spec`:** lo que hoy está muerto se poda con aviso (bindings a
  acciones que el binario ya no trae, declarativas sombreadas por una acción del binario,
  ADR-0052); un `required_tool` a una acción inexistente pasa a `forbidden_phrase` con las
  mismas frases y acción, que es lo que hace hoy (siempre falla cuando aparece la frase).
- **Sin secretos en la salida:** los valores literales de `Authorization`, `Cookie` y
  `X-Api-Key` salen como `SecretStr` (sin valor en el `repr`) y la tool queda con
  `{"secret": "<nombre>"}`. Los avisos nunca llevan valores.

Nadie lo llama en runtime: con `tenants.aos_runtime = 'legacy'` el loop sigue leyendo v2.
Es el lector del backfill (HU-AOS-13) y del build de releases (HU-AOS-14). Lee `spec.packs`
solo a través de `enabled_packs` (ADR-0118 §2).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agentcore_contracts import (
    SENSITIVE_HEADERS,
    AgentSpec,
    AgentSpecV3,
    KernelSpec,
)
from pydantic import SecretStr

from core.agents.flow import (
    OPEN_PROCEDURE_VERSION,
    FlowSection,
    confirmed_actions_v2,
    flow_to_procedures,
)
from core.kernel.procedures.catalogo import CORE_OWNER, Catalogo

if TYPE_CHECKING:
    from agentcore_contracts import Procedure

# Keys reservadas de los agentes que crea el lector tolerante (adenda de ADR-0113): el
# backfill de HU-AOS-13 crea `coordinador` (core) y `general` (specialist) por negocio.
KEY_GENERAL = "general"
KEY_COORDINADOR = "coordinador"
KEYS_RESERVADAS = frozenset({KEY_GENERAL, KEY_COORDINADOR})

_DECLARATIVE_NAME_RE = re.compile(r"^[a-zA-Z0-9_]{1,60}$")
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,80}$")
_ACTION_RE = re.compile(r"^[a-zA-Z0-9_]{1,80}$")
_PLAYBOOK_RE = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")
_SECRET_UNSAFE_RE = re.compile(r"[^a-z0-9_]+")
_MAX_DECLARATIVAS = 50
_MAX_GUARDRAILS = 100


@dataclass(frozen=True)
class IdentidadLegacy:
    """`id`, `version` y `tenantId` del v2: pasan a la fila (`agents`, `agent_versions`)."""

    id: str
    version: str
    tenant_id: str


@dataclass(frozen=True)
class ConversionV3:
    general: AgentSpecV3
    kernel: KernelSpec
    procedimientos: list[Procedure]
    # Nombre → valor. `SecretStr` no muestra el valor en `repr` ni en Sentry.
    secretos: dict[str, SecretStr]
    identidad: IdentidadLegacy
    avisos: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ utilidades
def _acotar(valor: Any, lo: float, hi: float, campo: str, avisos: list[str]) -> Any:
    if valor < lo or valor > hi:
        nuevo = type(valor)(min(max(valor, lo), hi))
        avisos.append(f"{campo}: {valor} fuera de rango, queda en {nuevo}")
        return nuevo
    return valor


def _nombre_secreto(tool: str, header: str, usados: set[str]) -> str:
    base = _SECRET_UNSAFE_RE.sub("_", f"{tool}_{header}".lower()).strip("_")[:56] or "secreto"
    nombre, n = base, 2
    while nombre in usados:
        nombre = f"{base}_{n}"
        n += 1
    usados.add(nombre)
    return nombre


def acciones_ofrecidas(
    secciones: list[FlowSection] | None, candidatas: Iterable[str], siempre: set[str]
) -> list[str]:
    """Las acciones que hoy ofrece el loop (`core/orchestrator/loop.py`, paso 1), en orden:
    con flujo, las de sus secciones más las `always_available`; sin flujo, todas las
    candidatas (el router por keywords elige entre ellas en cada turno).

    `candidatas` son las del loop sin los filtros transitorios (salud del plugin para el
    negocio y conexiones activas): una versión es inmutable y conectar Google después no
    puede exigir una versión nueva; esos filtros siguen aplicando en runtime."""
    candidatas = list(dict.fromkeys(candidatas))
    if not secciones:
        return candidatas
    ofrecidas = list(dict.fromkeys(t for s in secciones for t in s.tools))
    en_flujo = set(ofrecidas)
    ofrecidas += [t for t in candidatas if t in siempre and t not in en_flujo]
    return ofrecidas


# ------------------------------------------------------------------ partes
def _declarativas(
    raw: Any, catalogo: Catalogo, avisos: list[str]
) -> tuple[list[dict[str, Any]], dict[str, SecretStr]]:
    from core.mcp.declarative import rejected_domains, validate_declarative_tools

    crudas = raw if isinstance(raw, list) else []
    normalizadas = validate_declarative_tools(crudas)
    if len(normalizadas) < len(crudas):
        avisos.append(
            f"extensions.tools: {len(crudas) - len(normalizadas)} tool(s) malformada(s) descartada(s) "
            "(hoy tampoco corren)"
        )
    if rejected_domains(crudas):
        avisos.append("extensions.tools: dominios no públicos descartados de allowed_domains (hoy tampoco se usan)")

    usados: set[str] = set()
    secretos: dict[str, SecretStr] = {}
    salida: list[dict[str, Any]] = []
    nombres: set[str] = set()
    for tool in normalizadas:
        nombre = tool["name"]
        if nombre in catalogo:
            avisos.append(f"extensions.tools '{nombre}': sombreada por una acción del binario, se descarta")
            continue
        if nombre in nombres:
            avisos.append(f"extensions.tools '{nombre}': repetida, queda la primera")
            continue
        if len(salida) >= _MAX_DECLARATIVAS or len(tool["url"]) > 2000 or not _DECLARATIVE_NAME_RE.match(nombre):
            avisos.append(f"extensions.tools '{nombre}': excede los topes del contrato, se descarta")
            continue
        headers: dict[str, Any] = {}
        for header, valor in tool["headers"].items():
            if not _HEADER_NAME_RE.match(header) or len(valor) > 8000:
                avisos.append(f"extensions.tools '{nombre}': header inválido descartado")
                continue
            if header.lower() in SENSITIVE_HEADERS:
                ref = _nombre_secreto(nombre, header, usados)
                secretos[ref] = SecretStr(valor)
                headers[header] = {"secret": ref}
            else:
                headers[header] = valor
        if len(headers) > 30:
            avisos.append(f"extensions.tools '{nombre}': más de 30 headers, se conservan los primeros")
            headers = dict(list(headers.items())[:30])
        nombres.add(nombre)
        salida.append(
            {
                "name": nombre,
                "description": tool["description"],
                "method": tool["method"],
                "url": tool["url"],
                "allowedDomains": tool["allowed_domains"][:20],
                "headers": headers,
                "parameters": tool["parameters"],
                "body": tool["body"],
            }
        )
    return salida, secretos


def _guardrails(raw: Any, existentes: set[str], avisos: list[str]) -> list[dict[str, Any]]:
    from core.orchestrator.guardrails import validate_custom_rules

    crudas = raw if isinstance(raw, list) else []
    reglas = validate_custom_rules(crudas)
    if len(reglas) < len(crudas):
        avisos.append(
            f"extensions.guardrails: {len(crudas) - len(reglas)} regla(s) malformada(s) descartada(s) "
            "(hoy tampoco se evalúan)"
        )
    salida: list[dict[str, Any]] = []
    for regla in reglas:
        regla = dict(regla)
        if regla["kind"] == "required_tool" and (
            regla["tool"] not in existentes or not _ACTION_RE.match(regla["tool"])
        ):
            # La acción no existe: hoy la regla falla siempre que aparece la frase.
            avisos.append(
                f"guardrails '{regla['name']}': required_tool a '{regla['tool'][:80]}', que no existe; "
                "pasa a forbidden_phrase (misma conducta)"
            )
            regla = {k: v for k, v in regla.items() if k != "tool"} | {"kind": "forbidden_phrase"}
        if regla["kind"] == "max_length":
            regla["value"] = _acotar(regla["value"], 1, 100_000, f"guardrails '{regla['name']}'.value", avisos)
        if "phrases" in regla:
            regla["phrases"] = [p[:1000] for p in regla["phrases"]]
        salida.append(regla)
    if len(salida) > _MAX_GUARDRAILS:
        avisos.append(f"guardrails: más de {_MAX_GUARDRAILS} reglas, se conservan las primeras")
    return salida[:_MAX_GUARDRAILS]


def _bindings(
    spec: AgentSpec,
    ofrecidas: list[str],
    existentes: set[str],
    catalogo: Catalogo,
    avisos: list[str],
) -> list[dict[str, Any]]:
    """Bindings v2 (fusionados si están repetidos) más uno por cada acción ofrecida sin
    binding, con los scopes que la acción exige (hoy corre sin binding: fail-open)."""
    por_tool: dict[str, dict[str, Any]] = {}
    for b in spec.tools:
        if b.tool not in existentes or not _ACTION_RE.match(b.tool):
            avisos.append(f"tools '{b.tool[:80]}': la acción ya no existe en el binario, se descarta el binding")
            continue
        binding = {
            "tool": b.tool,
            "enabled": b.enabled,
            "scopes": [s[:100] for s in b.scopes][:20],
            "maxCallsPerConversation": b.maxCallsPerConversation,
            "requiresConfirmation": b.requiresConfirmation,
        }
        if binding["maxCallsPerConversation"] is not None and binding["maxCallsPerConversation"] <= 0:
            # v2 deniega desde la primera llamada (`used >= 0`): es un binding deshabilitado.
            avisos.append(f"tools '{b.tool}': maxCallsPerConversation {b.maxCallsPerConversation} → deshabilitada")
            binding["enabled"], binding["maxCallsPerConversation"] = False, None
        elif binding["maxCallsPerConversation"] is not None:
            binding["maxCallsPerConversation"] = _acotar(
                binding["maxCallsPerConversation"], 1, 10_000, f"tools '{b.tool}'.maxCallsPerConversation", avisos
            )
        previo = por_tool.get(b.tool)
        if previo is None:
            por_tool[b.tool] = binding
            continue
        # El loop aplica TODOS los bindings de la acción: deniega si alguno lo hace.
        avisos.append(f"tools '{b.tool}': bindings repetidos fusionados")
        previo["enabled"] = previo["enabled"] and binding["enabled"]
        previo["scopes"] = [s for s in previo["scopes"] if s in binding["scopes"]]
        topes = [t for t in (previo["maxCallsPerConversation"], binding["maxCallsPerConversation"]) if t is not None]
        previo["maxCallsPerConversation"] = min(topes) if topes else None
    for accion in ofrecidas:
        if accion in por_tool:
            continue
        if accion not in existentes:
            avisos.append(f"acción ofrecida '{accion}' sin entrada en el catálogo del binario, se omite")
            continue
        info = catalogo.get(accion)
        por_tool[accion] = {
            "tool": accion,
            "enabled": True,
            "scopes": list(info.scopes) if info is not None else [],
            "maxCallsPerConversation": None,
            "requiresConfirmation": False,
        }
    return list(por_tool.values())


def _kernel(spec: AgentSpec, avisos: list[str]) -> KernelSpec:
    """KernelSpec derivado en memoria: voz de la `persona` v2 y el presupuesto de `policies`."""
    persona = spec.persona
    rol = persona.role.strip()[:200]
    if not rol:
        avisos.append("persona.role vacío: la voz queda con el rol por defecto")
    presupuesto = spec.policies.budgetUsdPerConversation
    if presupuesto is not None:
        presupuesto = _acotar(float(presupuesto), 0.0, 1000.0, "policies.budgetUsdPerConversation", avisos)
    voz: dict[str, Any] = {"tone": persona.tone, "warmth": persona.warmth, "emojis": persona.emojis}
    if rol:
        voz["role"] = rol
    return KernelSpec.model_validate(
        {
            "voice": voz,
            "routing": {"defaultAgent": KEY_GENERAL},
            "policies": {"budgetUsdPerConversation": presupuesto},
        }
    )


# ------------------------------------------------------------------ conversión
def convertir_v2(
    spec: AgentSpec,
    *,
    catalogo: Catalogo,
    secciones: list[FlowSection] | None,
    candidatas: Iterable[str],
    siempre: set[str],
) -> ConversionV3:
    """Convierte el AgentSpec v2 según la tabla de ADR-0113 §5. Nunca lanza por datos v2:
    lo que no cabe en el contrato se normaliza con un aviso.

    - `catalogo`: acciones del binario (`catalogo_de_acciones`).
    - `secciones`: el flujo compuesto ya resuelto contra `candidatas` (`ResolvedFlow`), o
      None si el negocio no tiene flujo.
    - `candidatas`: acciones de los packs habilitados más las declarativas, como en el loop.
    - `siempre`: las acciones `always_available` (las del core).
    """
    avisos: list[str] = []
    ext = dict(spec.extensions or {})

    declarativas, secretos = _declarativas(ext.pop("tools", None), catalogo, avisos)
    existentes = set(catalogo) | {t["name"] for t in declarativas}
    guardrails = _guardrails(ext.pop("guardrails", None), existentes, avisos)

    ofrecidas = acciones_ofrecidas(secciones, candidatas, siempre)
    secciones = list(secciones or [])
    procedimientos = flow_to_procedures(
        secciones, catalogo=catalogo, confirmed_actions=confirmed_actions_v2(spec)
    )

    persona = spec.persona
    playbooks: list[dict[str, Any]] = []
    for ref in spec.playbooks:
        if not _PLAYBOOK_RE.match(ref.name):
            avisos.append(f"playbooks '{ref.name[:80]}': nombre inválido (hoy no se carga), se descarta")
            continue
        playbooks.append({"name": ref.name, "description": ref.description[:300]})

    rag = spec.memory.rag
    general = AgentSpecV3.model_validate(
        {
            "description": "Agente General: el AgentSpec v2 del negocio (lector tolerante, ADR-0113 §5).",
            "persona": {
                "inheritConnectionVoice": True,
                "maxLength": _acotar(persona.maxLength, 0, 10_000, "persona.maxLength", avisos),
                "forbiddenPhrases": [f[:1000] for f in persona.forbiddenPhrases][:100],
            },
            "model": {
                "primary": spec.model.primary[:100],
                "fallback": spec.model.fallback[:100] if spec.model.fallback else None,
                "router": spec.model.router[:100] if spec.model.router else None,
                "temperature": _acotar(float(spec.model.temperature), 0.0, 2.0, "model.temperature", avisos),
            },
            "playbooks": playbooks[:60],
            "procedures": [{"id": p.id, "version": f"={OPEN_PROCEDURE_VERSION}"} for p in procedimientos],
            "memory": {
                "shortTermTurns": _acotar(spec.memory.shortTermTurns, 1, 500, "memory.shortTermTurns", avisos),
                "rag": {
                    "topK": _acotar(rag.topK, 1, 100, "memory.rag.topK", avisos),
                    "minScore": _acotar(float(rag.minScore), 0.0, 1.0, "memory.rag.minScore", avisos),
                }
                if rag is not None
                else None,
            },
            "tools": _bindings(spec, ofrecidas, existentes, catalogo, avisos),
            "guardrails": guardrails,
            "declarativeTools": declarativas,
            "policies": {
                "maxToolCallsPerTurn": _acotar(
                    spec.policies.maxToolCallsPerTurn, 1, 100, "policies.maxToolCallsPerTurn", avisos
                )
            },
            "extensions": ext,
        }
    )
    return ConversionV3(
        general=general,
        kernel=_kernel(spec, avisos),
        procedimientos=procedimientos,
        secretos=secretos,
        identidad=IdentidadLegacy(id=spec.id, version=spec.version, tenant_id=spec.tenantId),
        avisos=avisos,
    )


# ------------------------------------------------------------------ carga
@dataclass(frozen=True)
class VistaV3:
    """El negocio visto como v3: un Coordinador y un General para cada conexión
    conversacional (es un solo agente `general` con una instancia por conexión)."""

    kernel: KernelSpec
    general: AgentSpecV3
    procedimientos: list[Procedure]
    conexiones: list[uuid.UUID]
    identidad: IdentidadLegacy
    # Solo nombres: los valores se mueven a la instalación `core` con el backfill.
    secretos_por_migrar: list[str]
    avisos: list[str]


def candidatas_del_spec(spec: AgentSpec, catalogo: Catalogo) -> tuple[list[str], set[str]]:
    """Acciones candidatas del loop para el spec (packs habilitados más declarativas) y las
    `always_available` entre ellas (las del core: `CoreSkill` y `SubscriptionSkill`)."""
    from core.agents.spec import enabled_packs
    from core.mcp.declarative import tools_for_spec

    habilitados = enabled_packs(spec)
    candidatas = [nombre for nombre, info in catalogo.items() if info.owner in habilitados]
    candidatas += [t.name for t in tools_for_spec(spec) if t.name not in catalogo]
    siempre = {nombre for nombre, info in catalogo.items() if info.owner == CORE_OWNER}
    return candidatas, siempre


async def _conexiones_conversacionales(tenant_id: uuid.UUID, avisos: list[str]) -> list[uuid.UUID]:
    from sqlalchemy import select

    from core.db.models import ChannelConnection, ConnectionTypeRecord
    from core.db.session import get_session

    async with get_session() as session:
        filas = (
            await session.execute(
                select(ChannelConnection.id, ConnectionTypeRecord.produces_conversations)
                .outerjoin(ConnectionTypeRecord, ConnectionTypeRecord.id == ChannelConnection.type_id)
                .where(ChannelConnection.tenant_id == tenant_id, ChannelConnection.deleted_at.is_(None))
                .order_by(ChannelConnection.created_at, ChannelConnection.id)
            )
        ).all()
    sin_tipo = sum(1 for _, conv in filas if conv is None)
    if sin_tipo:
        avisos.append(f"{sin_tipo} conexión(es) sin tipo de catálogo: se completan con el backfill de HU-AOS-11")
    return [cid for cid, conv in filas if conv]


async def cargar_como_v3(tenant_id: uuid.UUID | None = None, *, modules: Iterable[Any] | None = None) -> VistaV3:
    """Carga el AgentSpec v2 activo del negocio como v3 (dentro de su `tenant_scope`).

    `modules`: los módulos del binario para el catálogo; por defecto, los ya cargados
    (`plugin_manager.list_modules()`, como la API de procedimientos)."""
    from core.agents.flow import resolve_flow
    from core.agents.spec import load_active_spec
    from core.kernel.procedures import catalogo_de_acciones
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import get_current_tenant_id

    tid = tenant_id or get_current_tenant_id()
    if tid is None:
        raise ValueError("No hay negocio para cargar el AgentSpec")
    spec = await load_active_spec(tid)
    catalogo = catalogo_de_acciones(modules if modules is not None else plugin_manager.list_modules())
    candidatas, siempre = candidatas_del_spec(spec, catalogo)
    flujo = await resolve_flow(spec, available=set(candidatas))
    conversion = convertir_v2(
        spec,
        catalogo=catalogo,
        secciones=flujo.sections if flujo is not None else None,
        candidatas=candidatas,
        siempre=siempre,
    )
    avisos = list(conversion.avisos)
    conexiones = await _conexiones_conversacionales(tid, avisos)
    return VistaV3(
        kernel=conversion.kernel,
        general=conversion.general,
        procedimientos=conversion.procedimientos,
        conexiones=conexiones,
        identidad=conversion.identidad,
        secretos_por_migrar=sorted(conversion.secretos),
        avisos=avisos,
    )


__all__ = [
    "KEYS_RESERVADAS",
    "KEY_COORDINADOR",
    "KEY_GENERAL",
    "ConversionV3",
    "IdentidadLegacy",
    "VistaV3",
    "acciones_ofrecidas",
    "candidatas_del_spec",
    "cargar_como_v3",
    "convertir_v2",
]
