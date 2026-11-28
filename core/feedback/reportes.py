"""Envío y lectura de reportes del cliente (HU-FB-03, ADR-0213 §5, §8 y §9).

- `enviar_reporte`: idempotente por `Idempotency-Key` (obligatoria). Orden: replay → validar TODO (campos y
  adjuntos, sin escribir nada) → cupo → guardar los archivos → una transacción con el reporte y sus adjuntos. Si
  algo falla después de escribir archivos, se borran los de ese envío. Dos envíos simultáneos con la misma clave: el
  segundo choca con el índice único, borra sus archivos y devuelve el reporte del primero.
- `listar_reportes`: los míos, o los de todo el negocio para el owner (`alcance=negocio`), por cursor.
- `ver_reporte`: autor u owner; si no, 404 (nunca 403). Los adjuntos salen con URL firmada de vida corta.

El cliente nunca recibe `revisado_por`, `nota_interna` ni a qué reporte apunta `duplicado_de` (ADR-0213 §8).
Todo dentro de `tenant_scope` del negocio: la RLS forzada aplica a cada lectura y escritura.
"""

from __future__ import annotations

import base64
import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError

from core import almacenamiento
from core.almacenamiento.perfiles import validar as validar_archivo
from core.db.models import FeedbackAttachment, FeedbackReport
from core.db.session import get_session
from core.feedback import catalogo
from core.security import rate_limit
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope, tenant_slug

log = get_logger(__name__)

# Vida de la URL firmada de un adjunto: se emite solo después de verificar el acceso.
URL_TTL_ADJUNTO = 10 * 60
CUPO_AUTOR = (5, 60 * 60)  # 5 por hora por usuario
CUPO_NEGOCIO = (30, 24 * 60 * 60)  # 30 por día por negocio
RETRY_ALMACENAMIENTO = 30

_CLAVE = re.compile(r"[A-Za-z0-9_.:-]{8,128}")
_UQ_IDEMPOTENCIA = "uq_feedback_reports_idempotencia"

Alcance = Literal["mios", "negocio"]


