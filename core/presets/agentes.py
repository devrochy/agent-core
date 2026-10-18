"""Presets v2: agentes especialistas desde plantillas y reglas del Coordinador (ADR-0139, HU-AOS-46).

`aplicar_agentes(tenant_id, manifest, ...)` instala en un negocio del Agent OS los agentes «para un
fin» de un preset v2 (`PresetManifest.agentes`, plantillas en `core/presets/plantillas/`):

1. **Solo con el Agent OS encendido:** `AOS_CONNECTION_ROUTING` (global) y
   `tenants.aos_runtime = 'v3'`. Con cualquiera apagada no lee ni escribe nada más (no-op con
   motivo): la conducta de un negocio legacy no cambia. Un manifiesto sin `agentes` (v1) tampoco
   hace nada: el negocio sigue con el General.
2. **Precondiciones (D-35: un agente publicado tiene al menos una conexión):** el Coordinador y el
   General del backfill de HU-AOS-13 con versión publicada y al menos una conexión conversacional
   con Coordinador vivo. Sin eso no crea ningún agente.
3. **Agente por plantilla** (si sus `habilidades` están habilitadas en el negocio, única lectura
   de la habilitación: `enabled_business_packs` del v2 activo, ADR-0118 adenda HU-AOS-16):
   - si NO existe un agente con esa `key`, lo crea (`specialist`) y publica su versión 1 con un
     contenido `agentspec/3` DERIVADO de la última versión del General (`derivar_contenido`);
   - si existe y lo creó el preset (registro en `tenants.settings["preset"]["agentes"]`), no
     toca su contenido nunca (no pisa lo que el dueño cambió); si lo borró o archivó el dueño,
     o la key es de un agente que no creó el preset (`key_ocupada`), no lo toca ni lo instancia.
4. **Instancia en cada conexión conversacional con Coordinador** cuyas habilidades efectivas
   (ADR-0118 §2) cubren las de la plantilla, nunca por defecto (el General
   sigue atendiendo hasta que el enrutador de HU-AOS-17 lea las reglas). Si la conexión ya tuvo
   una instancia de ese agente, viva o quitada por el dueño, no se agrega otra: lo que el dueño
   quitó no vuelve. Sin ninguna conexión con sus habilidades no se crea el agente; si uno recién
   creado no entra en ninguna (conflicto), se archiva y la próxima corrida lo reintenta (D-35).
5. **Reglas del Coordinador:** el Coordinador es uno por negocio y entra en la release de todas
   las conexiones, así que solo se agregan las reglas de los agentes presentes en TODAS. Se
   agregan a `routing.rules` las que no estén y que el preset nunca agregó (lápida: una regla
   que el dueño borró no vuelve); nunca cambia ni quita reglas, ni `defaultAgent`. Publica una
   versión nueva solo si agregó alguna y si nadie publicó el Coordinador mientras tanto.
6. **Release candidata, sin activarla:** por cada conexión que cambió, `construir` (ADR-0117 §2).
   La promoción la decide el dueño (`/rollback`/promover de la API de releases).

`aplicar=False` (dry-run) calcula lo mismo sin escribir. Corre dentro del `tenant_scope` del
negocio (RLS) y además filtra por `tenant_id`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from core.db.session import get_session
from core.presets import PlantillaAgente, PresetError, PresetManifest, plantillas_del_preset
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

ACTOR = "system:preset"
# Tope de reglas del KernelSpec (`Routing.rules`, max_length=50).
MAX_REGLAS = 50


@dataclass
class ResultadoAgente:
    key: str
    # creado | ya_estaba | reactivado | sin_habilidades | sin_conexion | quitado_por_el_dueno |
    # key_ocupada | archivado | sin_version | sin_procedimientos | archivado_sin_conexion
    estado: str
    version: int | None = None
    # connection_id → creada | ya_estaba | quitada_por_el_dueno | <code de ConflictoInstancia>
    instancias: dict[str, str] = field(default_factory=dict)
    avisos: list[str] = field(default_factory=list)


@dataclass
class ReportePresetV2:
    tenant_id: uuid.UUID
    preset: str
    version: int
    aplicar: bool
    omitido: str | None = None
    agentes: list[ResultadoAgente] = field(default_factory=list)
    reglas_nuevas: list[str] = field(default_factory=list)
    coordinador_version: int | None = None
    # connection_id → número de la release candidata (o el código del error del build)
    releases: dict[str, str] = field(default_factory=dict)
    errores: list[str] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)

    @property
    def fallo(self) -> bool:
        return bool(self.errores)

    def resumen(self) -> dict[str, Any]:
        """Lo que se guarda en `tenants.settings["preset"]["agentes"]` y en `audit_log`: solo
        keys, ids, estados y números (sin contenido de los agentes)."""
        return {
            "agentes": [
                {"key": a.key, "estado": a.estado, "version": a.version, "instancias": a.instancias}
                for a in self.agentes
            ],
            "reglas_nuevas": self.reglas_nuevas,
            "coordinador_version": self.coordinador_version,
            "releases": self.releases,
            "errores": self.errores,
        }


def _exigir_scope(tenant_id: uuid.UUID) -> None:
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("presets v2: fuera del tenant_scope del negocio")


# ------------------------------------------------------------------ derivación (pura)
def _playbooks_de_packs() -> set[str]:
    """Nombres de los playbooks que publica algún pack (los del core no están aquí)."""
    from core.agents.playbooks import CORE_PLAYBOOKS, playbook_roots

    return {f.stem for root in playbook_roots() if root != CORE_PLAYBOOKS for f in root.glob("*.md")}


def derivar_contenido(
    plantilla: PlantillaAgente,
    general: Mapping[str, Any],
    *,
    catalogo: Mapping[str, Any],
    habilitados: set[str],
    publicados: Iterable[Any],
    playbooks_de_packs: set[str],
) -> tuple[dict[str, Any], list[str]]:
    """Contenido `agentspec/3` del especialista y sus avisos. Hereda del General (la última versión
    del negocio: modelo, persona, memoria, políticas y la configuración de cada acción) y lo acota
    a la plantilla:

    - `procedures`: los publicados que nombra la plantilla y son ofrecibles con lo habilitado
      (ADR-0113 §1), con el rango compatible `^M.0` (la misma regla que el lector tolerante);
    - `tools`: los bindings del General de las acciones del core o de sus habilidades;
    - `guardrails`: los del General (un `required_tool` solo si su acción sigue);
    - `playbooks`: los del General que nombra la plantilla o que no son de ningún pack;
    - `declarativeTools`: ninguna (siguen en el General: pueden llevar secretos por referencia).

    Lanza `PresetError("sin_procedimientos")` si no queda ningún procedimiento."""
    from agentcore_contracts import AgentSpecV3

    from core.agents.tolerante import _declarados
    from core.kernel.procedures.catalogo import CORE_OWNER
    from core.releases.procedimientos import ofrecible

    avisos: list[str] = []
    propias = set(plantilla.habilidades) | (set(plantilla.opcionales) & habilitados)
    con_core = habilitados | {CORE_OWNER}
    elegidos = [
        p for p in publicados if p.id in plantilla.procedimientos and ofrecible(p, con_core)
    ]
    faltan = sorted(set(plantilla.procedimientos) - {p.id for p in elegidos})
    if faltan:
        avisos.append(f"procedimientos no ofrecibles con las habilidades del negocio: {faltan}")
    procedimientos = _declarados(elegidos, set())
    if not procedimientos:
        raise PresetError("sin_procedimientos")

    def _propia(nombre: str) -> bool:
        info = catalogo.get(nombre)
        return info is not None and (info.owner in propias or info.owner == CORE_OWNER)

    tools = [dict(b) for b in general.get("tools") or [] if _propia(str(b.get("tool", "")))]
    nombres = {b["tool"] for b in tools}
    guardrails = [
        dict(g)
        for g in general.get("guardrails") or []
        if g.get("kind") != "required_tool" or g.get("tool") in nombres
    ]
    playbooks = [
        dict(p)
        for p in general.get("playbooks") or []
        if p.get("name") in plantilla.playbooks or p.get("name") not in playbooks_de_packs
    ]
    contenido = {
        "description": plantilla.descripcion,
        "persona": general.get("persona") or {},
        "model": general["model"],
        "playbooks": playbooks,
        "procedures": procedimientos,
        "memory": general.get("memory") or {},
        "tools": tools,
        "guardrails": guardrails,
        "declarativeTools": [],
        "policies": general.get("policies") or {},
    }
    return AgentSpecV3.model_validate(contenido).model_dump(mode="json", by_alias=True), avisos


def agregar_reglas(
    kernel: Mapping[str, Any], plantillas: Iterable[PlantillaAgente], *, excluir: Iterable[str] = ()
) -> tuple[dict[str, Any], list[str], list[str]]:
    """KernelSpec con las reglas de las plantillas que todavía no están (por `id`), al final y
    con destino `agent = key`. Nunca cambia ni quita reglas existentes ni `defaultAgent`.
    `excluir`: ids que el preset ya agregó alguna vez (si no están, el dueño las borró: no
    vuelven). Devuelve (kernel, ids agregados, avisos)."""
    from agentcore_contracts import KernelSpec

    nuevo = KernelSpec.model_validate(kernel).model_dump(mode="json", by_alias=True)
    reglas = list(nuevo["routing"]["rules"])
    existentes = {r["id"] for r in reglas} | set(excluir)
    agregadas: list[str] = []
    avisos: list[str] = []
    for plantilla in plantillas:
        for regla in plantilla.reglas:
            if regla.id in existentes:
                continue
            if len(reglas) >= MAX_REGLAS:
                avisos.append(f"el Coordinador ya tiene {MAX_REGLAS} reglas: no entra {regla.id}")
                continue
            reglas.append(
                {
                    "id": regla.id,
                    "when": regla.when.model_dump(mode="json", by_alias=True, exclude_none=True),
                    "agent": plantilla.key,
                }
            )
            existentes.add(regla.id)
            agregadas.append(regla.id)
    nuevo["routing"]["rules"] = reglas
    return KernelSpec.model_validate(nuevo).model_dump(mode="json", by_alias=True), agregadas, avisos


# ------------------------------------------------------------------ lecturas
async def _runtime(tenant_id: uuid.UUID) -> str:
    from core.kernel.banderas import banderas_del_negocio

    async with get_session() as session:
        return (await banderas_del_negocio(session, tenant_id)).runtime


async def _conexiones_con_coordinador(tenant_id: uuid.UUID) -> list[uuid.UUID]:
    """Conexiones conversacionales vivas con un Coordinador vivo, en orden estable."""
    async with get_session() as session:
        filas = await session.execute(
            text(
                "SELECT cc.id FROM channel_connections cc "
                "JOIN connection_types ct ON ct.id = cc.type_id AND ct.produces_conversations "
                "WHERE cc.tenant_id = :t AND cc.deleted_at IS NULL AND EXISTS ("
                "  SELECT 1 FROM agent_instances ai WHERE ai.tenant_id = cc.tenant_id "
                "  AND ai.connection_id = cc.id AND ai.role = 'core' AND ai.deleted_at IS NULL) "
                "ORDER BY cc.created_at, cc.id LIMIT 200"
            ),
            {"t": tenant_id},
        )
        return [f[0] for f in filas]


async def _ultima_version(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> tuple[int, dict[str, Any]] | None:
    async with get_session() as session:
        fila = (
            await session.execute(
                text(
                    "SELECT version, content FROM agent_versions WHERE tenant_id = :t AND agent_id = :a "
                    "ORDER BY version DESC LIMIT 1"
                ),
                {"t": tenant_id, "a": agent_id},
            )
        ).first()
    return (int(fila[0]), dict(fila[1])) if fila is not None else None


async def _agente_por_key(tenant_id: uuid.UUID, key: str) -> tuple[uuid.UUID, str, bool] | None:
    """(id, status, borrado) del agente con esa key, también si el dueño lo borró."""
    async with get_session() as session:
        fila = (
            await session.execute(
                text(
                    "SELECT id, status, deleted_at IS NOT NULL FROM agents WHERE tenant_id = :t AND key = :k "
                    "ORDER BY deleted_at IS NOT NULL, created_at DESC LIMIT 1"
                ),
                {"t": tenant_id, "k": key},
            )
        ).first()
    return (fila[0], str(fila[1]), bool(fila[2])) if fila is not None else None


@dataclass(frozen=True)
class Registro:
    """Lo que el preset ya hizo en el negocio (`tenants.settings["preset"]["agentes"]`): las keys
    que creó (solo adopta esas), las que archivó él mismo (D-35) y las reglas que agregó alguna
    vez (lápida: una regla que el dueño borró no vuelve)."""

    creados: frozenset[str] = frozenset()
    archivados: frozenset[str] = frozenset()
    reglas: frozenset[str] = frozenset()


async def _registro(tenant_id: uuid.UUID) -> Registro:
    async with get_session() as session:
        valor = (
            await session.execute(
                text("SELECT settings -> 'preset' -> 'agentes' FROM tenants WHERE id = :t"), {"t": tenant_id}
            )
        ).scalar_one_or_none()
    if not isinstance(valor, dict):
        return Registro()

    def _keys(campo: str) -> frozenset[str]:
        crudo = valor.get(campo)
        return frozenset(str(x) for x in crudo) if isinstance(crudo, list) else frozenset()

    return Registro(_keys("creados"), _keys("archivados"), _keys("reglas"))


async def _efectivas_por_conexion(
    tenant_id: uuid.UUID, conexiones: list[uuid.UUID], habilitados: set[str]
) -> dict[uuid.UUID, set[str]]:
    """Habilidades efectivas de cada conexión (ADR-0118 §2, la misma regla que el build): sus
    instalaciones propias si las tiene (reemplazo entero), si no las del negocio."""
    from core.agents.instalaciones import instaladas_en_conexion

    salida: dict[uuid.UUID, set[str]] = {}
    for cid in conexiones:
        propias = await instaladas_en_conexion(tenant_id, cid)
        salida[cid] = set(propias) if propias else set(habilitados)
    return salida


async def _instancias_previas(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> dict[uuid.UUID, bool]:
    """connection_id → ¿hay una instancia viva? Incluye las quitadas por el dueño (False)."""
    async with get_session() as session:
        filas = await session.execute(
            text(
                "SELECT connection_id, bool_or(deleted_at IS NULL) FROM agent_instances "
                "WHERE tenant_id = :t AND agent_id = :a GROUP BY connection_id"
            ),
            {"t": tenant_id, "a": agent_id},
        )
        return {f[0]: bool(f[1]) for f in filas}


def _publicados(habilitados: set[str]) -> list[Any]:
    from core.kernel.procedures.de_pack import cargar

    return [p for pack_id in sorted(habilitados) for p in cargar(pack_id)]


# ------------------------------------------------------------------ escritura
async def _instalar_plantilla(
    tenant_id: uuid.UUID,
    plantilla: PlantillaAgente,
    manifest: PresetManifest,
    *,
    general: Mapping[str, Any],
    efectivas: dict[uuid.UUID, set[str]],
    catalogo: Mapping[str, Any],
    habilitados: set[str],
    publicados: list[Any],
    playbooks_de_packs: set[str],
    registro: Registro,
    aplicar: bool,
    tocadas: set[uuid.UUID],
) -> ResultadoAgente:
    from core.agents import instancias, versiones

    faltan = sorted(set(plantilla.habilidades) - habilitados)
    if faltan:
        return ResultadoAgente(plantilla.key, "sin_habilidades", avisos=[f"sin habilitar: {faltan}"])
    # Precondición de D-35 antes de crear nada: alguna conexión con las habilidades de la plantilla.
    destino = [cid for cid, efect in efectivas.items() if set(plantilla.habilidades) <= efect]
    if not destino:
        return ResultadoAgente(plantilla.key, "sin_conexion", avisos=["ninguna conexión tiene sus habilidades"])

    existente = await _agente_por_key(tenant_id, plantilla.key)
    nuevo = False  # el preset lo crea o lo reactiva en esta corrida
    agent_id: uuid.UUID | None = None
    if existente is not None:
        agent_id, estado, borrado = existente
        if borrado:
            return ResultadoAgente(plantilla.key, "quitado_por_el_dueno")
        if plantilla.key not in registro.creados:
            # Lo armó el dueño (o algo que no es el preset): no se adopta.
            return ResultadoAgente(plantilla.key, "key_ocupada")
        ultima = await _ultima_version(tenant_id, agent_id)
        if ultima is None:
            return ResultadoAgente(plantilla.key, "sin_version")
        if estado != "active":
            if plantilla.key not in registro.archivados:
                return ResultadoAgente(plantilla.key, "archivado")
            # Lo archivó el preset (D-35) en una corrida anterior: se reintenta.
            if aplicar:
                await versiones.actualizar_agente(tenant_id, agent_id, status="active")
            nuevo = True
        resultado = ResultadoAgente(plantilla.key, "reactivado" if nuevo else "ya_estaba", version=ultima[0])
    else:
        try:
            contenido, avisos = derivar_contenido(
                plantilla,
                general,
                catalogo=catalogo,
                habilitados=habilitados,
                publicados=publicados,
                playbooks_de_packs=playbooks_de_packs,
            )
        except PresetError:
            return ResultadoAgente(plantilla.key, "sin_procedimientos")
        resultado = ResultadoAgente(plantilla.key, "creado", version=1, avisos=avisos)
        nuevo = True
        if aplicar:
            try:
                agente = await versiones.crear_agente(
                    tenant_id, key=plantilla.key, kind="specialist", name=plantilla.nombre,
                    description=plantilla.descripcion,
                )
            except versiones.Conflicto:
                # Otro proceso (la CLI y un `apply_preset` a la vez) lo creó: la próxima corrida sigue.
                return ResultadoAgente(plantilla.key, "ya_estaba", avisos=["creado en paralelo"])
            agent_id = agente.id
            version, _ = await versiones.publicar(
                tenant_id,
                agent_id,
                catalogo=catalogo,  # type: ignore[arg-type]
                contenido=contenido,
                label=f"preset {manifest.id}",
                notes=f"Plantilla '{plantilla.key}' del preset {manifest.id} v{manifest.version} (HU-AOS-46).",
            )
            resultado.version = version.version

    previas = await _instancias_previas(tenant_id, agent_id) if agent_id is not None else {}
    for cid in efectivas:
        if cid in previas:
            resultado.instancias[str(cid)] = "ya_estaba" if previas[cid] else "quitada_por_el_dueno"
            continue
        if cid not in destino:
            resultado.instancias[str(cid)] = "sin_habilidades_en_la_conexion"
            continue
        if not aplicar:
            resultado.instancias[str(cid)] = "creada"
            continue
        assert agent_id is not None
        try:
            await instancias.agregar_instancia(tenant_id, cid, agent_id, is_default=False, enabled=True)
        except instancias.ConflictoInstancia as exc:
            resultado.instancias[str(cid)] = exc.code
            continue
        resultado.instancias[str(cid)] = "creada"
        tocadas.add(cid)

    vivas = [e for e in resultado.instancias.values() if e in ("creada", "ya_estaba")]
    if nuevo and not vivas and agent_id is not None and aplicar:
        # D-35: un agente publicado tiene al menos una conexión. Si ninguna instancia entró (un
        # conflicto), se archiva y queda en el registro: la próxima corrida lo reintenta.
        await versiones.actualizar_agente(tenant_id, agent_id, status="archived")
        resultado.estado = "archivado_sin_conexion"
    return resultado


async def _reglas_del_coordinador(
    tenant_id: uuid.UUID,
    coordinador_id: uuid.UUID,
    plantillas: list[PlantillaAgente],
    manifest: PresetManifest,
    *,
    catalogo: Mapping[str, Any],
    registro: Registro,
    aplicar: bool,
    reporte: ReportePresetV2,
) -> bool:
    """Agrega las reglas nuevas al Coordinador. True si publicó una versión nueva."""
    from core.agents import versiones

    ultima = await _ultima_version(tenant_id, coordinador_id)
    if ultima is None:
        reporte.errores.append("el Coordinador no tiene versión publicada")
        return False
    kernel, agregadas, avisos = agregar_reglas(ultima[1], plantillas, excluir=registro.reglas)
    reporte.avisos.extend(avisos)
    reporte.reglas_nuevas = agregadas
    if not agregadas:
        return False
    if not aplicar:
        reporte.coordinador_version = ultima[0] + 1
        return True
    # Control optimista: si alguien publicó el Coordinador mientras tanto, no se publica encima
    # de su versión con una base vieja (la próxima corrida parte de la suya).
    vigente = await _ultima_version(tenant_id, coordinador_id)
    if vigente is None or vigente[0] != ultima[0]:
        reporte.errores.append("el Coordinador cambió mientras se aplicaba el preset: vuelve a correrlo")
        reporte.reglas_nuevas = []
        return False
    version, creada = await versiones.publicar(
        tenant_id,
        coordinador_id,
        catalogo=catalogo,  # type: ignore[arg-type]
        contenido=kernel,
        label=f"preset {manifest.id}",
        notes=f"Reglas del preset {manifest.id} v{manifest.version}: {', '.join(agregadas)} (HU-AOS-46).",
    )
    reporte.coordinador_version = version.version
    return creada


async def _registrar(tenant_id: uuid.UUID, reporte: ReportePresetV2, registro: Registro) -> None:
    from core.db.models import Tenant
    from core.shared.audit import record_audit

    resumen = reporte.resumen()
    creados = set(registro.creados) | {a.key for a in reporte.agentes if a.estado == "creado"}
    archivados = (set(registro.archivados) | {
        a.key for a in reporte.agentes if a.estado == "archivado_sin_conexion"
    }) - {a.key for a in reporte.agentes if a.estado == "reactivado"}
    async with get_session() as session:
        tenant = await session.get(Tenant, tenant_id, with_for_update=True)
        if tenant is not None:
            settings = dict(tenant.settings or {})
            preset = dict(settings.get("preset") or {})
            preset["agentes"] = {
                "preset": reporte.preset,
                "version": reporte.version,
                "aplicado_en": datetime.now(UTC).isoformat(),
                # Acumulados entre corridas: idempotencia (ver `Registro`).
                "creados": sorted(creados),
                "archivados": sorted(archivados),
                "reglas": sorted(set(registro.reglas) | set(reporte.reglas_nuevas)),
                "ultima": resumen,
            }
            settings["preset"] = preset
            tenant.settings = settings
    await record_audit(
        actor=ACTOR,
        action="preset_agentes_aplicados",
        output_data={"preset": reporte.preset, "version": reporte.version, **resumen},
    )


# ------------------------------------------------------------------ entrada
async def aplicar_agentes(
    tenant_id: uuid.UUID,
    manifest: PresetManifest,
    *,
    modules: Iterable[Any] | None = None,
    aplicar: bool = True,
) -> ReportePresetV2:
    """Instala los agentes del preset v2 en el negocio (ver el docstring del módulo)."""
    from core.agents.instancias import agentes_base
    from core.agents.spec import enabled_business_packs, leer_spec_activo
    from core.agents.tolerante import KEY_COORDINADOR, KEY_GENERAL
    from core.kernel.procedures import catalogo_de_acciones
    from core.orchestrator.ruta_de_conversacion import ruteo_por_conexion
    from core.plugin_manager import plugin_manager

    _exigir_scope(tenant_id)
    reporte = ReportePresetV2(tenant_id, manifest.id, manifest.version, aplicar)
    if not manifest.es_v2:
        reporte.omitido = "preset_v1"
        return reporte
    if not ruteo_por_conexion() or await _runtime(tenant_id) != "v3":
        reporte.omitido = "banderas_apagadas"
        return reporte
    plantillas = plantillas_del_preset(manifest)

    leido = await leer_spec_activo(tenant_id)
    if leido is None:
        reporte.omitido = "sin_spec"
        return reporte
    habilitados = enabled_business_packs(leido[1])
    del leido  # el v2 puede llevar literales de las declarativas
    async with get_session() as session:
        base = await agentes_base(session, tenant_id)
    general_id, coordinador_id = base.get(KEY_GENERAL), base.get(KEY_COORDINADOR)
    general = await _ultima_version(tenant_id, general_id) if general_id else None
    if general is None or coordinador_id is None:
        reporte.omitido = "sin_agentes_base"
        return reporte
    conexiones = await _conexiones_con_coordinador(tenant_id)
    if not conexiones:
        reporte.omitido = "sin_conexion"
        return reporte

    modulos = list(modules) if modules is not None else plugin_manager.list_modules()
    catalogo = catalogo_de_acciones(modulos)
    publicados = _publicados(habilitados)
    playbooks_de_packs = _playbooks_de_packs()
    efectivas = await _efectivas_por_conexion(tenant_id, conexiones, habilitados)
    registro = await _registro(tenant_id)
    tocadas: set[uuid.UUID] = set()
    en_todas: list[PlantillaAgente] = []
    for plantilla in plantillas:
        resultado = await _instalar_plantilla(
            tenant_id,
            plantilla,
            manifest,
            general=general[1],
            efectivas=efectivas,
            catalogo=catalogo,
            habilitados=habilitados,
            publicados=publicados,
            playbooks_de_packs=playbooks_de_packs,
            registro=registro,
            aplicar=aplicar,
            tocadas=tocadas,
        )
        reporte.agentes.append(resultado)
        # El Coordinador es uno por negocio y su KernelSpec entra en la release de TODAS las
        # conexiones: una regla solo se agrega si su agente está en todas (ADR-0139 §4).
        if all(resultado.instancias.get(str(c)) in ("creada", "ya_estaba") for c in conexiones):
            en_todas.append(plantilla)
        elif resultado.instancias:
            reporte.avisos.append(f"{plantilla.key}: no está en todas las conexiones, sus reglas no se agregan")

    if await _reglas_del_coordinador(
        tenant_id, coordinador_id, en_todas, manifest,
        catalogo=catalogo, registro=registro, aplicar=aplicar, reporte=reporte,
    ):
        tocadas.update(conexiones)

    if aplicar:
        for cid in [c for c in conexiones if c in tocadas]:
            reporte.releases[str(cid)] = await _candidata(tenant_id, cid, manifest, modulos)
    else:
        reporte.releases = {
            str(c): "se construiría"
            for c in conexiones
            if reporte.reglas_nuevas or any(a.instancias.get(str(c)) == "creada" for a in reporte.agentes)
        }
    reporte.errores.extend(
        f"conexión {cid}: build {codigo}" for cid, codigo in reporte.releases.items() if codigo.startswith("error:")
    )
    cambio = tocadas or any(a.estado in ("creado", "reactivado", "archivado_sin_conexion") for a in reporte.agentes)
    if aplicar and cambio:
        await _registrar(tenant_id, reporte, registro)
    log.info(
        "preset_agentes",
        tenant_id=str(tenant_id),
        preset=manifest.id,
        aplicar=aplicar,
        agentes={a.key: a.estado for a in reporte.agentes},
        reglas=reporte.reglas_nuevas,
        releases=reporte.releases,
    )
    return reporte


async def _candidata(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, manifest: PresetManifest, modulos: list[Any]
) -> str:
    """Construye la release candidata de la conexión SIN promoverla. Devuelve su número o
    `error:<código>` (un build que falla no tumba las demás conexiones)."""
    from core.releases.build import ErrorDelBuild, construir

    try:
        build = await construir(
            tenant_id,
            connection_id,
            actor=ACTOR,
            modules=modulos,
            notas=f"Preset {manifest.id} v{manifest.version}: agentes y reglas del Coordinador (HU-AOS-46).",
        )
    except ErrorDelBuild as exc:
        log.warning(
            "preset_agentes_build_fallo", tenant_id=str(tenant_id), connection_id=str(connection_id),
            codigo=exc.codigo, detalle=exc.detalle[:5],
        )
        return f"error:{exc.codigo}"
    return str(build.number)


# ------------------------------------------------------------------ backfill (CLI)
async def _negocios_v3(tenant_ids: list[uuid.UUID] | None) -> list[tuple[uuid.UUID, str, str | None]]:
    """(id, slug, preset registrado) de los negocios vivos en v3 (o de los pedidos, en el estado
    que estén: el paso mismo los omite si no son v3). `tenants` es tabla de sistema, sin RLS."""
    async with get_session() as session:
        filas = await session.execute(
            text(
                "SELECT id, slug, settings->'preset'->>'id' FROM tenants WHERE deleted_at IS NULL "
                "AND (cast(:ids AS uuid[]) IS NULL OR id = ANY(cast(:ids AS uuid[]))) "
                "AND (cast(:ids AS uuid[]) IS NOT NULL OR aos_runtime = 'v3') ORDER BY slug"
            ),
            {"ids": list(tenant_ids) if tenant_ids else None},
        )
        return [(f[0], str(f[1]), f[2]) for f in filas]


async def backfill_presets_v2(
    *,
    tenant_ids: list[uuid.UUID] | None = None,
    preset_id: str | None = None,
    aplicar: bool = False,
    modules: Iterable[Any] | None = None,
) -> list[ReportePresetV2]:
    """`agent-core db presets-v2`: aplica el paso de agentes del preset registrado de cada negocio
    v3 (`tenants.settings["preset"].id`), o de `preset_id` si se pasa (solo con negocios
    explícitos). Dry-run por defecto. Un negocio sin preset queda omitido (`sin_preset`)."""
    from core.presets import load_preset
    from core.shared.tenant import tenant_scope

    if preset_id is not None and not tenant_ids:
        raise PresetError("--preset solo con --tenant: no se impone un preset a todos los negocios")
    reportes: list[ReportePresetV2] = []
    for tid, slug, registrado in await _negocios_v3(tenant_ids):
        pid = preset_id or registrado
        if not pid:
            reporte = ReportePresetV2(tid, "", 0, aplicar, omitido="sin_preset")
        else:
            manifest = load_preset(pid)
            async with tenant_scope(tid):
                try:
                    reporte = await aplicar_agentes(tid, manifest, modules=modules, aplicar=aplicar)
                except PresetError as exc:
                    reporte = ReportePresetV2(tid, pid, manifest.version, aplicar, errores=[str(exc)[:300]])
        log.info("preset_agentes_backfill", tenant=slug, omitido=reporte.omitido, errores=len(reporte.errores))
        reportes.append(reporte)
    return reportes


__all__ = [
    "ACTOR",
    "ReportePresetV2",
    "ResultadoAgente",
    "agregar_reglas",
    "aplicar_agentes",
    "backfill_presets_v2",
    "derivar_contenido",
]
