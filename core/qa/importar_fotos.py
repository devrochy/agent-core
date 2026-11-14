"""Importar el catálogo desde fotos: una foto = una fila de la precarga (ADR-0194 §2 y §3).

Una petición = una foto, sin estado en el servidor ni Celery: el cliente manda las fotos de a una,
muestra el progreso y se detiene si llega `plan_limit_creditos`. Por foto:

1. **Guardar** la imagen con el perfil `producto` (valida tipo, firma y tamaño). El nombre es el
   sha256 del contenido, así que la misma foto da siempre la misma `image_key`.
2. **Idempotencia:** si el ledger ya tiene una fila `vision` del negocio con esa `image_key`, se
   responde desde ella (`result.salida`) sin llamar ni cobrar otra vez.
3. **Cotizar y verificar:** `cotizar('vision', 1, key_mode)` y `alcanza` bajo el advisory lock de
   créditos; si no alcanza, 409 `plan_limit_creditos` y no se llama al proveedor. La verificación
   no retiene el lock durante la llamada: dos fotos concurrentes pueden pasar con el mismo saldo
   (como mucho una foto de más, ADR-0194 §3); dos envíos simultáneos de la MISMA foto también pueden
   cobrarse dos veces (misma carrera). La foto se guarda antes de verificar el saldo: con un 409 queda
   en disco sin producto (la limpieza llega con el registro de archivos, `stored_files`).
4. **Reconocer** con el helper de visión (`core.ia.estructurado.reconocer_imagen`, playbook del kernel
   `reconocer_foto`, esquema de la plantilla) y registrar el consumo real con créditos.
5. Devolver la fila con `desde_foto` de la plantilla, para que el cliente la edite y la confirme con
   el resto de la precarga (`importar_datos.importar_filas`).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select

from core.db.models import UsageLedger
from core.db.session import get_session
from core.qa.datos_generados import ErrorDatosDePrueba
from core.qa.importar_datos import Destino, _destino_o_error
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

PLAYBOOK = "reconocer_foto"
PROPOSITO = "foto_catalogo"
MAX_TOKENS = 600
TOPE_FOTOS_COTIZACION = 500


@dataclass(frozen=True)
class FotoGuardada:
    key: str
    mimetype: str
    url: str


def _destino_con_fotos(d: Destino) -> Destino:
    if not d.plantilla.importa_fotos:
        raise ErrorDatosDePrueba(409, "sin_fotos", "Este agente no importa datos desde fotos.")
    return d


def _config_vision(app_settings: Any) -> Any:
    from core.media.config import MediaConfigError, resolve_vision_config

    try:
        return resolve_vision_config(app_settings)
    except MediaConfigError as exc:
        raise ErrorDatosDePrueba(
            409, "vision_no_disponible", "No hay un proveedor para reconocer fotos configurado."
        ) from exc


async def _app_settings(tenant_id: uuid.UUID) -> Any:
    from core.ia.estructurado import _app_settings as leer

    return await leer(tenant_id)


async def cotizacion(tenant_id: uuid.UUID, agent_id: uuid.UUID, fotos: int) -> dict[str, Any]:
    """Cuánto cuestan `fotos` fotos y si el crédito del mes alcanza. No escribe nada."""
    from core.billing.creditos import alcanza, cotizar, creditos_enteros
    from core.billing.enforcement import QuotaExceeded

    _destino_con_fotos(await _destino_o_error(tenant_id, agent_id))
    if not 1 <= fotos <= TOPE_FOTOS_COTIZACION:
        raise ErrorDatosDePrueba(422, "fotos_invalidas", f"Cotiza entre 1 y {TOPE_FOTOS_COTIZACION} fotos.")
    config = _config_vision(await _app_settings(tenant_id))
    total = cotizar("vision", fotos, config.key_mode)
    async with tenant_scope(tenant_id), get_session() as session:
        # Toma el advisory lock de créditos (transacción corta); con el bloqueo apagado `alcanza`
        # no lanza, así que «alcanza» se calcula del saldo para que la cotización diga la verdad.
        try:
            saldo = await alcanza(session, tenant_id, total)
            usado, limite = saldo.usado, saldo.limite
            suficiente = limite is None or usado + total <= limite
        except QuotaExceeded as exc:
            usado, limite, suficiente = Decimal(exc.used), exc.limit, False
    disponible = None if limite is None else max(int(limite) - creditos_enteros(usado), 0)
    return {
        "fotos": fotos,
        "creditos": total,
        "por_foto": cotizar("vision", 1, config.key_mode),
        "llave_propia": config.key_mode == "byok",
        "usado": creditos_enteros(usado),
        "limite": limite,
        "disponible": disponible,
        "alcanza": suficiente,
    }


async def _guardar(tenant_id: uuid.UUID, contenido: bytes, mimetype: str | None, nombre: str | None) -> FotoGuardada:
    import hashlib

    from core import almacenamiento
    from core.shared.tenant import tenant_slug

    slug = await tenant_slug(tenant_id)
    if slug is None:
        raise ErrorDatosDePrueba(404, "not_found", "Negocio no encontrado.")
    try:
        ref = almacenamiento.guardar(
            "producto",
            contenido,
            mimetype=mimetype,
            tenant_slug=slug,
            nombre=nombre,
            nombre_base=hashlib.sha256(contenido).hexdigest(),
        )
    except almacenamiento.StorageInvalido as exc:
        raise ErrorDatosDePrueba(422, f"foto_{exc.codigo}", str(exc)) from exc
    return FotoGuardada(key=ref.key, mimetype=ref.mimetype, url=ref.url)


async def _cobro_previo(tenant_id: uuid.UUID, image_key: str) -> dict[str, Any] | None:
    """`result` de la fila `vision` que ya se cobró por esta foto, o `None`."""
    async with tenant_scope(tenant_id), get_session() as session:
        fila = (
            await session.execute(
                select(UsageLedger.result)
                .where(
                    UsageLedger.tenant_id == tenant_id,
                    UsageLedger.kind == "vision",
                    UsageLedger.result["image_key"].astext == image_key,
                )
                .order_by(UsageLedger.created_at.desc())
                .limit(1)
            )
        ).first()
    if fila is None:
        return None
    return dict(fila[0] or {})


async def _verificar_saldo(tenant_id: uuid.UUID, key_mode: str) -> int:
    from core.billing.creditos import alcanza, cotizar

    creditos = cotizar("vision", 1, key_mode)  # type: ignore[arg-type]
    async with tenant_scope(tenant_id), get_session() as session:
        await alcanza(session, tenant_id, creditos)  # QuotaExceeded → 409 plan_limit_creditos
    return creditos


def _entrada(negocio: Any, d: Destino) -> str:
    assert d.plantilla.esquema_foto is not None
    return json.dumps(
        {
            "negocio": {"descripcion": negocio.descripcion, "moneda": negocio.moneda, "idioma": negocio.idioma},
            "que_importa": d.plantilla.titulo,
            "esquema": d.plantilla.esquema_foto.model_json_schema(),
        },
        ensure_ascii=False,
    )


def _respuesta(d: Destino, foto: FotoGuardada, salida: dict[str, Any], *, creditos: Decimal | None, repetida: bool) -> dict[str, Any]:
    assert d.plantilla.esquema_foto is not None and d.plantilla.desde_foto is not None
    try:
        datos = d.plantilla.esquema_foto.model_validate(salida)
    except ValidationError as exc:  # salida guardada con un esquema anterior
        raise ErrorDatosDePrueba(
            422, "foto_no_reconocida", "No pudimos usar el resultado de esta foto. Prueba con otra."
        ) from exc
    return {
        "image_key": foto.key,
        "image_url": foto.url,
        "valores": d.plantilla.desde_foto(datos, foto.key),
        "creditos": None if creditos is None else float(creditos),
        "repetida": repetida,
    }


async def procesar_foto(
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    contenido: bytes,
    *,
    mimetype: str | None,
    nombre: str | None,
) -> dict[str, Any]:
    """Una foto → una fila propuesta. Ver el docstring del módulo."""
    from core.ia.estructurado import ErrorFormato, ErrorProveedor, Imagen, reconocer_imagen
    from core.qa.datos_generados import _negocio

    d = _destino_con_fotos(await _destino_o_error(tenant_id, agent_id))
    assert d.plantilla.esquema_foto is not None
    foto = await _guardar(tenant_id, contenido, mimetype, nombre)

    previo = await _cobro_previo(tenant_id, foto.key)
    if previo is not None:
        if isinstance(previo.get("salida"), dict):
            return _respuesta(d, foto, previo["salida"], creditos=Decimal(0), repetida=True)
        raise ErrorDatosDePrueba(
            422, "foto_no_reconocida", "Ya intentamos con esta foto y no se reconoció el producto. Prueba con otra."
        )

    config = _config_vision(await _app_settings(tenant_id))
    await _verificar_saldo(tenant_id, config.key_mode)
    try:
        reconocido = await reconocer_imagen(
            tenant_id,
            playbook=PLAYBOOK,
            esquema=d.plantilla.esquema_foto,
            imagen=Imagen(contenido=contenido, mime_type=foto.mimetype),
            entrada=_entrada(await _negocio(tenant_id), d),
            kind="vision",
            result={
                "proposito": PROPOSITO,
                "image_key": foto.key,
                "agent_id": str(agent_id),
                "plantilla": d.plantilla.id,
            },
            max_tokens=MAX_TOKENS,
            config=config,
            guardar_salida=True,
        )
    except ErrorProveedor as exc:
        log.warning("foto_catalogo_proveedor_fallo", error=exc.causa, tenant_id=str(tenant_id))
        raise ErrorDatosDePrueba(
            503, "vision_no_respondio", "No pudimos reconocer la foto ahora. Inténtalo de nuevo; no se cobró."
        ) from exc
    except ErrorFormato as exc:
        log.info("foto_catalogo_no_reconocida", error=exc.causa, tenant_id=str(tenant_id))
        raise ErrorDatosDePrueba(
            422, "foto_no_reconocida", "No se reconoció un producto en la foto; se cobró igual porque el proveedor ya la procesó. Prueba con otra más clara."
        ) from exc
    if reconocido.consumo.credits is None:
        # Sin fila en el ledger no hay cobro ni idempotencia: devolver la fila daría visión gratis
        # en cada reintento mientras la escritura siga fallando.
        raise ErrorDatosDePrueba(
            503, "vision_no_respondio", "No pudimos registrar la foto. Inténtalo de nuevo en un momento."
        )
    log.info("foto_catalogo_reconocida", tenant_id=str(tenant_id), agent_id=str(agent_id))
    return _respuesta(
        d,
        foto,
        reconocido.datos.model_dump(mode="json"),
        creditos=reconocido.consumo.credits,
        repetida=False,
    )


__all__ = ["cotizacion", "procesar_foto"]
