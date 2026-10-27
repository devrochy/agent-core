"""Editor de agentes desde plantillas (HU-AOS-24, adenda de ADR-0139 del 2026-10-18).

Lo que necesita el panel para que un dueño arme un especialista sin ayuda técnica, sobre las
piezas que ya existen (borrador y versiones de HU-AOS-12, instancias de HU-AOS-13, releases de
HU-AOS-14 y plantillas de HU-AOS-46):

- `plantillas(habilitados)`: las plantillas de `core/presets/plantillas/` con sus superpoderes
  (procedimientos publicados) y las frases de la habilidad; disponible si el negocio tiene
  habilitadas todas sus `habilidades`.
- `crear_desde_plantilla(...)`: agente `specialist` con la key de la plantilla (409 si ya existe:
  dos especialistas con los mismos superpoderes en una conexión dejarían uno muerto, ADR-0138) y
  su borrador DERIVADO como en los presets (`derivar_contenido`, desde la última versión del
  General o los defaults del contrato). No publica nada. Las frases de la habilidad NO se copian
  a `overrides.examples`: ya cuentan en el ruteo y una copia congelada envejece con el pack.
- `problemas_del_editor(...)`: lo que el editor exige antes de publicar, además del contrato:
  al menos un superpoder, cada uno disponible y con `MIN_FRASES` frases distintas (las de la
  habilidad más las propias; una propia que repite una de la habilidad no suma) y un modelo
  que el proveedor del negocio pueda servir. Un error por campo (`procedures[i]...`).
- `publicar_en_conexiones(...)`: D-35, un agente no se publica sin al menos una conexión donde
  instalarlo. Valida todo antes de escribir, luego publica la versión (idempotente por hash),
  instala el agente en cada conexión (nunca por defecto) y construye la release candidata de
  cada una (idempotente por `manifest_hash`). NO la activa: eso es el promote de la API de
  releases, solo del propietario. Si algo falla a mitad, reintentar es el camino: nada se duplica.
- `modelos(...)`: catálogo de modelos desde las tablas de precios del core (`PRICING`) con el
  costo estimado por 1.000 turnos según el promedio REAL de tokens por turno del negocio (30
  días de `usage_ledger`); sin historial, sin estimado (no se inventan tokens).

Todo corre dentro del `tenant_scope` del negocio (RLS) y además filtra por `tenant_id`. El
límite de agentes del plan Free queda pendiente: no hay métrica de agentes en
`core.billing.metrics` y aquí no se inventa.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from core.db.session import get_session
from core.kernel.procedures import Issue
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

# Frases mínimas de un superpoder (las de la habilidad más las propias del agente, distintas).
MIN_FRASES = 3
# Tope de conexiones por publicación (una release por conexión).
MAX_CONEXIONES = 20
# Ventana del promedio de tokens por turno del costo estimado.
DIAS_DE_CONSUMO = 30


class ErrorDelEditor(RuntimeError):
    """Error con `code` estable para la API (409/422 según el caso)."""

    def __init__(self, status: int, code: str, mensaje: str, campos: dict[str, str] | None = None) -> None:
        super().__init__(mensaje)
        self.status = status
        self.code = code
        self.campos = campos or {}


def _exigir_scope(tenant_id: uuid.UUID) -> None:
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("editor de agentes: fuera del tenant_scope del negocio")


# ------------------------------------------------------------------ superpoderes
@dataclass(frozen=True)
class Superpoder:
    id: str
    version: str
    pack: str
    title: str
    description: str
    phrases: tuple[str, ...]


def _ofrecibles(habilitados: Iterable[str]) -> list[Any]:
    """Procedimientos publicados ofrecibles con lo habilitado (la versión más alta de cada id)."""
    from core.kernel.procedures.de_pack import cargar
    from core.releases.procedimientos import ofrecible, parsear_version

    presentes = set(habilitados)
    por_id: dict[str, Any] = {}
    for pack_id in sorted(presentes):
        for proc in cargar(pack_id):
            if not ofrecible(proc, presentes):
                continue
            actual = por_id.get(proc.id)
            if actual is None or parsear_version(proc.version) > parsear_version(actual.version):
                por_id[proc.id] = proc
    return [p for _, p in sorted(por_id.items())]


def superpoderes(habilitados: Iterable[str]) -> list[Superpoder]:
    return [
        Superpoder(
            id=p.id,
            version=p.version,
            pack=p.owner,
            title=p.title,
            description=p.triggers.description,
            phrases=tuple(p.triggers.examples),
        )
        for p in _ofrecibles(habilitados)
    ]


# ------------------------------------------------------------------ plantillas
@dataclass(frozen=True)
class PlantillaVista:
    key: str
    name: str
    description: str
    skills: tuple[str, ...]
    optional_skills: tuple[str, ...]
    available: bool
    missing_skills: tuple[str, ...]
    superpowers: tuple[Superpoder, ...]


def keys_de_plantillas() -> list[str]:
    from core.presets import PLANTILLAS_DIR

    return sorted(p.stem for p in PLANTILLAS_DIR.glob("*.json"))


def plantillas(habilitados: Iterable[str]) -> list[PlantillaVista]:
    from core.presets import PresetError, load_plantilla

    presentes = set(habilitados)
    por_id = {s.id: s for s in superpoderes(presentes)}
    salida: list[PlantillaVista] = []
    for key in keys_de_plantillas():
        try:
            plantilla = load_plantilla(key)
        except PresetError:
            # Una plantilla rota (un deploy defectuoso) no tumba la lista: se omite y se registra.
            log.error("plantilla_invalida", template=key)
            continue
        faltan = tuple(sorted(set(plantilla.habilidades) - presentes))
        poderes = tuple(por_id[p] for p in plantilla.procedimientos if p in por_id)
        salida.append(
            PlantillaVista(
                key=plantilla.key,
                name=plantilla.nombre,
                description=plantilla.descripcion,
                skills=tuple(plantilla.habilidades),
                optional_skills=tuple(plantilla.opcionales),
                available=not faltan and bool(poderes),
                missing_skills=faltan,
                superpowers=poderes,
            )
        )
    return salida


def procedimientos_del_enfoque(key: str) -> frozenset[str] | None:
    """Ids de procedimiento que le tocan al agente con esa key, o `None` si la key no es de una
    plantilla (`general`, `coordinador`, especialistas por API o presets v1): sin restricción.
    Una plantilla rota tampoco restringe (se registra en `plantillas`)."""
    from core.presets import PresetError, load_plantilla

    try:
        return frozenset(load_plantilla(key).procedimientos)
    except PresetError:
        return None


async def superpoderes_del_agente(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> list[Superpoder]:
    """Los superpoderes del negocio que le corresponden al enfoque de ese agente. `AgenteNoEncontrado`
    si el agente no es del negocio."""
    from core.agents import versiones

    _exigir_scope(tenant_id)
    agente = await versiones.obtener_agente(tenant_id, agent_id)
    todos = superpoderes(await habilitados_del_negocio(tenant_id))
    permitidos = procedimientos_del_enfoque(agente.key)
    return todos if permitidos is None else [s for s in todos if s.id in permitidos]


async def habilitados_del_negocio(tenant_id: uuid.UUID) -> set[str]:
    """Packs de negocio habilitados (la única lectura de la habilitación, ADR-0118 adenda)."""
    from core.agents.spec import enabled_business_packs, leer_spec_activo

    leido = await leer_spec_activo(tenant_id)
    return set(enabled_business_packs(leido[1])) if leido is not None else set()


async def _base_del_general(tenant_id: uuid.UUID) -> dict[str, Any]:
    """La última versión del General (modelo, persona, memoria...) o los defaults del contrato.

    Si el General fue recortado (HU-AOS-52, `coordinacion.recortar_general`), sus `tools` y `playbooks`
    ya no traen lo del pack que pasó a un especialista; se recuperan de sus versiones anteriores (la más
    reciente que los tuvo) para que otra plantilla siga derivando con la configuración que el dueño dio."""
    from core.agents.tolerante import KEY_GENERAL
    from core.agents.versiones import contenido_inicial

    async with get_session() as session:
        filas = (
            await session.execute(
                text(
                    "SELECT v.content FROM agent_versions v JOIN agents a ON a.id = v.agent_id "
                    "AND a.tenant_id = v.tenant_id WHERE v.tenant_id = :t AND a.key = :k "
                    "AND a.deleted_at IS NULL ORDER BY v.version DESC LIMIT 20"
                ),
                {"t": tenant_id, "k": KEY_GENERAL},
            )
        ).all()
    versiones_v3 = [dict(f[0]) for f in filas if isinstance(f[0], dict) and f[0].get("contract") == "agentspec/3"]
    if not versiones_v3:
        return contenido_inicial("specialist")
    base = versiones_v3[0]
    for campo, clave in (("tools", "tool"), ("playbooks", "name")):
        vistos = {x.get(clave) for x in base.get(campo) or [] if isinstance(x, dict)}
        recuperados = [
            x
            for antes in versiones_v3[1:]
            for x in antes.get(campo) or []
            if isinstance(x, dict) and x.get(clave) not in vistos and not vistos.add(x.get(clave))
        ]
        if recuperados:
            base[campo] = [*(base.get(campo) or []), *recuperados]
    return base


async def _agente_por_key(tenant_id: uuid.UUID, key: str) -> uuid.UUID | None:
    async with get_session() as session:
        return (
            await session.execute(
                text("SELECT id FROM agents WHERE tenant_id = :t AND key = :k AND deleted_at IS NULL LIMIT 1"),
                {"t": tenant_id, "k": key},
            )
        ).scalar_one_or_none()


async def crear_desde_plantilla(
    tenant_id: uuid.UUID,
    key_plantilla: str,
    *,
    nombre: str | None,
    catalogo: Mapping[str, Any],
    actor: uuid.UUID | None,
    respuestas: Mapping[str, Any] | None = None,
) -> tuple[Any, Any, list[str]]:
    """Crea el especialista y su borrador derivado de la plantilla. Devuelve (agente, borrador,
    avisos). `ErrorDelEditor` 404 si la plantilla no existe, 409 si falta alguna habilidad
    (`template_unavailable`) o si ya hay un agente con esa key (`key_taken`, con su id).

    Con `respuestas` (el guion del asistente, HU-AOS-48) el agente conserva solo los superpoderes que
    esas respuestas dejan y toma su largo máximo y su nombre; una respuesta fuera del guion es 422
    (`invalid_answers`, un campo por pregunta) y no se crea nada."""
    from core.agents import versiones
    from core.presets import PresetError, load_plantilla
    from core.presets.agentes import _playbooks_de_packs, derivar_contenido
    from core.presets.guion import RespuestasInvalidas, resolver

    _exigir_scope(tenant_id)
    if key_plantilla not in keys_de_plantillas():
        raise ErrorDelEditor(404, "not_found", "Plantilla no encontrada.")
    try:
        plantilla = load_plantilla(key_plantilla)
    except PresetError:
        log.error("plantilla_invalida", template=key_plantilla)
        raise ErrorDelEditor(404, "not_found", "Plantilla no encontrada.") from None
    ajustes = None
    if respuestas is not None:
        try:
            ajustes = resolver(plantilla, respuestas)
        except RespuestasInvalidas as exc:
            raise ErrorDelEditor(422, "invalid_answers", "Revisa tus respuestas.", exc.por_pregunta) from None
        plantilla = plantilla.model_copy(update={"procedimientos": ajustes.procedimientos})
        nombre = nombre or ajustes.nombre
    habilitados = await habilitados_del_negocio(tenant_id)
    faltan = sorted(set(plantilla.habilidades) - habilitados)
    if faltan:
        raise ErrorDelEditor(
            409,
            "template_unavailable",
            "Esta plantilla necesita habilidades que tu negocio no tiene activas.",
            {"template": ", ".join(faltan)},
        )
    existente = await _agente_por_key(tenant_id, plantilla.key)
    if existente is not None:
        raise ErrorDelEditor(
            409,
            "key_taken",
            f"Ya tienes un agente «{plantilla.nombre}»: edítalo en vez de crear otro.",
            {"agent_id": str(existente)},
        )
    publicados = _ofrecibles(habilitados)
    try:
        contenido, avisos = derivar_contenido(
            plantilla,
            await _base_del_general(tenant_id),
            catalogo=catalogo,
            habilitados=habilitados,
            publicados=publicados,
            playbooks_de_packs=_playbooks_de_packs(),
            proveedor_suscripciones=await _proveedor_de_suscripciones(tenant_id),
        )
    except PresetError:
        raise ErrorDelEditor(
            409, "template_unavailable", "Tu negocio no ofrece ninguno de los superpoderes de esta plantilla."
        ) from None
    if ajustes is not None and ajustes.largo_maximo is not None:
        contenido["persona"] = {**contenido.get("persona", {}), "maxLength": ajustes.largo_maximo}
    try:
        agente = await versiones.crear_agente(
            tenant_id,
            key=plantilla.key,
            kind="specialist",
            name=(nombre or plantilla.nombre).strip()[:120] or plantilla.nombre,
            description=plantilla.descripcion,
            actor=actor,
        )
    except versiones.Conflicto:
        raise ErrorDelEditor(409, "key_taken", f"Ya tienes un agente «{plantilla.nombre}».") from None
    borrador = await versiones.guardar_borrador(tenant_id, agente.id, contenido, 0, catalogo=catalogo, actor=actor)  # type: ignore[arg-type]
    return agente, borrador, avisos


# ------------------------------------------------------------------ validación del editor
async def _proveedor_de_suscripciones(tenant_id: uuid.UUID) -> str | None:
    """Pack que cobra las suscripciones del negocio (`tenants.settings["subscriptions"]["provider"]`)."""
    from core.subscriptions import proveedor_configurado

    return await proveedor_configurado(tenant_id)


def _normalizada(frase: str) -> str:
    from core.kernel.procedures.textos import normalizar

    return normalizar(frase)


def problemas_de_superpoderes(
    contenido: Mapping[str, Any],
    disponibles: Mapping[str, Superpoder],
    permitidos: Collection[str] | None = None,
) -> list[Issue]:
    """Al menos un superpoder; cada uno disponible, del enfoque del agente (`permitidos`; `None` =
    sin restricción) y con `MIN_FRASES` frases distintas."""
    refs = contenido.get("procedures")
    if not isinstance(refs, list) or not refs:
        return [Issue("sin_superpoderes", "procedures", "Marca al menos un superpoder para este agente.")]
    issues: list[Issue] = []
    for i, ref in enumerate(refs):
        if not isinstance(ref, Mapping):
            continue
        poder = disponibles.get(str(ref.get("id")))
        if poder is None:
            issues.append(
                Issue(
                    "superpoder_no_disponible",
                    f"procedures[{i}].id",
                    "Este superpoder no está disponible con las habilidades activas de tu negocio.",
                )
            )
            continue
        if permitidos is not None and poder.id not in permitidos:
            issues.append(
                Issue(
                    "superpoder_fuera_de_enfoque",
                    f"procedures[{i}].id",
                    f"«{poder.title}» no es parte del enfoque de este agente. Quítalo para poder publicar.",
                )
            )
            continue
        sobre = ref.get("overrides")
        propias = sobre.get("examples") if isinstance(sobre, Mapping) else None
        distintas = {_normalizada(f) for f in poder.phrases} | {
            _normalizada(str(f)) for f in (propias if isinstance(propias, list) else [])
        }
        distintas.discard("")
        if len(distintas) < MIN_FRASES:
            faltan = MIN_FRASES - len(distintas)
            issues.append(
                Issue(
                    "frases_minimas",
                    f"procedures[{i}].overrides.examples",
                    f"«{poder.title}» necesita al menos {MIN_FRASES} frases distintas de clientes: "
                    f"agrega {faltan} más.",
                )
            )
    return issues


# ------------------------------------------------------------------ modelos y costo
@dataclass(frozen=True)
class Modelo:
    provider: str
    model: str
    input_usd_per_mtok: float
    output_usd_per_mtok: float
    selectable: bool
    retired: bool
    estimated_usd_per_1000_turns: float | None


@dataclass(frozen=True)
class ContextoDeModelos:
    provider: str
    byok_model: str | None
    avg_input_tokens: float | None
    avg_output_tokens: float | None
    sample_turns: int
    models: list[Modelo] = field(default_factory=list)


def _familia(model: str) -> str:
    """Proveedor de un modelo de la tabla OpenAI-compatible por su prefijo."""
    return "deepseek" if model.startswith("deepseek") else "openai"


def tabla_de_precios() -> list[tuple[str, str, float, float, bool]]:
    """(proveedor, modelo, USD/MTok entrada, USD/MTok salida, retirado) de las tablas del core."""
    from core.llm.anthropic_provider import PRICING as ANTHROPIC
    from core.llm.openai_compatible import PRICING as COMPATIBLES
    from core.llm.openai_compatible import RETIRED_DEEPSEEK_MODELS

    filas = [(_familia(m), m, p[0], p[1], m in RETIRED_DEEPSEEK_MODELS) for m, p in COMPATIBLES.items()]
    filas += [("anthropic", m, p[0], p[1], False) for m, p in ANTHROPIC.items()]
    return sorted(filas, key=lambda f: (f[0], f[2] + f[3], f[1]))


def sirve(provider: str, familia: str) -> bool:
    """¿El proveedor del negocio puede servir un modelo de esa familia? `openai_compatible`
    habla con cualquiera de la tabla compatible."""
    if provider == "openai_compatible":
        return familia in ("deepseek", "openai")
    return provider == familia


def problema_de_modelo(contenido: Mapping[str, Any], provider: str) -> Issue | None:
    """Un modelo de la tabla que el proveedor del negocio no sirve. Uno fuera de la tabla pasa
    (lo conoce quien lo configuró)."""
    modelo = contenido.get("model")
    primario = modelo.get("primary") if isinstance(modelo, Mapping) else None
    for familia, nombre, _i, _o, retirado in tabla_de_precios():
        if nombre != primario:
            continue
        if retirado:
            return Issue("modelo_retirado", "model.primary", "Este modelo ya no existe: elige otro.")
        if not sirve(provider, familia):
            return Issue(
                "modelo_de_otro_proveedor",
                "model.primary",
                "Tu negocio usa otro proveedor de IA: elige un modelo de ese proveedor.",
            )
    return None


def estimado(entrada: float, salida: float, avg_in: float | None, avg_out: float | None) -> float | None:
    if avg_in is None or avg_out is None:
        return None
    return round((avg_in * entrada + avg_out * salida) / 1_000_000 * 1000, 4)


async def contexto_de_modelos(tenant_id: uuid.UUID) -> ContextoDeModelos:
    from core.config import get_settings

    _exigir_scope(tenant_id)
    async with get_session() as session:
        ajustes = (
            await session.execute(
                text(
                    "SELECT llm_provider, llm_model FROM app_settings WHERE tenant_id = :t AND deleted_at IS NULL LIMIT 1"
                ),
                {"t": tenant_id},
            )
        ).first()
        # Una sola consulta agregada sobre `ix_usage_ledger_tenant_created`.
        consumo = (
            await session.execute(
                text(
                    "SELECT count(*), avg(input_tokens), avg(output_tokens) FROM usage_ledger "
                    "WHERE tenant_id = :t AND created_at > now() - make_interval(days => :d)"
                ),
                {"t": tenant_id, "d": DIAS_DE_CONSUMO},
            )
        ).one()
    provider = (ajustes[0] if ajustes is not None and ajustes[0] else None) or get_settings().llm_provider
    byok = ajustes[1] if ajustes is not None and ajustes[1] else None
    turnos = int(consumo[0] or 0)
    avg_in = float(consumo[1]) if turnos and consumo[1] is not None else None
    avg_out = float(consumo[2]) if turnos and consumo[2] is not None else None
    modelos = [
        Modelo(
            provider=familia,
            model=nombre,
            input_usd_per_mtok=entrada,
            output_usd_per_mtok=salida,
            selectable=sirve(provider, familia) and not retirado,
            retired=retirado,
            estimated_usd_per_1000_turns=estimado(entrada, salida, avg_in, avg_out),
        )
        for familia, nombre, entrada, salida, retirado in tabla_de_precios()
    ]
    return ContextoDeModelos(provider, byok, avg_in, avg_out, turnos, modelos)


# ------------------------------------------------------------------ conexiones destino
@dataclass(frozen=True)
class Destino:
    connection_id: uuid.UUID
    label: str
    channel: str
    installed: bool
    covers_skills: bool
    missing_skills: tuple[str, ...]
    active_release_id: uuid.UUID | None


async def _instaladas_por_conexion(tenant_id: uuid.UUID, conexiones: list[uuid.UUID]) -> dict[uuid.UUID, set[str]]:
    """Las instalaciones propias ACTIVAS y vivas de cada conexión (sin `core`), en una sola
    consulta: la misma regla que `instalaciones.instaladas_en_conexion` (ADR-0118 §2)."""
    from sqlalchemy import select

    from core.agents.instalaciones import ACTIVA, CORE_INSTALLATION_PACK, SCOPE_CONEXION
    from core.db.models import PackInstallationRecord

    if not conexiones:
        return {}
    async with get_session() as session:
        filas = (
            await session.execute(
                select(PackInstallationRecord.connection_id, PackInstallationRecord.pack_id).where(
                    PackInstallationRecord.tenant_id == tenant_id,
                    PackInstallationRecord.scope == SCOPE_CONEXION,
                    PackInstallationRecord.connection_id.in_(conexiones),
                    PackInstallationRecord.status == ACTIVA,
                    PackInstallationRecord.pack_id != CORE_INSTALLATION_PACK,
                    PackInstallationRecord.deleted_at.is_(None),
                )
            )
        ).all()
    salida: dict[uuid.UUID, set[str]] = {}
    for cid, pack in filas:
        if cid is not None:
            salida.setdefault(cid, set()).add(str(pack))
    return salida


def habilidades_del_contenido(contenido: Mapping[str, Any]) -> set[str]:
    """Las habilidades (dueños) de los superpoderes que declara el agente."""
    refs = contenido.get("procedures")
    return {
        str(r["id"]).split(".", 1)[0]
        for r in (refs if isinstance(refs, list) else [])
        if isinstance(r, Mapping) and isinstance(r.get("id"), str) and "." in r["id"]
    }


async def destinos(
    tenant_id: uuid.UUID, agent_id: uuid.UUID, necesarias: set[str], habilitados: set[str]
) -> list[Destino]:
    """Conexiones conversacionales vivas con Coordinador (donde se puede instalar un
    especialista), si el agente ya está en cada una y si sus habilidades efectivas (ADR-0118 §2)
    cubren las que necesita."""
    _exigir_scope(tenant_id)
    async with get_session() as session:
        filas = (
            await session.execute(
                text(
                    "SELECT cc.id, cc.label, cc.channel, cc.active_release_id, EXISTS ("
                    "  SELECT 1 FROM agent_instances ai WHERE ai.tenant_id = cc.tenant_id "
                    "  AND ai.connection_id = cc.id AND ai.agent_id = :a AND ai.deleted_at IS NULL) "
                    "FROM channel_connections cc "
                    "JOIN connection_types ct ON ct.id = cc.type_id AND ct.produces_conversations "
                    "WHERE cc.tenant_id = :t AND cc.deleted_at IS NULL AND EXISTS ("
                    "  SELECT 1 FROM agent_instances ai WHERE ai.tenant_id = cc.tenant_id "
                    "  AND ai.connection_id = cc.id AND ai.role = 'core' AND ai.deleted_at IS NULL) "
                    "ORDER BY cc.created_at, cc.id LIMIT 200"
                ),
                {"t": tenant_id, "a": agent_id},
            )
        ).all()
    propias = await _instaladas_por_conexion(tenant_id, [f[0] for f in filas])
    salida: list[Destino] = []
    for cid, label, channel, activa, instalado in filas:
        efectivas = set(propias[cid]) if propias.get(cid) else set(habilitados)
        faltan = tuple(sorted(necesarias - efectivas))
        salida.append(
            Destino(
                connection_id=cid,
                label=str(label or ""),
                channel=str(channel or ""),
                installed=bool(instalado),
                covers_skills=not faltan,
                missing_skills=faltan,
                active_release_id=activa,
            )
        )
    return salida


# ------------------------------------------------------------------ publicar (D-35)
@dataclass
class ResultadoConexion:
    connection_id: uuid.UUID
    instance: str  # created | existing | <code de ConflictoInstancia>
    release_id: uuid.UUID | None = None
    release_number: int | None = None
    release_created: bool | None = None
    error: str | None = None


@dataclass
class ResultadoPublicacion:
    version: Any
    created: bool
    connections: list[ResultadoConexion]
    warnings: list[str]
    # Versión nueva del General si esta publicación lo recortó (HU-AOS-52); `None` si no cambió.
    general_actualizado: int | None = None


async def _acoplar_al_general(
    tenant_id: uuid.UUID, agent_id: uuid.UUID, catalogo: Mapping[str, Any], admin_id: uuid.UUID | None
) -> Any:
    """Enlace A2A y recorte del General (`coordinacion`). Un fallo se registra y no tumba la publicación
    (el especialista ya está publicado); se reintenta al volver a publicar."""
    from core.agents import coordinacion

    try:
        return await coordinacion.acoplar_especialista(tenant_id, agent_id, catalogo=catalogo, admin_id=admin_id)
    except Exception:
        log.exception("acople_general_fallo", tenant_id=str(tenant_id), agent_id=str(agent_id))
        return None


def _validar_conexiones(pedidas: Sequence[uuid.UUID], disponibles: Mapping[uuid.UUID, Destino]) -> dict[str, str]:
    campos: dict[str, str] = {}
    if not pedidas:
        campos["connection_ids"] = "Elige al menos una conexión donde instalar el agente."
    if len(set(pedidas)) != len(pedidas):
        campos["connection_ids"] = "Hay conexiones repetidas."
    for i, cid in enumerate(pedidas):
        destino = disponibles.get(cid)
        if destino is None:
            campos[f"connection_ids[{i}]"] = "Esta conexión no está disponible para agentes."
        elif not destino.covers_skills:
            campos[f"connection_ids[{i}]"] = (
                "A esta conexión le faltan habilidades: " + ", ".join(destino.missing_skills) + "."
            )
    return campos


async def publicar_en_conexiones(
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    connection_ids: Sequence[uuid.UUID],
    *,
    catalogo: Mapping[str, Any],
    admin_id: uuid.UUID | None,
    actor: str,
    lock_version: int | None,
    notes: str,
    label: str | None = None,
) -> ResultadoPublicacion:
    """D-35: valida todo, publica la versión, instala el agente en cada conexión y construye su
    release candidata sin activarla. `ErrorDelEditor` (409/422) o `versiones.ErroresDeContrato`
    (422 por campo) antes de escribir nada."""
    from core.agents import instancias, versiones
    from core.billing.enforcement import QuotaExceeded
    from core.releases import build

    _exigir_scope(tenant_id)
    agente = await versiones.obtener_agente(tenant_id, agent_id)
    if agente.kind != "specialist":
        raise ErrorDelEditor(409, "not_specialist", "El editor publica solo agentes especialistas.")
    if agente.status != "active":
        raise ErrorDelEditor(409, "agent_archived", "El agente está archivado.")
    if len(connection_ids) > MAX_CONEXIONES:
        raise ErrorDelEditor(422, "too_many_connections", f"Máximo {MAX_CONEXIONES} conexiones por publicación.")
    borrador = await versiones.obtener_borrador(tenant_id, agent_id)
    if lock_version is not None and borrador.lock_version != lock_version:
        raise ErrorDelEditor(
            409, "stale_lock_version", "El borrador cambió desde que lo abriste: recárgalo antes de publicar."
        )
    contenido: dict[str, Any] = dict(borrador.content)
    habilitados = await habilitados_del_negocio(tenant_id)
    issues = problemas_de_superpoderes(
        contenido, {s.id: s for s in superpoderes(habilitados)}, procedimientos_del_enfoque(agente.key)
    )
    contexto = await contexto_de_modelos(tenant_id)
    modelo = problema_de_modelo(contenido, contexto.provider)
    if modelo is not None:
        issues.append(modelo)
    disponibles = {
        d.connection_id: d
        for d in await destinos(tenant_id, agent_id, habilidades_del_contenido(contenido), habilitados)
    }
    campos = _validar_conexiones(connection_ids, disponibles)
    if campos and not issues:
        raise ErrorDelEditor(422, "invalid_connections", "Revisa las conexiones elegidas.", campos)
    if issues:
        issues += [Issue("conexion", k, v) for k, v in campos.items()]
        raise versiones.ErroresDeContrato(issues)
    nuevas = [cid for cid in connection_ids if not disponibles[cid].installed]
    if nuevas and not await instancias.agente_cuenta_en_el_plan(tenant_id, agent_id):
        # Tope de agentes publicados (D-35, ADR-0142): se avisa ANTES de publicar la versión,
        # para no dejar una versión nueva sin conexión. `agregar_instancia` lo vuelve a medir
        # con el lock (una carrera da `plan_limit_agentes` en esa conexión).
        from core.billing.enforcement import quota_decision

        decision = await quota_decision(tenant_id, "agentes")
        if not decision.allowed and decision.limit is not None:
            raise QuotaExceeded("agentes", decision.used, decision.limit)

    version, creada = await versiones.publicar(
        tenant_id,
        agent_id,
        catalogo=catalogo,  # type: ignore[arg-type]
        actor=admin_id,
        notes=notes,
        label=label,
        lock_version=borrador.lock_version,
    )
    avisos: list[str] = []
    if contexto.byok_model:
        avisos.append(
            f"Tu negocio fija el modelo {contexto.byok_model} en Configuración: ese manda sobre el del agente."
        )
    acople = await _acoplar_al_general(tenant_id, agent_id, catalogo, admin_id)
    resultados: list[ResultadoConexion] = []
    for cid in connection_ids:
        resultado = ResultadoConexion(cid, "existing" if disponibles[cid].installed else "created")
        resultados.append(resultado)
        if not disponibles[cid].installed:
            try:
                await instancias.agregar_instancia(tenant_id, cid, agent_id, is_default=False, enabled=True, actor=admin_id)
            except instancias.ConflictoInstancia as exc:
                if exc.code != "already_assigned":
                    resultado.instance = exc.code
                    resultado.error = exc.code
                    continue
                resultado.instance = "existing"
            except QuotaExceeded:
                # El plan llegó al tope entre el pre-chequeo y la escritura (otra publicación).
                resultado.instance = resultado.error = "plan_limit_agentes"
                continue
            except (instancias.ConexionNoEncontrada, instancias.ConexionNoConversacional, instancias.AgenteNoInstanciable):
                # La conexión (o el agente) cambió entre la validación y la escritura: se sigue
                # con las demás y el reintento lo resuelve.
                resultado.instance = resultado.error = "connection_unavailable"
                continue
        try:
            hecha = await build.construir(
                tenant_id, cid, actor=actor, created_by=admin_id, notas=f"Agente {agente.key} v{version.version}"
            )
        except build.ErrorDelBuild as exc:
            resultado.error = exc.codigo
            continue
        except build.ConexionNoEncontrada:
            resultado.error = "connection_unavailable"
            continue
        resultado.release_id = hecha.release_id
        resultado.release_number = hecha.number
        resultado.release_created = hecha.creada
    return ResultadoPublicacion(
        version=version,
        created=creada,
        connections=resultados,
        warnings=avisos,
        general_actualizado=acople.general_actualizado if acople else None,
    )


__all__ = [
    "MAX_CONEXIONES",
    "MIN_FRASES",
    "ContextoDeModelos",
    "Destino",
    "ErrorDelEditor",
    "Modelo",
    "PlantillaVista",
    "ResultadoConexion",
    "ResultadoPublicacion",
    "Superpoder",
    "contexto_de_modelos",
    "crear_desde_plantilla",
    "destinos",
    "estimado",
    "habilidades_del_contenido",
    "habilitados_del_negocio",
    "keys_de_plantillas",
    "plantillas",
    "problema_de_modelo",
    "problemas_de_superpoderes",
    "procedimientos_del_enfoque",
    "publicar_en_conexiones",
    "sirve",
    "superpoderes",
    "superpoderes_del_agente",
    "tabla_de_precios",
]
