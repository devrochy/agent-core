"""Servicio de las plantillas con opciones del negocio (SPEC-40, ADR-0172, HU-OPC-05).

Lo usa `core/api/routers/plantillas_opciones.py`; el router solo traduce HTTP. Aquí viven las reglas:

- **Borrador y versiones.** El borrador (`option_templates.draft_definition`) se guarda con la FORMA validada
  (`Definicion`), aunque esté incompleto: el lint completo corre en `validar`, `vista_previa` y `publicar`. Publicar
  congela una fila nueva de `option_template_versions` (inmutable por trigger) con `version_n = max + 1` bajo
  `SELECT … FOR UPDATE` de la plantilla: dos publicaciones simultáneas no repiten número. La versión anterior NO se
  retira: un botón viejo ejecuta SU versión mientras no venza (ADR-0172 §3).
- **Contexto del lint = el del negocio.** Las fichas de las tools salen del AgentSpec activo del negocio SIN escribir
  (`leer_spec_activo`: no siembra el default) y con las mismas piezas que el resolver del toque
  (`lint.fichas_de_tools`, `lint.motivos_de_rechazo`). Límite: con `aos_runtime = v3` los bindings reales son los de
  la release del agente en foco, que aquí no hay; el resolver vuelve a lintar al tocar (ADR-0172 adenda del PR 6).
- **Catálogo de acciones.** Solo las tools que `motivos_de_rechazo` deja elegibles (marca `apta_para_opcion`, pack
  activo, binding EXPLÍCITO y habilitado, no gobernada por un procedimiento). Una tool de otro negocio no existe aquí:
  el AgentSpec, los packs deshabilitados y las declarativas son del negocio de la sesión.
- **Errores** como `ErrorPlantillaOpciones` (status, código, mensaje, detalles): el router los vuelve `ApiError`.
- **Auditoría** de cada escritura con ids y claves, nunca valores de la definición.

Toda consulta filtra por `tenant_id` además de la RLS (defensa en profundidad). Publicar puede encadenar la alta de la
versión como plantilla de Meta (HU-OPC-04, `core/gateway/opciones_meta.py`): es opcional, ocurre DESPUÉS de congelar y
si Meta falla la versión sigue publicada (el resultado de Meta viaja aparte y se reintenta). Nada de esto envía un
mensaje a un cliente.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from core.db.models import Conversation, OptionTemplate, OptionTemplateVersion, utcnow
from core.db.session import get_session
from core.gateway.opciones_meta import EstadoMeta, estado_de_publicacion, publicar_en_meta
from core.kernel.opciones import contexto, lint, mapeo
from core.kernel.opciones import envio as ev
from core.kernel.opciones import meta as meta_k
from core.kernel.opciones.definicion import Accion, Definicion, hallazgos_de_forma, hash_de
from core.kernel.opciones.lint import (
    ContextoLint,
    FichaTool,
    Hallazgo,
    confirmacion_efectiva,
    hay_errores,
    motivos_de_rechazo,
)
from core.shared.audit import record_audit
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

CLAVE = re.compile(r"^[a-z0-9_]{1,40}$")  # misma forma que `ck_option_templates_key`
MAX_PLANTILLAS = 100                      # plantillas vivas por negocio (acota el listado y el abuso)
MAX_BYTES_DEFINICION = 100_000            # tamaño del JSON de una definición que se acepta
MAX_NOMBRE = 120

# Valores de muestra de la vista previa para las variables base (nunca datos reales de un cliente).
MUESTRAS = {"cliente.nombre": "Ana", "negocio.nombre": "Mi negocio"}

ACC_CREAR = "option_template_create"
ACC_BORRADOR = "option_template_draft"
ACC_PUBLICAR = "option_template_publish"
ACC_RETIRAR = "option_template_retire"
ACC_BORRAR = "option_template_delete"


class ErrorPlantillaOpciones(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def _scope(tenant_id: uuid.UUID) -> None:
    """El `tenant_id` explícito es el del `tenant_scope` en curso: las consultas filtran por él y la RLS y el envío
    usan el del contexto, así que un llamador que los desacople se detiene aquí (como `bandera.habilitada`)."""
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("plantillas con opciones: fuera del tenant_scope del negocio")


def _no_encontrada() -> ErrorPlantillaOpciones:
    return ErrorPlantillaOpciones(404, "not_found", "No existe esa plantilla con opciones.")


def hallazgos_como_dicts(hallazgos: list[Hallazgo]) -> list[dict[str, str]]:
    return [h.as_dict() for h in hallazgos]


# ------------------------------------------------------------------ definición cruda
def leer_forma(crudo: Any) -> Definicion:
    """Valida la FORMA de lo que llega del panel; `ErrorPlantillaOpciones` 422 con un hallazgo por falla."""
    import json

    try:
        if len(json.dumps(crudo, ensure_ascii=False, default=str)) > MAX_BYTES_DEFINICION:
            raise ErrorPlantillaOpciones(
                422, "definicion_invalida", "La definición es demasiado grande.",
                {"hallazgos": [{"path": "definicion", "code": "esquema", "severity": "error", "mensaje": "Es demasiado grande."}]},
            )
        return Definicion.model_validate(crudo)
    except ValidationError as exc:
        hallazgos = [Hallazgo(p, "esquema", "error", m) for p, m in hallazgos_de_forma(exc)]
        raise ErrorPlantillaOpciones(
            422, "definicion_invalida", "La definición no tiene la forma esperada.", {"hallazgos": hallazgos_como_dicts(hallazgos)}
        ) from None


def _forma_o_vacia(almacenada: Any) -> Definicion | None:
    """El borrador/versión ya guardado como `Definicion`; None si no hay o ya no valida (no debería pasar)."""
    if almacenada is None:
        return None
    try:
        return Definicion.model_validate(almacenada)
    except ValidationError:
        log.error("option_definition_unreadable", tenant_id=str(get_current_tenant_id()))
        return None


# ------------------------------------------------------------------ catálogos
async def fichas_del_negocio(tenant_id: uuid.UUID) -> dict[str, FichaTool]:
    """Una ficha por tool del negocio (registradas en el host y declarativas del AgentSpec), con el binding
    EXPLÍCITO del AgentSpec activo. Sin AgentSpec activo: vacío (no se siembra nada desde una consulta)."""
    _scope(tenant_id)
    from core.agents.spec import enabled_packs, leer_spec_activo
    from core.kernel.procedures import registro
    from core.mcp.declarative import PLUGIN_ID as DECLARATIVE_PLUGIN_ID
    from core.mcp.declarative import tools_for_spec
    from core.mcp.host import mcp_host
    from core.plugin_manager import plugin_manager

    activo = await leer_spec_activo(tenant_id)
    if activo is None:
        return {}
    spec = activo[1]
    registradas = {t.name: t for t in mcp_host.get_tools()}
    # Prioridad al host, como el loop: una declarativa no puede ocultar una tool registrada.
    declarativas = [t for t in tools_for_spec(spec) if t.name not in registradas]
    habilitados = enabled_packs(spec)
    inactivos = await plugin_manager.disabled_plugins_for_tenant(tenant_id)
    packs_activos = set(habilitados) - set(inactivos)
    if declarativas:
        packs_activos.add(DECLARATIVE_PLUGIN_ID)
    gobernadas = lint.acciones_de_procedimientos(registro.disponibles(habilitados, inactivos).values())
    return lint.fichas_de_tools([*registradas.values(), *declarativas], spec.tools, packs_activos, gobernadas)


async def contexto_del_lint(tenant_id: uuid.UUID) -> ContextoLint:
    return ContextoLint(herramientas=await fichas_del_negocio(tenant_id), variables=set(contexto.catalogo_de_variables()))


async def catalogo_acciones(tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    """Las tools elegibles como acción de una opción, con su JSON Schema, su marca de sensible y si la
    confirmación del cliente es obligatoria. Orden estable por nombre."""
    from core.mcp.host import mcp_host

    fichas = await fichas_del_negocio(tenant_id)
    descripciones = {t.name: t.description for t in mcp_host.get_tools()}
    salida: list[dict[str, Any]] = []
    for nombre in sorted(fichas):
        ficha = fichas[nombre]
        if motivos_de_rechazo(nombre, ficha):
            continue
        salida.append(
            {
                "tool": nombre,
                "descripcion": descripciones.get(nombre, ""),
                "parameters": dict(ficha.parameters or {}),
                "sensible": ficha.sensible,
                "confirmacion_obligatoria": confirmacion_efectiva(Accion(tool=nombre), ficha),
            }
        )
    return salida


def catalogo_variables() -> list[dict[str, str]]:
    base = contexto.VARIABLES_BASE
    return [
        {"ruta": ruta, "descripcion": texto, "origen": "conversacion" if ruta in base else "servidor"}
        for ruta, texto in contexto.catalogo_de_variables().items()
    ]


# ------------------------------------------------------------------ validar y vista previa
async def validar(tenant_id: uuid.UUID, definicion: Any) -> list[Hallazgo]:
    """Los hallazgos del lint (errores y avisos) de la definición, sin guardar nada."""
    return lint.lintar(definicion, await contexto_del_lint(tenant_id))


async def vista_previa(tenant_id: uuid.UUID, definicion: Any, ejemplos: dict[str, str]) -> dict[str, Any]:
    """Lo que saldría por cada vía, con valores de muestra y payloads de relleno (nada firmado, nada enviado).
    Con errores en la definición: 422 con los hallazgos."""
    from core.gateway.event_notices import render_body
    from core.gateway.sender import interactivo_valido

    hallazgos = await validar(tenant_id, definicion)
    if hay_errores(hallazgos):
        raise ErrorPlantillaOpciones(
            422, "definicion_con_errores", "La definición tiene errores: corrígelos para ver la vista previa.",
            {"hallazgos": hallazgos_como_dicts(hallazgos)},
        )
    d = Definicion.model_validate(definicion)
    valores = {ruta: ejemplos.get(ruta) or MUESTRAS.get(ruta) or mapeo.EJEMPLO_POR_DEFECTO for ruta in mapeo.variables_del_cuerpo(d.body)}
    cuerpo = mapeo.renderizar(d.body, valores)
    # Mismo largo que un payload real (`opt:<32 hex>:<id>:<16 hex>`) para que `cabe_en_whatsapp` sea fiel.
    relleno = {o.id: f"opt:{'0' * 32}:{o.id}:{'0' * 16}" for o in d.opciones}
    interactivo = mapeo.a_interactivo(d, cuerpo, relleno)
    componentes = mapeo.componentes_meta(d, valores)
    return {
        "interactivo": interactivo,
        "cabe_en_whatsapp": interactivo_valido(interactivo),
        "componentes_meta": componentes,
        "cuerpo_meta_renderizado": render_body(mapeo.plantilla_transitoria(d, valores), mapeo.valores_para_meta(d, valores)),
        "texto_de_respaldo": mapeo.texto_de_respaldo(d, cuerpo),
        "hallazgos": hallazgos_como_dicts(hallazgos),
    }


# ------------------------------------------------------------------ lectura
def _version_vigente(plantilla: OptionTemplate, versiones: list[OptionTemplateVersion]) -> OptionTemplateVersion | None:
    return next((v for v in versiones if v.version_n == plantilla.current_version_n), None)


async def _versiones(session: Any, tenant_id: uuid.UUID, ids: list[uuid.UUID]) -> dict[uuid.UUID, list[OptionTemplateVersion]]:
    if not ids:
        return {}
    filas = (
        await session.execute(
            select(OptionTemplateVersion)
            .where(OptionTemplateVersion.tenant_id == tenant_id, OptionTemplateVersion.option_template_id.in_(ids))
            .order_by(OptionTemplateVersion.option_template_id, OptionTemplateVersion.version_n.desc())
        )
    ).scalars().all()
    por_plantilla: dict[uuid.UUID, list[OptionTemplateVersion]] = {}
    for v in filas:
        por_plantilla.setdefault(v.option_template_id, []).append(v)
    return por_plantilla


def _resumen(p: OptionTemplate, versiones: list[OptionTemplateVersion]) -> dict[str, Any]:
    vigente = _version_vigente(p, versiones)
    borrador = _forma_o_vacia(p.draft_definition)
    sin_publicar = borrador is not None and (vigente is None or hash_de(borrador) != vigente.definition_hash)
    return {
        "clave": p.key,
        "nombre": p.name,
        "estado": p.status,
        "version_vigente": p.current_version_n,
        "tiene_borrador_sin_publicar": sin_publicar,
        "creada_en": p.created_at,
        "actualizada_en": p.updated_at,
    }


def _estado_meta(e: EstadoMeta | None) -> dict[str, Any] | None:
    """Lo que Meta dice de la plantilla ligada a una versión (se lee de `message_templates`, no se duplica)."""
    if e is None:
        return None
    return {
        "message_template_id": e.message_template_id, "nombre": e.nombre, "estado": e.estado, "categoria": e.categoria,
        "categoria_anterior": e.categoria_anterior, "motivo_rechazo": e.motivo_rechazo, "recomendacion": e.recomendacion,
        "error_de_publicacion": e.error_de_publicacion,
    }


async def _estados(tenant_id: uuid.UUID, versiones: list[OptionTemplateVersion]) -> dict[uuid.UUID, EstadoMeta]:
    return await estado_de_publicacion(tenant_id, [v.id for v in versiones if v.message_template_id is not None])


def _detalle(
    p: OptionTemplate, versiones: list[OptionTemplateVersion], estados: dict[uuid.UUID, EstadoMeta] | None = None
) -> dict[str, Any]:
    vigente = _version_vigente(p, versiones)
    return {
        **_resumen(p, versiones),
        "borrador": _forma_o_vacia(p.draft_definition),
        "definicion_vigente": _forma_o_vacia(vigente.definition) if vigente is not None else None,
        "versiones": [
            {
                "version_n": v.version_n,
                "publicada_en": v.published_at,
                "retirada_en": v.retired_at,
                "definition_hash": v.definition_hash,
                "message_template_id": v.message_template_id,
                "meta": _estado_meta((estados or {}).get(v.id)),
                "vigente": v.version_n == p.current_version_n,
            }
            for v in versiones
        ],
    }


def _viva(tenant_id: uuid.UUID, clave: str) -> tuple[Any, ...]:
    return (OptionTemplate.tenant_id == tenant_id, OptionTemplate.key == clave, OptionTemplate.deleted_at.is_(None))


async def listar(tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    _scope(tenant_id)
    async with get_session() as session:
        plantillas = (
            await session.execute(
                select(OptionTemplate)
                .where(OptionTemplate.tenant_id == tenant_id, OptionTemplate.deleted_at.is_(None))
                .order_by(OptionTemplate.key)
                .limit(MAX_PLANTILLAS)
            )
        ).scalars().all()
        versiones = await _versiones(session, tenant_id, [p.id for p in plantillas])
        return [_resumen(p, versiones.get(p.id, [])) for p in plantillas]


async def obtener(tenant_id: uuid.UUID, clave: str) -> dict[str, Any]:
    _scope(tenant_id)
    if not CLAVE.fullmatch(clave):
        raise _no_encontrada()
    async with get_session() as session:
        p = (await session.execute(select(OptionTemplate).where(*_viva(tenant_id, clave)))).scalar_one_or_none()
        if p is None:
            raise _no_encontrada()
        versiones = await _versiones(session, tenant_id, [p.id])
        versiones_de = versiones.get(p.id, [])
        return _detalle(p, versiones_de, await _estados(tenant_id, versiones_de))


# ------------------------------------------------------------------ borradores
async def crear(tenant_id: uuid.UUID, actor: str, clave: str, nombre: str, definicion: Any | None) -> dict[str, Any]:
    _scope(tenant_id)
    if not CLAVE.fullmatch(clave):
        raise ErrorPlantillaOpciones(422, "clave_invalida", "La clave admite minúsculas, números y _ (hasta 40).")
    forma = leer_forma(definicion) if definicion is not None else None
    async with get_session() as session:
        vivas = (
            await session.execute(
                select(func.count()).select_from(OptionTemplate).where(OptionTemplate.tenant_id == tenant_id, OptionTemplate.deleted_at.is_(None))
            )
        ).scalar_one()
        if vivas >= MAX_PLANTILLAS:
            raise ErrorPlantillaOpciones(409, "limite_de_plantillas", f"Un negocio admite hasta {MAX_PLANTILLAS} plantillas con opciones.")
        existe = (await session.execute(select(OptionTemplate.id).where(*_viva(tenant_id, clave)))).first()
        if existe is not None:
            raise _clave_repetida()
        p = OptionTemplate(
            tenant_id=tenant_id, key=clave, name=nombre.strip()[:MAX_NOMBRE], status="draft",
            draft_definition=forma.model_dump(mode="json") if forma is not None else None,
        )
        session.add(p)
        try:
            await session.flush()
        except IntegrityError:  # carrera con otra creación de la misma clave: el índice parcial único manda
            raise _clave_repetida() from None
        resultado = _detalle(p, [])
    await record_audit(actor, ACC_CREAR, input_data={"template_key": clave}, output_data={"status": "draft"})
    return resultado


def _clave_repetida() -> ErrorPlantillaOpciones:
    return ErrorPlantillaOpciones(409, "clave_repetida", "Ya tienes una plantilla con esa clave.")


async def guardar_borrador(tenant_id: uuid.UUID, actor: str, clave: str, nombre: str | None, definicion: Any) -> dict[str, Any]:
    """Reemplaza el borrador (y el nombre, si llega). No toca ninguna versión publicada."""
    _scope(tenant_id)
    if not CLAVE.fullmatch(clave):
        raise _no_encontrada()
    forma = leer_forma(definicion)
    async with get_session() as session:
        p = (await session.execute(select(OptionTemplate).where(*_viva(tenant_id, clave)).with_for_update())).scalar_one_or_none()
        if p is None:
            raise _no_encontrada()
        p.draft_definition = forma.model_dump(mode="json")
        if nombre is not None:
            p.name = nombre.strip()[:MAX_NOMBRE]
        p.updated_at = utcnow()
        versiones = await _versiones(session, tenant_id, [p.id])
        versiones_de = versiones.get(p.id, [])
        resultado = _detalle(p, versiones_de, await _estados(tenant_id, versiones_de))
    await record_audit(actor, ACC_BORRADOR, input_data={"template_key": clave}, output_data={"status": resultado["estado"]})
    return resultado


async def eliminar(tenant_id: uuid.UUID, actor: str, clave: str) -> None:
    """Borrado lógico (libera la clave). Solo una plantilla que nunca se publicó o ya está retirada: la publicada
    se retira primero. La historia (versiones, envíos y toques) se conserva."""
    _scope(tenant_id)
    if not CLAVE.fullmatch(clave):
        raise _no_encontrada()
    async with get_session() as session:
        p = (await session.execute(select(OptionTemplate).where(*_viva(tenant_id, clave)).with_for_update())).scalar_one_or_none()
        if p is None:
            raise _no_encontrada()
        if p.status == "published":
            raise ErrorPlantillaOpciones(409, "plantilla_publicada", "Retira la plantilla antes de borrarla.")
        p.deleted_at = utcnow()
    await record_audit(actor, ACC_BORRAR, input_data={"template_key": clave})


# ------------------------------------------------------------------ publicar y retirar
def _validar_meta(meta: dict[str, Any]) -> tuple[uuid.UUID, str, str, dict[str, str]]:
    """`(conexión, categoría, idioma, ejemplos)` de la petición de publicar en Meta, o 422. Se comprueba ANTES de congelar
    una versión: una categoría inválida no debe dejar una versión publicada sin plantilla de Meta."""
    categoria = meta_k.categoria_valida(str(meta.get("categoria") or ""))
    if categoria is None:
        raise ErrorPlantillaOpciones(422, "categoria_invalida", "La categoría debe ser UTILITY (utilidad) o MARKETING (marketing).")
    idioma = str(meta.get("idioma") or meta_k.IDIOMA_POR_DEFECTO)
    if not meta_k.idioma_valido(idioma):
        raise ErrorPlantillaOpciones(422, "idioma_invalido", "El idioma debe tener la forma `es` o `es_AR`.")
    try:
        conexion = uuid.UUID(str(meta.get("connection_id")))
    except ValueError as exc:
        raise ErrorPlantillaOpciones(422, "conexion_invalida", "La conexión no es válida.") from exc
    ejemplos = {str(k): str(v) for k, v in (meta.get("ejemplos") or {}).items()}
    return conexion, categoria, idioma, ejemplos


def _meta_como_dict(r: meta_k.ResultadoPublicacion) -> dict[str, Any]:
    return {
        "ok": r.ok, "outcome": r.outcome, "reintentable": r.reintentable, "nombre": r.nombre or None, "estado": r.estado,
        "categoria": r.categoria, "message_template_id": r.message_template_id, "meta_template_id": r.meta_template_id,
        "motivo": r.motivo or None,
    }


async def publicar(tenant_id: uuid.UUID, actor: str, clave: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Congela el borrador como la versión `n + 1`. Con errores en el lint (o sin borrador, o sin cambios) se
    rechaza y NO cambia nada. Los avisos (`warning`) no impiden publicar y viajan en la respuesta.

    Con `meta` (`connection_id`, `categoria`, `idioma`, `ejemplos`) encadena, DESPUÉS de congelar, la alta de la
    versión como plantilla de Meta `<clave>_v<n>` (HU-OPC-04). Si Meta falla la versión sigue publicada: el resultado
    va en `meta` (`ok`, `outcome`, `reintentable`) y se reintenta con `publicar_version_en_meta`."""
    _scope(tenant_id)
    if not CLAVE.fullmatch(clave):
        raise _no_encontrada()
    pedido = _validar_meta(meta) if meta is not None else None
    ctx = await contexto_del_lint(tenant_id)  # antes del candado: no se retiene la fila mientras se consulta
    async with get_session() as session:
        p = (await session.execute(select(OptionTemplate).where(*_viva(tenant_id, clave)).with_for_update())).scalar_one_or_none()
        if p is None:
            raise _no_encontrada()
        if p.draft_definition is None:
            raise ErrorPlantillaOpciones(422, "sin_borrador", "La plantilla no tiene borrador que publicar.")
        hallazgos = lint.lintar(p.draft_definition, ctx)
        if hay_errores(hallazgos):
            raise ErrorPlantillaOpciones(
                422, "definicion_con_errores", "La definición tiene errores: corrígelos para publicar.",
                {"hallazgos": hallazgos_como_dicts(hallazgos)},
            )
        definicion = Definicion.model_validate(p.draft_definition)
        hash_ = hash_de(definicion)
        versiones = (await _versiones(session, tenant_id, [p.id])).get(p.id, [])
        vigente = _version_vigente(p, versiones)
        if p.status == "published" and vigente is not None and vigente.definition_hash == hash_:
            raise ErrorPlantillaOpciones(409, "sin_cambios", "El borrador es igual a la versión publicada.")
        n = (versiones[0].version_n if versiones else 0) + 1  # `_versiones` ordena de la más nueva a la más vieja
        version = OptionTemplateVersion(
            tenant_id=tenant_id, option_template_id=p.id, version_n=n,
            definition=definicion.model_dump(mode="json"), definition_hash=hash_,
        )
        session.add(version)
        p.status = "published"
        p.current_version_n = n
        p.updated_at = utcnow()
        await session.flush()
        resultado = {
            "clave": clave, "version_n": n, "definition_hash": hash_, "publicada_en": version.published_at,
            "message_template_id": None, "meta": None, "hallazgos": hallazgos_como_dicts(hallazgos),
        }
        version_id = version.id
    await record_audit(actor, ACC_PUBLICAR, input_data={"template_key": clave}, output_data={"version": n})
    if pedido is not None:
        conexion, categoria, idioma, ejemplos = pedido
        r = await publicar_en_meta(
            tenant_id, version_id, conexion, categoria, idioma=idioma, ejemplos=ejemplos, actor=actor
        )
        resultado["meta"] = _meta_como_dict(r)
        resultado["message_template_id"] = r.message_template_id
    return resultado


