"""Lector tolerante AgentSpec v2 → `agentspec/3` + `kernelspec/1` (ADR-0113 §5, HU-AOS-12).

El AgentSpec v2 activo de un negocio se carga como el agente **General** de cada conexión
conversacional, y hasta la release 1 de la conexión (ADR-0117 §2) el KernelSpec del
Coordinador se deriva en memoria de la `persona` v2 y de los valores por defecto.

- `convertir_v2(spec, ...)`: puro. Aplica la tabla campo por campo de ADR-0113 y devuelve el
  General, el KernelSpec, los procedimientos abiertos del flujo, los secretos literales que
  hay que mover a la configuración cifrada y los avisos de lo que se normalizó.
- `cargar_como_v3()`: arma las entradas como el loop (acciones de los packs habilitados,
  declarativas, flujo compuesto) para el negocio del `tenant_scope` activo y lista sus
  conexiones conversacionales. No crea filas propias ni devuelve secretos (`load_active_spec`
  conserva su conducta de siempre: puede sembrar el spec default del negocio).
- El negocio es SIEMPRE el del `tenant_scope`, nunca el `tenantId` del JSON v2 (un negocio
  puede editar ese campo): la identidad, el slug del flujo y las conexiones salen de él.

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
    AgentSpec,
    AgentSpecV3,
    KernelSpec,
)
from pydantic import SecretStr

from core.agents.credenciales import (
    credenciales_en_body,
    credenciales_en_url,
    es_sensible,
    extraer_de_body,
    extraer_de_url,
    referencias_de_tool,
)
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
# Marca interna (no es un nombre de secreto válido) de lo que se descarta de una tool sin dominios.
_DESCARTADO = "-DESCARTADO-"
_MAX_GUARDRAILS = 100


class ConversionFallida(ValueError):
    """El v2 no cupo en v3 ni normalizado. El mensaje lleva solo rutas, nunca valores, y
    se lanza sin la excepción de pydantic encadenada ni el spec en las variables locales
    (Sentry captura los locales de cada frame)."""


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
    base = _SECRET_UNSAFE_RE.sub("_", f"{tool}_{header}".lower()).strip("_")[:52] or "secreto"
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
    from core.mcp.declarative import rejected_domains, secret_ref, validate_declarative_tools

    crudas = raw if isinstance(raw, list) else []
    normalizadas = validate_declarative_tools(crudas)
    if len(normalizadas) < len(crudas):
        avisos.append(
            f"extensions.tools: {len(crudas) - len(normalizadas)} tool(s) malformada(s) descartada(s) "
            "(hoy tampoco corren)"
        )
    if rejected_domains(crudas):
        avisos.append("extensions.tools: dominios no públicos descartados de allowed_domains (hoy tampoco se usan)")

    # Nombres ya usados por referencias existentes: un secreto extraído no los pisa.
    usados: set[str] = {n for t in normalizadas for n in referencias_de_tool(t)}
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
        if len(salida) >= _MAX_DECLARATIVAS or len(tool["url"]) > 2000 or not _DECLARATIVE_NAME_RE.fullmatch(nombre):
            avisos.append(f"extensions.tools '{nombre}': excede los topes del contrato, se descarta")
            continue
        headers: dict[str, Any] = {}
        if len(tool["headers"]) > 30:
            # Antes de extraer secretos: ninguno queda huérfano (sin tool que lo use).
            avisos.append(f"extensions.tools '{nombre}': más de 30 headers, se conservan los primeros")
        for header, valor in list(tool["headers"].items())[:30]:
            ref = secret_ref(valor)
            if ref is not None and _HEADER_NAME_RE.fullmatch(header):
                # Ya es una referencia (la escribió el editor): el valor está en la instalación.
                headers[header] = {"secret": ref}
                continue
            if (
                not _HEADER_NAME_RE.fullmatch(header)
                or not isinstance(valor, str)
                or len(valor) > 8000
                or any(c in valor for c in "\r\n\x00")
            ):
                avisos.append(f"extensions.tools '{nombre}': header inválido descartado")
                continue
            if es_sensible(header) and valor.strip():
                if not tool["allowed_domains"]:
                    # Sin allowlist la tool hoy no llama a nadie (el loop arma un ScopedHttp por
                    # tool con sus dominios): el literal no tiene a dónde viajar ni se migra.
                    avisos.append(
                        f"extensions.tools '{nombre}': el header {header} se descarta (la tool no tiene "
                        "allowed_domains y hoy tampoco puede llamar a nadie)"
                    )
                    continue
                ref = _nombre_secreto(nombre, header, usados)
                secretos[ref] = SecretStr(valor)
                headers[header] = {"secret": ref}
            else:
                headers[header] = valor
        url, body = tool["url"], tool["body"]
        # Credenciales en la URL y el body (decisión del dueño 2026-10-11): también a secretos.
        if credenciales_en_url(url) or credenciales_en_body(body):
            if not tool["allowed_domains"]:
                # Como con los headers: sin allowlist la tool no llama a nadie; se descartan.
                # El userinfo se va sin más; la query y el body quedan sin el valor.
                url, _, _ = extraer_de_url(url, lambda _n: _DESCARTADO)
                url = url.replace(f"{{secret:{_DESCARTADO}}}", "")
                body, _ = extraer_de_body(body, lambda _n: _DESCARTADO)
                body = _sin_refs(body, _DESCARTADO)
                avisos.append(
                    f"extensions.tools '{nombre}': credenciales de la URL o el body descartadas (la tool no "
                    "tiene allowed_domains y hoy tampoco puede llamar a nadie)"
                )
            else:
                url, de_query, basic = extraer_de_url(url, lambda n, t=nombre: _nombre_secreto(t, n, usados))
                body, de_body = extraer_de_body(body, lambda r, t=nombre: _nombre_secreto(t, r, usados))
                for ref, valor in (de_query | de_body).items():
                    secretos[ref] = SecretStr(valor)
                if basic is not None:
                    previo = next((h for h in headers if h.lower() == "authorization"), None)
                    if previo is not None:
                        # httpx aplica el userinfo como Basic y pisa el header: queda el del userinfo.
                        del headers[previo]
                        avisos.append(
                            f"extensions.tools '{nombre}': el userinfo de la URL reemplaza al header Authorization"
                        )
                    ref = _nombre_secreto(nombre, "authorization", usados)
                    secretos[ref] = SecretStr(basic)
                    headers["Authorization"] = {"secret": ref}
                avisos.append(f"extensions.tools '{nombre}': credenciales de la URL o el body pasan a secretos")
        if url.startswith("http://") and (
            any(isinstance(v, dict) for v in headers.values()) or referencias_de_tool({"url": url, "body": body})
        ):
            # La credencial no viaja en claro (adenda de ADR-0113): misma URL por https.
            url = "https://" + url.removeprefix("http://")
            avisos.append(f"extensions.tools '{nombre}': lleva credenciales por http://, pasa a https://")
        nombres.add(nombre)
        salida.append(
            {
                "name": nombre,
                "description": tool["description"],
                "method": tool["method"],
                "url": url,
                "allowedDomains": tool["allowed_domains"][:20],
                "headers": headers,
                "parameters": tool["parameters"],
                "body": body,
            }
        )
    return salida, secretos


def _sin_refs(valor: Any, nombre: str) -> Any:
    """Quita del body las claves cuyo valor es la referencia `nombre` (descartadas)."""
    if isinstance(valor, dict):
        return {k: _sin_refs(v, nombre) for k, v in valor.items() if v != {"secret": nombre}}
    if isinstance(valor, list):
        return [_sin_refs(v, nombre) for v in valor if v != {"secret": nombre}]
    return valor


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
            regla["tool"] not in existentes or not _ACTION_RE.fullmatch(regla["tool"])
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
        if b.tool not in existentes or not _ACTION_RE.fullmatch(b.tool):
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
    tenant_id: str | None = None,
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
    - `tenant_id`: el negocio de la fila (el del `tenant_scope`). Gana sobre el `tenantId`
      del JSON, que el negocio puede editar; si difieren, queda un aviso.
    """
    avisos: list[str] = []
    ext = dict(spec.extensions or {})
    negocio = tenant_id or spec.tenantId
    if tenant_id is not None and spec.tenantId != tenant_id:
        avisos.append("tenantId del AgentSpec v2 distinto del negocio de la fila: se usa el de la fila")

    declarativas, secretos = _declarativas(ext.pop("tools", None), catalogo, avisos)
    existentes = set(catalogo) | {t["name"] for t in declarativas}
    guardrails = _guardrails(ext.pop("guardrails", None), existentes, avisos)
    for clave in sorted(ext):
        avisos.append(f"extensions.{str(clave)[:60]}: se conserva en extensions (reservado, sin semántica)")

    ofrecidas = acciones_ofrecidas(secciones, candidatas, siempre)
    secciones = list(secciones or [])
    procedimientos = flow_to_procedures(
        secciones, catalogo=catalogo, confirmed_actions=confirmed_actions_v2(spec)
    )

    persona = spec.persona
    playbooks: list[dict[str, Any]] = []
    for ref in spec.playbooks:
        if not _PLAYBOOK_RE.fullmatch(ref.name):
            avisos.append(f"playbooks '{ref.name[:80]}': nombre inválido (hoy no se carga), se descarta")
            continue
        playbooks.append({"name": ref.name, "description": ref.description[:300]})

    rag = spec.memory.rag
    primario = spec.model.primary.strip()[:100]
    if not primario:
        from core.agents.spec import DEFAULT_SPEC

        primario = str(DEFAULT_SPEC["model"]["primary"])
        avisos.append("model.primary vacío: queda el modelo por defecto del alta")
    contenido: dict[str, Any] = {
        "description": "Agente General: el AgentSpec v2 del negocio (lector tolerante, ADR-0113 §5).",
        "persona": {
            "inheritConnectionVoice": True,
            "maxLength": _acotar(persona.maxLength, 0, 10_000, "persona.maxLength", avisos),
            "forbiddenPhrases": [f[:1000] for f in persona.forbiddenPhrases][:100],
        },
        "model": {
            "primary": primario,
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
    try:
        general = AgentSpecV3.model_validate(contenido)
    except ValueError as exc:
        rutas = sorted({".".join(str(p) for p in e["loc"]) for e in getattr(exc, "errors", lambda: [])()})
        # Fuera de los locales antes de lanzar: el spec y lo extraído llevan secretos.
        del spec, ext, declarativas, secretos, contenido, exc
        raise ConversionFallida(f"el AgentSpec v2 no cabe en agentspec/3: {', '.join(rutas)[:500]}") from None
    return ConversionV3(
        general=general,
        kernel=_kernel(spec, avisos),
        procedimientos=procedimientos,
        secretos=secretos,
        identidad=IdentidadLegacy(id=spec.id, version=spec.version, tenant_id=negocio),
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


async def convertir_negocio(spec: AgentSpec, *, modules: Iterable[Any] | None = None) -> ConversionV3:
    """Convierte `spec` (el v2 activo del negocio del `tenant_scope` activo) con las mismas
    entradas que el loop: catálogo del binario, candidatas de los packs habilitados y el flujo
    compuesto resuelto con el slug del negocio del scope (nunca el `tenantId` del JSON).

    Devuelve la conversión CON los secretos literales (`SecretStr`): la usan `cargar_como_v3`
    (que solo expone los nombres) y el backfill de HU-AOS-13 (que los mueve a la instalación
    `core` antes de crear la primera versión). `modules`: los módulos del binario para el
    catálogo; por defecto, los ya cargados (`plugin_manager.list_modules()`)."""
    from core.agents.flow import resolve_flow
    from core.kernel.procedures import catalogo_de_acciones
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import get_current_tenant_id, tenant_slug

    tid = get_current_tenant_id()
    if tid is None:
        raise ValueError("convertir_negocio corre dentro del tenant_scope del negocio")
    catalogo = catalogo_de_acciones(modules if modules is not None else plugin_manager.list_modules())
    candidatas, siempre = candidatas_del_spec(spec, catalogo)
    # El slug sale del negocio del scope, nunca del `tenantId` del JSON.
    slug = await tenant_slug(tid)
    flujo = await resolve_flow(spec, slug=slug, available=set(candidatas)) if slug else None
    try:
        return convertir_v2(
            spec,
            tenant_id=str(tid),
            catalogo=catalogo,
            secciones=flujo.sections if flujo is not None else None,
            candidatas=candidatas,
            siempre=siempre,
        )
    except ConversionFallida:
        del spec  # el v2 lleva los secretos literales: fuera de los locales del frame
        raise


async def cargar_como_v3(tenant_id: uuid.UUID | None = None, *, modules: Iterable[Any] | None = None) -> VistaV3:
    """Carga el AgentSpec v2 activo del negocio del `tenant_scope` activo como v3.

    `tenant_id`, si se pasa, debe ser el del scope (si no, `PermissionError`): el lector no
    lee un negocio desde el contexto de otro. `modules`: los módulos del binario para el
    catálogo; por defecto, los ya cargados (`plugin_manager.list_modules()`)."""
    from core.agents.spec import load_active_spec
    from core.shared.tenant import get_current_tenant_id

    tid = get_current_tenant_id()
    if tid is None:
        raise ValueError("cargar_como_v3 corre dentro del tenant_scope del negocio")
    if tenant_id is not None and tenant_id != tid:
        raise PermissionError("cargar_como_v3: el negocio pedido no es el del tenant_scope")
    conversion = await convertir_negocio(await load_active_spec(tid), modules=modules)
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


async def migrar_secretos(tenant_id: uuid.UUID, conversion: ConversionV3) -> list[str]:
    """Mueve los secretos literales que sacó el lector a la instalación `core` del negocio,
    cada uno atado a los dominios de las tools que lo usan (ADR-0113 §2). Es el paso que el
    backfill (HU-AOS-13) corre ANTES de crear la primera `agent_version`, que es inmutable.

    Copia, no mueve: el loop legacy sigue usando los literales de `agent_specs` (adenda de
    HU-AOS-13 en ADR-0113). Idempotente: con el mismo valor y los dominios ya cubiertos no
    escribe. Nunca pisa un secreto existente ni le suma dominios solo (`ValueError` sin
    valores). Devuelve avisos (sin valores); hoy siempre vacío."""

    from core.agents.secretos import (
        SecretoDeclarativo,
        comparar_secreto,
        guardar_secretos_declarativos,
    )

    if conversion.identidad.tenant_id != str(tenant_id):
        raise PermissionError("migrar_secretos: la conversión es de otro negocio")

    dominios: dict[str, set[str]] = {}
    for tool in conversion.general.declarativeTools:
        for ref in referencias_de_tool(tool):
            dominios.setdefault(ref, set()).update(tool.allowedDomains)
    avisos: list[str] = []
    a_guardar: dict[str, SecretoDeclarativo] = {}
    for nombre, valor in conversion.secretos.items():
        # El lector solo extrae secretos de tools con `allowedDomains` (las demás no llaman a nadie).
        necesarios = dominios.get(nombre, set())
        igual, actuales = await comparar_secreto(tenant_id, nombre, valor)
        if igual is False:
            # Nunca se pisa un secreto del editor con el mismo nombre y otro valor.
            raise ValueError(f"migrar_secretos: el secreto '{nombre}' ya existe con otro valor")
        if igual:
            faltan = sorted(necesarios - set(actuales))
            if faltan:
                # Nunca se amplían solos: un secreto se ata a dominios nuevos solo reescribiéndolo
                # (un rollback del v2 podría traer dominios que alguien quitó a propósito).
                raise ValueError(
                    f"migrar_secretos: el secreto '{nombre}' ya migrado no cubre {', '.join(faltan)}: "
                    "reescríbelo en el editor de tools declarativas"
                )
            continue  # ya migrado: idempotente
        a_guardar[nombre] = SecretoDeclarativo(valor=valor, dominios=tuple(sorted(necesarios)))
    if a_guardar:
        # `solo_nuevos`: si el negocio lo escribió entre la comparación y aquí, no se pisa.
        await guardar_secretos_declarativos(tenant_id, a_guardar, solo_nuevos=True)
    return avisos


__all__ = [
    "KEYS_RESERVADAS",
    "KEY_COORDINADOR",
    "KEY_GENERAL",
    "ConversionFallida",
    "ConversionV3",
    "IdentidadLegacy",
    "VistaV3",
    "acciones_ofrecidas",
    "candidatas_del_spec",
    "cargar_como_v3",
    "convertir_negocio",
    "convertir_v2",
    "migrar_secretos",
]