class ErrorFeedback(Exception):
    """Error de dominio con status y código estables; el router lo traduce a `ApiError`."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        self.headers = headers


@dataclass(frozen=True)
class AdjuntoEntrada:
    """Un archivo tal como llegó (ya leído hasta `MAX_BYTES_POR_ARCHIVO + 1`)."""

    nombre: str | None
    mimetype: str | None
    contenido: bytes


@dataclass
class _AdjuntoValido:
    orden: int
    nombre: str | None
    mimetype: str
    contenido: bytes
    sha256: str


@dataclass
class _Envio:
    report_id: uuid.UUID
    guardados: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- validación


def validar_clave(clave: str | None) -> str:
    valor = (clave or "").strip()
    if not valor:
        raise ErrorFeedback(400, "idempotency_key_requerida", "Falta la cabecera Idempotency-Key.")
    if not _CLAVE.fullmatch(valor):
        raise ErrorFeedback(
            400, "idempotency_key_invalida", "La Idempotency-Key debe tener de 8 a 128 caracteres sin espacios."
        )
    return valor


def _validar_campos(tipo: str, area: str, titulo: str, descripcion: str) -> tuple[str, str, str]:
    if tipo not in catalogo.TIPOS:
        raise ErrorFeedback(422, "tipo_invalido", "El tipo debe ser error, mejora o idea.")
    if area not in catalogo.areas():
        raise ErrorFeedback(422, "area_invalida", "Elige un área de la lista.")
    titulo_limpio = titulo.strip()
    descripcion_limpia = descripcion.strip()
    if not 3 <= len(titulo_limpio) <= 120:
        raise ErrorFeedback(422, "titulo_invalido", "El título debe tener de 3 a 120 caracteres.")
    if not 10 <= len(descripcion_limpia) <= 2000:
        raise ErrorFeedback(422, "descripcion_invalida", "La descripción debe tener de 10 a 2000 caracteres.")
    return area, titulo_limpio, descripcion_limpia


def _nombre(nombre: str | None) -> str | None:
    if not nombre:
        return None
    limpio = "".join(c for c in nombre if c.isprintable()).strip()
    return limpio[:255] or None


def demasiados_adjuntos() -> ErrorFeedback:
    return ErrorFeedback(
        422, "demasiados_adjuntos", f"Puedes adjuntar hasta {catalogo.MAX_ADJUNTOS} archivos por reporte."
    )


def validar_adjuntos(adjuntos: list[AdjuntoEntrada]) -> list[_AdjuntoValido]:
    """Valida TODOS los adjuntos antes de guardar ninguno: cantidad, tamaño, total, firma y repetidos."""
    if len(adjuntos) > catalogo.MAX_ADJUNTOS:
        raise demasiados_adjuntos()
    perfil = almacenamiento.PERFILES["feedback"]
    total = 0
    validos: list[_AdjuntoValido] = []
    vistos: set[str] = set()
    for orden, adjunto in enumerate(adjuntos):
        total += len(adjunto.contenido)
        if total > catalogo.MAX_BYTES_TOTAL:
            raise ErrorFeedback(
                413,
                "adjuntos_muy_grandes",
                f"Los adjuntos suman más de {catalogo.MAX_BYTES_TOTAL // (1024 * 1024)} MB.",
            )
        try:
            mimetype, _ = validar_archivo(perfil, adjunto.mimetype, adjunto.nombre, adjunto.contenido)
        except almacenamiento.StorageInvalido as exc:
            log.info("feedback.adjunto_rechazado", orden=orden, motivo=exc.codigo)
            if exc.codigo == "muy_grande":
                raise ErrorFeedback(413, "adjunto_muy_grande", str(exc), details={"adjunto": orden}) from exc
            raise ErrorFeedback(
                422, "adjunto_invalido", str(exc), details={"adjunto": orden, "motivo": exc.codigo}
            ) from exc
        sha = hashlib.sha256(adjunto.contenido).hexdigest()
        if sha in vistos:
            raise ErrorFeedback(422, "adjunto_repetido", "Adjuntaste el mismo archivo dos veces.", details={"adjunto": orden})
        vistos.add(sha)
        validos.append(_AdjuntoValido(orden, _nombre(adjunto.nombre), mimetype, adjunto.contenido, sha))
    return validos


# --------------------------------------------------------------------------- envío


async def _cupo(tenant_id: uuid.UUID, autor_admin_id: uuid.UUID) -> None:
    """5 por hora por usuario y 30 por día por negocio. Si Redis cae, deja pasar (`rate_limit` avisa con `warn`)."""
    limite, ventana = CUPO_AUTOR
    autor = rate_limit.Bucket(f"feedback:autor:{tenant_id}:{autor_admin_id}", limite, ventana)
    if await rate_limit.hit(autor, name="feedback_autor"):
        raise ErrorFeedback(
            429,
            "demasiados_reportes",
            f"Enviaste {limite} reportes en la última hora. Intenta más tarde.",
            headers={"Retry-After": str(ventana)},
        )
    limite, ventana = CUPO_NEGOCIO
    negocio = rate_limit.Bucket(f"feedback:negocio:{tenant_id}", limite, ventana)
    if await rate_limit.hit(negocio, name="feedback_negocio"):
        raise ErrorFeedback(
            429,
            "demasiados_reportes_del_negocio",
            f"El negocio envió {limite} reportes hoy. Intenta mañana.",
            headers={"Retry-After": str(ventana)},
        )


async def _por_clave(tenant_id: uuid.UUID, clave: str) -> FeedbackReport | None:
    async with get_session() as session:
        return (
            await session.execute(
                select(FeedbackReport).where(
                    FeedbackReport.tenant_id == tenant_id, FeedbackReport.idempotency_key == clave
                )
            )
        ).scalar_one_or_none()


async def _replay(tenant_id: uuid.UUID, autor_admin_id: uuid.UUID, clave: str) -> dict[str, Any] | None:
    """El reporte ya creado con esta clave, si es del mismo autor. Otro autor con la misma clave: 409."""
    previo = await _por_clave(tenant_id, clave)
    if previo is None:
        return None
    if previo.autor_admin_id != autor_admin_id:
        raise ErrorFeedback(409, "idempotency_key_en_uso", "Esa Idempotency-Key ya se usó para otro reporte.")
    log.info("feedback.replay", tenant_id=str(tenant_id), report_id=str(previo.id))
    return await _detalle(tenant_id, previo)


def _limpiar(envio: _Envio, slug: str) -> None:
    carpeta = almacenamiento.carpeta_de("feedback", slug)
    for key in envio.guardados:
        try:
            almacenamiento.borrar(key, dentro_de=carpeta)
        except OSError:
            log.warning("feedback.limpieza_fallo", report_id=str(envio.report_id))


async def enviar_reporte(
    *,
    tenant_id: uuid.UUID,
    autor_admin_id: uuid.UUID,
    idempotency_key: str | None,
    tipo: str,
    area: str,
    titulo: str,
    descripcion: str,
    adjuntos: list[AdjuntoEntrada],
) -> tuple[dict[str, Any], bool]:
    """Crea el reporte con su evidencia. Devuelve `(reporte, creado)`; `creado=False` es un replay."""
    clave = validar_clave(idempotency_key)
    async with tenant_scope(tenant_id):
        previo = await _replay(tenant_id, autor_admin_id, clave)
        if previo is not None:
            return previo, False

        area, titulo, descripcion = _validar_campos(tipo, area, titulo, descripcion)
        validos = validar_adjuntos(adjuntos)
        await _cupo(tenant_id, autor_admin_id)

        slug = await tenant_slug(tenant_id)
        if slug is None:
            raise ErrorFeedback(404, "not_found", "No encontrado.")
        envio = _Envio(report_id=uuid.uuid4())
        try:
            refs = []
            for adjunto in validos:
                ref = almacenamiento.guardar(
                    "feedback",
                    adjunto.contenido,
                    mimetype=adjunto.mimetype,
                    tenant_slug=slug,
                    subcarpeta=str(envio.report_id),
                )
                envio.guardados.append(ref.key)
                refs.append((adjunto, ref))
        except OSError as exc:
            _limpiar(envio, slug)
            log.error("feedback.almacenamiento_fallo", tenant_id=str(tenant_id), error=type(exc).__name__)
            raise ErrorFeedback(
                503,
                "almacenamiento_no_disponible",
                "No pudimos guardar los archivos. Intenta de nuevo en un momento.",
                headers={"Retry-After": str(RETRY_ALMACENAMIENTO)},
            ) from exc
        except BaseException:
            _limpiar(envio, slug)
            raise

        reporte = FeedbackReport(
            id=envio.report_id,
            tenant_id=tenant_id,
            autor_admin_id=autor_admin_id,
            tipo=tipo,
            area=area,
            titulo=titulo,
            descripcion=descripcion,
            estado="recibido",
            idempotency_key=clave,
        )
        try:
            async with get_session() as session:
                session.add(reporte)
                await session.flush()
                session.add_all(
                    FeedbackAttachment(
                        tenant_id=tenant_id,
                        report_id=envio.report_id,
                        orden=adjunto.orden,
                        storage_key=ref.key,
                        mimetype=ref.mimetype,
                        bytes=ref.bytes,
                        sha256=ref.sha256,
                        nombre_original=adjunto.nombre,
                    )
                    for adjunto, ref in refs
                )
                await session.flush()
        except IntegrityError as exc:
            _limpiar(envio, slug)
            if _UQ_IDEMPOTENCIA not in str(exc.orig):
                raise
            # Carrera: otro envío con la misma clave ganó entre la lectura y el INSERT.
            ganador = await _replay(tenant_id, autor_admin_id, clave)
            if ganador is None:
                raise
            return ganador, False
        except BaseException:
            _limpiar(envio, slug)
            raise

        log.info(
            "feedback.enviado",
            tenant_id=str(tenant_id),
            report_id=str(envio.report_id),
            tipo=tipo,
            area=area,
            adjuntos=len(refs),
        )
        return await _detalle(tenant_id, reporte), True


# --------------------------------------------------------------------------- lectura


def _cursor(ts: datetime, fila_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(f"{ts.isoformat()}|{fila_id}".encode()).decode()


def _parse_cursor(raw: str) -> tuple[datetime, uuid.UUID]:
    try:
        ts, _, fid = base64.urlsafe_b64decode(raw.encode()).decode().partition("|")
        return datetime.fromisoformat(ts), uuid.UUID(fid)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ErrorFeedback(400, "cursor_invalido", "El cursor de paginación no es válido.") from exc


def _base(reporte: FeedbackReport) -> dict[str, Any]:
    """Lo que ve el cliente: sin revisor, sin nota interna y sin el id del duplicado."""
    return {
        "id": reporte.id,
        "autor_admin_id": reporte.autor_admin_id,
        "tipo": reporte.tipo,
        "area": reporte.area,
        "titulo": reporte.titulo,
        "descripcion": reporte.descripcion,
        "estado": reporte.estado,
        "motivo_codigo": reporte.motivo_codigo,
        "motivo": reporte.motivo,
        "creditos_otorgados": reporte.creditos_otorgados,
        "created_at": reporte.created_at,
        "decidido_en": reporte.decidido_en,
    }


def _clave_del_reporte(key: str, slug: str | None, report_id: uuid.UUID) -> bool:
    """La firma es la única autorización de `/api/v1/files`: solo se firma una clave de la carpeta del reporte."""
    if slug is None or not almacenamiento.key_limpia(key):
        return False
    return key.startswith(f"{almacenamiento.carpeta_de('feedback', slug)}/{report_id}/")


async def _detalle(tenant_id: uuid.UUID, reporte: FeedbackReport) -> dict[str, Any]:
    slug = await tenant_slug(tenant_id)
    async with get_session() as session:
        adjuntos = (
            (
                await session.execute(
                    select(FeedbackAttachment)
                    .where(FeedbackAttachment.tenant_id == tenant_id, FeedbackAttachment.report_id == reporte.id)
                    .order_by(FeedbackAttachment.orden)
                )
            )
            .scalars()
            .all()
        )
    salida = _base(reporte)
    expira = datetime.now(UTC) + timedelta(seconds=URL_TTL_ADJUNTO)
    salida["adjuntos"] = [
        {
            "id": a.id,
            "mimetype": a.mimetype,
            "bytes": a.bytes,
            "nombre_original": a.nombre_original,
            "url": almacenamiento.url(a.storage_key, ttl_s=URL_TTL_ADJUNTO),
            "url_expira_en": expira,
        }
        for a in adjuntos
        if _clave_del_reporte(a.storage_key, slug, reporte.id)
    ]
    if len(salida["adjuntos"]) != len(adjuntos):
        log.warning("feedback.adjunto_fuera_de_carpeta", tenant_id=str(tenant_id), report_id=str(reporte.id))
    return salida


async def listar_reportes(
    *,
    tenant_id: uuid.UUID,
    admin_id: uuid.UUID,
    puede_ver_negocio: bool,
    alcance: Alcance = "mios",
    estado: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Página de reportes, del más nuevo al más viejo. `alcance=negocio` solo para el owner (403 si no)."""
    if alcance == "negocio" and not puede_ver_negocio:
        raise ErrorFeedback(403, "alcance_no_permitido", "Solo el propietario ve los reportes de todo el negocio.")
    consulta = select(FeedbackReport).where(FeedbackReport.tenant_id == tenant_id)
    if alcance == "mios":
        consulta = consulta.where(FeedbackReport.autor_admin_id == admin_id)
    if estado is not None:
        consulta = consulta.where(FeedbackReport.estado == estado)
    if cursor:
        ts, fid = _parse_cursor(cursor)
        consulta = consulta.where(
            or_(FeedbackReport.created_at < ts, and_(FeedbackReport.created_at == ts, FeedbackReport.id < fid))
        )
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            filas = list(
                (
                    await session.execute(
                        consulta.order_by(FeedbackReport.created_at.desc(), FeedbackReport.id.desc()).limit(limit + 1)
                    )
                )
                .scalars()
                .all()
            )
            hay_mas = len(filas) > limit
            filas = filas[:limit]
            conteo: dict[uuid.UUID, int] = {}
            if filas:
                conteo = {
                    rid: int(n)
                    for rid, n in (
                        await session.execute(
                            select(FeedbackAttachment.report_id, func.count())
                            .where(
                                FeedbackAttachment.tenant_id == tenant_id,
                                FeedbackAttachment.report_id.in_([f.id for f in filas]),
                            )
                            .group_by(FeedbackAttachment.report_id)
                        )
                    ).all()
                }
    return {
        "items": [{**_base(f), "adjuntos_total": conteo.get(f.id, 0)} for f in filas],
        "next_cursor": _cursor(filas[-1].created_at, filas[-1].id) if hay_mas and filas else None,
    }


async def ver_reporte(
    *, tenant_id: uuid.UUID, admin_id: uuid.UUID, puede_ver_negocio: bool, report_id: uuid.UUID
) -> dict[str, Any]:
    """Detalle con URLs firmadas. 404 si no existe, es de otro negocio o (sin ser owner) de otro usuario."""
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            reporte = (
                await session.execute(
                    select(FeedbackReport).where(FeedbackReport.tenant_id == tenant_id, FeedbackReport.id == report_id)
                )
            ).scalar_one_or_none()
        if reporte is None or (not puede_ver_negocio and reporte.autor_admin_id != admin_id):
            raise ErrorFeedback(404, "not_found", "No encontrado.")
        return await _detalle(tenant_id, reporte)