async def publicar_version_en_meta(
    tenant_id: uuid.UUID, actor: str, clave: str, version_n: int, meta: dict[str, Any]
) -> dict[str, Any]:
    """Alta (o reintento) de UNA versión ya publicada como plantilla de Meta. Repetirla es seguro: no duplica la
    plantilla ni gasta otra creación (ver `core/gateway/opciones_meta.py`). Una versión retirada no se publica."""
    _scope(tenant_id)
    if not CLAVE.fullmatch(clave):
        raise _no_encontrada()
    conexion, categoria, idioma, ejemplos = _validar_meta(meta)
    async with get_session() as session:
        version_id = (
            await session.execute(
                select(OptionTemplateVersion.id)
                .join(
                    OptionTemplate,
                    (OptionTemplate.tenant_id == OptionTemplateVersion.tenant_id)
                    & (OptionTemplate.id == OptionTemplateVersion.option_template_id),
                )
                .where(*_viva(tenant_id, clave), OptionTemplateVersion.version_n == version_n)
            )
        ).scalar_one_or_none()
    if version_id is None:
        raise _no_encontrada()
    r = await publicar_en_meta(tenant_id, version_id, conexion, categoria, idioma=idioma, ejemplos=ejemplos, actor=actor)
    if r.outcome == meta_k.VERSION_RETIRADA:
        raise ErrorPlantillaOpciones(409, "version_retirada", "Esa versión está retirada: publica una versión nueva.")
    return _meta_como_dict(r)


