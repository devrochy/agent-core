"""Publicación de una versión de una plantilla con opciones como plantilla de Meta (SPEC-40, ADR-0172 §2,
HU-OPC-04). Lo puro (nombre, categoría, resultados) está en `core/kernel/opciones/meta.py`; aquí, la base, Meta y
el candado.

Reglas de este módulo:

- **Nunca se edita una aprobada.** Cada versión es una plantilla nueva `<clave>_v<n>`; la `n` sigue ligada a su fila
  de `message_templates` y válida para los envíos que ya la usan (PR 5). No se llama a `update_template`: publicar la
  versión `n + 1` no consume ninguna de las 10 ediciones mensuales de la `n`.
- **Validar antes de tocar nada.** Componentes de `mapeo.componentes_meta`, `compatibilidad_plantilla` (lo mismo que
  exige el envío) y `validate_template_components` ANTES de reservar y de llamar a Meta.
- **Reintento seguro sin tabla nueva.** `message_templates` ya tiene `UNIQUE (waba_account_id, name, language)` y
  admite `DRAFT`: se RESERVA la fila (`DRAFT`, sin `meta_template_id`, con la categoría y los componentes que se van a
  mandar) y se liga `option_template_versions.message_template_id` en la misma transacción, ANTES del POST. Eso es
  la clave de idempotencia y el estado persistido:
    * fila con `meta_template_id` → ya está en Meta: no se llama de nuevo (`already_published`);
    * fila `DRAFT` sin id (el POST pudo o no llegar a Meta: corte de red, timeout, caída del worker) → se CONCILIA con
      un GET de Meta FILTRADO por nombre ANTES de crear (no el sync de toda la WABA: este reescribiría las demás
      filas): si Meta ya la tiene se adopta su id y estado, y no se gasta otro cupo del límite de creación; si no,
      Meta no la tiene y se crea completando la misma fila (con los componentes y la categoría de ESTA llamada);
    * dos publicaciones simultáneas de la misma versión: candado Redis corto por (negocio, versión) y, detrás, el
      UNIQUE de la fila reservada.
- **Categoría visible antes de enviarla.** Queda en la fila reservada (`message_templates.category`) antes del POST.
  `allow_category_change=False`: si Meta quisiera recategorizar la rechaza en vez de cambiarla en silencio (el dueño
  eligió la categoría y el costo cambia con ella).
- **El completado no pisa al webhook.** Entre la respuesta de Meta y el guardado local puede llegar el webhook de
  estado; sin `meta_template_id` en la fila no la encuentra y se descarta. El guardado solo completa una fila que
  sigue sin id y avanza el estado solo si sigue en `DRAFT`; si el aviso se perdió en esa ventana (milisegundos) lo
  corrige el sync horario.
- **Cupo de la WABA.** Cada versión gasta una de las plantillas que Meta permite por cuenta: con el contador ya
  sincronizado en `waba_accounts` lleno, no se llama a Meta (`template_quota_full`).
- **El estado es el de `message_templates`** (lo escribe el webhook de estado de plantillas que ya existe) y la
  versión lo lee por el vínculo `message_template_id`: no se duplica en la versión.
- **Errores como resultado.** `ResultadoPublicacion` con un `outcome` cerrado; el mensaje de Meta va acotado y solo
  al resultado y al log, nunca a la auditoría. Si falla, la versión publicada NO se revierte.
- **No toca la bandera `opciones_directas`**: publicar en Meta no envía nada a ningún cliente; la bandera gobierna
  envíos y toques.

Toda consulta filtra por `tenant_id` además de la RLS (defensa en profundidad); el candado de Redis lleva el negocio.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.db.models import (
    MessageTemplate,
    OptionTemplate,
    OptionTemplateVersion,
    WabaAccount,
    utcnow,
)
from core.db.session import get_session
from core.gateway.meta_errors import GraphError
from core.gateway.meta_templates import (
    TemplateRateLimitError,
    TemplateValidationError,
    find_template_by_name,
    post_template,
    validate_template_components,
)
from core.kernel.opciones import meta as m
from core.kernel.opciones.definicion import Definicion
from core.shared.audit import record_audit
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

ACCION_AUDITORIA = "option_meta_publish"
_CANDADO_S = 120
_ESTADOS_BORRADOS = ("DELETED", "PENDING_DELETION")
_TOPE_MOTIVO = 200
_TOPE_EJEMPLO = 60
# Compare-and-delete atómico: libera el candado solo si sigue siendo el de esta llamada.
_LIBERAR = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


def _clave_candado(tenant_id: uuid.UUID, version_id: uuid.UUID) -> str:
    return f"opt_meta_pub:{tenant_id}:{version_id}"


async def publicar_en_meta(
    tenant_id: uuid.UUID,
    version_id: uuid.UUID,
    connection_id: uuid.UUID,
    categoria: str,
    *,
    idioma: str = m.IDIOMA_POR_DEFECTO,
    ejemplos: dict[str, str] | None = None,
    actor: str = "owner",
) -> m.ResultadoPublicacion:
    """Crea (o concilia) la plantilla `<clave>_v<n>` de Meta para una versión publicada, en la WABA de la conexión.
    Nunca lanza: devuelve un `ResultadoPublicacion`. `ejemplos` da el valor de muestra de cada variable del cuerpo
    (Meta los exige al revisar). Repetir la llamada es seguro: ver el docstring del módulo."""
    from core.gateway.event_notices import clean_param

    ejemplos = {str(k)[:120]: clean_param(str(v))[:_TOPE_EJEMPLO] for k, v in (ejemplos or {}).items()}  # texto libre → acotado
    categoria_n = m.categoria_valida(categoria)
    if categoria_n is None:
        resultado = m.ResultadoPublicacion(False, m.CATEGORIA_INVALIDA)
        await _rastro(tenant_id, version_id, actor, resultado)
        return resultado
    try:
        async with tenant_scope(tenant_id):
            resultado = await _publicar(tenant_id, version_id, connection_id, categoria_n, idioma, ejemplos)
    except Exception as exc:
        log.error("option_meta_publish_internal_error", tenant_id=str(tenant_id), error=type(exc).__name__)
        resultado = m.ResultadoPublicacion(False, m.ERROR_INTERNO, motivo=type(exc).__name__)
    await _rastro(tenant_id, version_id, actor, resultado)
    return resultado


async def _publicar(
    tenant_id: uuid.UUID, version_id: uuid.UUID, connection_id: uuid.UUID, categoria: str, idioma: str,
    ejemplos: dict[str, str],
) -> m.ResultadoPublicacion:
    from core.admin.meta_numbers_service import resolve_waba_and_credentials
    from core.memory.working import get_redis

    if not m.idioma_valido(idioma):
        return m.ResultadoPublicacion(False, m.IDIOMA_INVALIDO)

    # 1. Lo que se va a mandar, validado antes de reservar o llamar a nadie.
    async with get_session() as session:
        fila = (
            await session.execute(
                select(OptionTemplateVersion, OptionTemplate.key)
                .join(
                    OptionTemplate,
                    (OptionTemplate.tenant_id == OptionTemplateVersion.tenant_id)
                    & (OptionTemplate.id == OptionTemplateVersion.option_template_id),
                )
                .where(OptionTemplateVersion.tenant_id == tenant_id, OptionTemplateVersion.id == version_id)
            )
        ).first()
    if fila is None:
        return m.ResultadoPublicacion(False, m.VERSION_NO_ENCONTRADA)
    version, clave = fila
    if version.retired_at is not None:
        return m.ResultadoPublicacion(False, m.VERSION_RETIRADA)
    nombre = m.nombre_en_meta(clave, version.version_n)
    try:
        definicion = Definicion.model_validate(version.definition)
        componentes = m.componentes_para_publicar(definicion, ejemplos)
        validate_template_components(categoria, componentes, m.FORMATO_PARAMETROS)
    except (ValidationError, ValueError, TemplateValidationError) as exc:
        motivo = str(exc)[:_TOPE_MOTIVO] if isinstance(exc, (ValueError, TemplateValidationError)) else "definicion_invalida"
        return m.ResultadoPublicacion(False, m.DEFINICION_INVALIDA, nombre=nombre, categoria=categoria, motivo=motivo)

    # 2. La conexión y su WABA (las credenciales son las de ESA conexión).
    try:
        connection, waba, creds = await resolve_waba_and_credentials(connection_id, tenant_id)
    except Exception as exc:
        log.error("option_meta_publish_resolucion_fallida", tenant_id=str(tenant_id), error=type(exc).__name__)
        connection = waba = creds = None
    if connection is None or waba is None or creds is None:
        return m.ResultadoPublicacion(False, m.CONEXION_NO_LISTA, nombre=nombre, categoria=categoria)

    # 3. Cupo de plantillas de la cuenta (contadores del último sync; None = sin dato, se intenta).
    if (
        waba.message_template_limit is not None
        and waba.message_template_count is not None
        and waba.message_template_count >= waba.message_template_limit
    ):
        return m.ResultadoPublicacion(False, m.LIMITE_DE_CUPO, nombre=nombre, categoria=categoria)

    # 4. Una publicación a la vez por (negocio, versión). Sin Redis no se publica (fail-closed).
    token = uuid.uuid4().hex
    candado = _clave_candado(tenant_id, version_id)
    redis = get_redis()
    try:
        tomado = await redis.set(candado, token, ex=_CANDADO_S, nx=True)
    except Exception as exc:
        log.error("option_meta_publish_sin_candado", tenant_id=str(tenant_id), error=type(exc).__name__)
        return m.ResultadoPublicacion(False, m.EN_CURSO, nombre=nombre, categoria=categoria, motivo="sin_candado")
    if not tomado:
        return m.ResultadoPublicacion(False, m.EN_CURSO, nombre=nombre, categoria=categoria)
    try:
        return await _con_candado(
            tenant_id, version, definicion, nombre, categoria, idioma, componentes, connection, waba, creds
        )
    finally:
        try:
            await redis.eval(_LIBERAR, 1, candado, token)  # solo el propio: uno vencido no se borra
        except Exception:
            log.warning("option_meta_publish_candado_sin_liberar", tenant_id=str(tenant_id))


def _resultado(fila: MessageTemplate, outcome: str, *, motivo: str = "") -> m.ResultadoPublicacion:
    return m.ResultadoPublicacion(
        outcome in (m.PUBLICADA, m.YA_PUBLICADA), outcome, nombre=fila.name, estado=fila.status, categoria=fila.category,
        message_template_id=fila.id, meta_template_id=fila.meta_template_id, motivo=motivo,
    )


async def _con_candado(
    tenant_id: uuid.UUID, version: OptionTemplateVersion, definicion: Definicion, nombre: str, categoria: str,
    idioma: str, componentes: list[dict[str, Any]], connection: Any, waba: WabaAccount, creds: Any,
) -> m.ResultadoPublicacion:
    # 5. Reservar (o releer) la fila: la clave de idempotencia y el estado persistida, antes de llamar a Meta.
    reservada = await _reservar(tenant_id, version.id, waba.id, nombre, idioma, categoria, componentes, definicion)
    if isinstance(reservada, m.ResultadoPublicacion):
        return reservada
    fila, nueva = reservada
    if fila.meta_template_id:
        return _resultado(fila, m.YA_PUBLICADA)
    fila_id = fila.id

    # 6. Una fila que ya existía sin id es un intento anterior de resultado desconocido (corte, timeout, caída del
    # worker entre la reserva y la respuesta): se pregunta a Meta por ESE nombre ANTES de crear.
    if not nueva:
        try:
            en_meta = await find_template_by_name(
                waba.waba_id, creds.access_token, creds.app_secret, creds.graph_api_version, connection.id,
                name=nombre, language=idioma,
            )
        except GraphError as exc:
            return _resultado(fila, m.ERROR_DE_RED, motivo=_motivo(exc))
        if en_meta is not None and en_meta.get("id"):
            try:
                adoptada = await _completar(
                    tenant_id, fila_id, str(en_meta["id"]), str(en_meta.get("status") or "PENDING"), en_meta.get("category")
                )
            except IntegrityError:
                return await _fallo(tenant_id, fila_id, m.ID_REPETIDO, "meta_id_taken", marcar=True)
            return _resultado(adoptada, m.YA_PUBLICADA)
        # Meta no la tiene: nada se creó, así que la solicitud es la de ESTA llamada (categoría y componentes).
        await _actualizar_reserva(tenant_id, fila_id, categoria, componentes)

    # 7. Crear en Meta y completar la fila reservada.
    try:
        meta_id, estado = await post_template(
            waba.waba_id, creds.access_token, creds.app_secret, creds.graph_api_version, connection.id, tenant_id,
            name=nombre, language=idioma, category=categoria, components=componentes,
            parameter_format=m.FORMATO_PARAMETROS, allow_category_change=False,
        )
    except TemplateRateLimitError as exc:
        return await _fallo(tenant_id, fila_id, m.LIMITE_DE_CREACION, str(exc), marcar=False)
    except TemplateValidationError as exc:
        return await _fallo(tenant_id, fila_id, m.DEFINICION_INVALIDA, str(exc), marcar=False)
    except GraphError as exc:
        if exc.category == "RATE_LIMIT":
            return await _fallo(tenant_id, fila_id, m.LIMITE_DE_CREACION, _motivo(exc), marcar=True)
        if exc.category == "AUTH":
            return await _fallo(tenant_id, fila_id, m.CONEXION_NO_LISTA, _motivo(exc), marcar=True)
        # `TRANSIENT` (corte, timeout, 5xx): no se sabe si Meta la creó. Las demás: Meta recibió y rechazó.
        salida = m.ERROR_DE_RED if exc.is_transient else m.RECHAZADA_POR_META
        return await _fallo(tenant_id, fila_id, salida, _motivo(exc), marcar=True)
    try:
        completada = await _completar(tenant_id, fila_id, meta_id, estado, None)
    except IntegrityError:
        # `uq_message_templates_tenant_meta_id`: ese id ya está en OTRA fila del negocio. No se repite solo.
        return await _fallo(tenant_id, fila_id, m.ID_REPETIDO, "meta_id_taken", marcar=True)
    return _resultado(completada, m.PUBLICADA)


async def _reservar(
    tenant_id: uuid.UUID, version_id: uuid.UUID, waba_account_id: uuid.UUID, nombre: str, idioma: str,
    categoria: str, componentes: list[dict[str, Any]], definicion: Definicion,
) -> tuple[MessageTemplate, bool] | m.ResultadoPublicacion:
    """`(fila, nueva)`: la fila reservada de ESTA versión (creándola si no existe) y su vínculo, en una
    transacción. Devuelve un resultado cuando hay un conflicto que impide publicar."""
    try:
        async with get_session() as session:
            version = (
                await session.execute(
                    select(OptionTemplateVersion)
                    .where(OptionTemplateVersion.tenant_id == tenant_id, OptionTemplateVersion.id == version_id)
                    .with_for_update()
                )
            ).scalar_one()
            if version.message_template_id is not None:
                ligada = (
                    await session.execute(
                        select(MessageTemplate).where(
                            MessageTemplate.tenant_id == tenant_id, MessageTemplate.id == version.message_template_id
                        )
                    )
                ).scalar_one_or_none()
                if ligada is not None:
                    if ligada.waba_account_id != waba_account_id:
                        return m.ResultadoPublicacion(
                            False, m.OTRA_CUENTA, nombre=ligada.name, estado=ligada.status, categoria=ligada.category,
                            message_template_id=ligada.id,
                        )
                    if ligada.status in _ESTADOS_BORRADOS:
                        return _resultado(ligada, m.PLANTILLA_BORRADA)
                    session.expunge(ligada)
                    return ligada, False
            existente = (
                await session.execute(
                    select(MessageTemplate).where(
                        MessageTemplate.tenant_id == tenant_id,
                        MessageTemplate.waba_account_id == waba_account_id,
                        MessageTemplate.name == nombre,
                        MessageTemplate.language == idioma,
                    )
                )
            ).scalar_one_or_none()
            if existente is not None:
                # El nombre ya existe en la cuenta sin estar ligado a esta versión: lo creó el sync, otra
                # publicación que no llegó a ligar, el dueño a mano, o una clave borrada y recreada (`_v1` otra
                # vez). Solo se adopta si dice EXACTAMENTE lo que esta versión diría; si no, no se pisa nada ajeno.
                if existente.status in _ESTADOS_BORRADOS or not m.mismos_textos(existente.components, definicion):
                    return _resultado(existente, m.NOMBRE_OCUPADO)
                version.message_template_id = existente.id
                session.expunge(existente)
                return existente, False
            fila = MessageTemplate(
                tenant_id=tenant_id, waba_account_id=waba_account_id, name=nombre, language=idioma,
                category=categoria, parameter_format=m.FORMATO_PARAMETROS, status="DRAFT", components=componentes,
            )
            session.add(fila)
            await session.flush()
            version.message_template_id = fila.id
            session.expunge(fila)
            return fila, True
    except IntegrityError as exc:
        if "uq_message_templates_waba_name_lang" not in str(exc):
            raise  # otra restricción (p. ej. una FK): no es una carrera, la atrapa `publicar_en_meta` como error interno
        # Carrera perdida contra otra reserva del mismo nombre: el reintento relee la ganadora.
        return m.ResultadoPublicacion(False, m.EN_CURSO, nombre=nombre, categoria=categoria)


async def _actualizar_reserva(
    tenant_id: uuid.UUID, fila_id: uuid.UUID, categoria: str, componentes: list[dict[str, Any]]
) -> None:
    """Meta no tiene la plantilla: la fila `DRAFT` sin id pasa a describir lo que se va a pedir ahora."""
    async with get_session() as session:
        fila = (
            await session.execute(
                select(MessageTemplate)
                .where(MessageTemplate.tenant_id == tenant_id, MessageTemplate.id == fila_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if fila is not None and fila.meta_template_id is None:
            fila.category = categoria
            fila.components = componentes
            fila.updated_at = utcnow()


async def _completar(
    tenant_id: uuid.UUID, fila_id: uuid.UUID, meta_id: str | None, estado: str, categoria: str | None
) -> MessageTemplate:
    """Guarda lo que respondió Meta SOLO si la fila sigue sin id, y avanza el estado solo si sigue en `DRAFT`: si el
    webhook o el sync ya la completaron, no se pisa su estado con el `PENDING` de la respuesta de creación."""
    async with get_session() as session:
        fila = (
            await session.execute(
                select(MessageTemplate)
                .where(MessageTemplate.tenant_id == tenant_id, MessageTemplate.id == fila_id)
                .with_for_update()
            )
        ).scalar_one()
        if fila.meta_template_id is None:
            fila.meta_template_id = meta_id
            if fila.status == "DRAFT":
                fila.status = estado
            if categoria:
                fila.category = categoria
            fila.sync_error = None
            fila.synced_at = utcnow()
            fila.updated_at = utcnow()
            await session.flush()  # antes de soltar el objeto: un cambio pendiente de una fila expulsada se pierde
        session.expunge(fila)
        return fila


async def _leer(tenant_id: uuid.UUID, fila_id: uuid.UUID) -> MessageTemplate | None:
    async with get_session() as session:
        fila = (
            await session.execute(
                select(MessageTemplate).where(MessageTemplate.tenant_id == tenant_id, MessageTemplate.id == fila_id)
            )
        ).scalar_one_or_none()
        if fila is not None:
            session.expunge(fila)
        return fila


async def _fallo(
    tenant_id: uuid.UUID, fila_id: uuid.UUID, outcome: str, motivo: str, *, marcar: bool
) -> m.ResultadoPublicacion:
    """Deja el error en la fila reservada (`sync_error`, visible en el panel) y devuelve el resultado. La fila sigue
    en `DRAFT`: el reintento la completa. `marcar=False` cuando no se llegó a llamar a Meta."""
    motivo = motivo[:_TOPE_MOTIVO]
    async with get_session() as session:
        fila = (
            await session.execute(
                select(MessageTemplate).where(MessageTemplate.tenant_id == tenant_id, MessageTemplate.id == fila_id)
            )
        ).scalar_one_or_none()
        if fila is not None:
            if marcar:
                fila.sync_error = motivo or outcome
                fila.synced_at = utcnow()
                await session.flush()  # antes de soltar el objeto: un cambio pendiente de una fila expulsada se pierde
            session.expunge(fila)
    if fila is None:
        return m.ResultadoPublicacion(False, outcome, motivo=motivo)
    return _resultado(fila, outcome, motivo=motivo)


def _motivo(exc: Exception) -> str:
    return str(getattr(exc, "message", exc))[:_TOPE_MOTIVO]


async def _rastro(tenant_id: uuid.UUID, version_id: uuid.UUID, actor: str, r: m.ResultadoPublicacion) -> None:
    """Un log por publicación, la métrica y la auditoría con ids y claves (el motivo de Meta solo va al log)."""
    from core.kernel.opciones import metricas

    campos = {
        "tenant_id": str(tenant_id), "version_id": str(version_id), "nombre": r.nombre, "outcome": r.outcome,
        "estado": r.estado, "categoria": r.categoria, "motivo": r.motivo,
    }
    if r.ok:
        log.info("option_meta_publish", **campos)
    else:
        log.warning("option_meta_publish", **campos)
    metricas.publicacion_meta(r.outcome)
    try:
        async with tenant_scope(tenant_id):
            await record_audit(
                actor=f"{actor}"[:120],
                action=ACCION_AUDITORIA,
                input_data={"version_id": str(version_id), "nombre": r.nombre, "categoria": r.categoria},
                output_data={"outcome": r.outcome, "estado": r.estado, "message_template_id": str(r.message_template_id) if r.message_template_id else None},
            )
    except Exception:
        log.warning("option_meta_publish_auditoria_fallida", tenant_id=str(tenant_id))


@dataclass(frozen=True)
class EstadoMeta:
    """Lo que Meta dice de la plantilla ligada a una versión. Vive en `message_templates` (lo escribe el webhook
    de estado y el de categoría): la versión solo lo lee, no lo duplica."""

    message_template_id: uuid.UUID
    nombre: str
    estado: str
    categoria: str | None
    categoria_anterior: str | None
    motivo_rechazo: str | None
    recomendacion: str | None
    error_de_publicacion: str | None


async def estado_de_publicacion(tenant_id: uuid.UUID, version_ids: list[uuid.UUID]) -> dict[uuid.UUID, EstadoMeta]:
    """`version_id → EstadoMeta` de las versiones que tienen plantilla ligada, en UNA consulta (sin N+1). La
    categoría sale de `message_templates` y no de la publicación: `template_category_update` puede cambiarla. Una
    versión sin plantilla de Meta no aparece. Para que la lea el listado del panel (HU-OPC-05/06)."""
    if not version_ids:
        return {}
    async with tenant_scope(tenant_id), get_session() as session:
        filas = (
            await session.execute(
                select(OptionTemplateVersion.id, MessageTemplate)
                .join(
                    MessageTemplate,
                    (MessageTemplate.tenant_id == OptionTemplateVersion.tenant_id)
                    & (MessageTemplate.id == OptionTemplateVersion.message_template_id),
                )
                .where(OptionTemplateVersion.tenant_id == tenant_id, OptionTemplateVersion.id.in_(version_ids))
            )
        ).all()
    return {
        vid: EstadoMeta(
            mt.id, mt.name, mt.status, mt.category, mt.previous_category, mt.rejected_reason,
            mt.rejection_recommendation, mt.sync_error,
        )
        for vid, mt in filas
    }
