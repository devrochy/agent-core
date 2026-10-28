"""Catálogo unificado de mensajes del negocio, de SOLO LECTURA (pista D0, ADR-0178).

Un negocio tiene sus mensajes repartidos en cinco inventarios, con editores y dialectos de variables distintos:

- `meta`: plantillas de WhatsApp (`message_templates`, espejo de lo que dice Meta);
- `evento`: avisos por evento (ADR-0108): cada clave de `event_notices.EVENTOS` con su mapeo en
  `app_settings.message_templates_map`;
- `opciones`: plantillas con opciones (SPEC-40, ADR-0172): `option_templates` y su versión vigente;
- `texto_pack`: los textos configurables de los packs habilitados (`plugin_configs`, cifrado con Fernet);
- `guardado`: los mensajes guardados del negocio (`mensajes_guardados`, ADR-0178 D4): respuestas rápidas del operador.

Este módulo los lista juntos, con una forma común, y apunta a cada editor. NO migra, NO escribe y NO cambia el
comportamiento de ninguno: cada fuente sigue siendo la de siempre. Tampoco toca la red: a diferencia de
`GET /connections/{id}/templates`, no sincroniza con Meta; muestra lo último guardado.

Permisos (los pone el router, ver `core/api/routers/mensajes.py`): cada tipo se incluye solo si quien consulta ya
puede leer su fuente hoy (`connections` → meta y opciones, `settings` → evento, `plugins` → texto_pack), y las
entradas `opciones` solo si la bandera `opciones_directas.habilitada` del negocio está encendida (con ella apagada
`/plantillas-opciones` responde 404 y no revela nada). Lo omitido se informa en `omitidas`.

Los textos de packs se leen SOLO con `plugin_manager.load_config` (la ruta de lectura de siempre, que descifra y
valida) y de ella se extraen únicamente los campos que el propio pack marca como mensaje al cliente
(`json_schema_extra={"mensaje": "<canal>"}`); el core no conoce los nombres de esos campos y nunca se vuelca la
configuración entera. Los packs habilitados se leen sin escribir (`leer_spec_activo` no siembra el AgentSpec).

Las variables se devuelven tal como las usa cada fuente (`{{1}}`, `{cliente.nombre}`, `{fecha}`): normalizarlas es
trabajo de la pista D1. Todo filtra por `tenant_id` explícito además de la RLS.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import select

from core.agents.spec import CORE_PACK_ID, enabled_packs, leer_spec_activo
from core.db.models import (
    MensajeGuardado,
    MessageTemplate,
    OptionTemplate,
    OptionTemplateVersion,
    WabaAccount,
)
from core.db.session import get_session
from core.gateway.connection_types import canales_con, connection_types
from core.gateway.event_notices import EVENTOS, load_templates_map, template_placeholders
from core.kernel.mensajes import variables_de
from core.kernel.opciones import bandera
from core.kernel.opciones.definicion import Definicion
from core.plugin_manager import plugin_manager
from core.shared.exceptions import PluginConfigError
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

TipoMensaje = Literal["meta", "evento", "opciones", "texto_pack", "guardado"]
TIPOS: tuple[TipoMensaje, ...] = ("meta", "evento", "opciones", "texto_pack", "guardado")

# Tope de entradas por tipo: Meta permite 250 plantillas por WABA y un negocio tiene pocas conexiones, así que en
# la práctica no se alcanza; si pasara, la respuesta lo dice (`truncado`) en vez de crecer sin límite.
MAX_POR_TIPO = 500
MAX_RESUMEN = 160

# Marca que pone un pack en un campo `str` de su configuración que es un MENSAJE al cliente:
# `Field(..., json_schema_extra={"mensaje": "whatsapp"})` (o `"email"`). El valor es el canal por el que sale. El core
# lee el schema de forma genérica; `tests/test_catalogo_mensajes.py` falla si un campo `str` cuyo default tiene
# `{variables}` no está marcado ni en la lista de exclusiones, para que un campo nuevo no se pierda en silencio.
MARCA_DE_MENSAJE = "mensaje"
CANALES_DE_MARCA = frozenset({"whatsapp", "email"})

# `{a}` y `{a.b}` (los dos dialectos de llaves que usan eventos, packs y opciones).
_LLAVES = re.compile(r"\{([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*)\}")


# ---------------------------------------------------------------- capacidades por canal
# ÚNICO PUNTO que decide en qué canales sale cada tipo de mensaje; sale de la tabla de capacidades por canal
# (`connection_types.CAPACIDADES_POR_TIPO`, pista C1). Capacidad que exige cada tipo:
# - `meta`: plantilla aprobada por el proveedor (WhatsApp Cloud).
# - `evento`: los avisos por evento se mandan con plantilla aprobada y solo buscan conversaciones de ese canal
#   (`core_handlers`); D2 los llevará a los canales de texto.
# - `opciones`: botones (la ejecución directa); en los canales de texto hoy solo sale el texto numerado sin acción.
# - `texto_pack`: un texto de pack sale por WhatsApp salvo que la marca de su campo diga otro canal.
# - `guardado`: texto libre, no exige capacidad: sus canales son los que el mensaje declara (`canales_de_guardado`).
_CAPACIDAD_POR_TIPO: dict[TipoMensaje, str] = {
    "meta": "plantilla_aprobada",
    "evento": "plantilla_aprobada",
    "opciones": "botones",
    "texto_pack": "plantilla_aprobada",
}


def canales_de_guardado(tipos_de_conexion: list[str]) -> list[str]:
    """Canales de un mensaje guardado: los de los tipos de conexión que declara, sin repetir y en el orden del
    catálogo; vacío = todos los que atienden conversaciones de clientes (no el playground)."""
    elegidos = set(tipos_de_conexion)
    canales: list[str] = []
    for t in connection_types():
        if t.produces_conversations and t.channel and (not elegidos or t.id in elegidos) and t.channel not in canales:
            canales.append(t.channel)
    return canales


def canales_de(tipo: TipoMensaje, marca: str | None = None) -> list[str]:
    """Canales en los que este tipo de mensaje se entrega hoy. Un texto de pack trae su canal en la marca del campo
    (hay textos de correo); si la marca no es un canal conocido, vale el del tipo."""
    if marca in CANALES_DE_MARCA:
        return [str(marca)]
    if tipo == "guardado":
        return canales_de_guardado([])
    return canales_con(_CAPACIDAD_POR_TIPO[tipo])


# ---------------------------------------------------------------- forma de salida
@dataclass(frozen=True)
class Fuente:
    """Puntero estructurado al editor de origen; el panel arma el enlace. Solo trae los ids del tipo."""

    editor: TipoMensaje
    connection_id: uuid.UUID | None = None
    message_template_id: uuid.UUID | None = None
    evento: str | None = None
    clave: str | None = None
    plugin_id: str | None = None
    campo: str | None = None


@dataclass(frozen=True)
class Entrada:
    id: str
    tipo: TipoMensaje
    clave: str
    nombre: str
    idioma: str | None
    estado: str
    estado_detalle: str | None
    canales_soportados: list[str]
    variables: list[str]
    dialecto_variables: str
    resumen: str | None
    actualizado_en: datetime | None
    fuente: Fuente


@dataclass(frozen=True)
class Omitida:
    tipo: TipoMensaje
    motivo: Literal["sin_permiso", "bandera_apagada"]


@dataclass
class Catalogo:
    entradas: list[Entrada] = field(default_factory=list)
    omitidas: list[Omitida] = field(default_factory=list)
    truncado: list[TipoMensaje] = field(default_factory=list)


def _scope(tenant_id: uuid.UUID) -> None:
    """El `tenant_id` explícito es el del `tenant_scope` en curso (como `plantillas_opciones_service`)."""
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("catálogo de mensajes: fuera del tenant_scope del negocio")


def _resumen(texto: str | None) -> str | None:
    limpio = re.sub(r"\s+", " ", texto or "").strip()
    if not limpio:
        return None
    return limpio if len(limpio) <= MAX_RESUMEN else limpio[: MAX_RESUMEN - 1].rstrip() + "…"


def _llaves(texto: str) -> list[str]:
    vistas: list[str] = []
    for nombre in _LLAVES.findall(texto or ""):
        if nombre not in vistas:
            vistas.append(nombre)
    return vistas


# ---------------------------------------------------------------- meta
async def _plantillas_de_meta(tenant_id: uuid.UUID) -> list[tuple[MessageTemplate, WabaAccount]]:
    """Todas las plantillas de las WABA vivas del negocio, salvo las ligadas a la versión VIGENTE de una plantilla
    con opciones viva (esas se muestran dentro de la entrada `opciones`, con su estado de Meta). Las versiones
    anteriores o de plantillas borradas siguen aprobadas en Meta y cuentan para su tope: salen como `meta`."""
    ligadas = (
        select(OptionTemplateVersion.message_template_id)
        .join(
            OptionTemplate,
            (OptionTemplate.tenant_id == OptionTemplateVersion.tenant_id)
            & (OptionTemplate.id == OptionTemplateVersion.option_template_id)
            & (OptionTemplate.current_version_n == OptionTemplateVersion.version_n),
        )
        .where(
            OptionTemplateVersion.tenant_id == tenant_id,
            OptionTemplateVersion.message_template_id.is_not(None),
            OptionTemplate.deleted_at.is_(None),
        )
    )
    async with get_session() as session:
        filas = (
            await session.execute(
                select(MessageTemplate, WabaAccount)
                .join(WabaAccount, WabaAccount.id == MessageTemplate.waba_account_id)
                .where(
                    MessageTemplate.tenant_id == tenant_id,
                    WabaAccount.tenant_id == tenant_id,
                    WabaAccount.deleted_at.is_(None),
                    MessageTemplate.id.not_in(ligadas),
                )
                .order_by(MessageTemplate.name, MessageTemplate.language, WabaAccount.created_at)
                .limit(MAX_POR_TIPO + 1)
            )
        ).all()
    return [(t, w) for t, w in filas]


def _entrada_meta(t: MessageTemplate, waba: WabaAccount) -> Entrada:
    cuerpo = next(
        (c for c in (t.components or []) if isinstance(c, dict) and str(c.get("type", "")).upper() == "BODY"), None
    )
    nombrada = (t.parameter_format or "").upper() == "NAMED"
    return Entrada(
        id=f"meta:{waba.connection_id}:{t.name}:{t.language}",
        tipo="meta",
        clave=t.name,
        nombre=t.name,
        idioma=t.language,
        estado=t.status,
        estado_detalle=t.category,
        canales_soportados=canales_de("meta"),
        variables=template_placeholders(t.components),
        dialecto_variables="meta_nombradas" if nombrada else "meta_posicionales",
        resumen=_resumen((cuerpo or {}).get("text")),
        actualizado_en=t.synced_at or t.updated_at,
        fuente=Fuente(editor="meta", connection_id=waba.connection_id, message_template_id=t.id),
    )


# ---------------------------------------------------------------- evento
async def _entradas_de_evento(tenant_id: uuid.UUID) -> list[Entrada]:
    mapa = await load_templates_map(tenant_id)
    async with get_session() as session:
        aprobadas = {
            (nombre, idioma)
            for nombre, idioma in (
                await session.execute(
                    select(MessageTemplate.name, MessageTemplate.language)
                    .join(WabaAccount, WabaAccount.id == MessageTemplate.waba_account_id)
                    .where(
                        MessageTemplate.tenant_id == tenant_id,
                        MessageTemplate.status == "APPROVED",
                        WabaAccount.tenant_id == tenant_id,
                        WabaAccount.deleted_at.is_(None),
                    )
                )
            ).all()
        }
    salida: list[Entrada] = []
    for evento, rutas in EVENTOS.items():
        cfg = mapa.get(evento)
        cfg = cfg if isinstance(cfg, dict) else {}
        plantilla = str(cfg.get("template") or "")
        if not plantilla:
            estado, detalle = "sin_mapear", None
        elif (plantilla, str(cfg.get("language") or "es")) in aprobadas:
            estado, detalle = "mapeado", plantilla
        else:
            estado, detalle = "plantilla_no_aprobada", plantilla
        salida.append(
            Entrada(
                id=f"evento:{evento}",
                tipo="evento",
                clave=evento,
                nombre=evento,
                idioma=str(cfg.get("language") or "") or None,
                estado=estado,
                estado_detalle=detalle,
                canales_soportados=canales_de("evento"),
                variables=list(rutas),
                dialecto_variables="llaves_con_ruta",
                resumen=_resumen(str(cfg.get("fallback_text") or "")),
                actualizado_en=None,
                fuente=Fuente(editor="evento", evento=evento),
            )
        )
    return salida


# ---------------------------------------------------------------- opciones
async def _entradas_de_opciones(tenant_id: uuid.UUID) -> tuple[list[Entrada], bool]:
    from core.gateway.opciones_meta import estado_de_publicacion

    async with get_session() as session:
        plantillas = (
            (
                await session.execute(
                    select(OptionTemplate)
                    .where(OptionTemplate.tenant_id == tenant_id, OptionTemplate.deleted_at.is_(None))
                    .order_by(OptionTemplate.key)
                    .limit(MAX_POR_TIPO + 1)
                )
            )
            .scalars()
            .all()
        )
        truncado = len(plantillas) > MAX_POR_TIPO
        plantillas = plantillas[:MAX_POR_TIPO]
        versiones: dict[tuple[uuid.UUID, int], OptionTemplateVersion] = {}
        if plantillas:
            for v in (
                (
                    await session.execute(
                        select(OptionTemplateVersion).where(
                            OptionTemplateVersion.tenant_id == tenant_id,
                            OptionTemplateVersion.option_template_id.in_([p.id for p in plantillas]),
                        )
                    )
                )
                .scalars()
                .all()
            ):
                versiones[(v.option_template_id, v.version_n)] = v
    vigentes = {p.id: versiones.get((p.id, p.current_version_n)) for p in plantillas if p.current_version_n is not None}
    estados = await estado_de_publicacion(
        tenant_id, [v.id for v in vigentes.values() if v is not None and v.message_template_id is not None]
    )
    salida: list[Entrada] = []
    for p in plantillas:
        vigente = vigentes.get(p.id)
        crudo = vigente.definition if vigente is not None else p.draft_definition
        try:
            definicion = Definicion.model_validate(crudo) if crudo else None
        except ValueError:
            definicion = None  # una forma vieja que ya no valida no tumba el catálogo
        meta = estados.get(vigente.id) if vigente is not None else None
        salida.append(
            Entrada(
                id=f"opciones:{p.key}",
                tipo="opciones",
                clave=p.key,
                nombre=p.name or p.key,
                idioma=None,
                estado=p.status,
                estado_detalle=meta.estado if meta is not None else None,
                canales_soportados=canales_de("opciones"),
                variables=list(definicion.variables) if definicion is not None else [],
                dialecto_variables="llaves_con_ruta",
                resumen=_resumen(definicion.body if definicion is not None else None),
                actualizado_en=p.updated_at,
                fuente=Fuente(editor="opciones", clave=p.key),
            )
        )
    return salida, truncado


# ---------------------------------------------------------------- texto_pack
def campos_de_texto(schema: Any) -> dict[str, str]:
    """`campo → canal` de los campos `str` de la configuración de un pack que el PACK marcó como mensaje al cliente
    (`json_schema_extra={"mensaje": "<canal>"}`). El core no conoce nombres de campo."""
    salida: dict[str, str] = {}
    for nombre, info in (getattr(schema, "model_fields", {}) or {}).items():
        extra = info.json_schema_extra
        if info.annotation is str and isinstance(extra, dict) and MARCA_DE_MENSAJE in extra:
            salida[nombre] = str(extra[MARCA_DE_MENSAJE])
    return salida


async def _packs_habilitados(tenant_id: uuid.UUID) -> set[str]:
    """Packs del AgentSpec activo que no están apagados para el negocio. Solo lee: sin spec activo, ninguno (no
    se siembra el default desde una consulta)."""
    activo = await leer_spec_activo(tenant_id)
    if activo is None:
        return set()
    inactivos = await plugin_manager.disabled_plugins_for_tenant(tenant_id)
    return (set(enabled_packs(activo[1])) - set(inactivos)) - {CORE_PACK_ID}


async def _entradas_de_packs(tenant_id: uuid.UUID) -> list[Entrada]:
    habilitados = await _packs_habilitados(tenant_id)
    salida: list[Entrada] = []
    for modulo in sorted(plugin_manager.list_modules(), key=lambda m: m.id):
        if modulo.id not in habilitados or modulo.config_schema is None:
            continue
        campos = campos_de_texto(modulo.config_schema)
        if not campos:
            continue
        valores: dict[str, Any] = {}
        invalida = False
        try:
            config = await plugin_manager.load_config(modulo.id)
            valores = {c: getattr(config, c, None) for c in campos} if config is not None else {}
        except PluginConfigError:
            invalida = True  # sin fila obligatoria o guardada con un schema viejo: se informa, no se inventa un texto
        except Exception:
            # Fila que no descifra (otra clave, dato corrupto): un pack roto no tumba el catálogo. Sin contenido en el log.
            log.warning("catalogo_mensajes_config_ilegible", plugin_id=modulo.id)
            invalida = True
        modelo = modulo.config_schema.model_fields
        for campo, marca in campos.items():
            predeterminado = modelo[campo].default
            texto = valores.get(campo, predeterminado)
            texto = texto if isinstance(texto, str) else str(predeterminado or "")
            if invalida:
                estado, texto = "config_invalida", ""
            else:
                estado = "por_defecto" if texto == predeterminado else "personalizado"
            salida.append(
                Entrada(
                    id=f"texto_pack:{modulo.id}:{campo}",
                    tipo="texto_pack",
                    clave=campo,
                    nombre=modelo[campo].description or campo,
                    idioma=None,
                    estado=estado,
                    estado_detalle=modulo.display_name or modulo.name or modulo.id,
                    canales_soportados=canales_de("texto_pack", marca),
                    variables=_llaves(texto),
                    dialecto_variables="llaves_con_ruta",
                    resumen=_resumen(texto),
                    actualizado_en=None,
                    fuente=Fuente(editor="texto_pack", plugin_id=modulo.id, campo=campo),
                )
            )
    return salida[:MAX_POR_TIPO]


# ---------------------------------------------------------------- guardado
async def _entradas_de_guardados(tenant_id: uuid.UUID) -> tuple[list[Entrada], bool]:
    """Los mensajes guardados vivos del negocio, archivados incluidos (el estado lo dice)."""
    async with get_session() as session:
        filas = (
            (
                await session.execute(
                    select(MensajeGuardado)
                    .where(MensajeGuardado.tenant_id == tenant_id, MensajeGuardado.deleted_at.is_(None))
                    .order_by(MensajeGuardado.clave)
                    .limit(MAX_POR_TIPO + 1)
                )
            )
            .scalars()
            .all()
        )
    salida = [
        Entrada(
            id=f"guardado:{m.clave}",
            tipo="guardado",
            clave=m.clave,
            nombre=m.titulo,
            idioma=None,
            estado=m.estado,
            estado_detalle=None,
            canales_soportados=canales_de_guardado(list(m.canales or [])),
            variables=variables_de(m.texto),
            dialecto_variables="llaves_con_ruta",
            resumen=_resumen(m.texto),
            actualizado_en=m.updated_at,
            fuente=Fuente(editor="guardado", clave=m.clave),
        )
        for m in filas[:MAX_POR_TIPO]
    ]
    return salida, len(filas) > MAX_POR_TIPO


# ---------------------------------------------------------------- servicio
async def catalogo_de_mensajes(tenant_id: uuid.UUID, tipos: Iterable[TipoMensaje] = TIPOS) -> Catalogo:
    """El catálogo del negocio. `tipos` son los que quien llama puede leer (el router los decide por permiso); los
    demás se informan en `omitidas`. Solo lee: no hay escritura, red ni descifrado fuera de `load_config`."""
    _scope(tenant_id)
    permitidos = set(tipos)
    catalogo = Catalogo()

    if "meta" in permitidos:
        plantillas = await _plantillas_de_meta(tenant_id)
        if len(plantillas) > MAX_POR_TIPO:
            catalogo.truncado.append("meta")
        catalogo.entradas += [_entrada_meta(t, w) for t, w in plantillas[:MAX_POR_TIPO]]
    if "evento" in permitidos:
        catalogo.entradas += await _entradas_de_evento(tenant_id)
    if "opciones" in permitidos:
        async with get_session() as session:
            encendida = await bandera.habilitada(session, tenant_id)
        if encendida:
            entradas, truncado = await _entradas_de_opciones(tenant_id)
            catalogo.entradas += entradas
            if truncado:
                catalogo.truncado.append("opciones")
        else:
            catalogo.omitidas.append(Omitida("opciones", "bandera_apagada"))
    if "texto_pack" in permitidos:
        catalogo.entradas += await _entradas_de_packs(tenant_id)
    if "guardado" in permitidos:
        entradas, truncado = await _entradas_de_guardados(tenant_id)
        catalogo.entradas += entradas
        if truncado:
            catalogo.truncado.append("guardado")

    for tipo in TIPOS:
        if tipo not in permitidos:
            catalogo.omitidas.append(Omitida(tipo, "sin_permiso"))
    return catalogo