async def retirar(tenant_id: uuid.UUID, actor: str, clave: str) -> dict[str, Any]:
    """Retira la plantilla publicada: ya no se envía, y el toque de un botón que quedó en vuelo responde el texto
    fijo de «ya no está disponible». Marca `retired_at` en TODAS sus versiones (lo único que el trigger de
    inmutabilidad deja cambiar junto con `message_template_id`). Se puede volver a publicar un borrador después."""
    _scope(tenant_id)
    if not CLAVE.fullmatch(clave):
        raise _no_encontrada()
    async with get_session() as session:
        p = (await session.execute(select(OptionTemplate).where(*_viva(tenant_id, clave)).with_for_update())).scalar_one_or_none()
        if p is None:
            raise _no_encontrada()
        if p.status != "published":
            raise ErrorPlantillaOpciones(409, "no_publicada", "Solo se retira una plantilla publicada.")
        ahora = utcnow()
        await session.execute(
            update(OptionTemplateVersion)
            .where(
                OptionTemplateVersion.tenant_id == tenant_id,
                OptionTemplateVersion.option_template_id == p.id,
                OptionTemplateVersion.retired_at.is_(None),
            )
            .values(retired_at=ahora)
        )
        p.status = "retired"
        p.updated_at = ahora
        versiones = (await _versiones(session, tenant_id, [p.id])).get(p.id, [])
        resultado = _detalle(p, versiones, await _estados(tenant_id, versiones))
    await record_audit(actor, ACC_RETIRAR, input_data={"template_key": clave})
    return resultado


# ------------------------------------------------------------------ enviar prueba
_HTTP_DE_SALIDA = {
    ev.BANDERA_APAGADA: (404, "opciones_directas_no_habilitadas"),
    ev.CLAVE_INVALIDA: (404, "not_found"),
    ev.NO_ENCONTRADA: (404, "not_found"),
    ev.CONVERSACION_NO_ENCONTRADA: (404, "conversacion_no_encontrada"),
    ev.NO_PUBLICADA: (409, "no_publicada"),
    ev.CONTEXTO_INCOMPLETO: (422, "contexto_incompleto"),
    ev.SIN_PLANTILLA: (409, "sin_plantilla"),
    ev.PAUSADA: (409, "pausada"),
}


async def enviar_prueba(tenant_id: uuid.UUID, actor: str, clave: str, conversation_id: uuid.UUID, clave_idempotencia: str) -> dict[str, Any]:
    """Envía la versión vigente a UNA conversación del propio negocio (nunca a un número libre) por la vía que
    corresponda, con `sender.send_options_to_conversation`: crea el `option_sends`, queda en `audit_log` y respeta
    la bandera, la pausa y las plantillas. Idempotente por `clave_idempotencia`."""
    _scope(tenant_id)
    from core.gateway.sender import send_options_to_conversation

    if not CLAVE.fullmatch(clave):
        raise _no_encontrada()
    async with get_session() as session:
        existe = (
            await session.execute(
                select(Conversation.id).where(
                    Conversation.tenant_id == tenant_id, Conversation.id == conversation_id, Conversation.deleted_at.is_(None)
                )
            )
        ).first()
    if existe is None:
        raise ErrorPlantillaOpciones(404, "conversacion_no_encontrada", "No existe esa conversación en tu negocio.")
    r = await send_options_to_conversation(conversation_id, clave, clave_idempotencia=clave_idempotencia, actor=actor)
    if r.enviado or r.outcome == ev.DUPLICADO:
        return {
            "enviado": True,
            "ya_enviado": r.outcome == ev.DUPLICADO,
            "via": ev.ETIQUETA_VIA.get(r.via or "", None),
            "ejecucion_directa": r.ejecucion_directa,
            "degradado": r.degradado,
            "send_id": r.send_id,
        }
    if r.outcome in (ev.NO_ENCONTRADA, ev.CLAVE_INVALIDA):
        raise _no_encontrada()  # un solo mensaje para «inexistente» y «de otro negocio»
    status, code = _HTTP_DE_SALIDA.get(r.outcome, (502, "no_se_pudo_enviar"))
    mensaje = ev.para_el_agente(r).get("mensaje") or "No se pudo enviar la plantilla."
    # `motivo` es un código cerrado solo en estos resultados; en un fallo interno es el nombre de una excepción.
    cerrado = r.outcome in (ev.SIN_PLANTILLA, ev.PAUSADA, ev.CONTEXTO_INCOMPLETO, ev.NO_PUBLICADA)
    raise ErrorPlantillaOpciones(status, code, str(mensaje), {"motivo": r.motivo} if r.motivo and cerrado else None)
