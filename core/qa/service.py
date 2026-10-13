"""Servicio del centro de pruebas (SPEC-30, HU-QA-01/02, ADR-0133).

Todo pasa por aquí: importar el catálogo, la ejecución de cada tester (pasos,
notas, capturas), el informe, el historial, los ciclos y la configuración de
test/producción. Las tablas `qa_*` son de SISTEMA (sin RLS): se consultan sin
filtro de negocio porque no tienen ninguno; el acceso lo decide la sección `qa`.

Reglas de escritura:
- Solo se escribe sobre **la propia** ejecución del ciclo abierto y mientras esté
  `en_curso`; enviada o con el ciclo cerrado queda en solo lectura (409).
- Cada cambio deja un `qa_events` con quién, cuándo, antes y después.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.db.models import (
    Admin,
    QaAttachment,
    QaCatalogVersion,
    QaConfigStatus,
    QaCycle,
    QaEvent,
    QaItem,
    QaItemResult,
    QaRun,
    QaStepResult,
    utcnow,
)
from core.db.session import get_session
from core.qa import evidencias
from core.qa.catalogo import Catalogo, parse_catalogo
from core.qa.estados import (
    ESTADOS_CONFIG,
    ESTADOS_PASO,
    consolidar,
    estado_de_prueba,
    progreso,
)
from core.qa.informe import armar_informe, cuerpo_correo
from core.shared.logger import get_logger

log = get_logger(__name__)

MAX_NOTA = 4000


class QaError(Exception):
    """Error de dominio del centro de pruebas. `tipo`: invalido | no_encontrado | conflicto."""

    def __init__(self, tipo: str, mensaje: str) -> None:
        super().__init__(mensaje)
        self.tipo = tipo
        self.mensaje = mensaje


def ve_todas_las_ejecuciones(admin: Admin) -> bool:
    """El dueño (master) ve los informes de todos; el resto, solo los suyos."""
    return admin.role == "master"


def _nota(valor: str | None) -> str | None:
    if valor is None:
        return None
    texto = valor.strip()
    if len(texto) > MAX_NOTA:
        raise QaError("invalido", f"La nota supera {MAX_NOTA} caracteres.")
    return texto or None


def _iso(valor: datetime | None) -> str | None:
    return valor.isoformat() if valor else None


def _nombre(admin: Admin | None) -> str:
    if admin is None:
        return ""
    return admin.username or admin.email


# ------------------------------------------------------------------ serialización
def item_a_dict(item: QaItem) -> dict[str, Any]:
    return {
        "slug": item.slug,
        "seccion": item.seccion,
        "orden": item.orden,
        "modulo": item.modulo,
        "grupo": item.grupo,
        "titulo": item.titulo,
        "resumen": item.resumen,
        "contexto": item.contexto,
        "prioridad": item.prioridad,
        "plataforma": list(item.plataforma or []),
        "precondiciones": list(item.precondiciones or []),
        "datos": list(item.datos or []),
        "pasos": list(item.pasos or []),
        "esperado": item.esperado,
        "porque": item.porque,
        "como": item.como,
        "vars": list(item.vars or []),
        "quien": item.quien,
        "hu": list(item.hu or []),
        "origen": list(item.origen or []),
    }


def _adjunto_a_dict(adj: QaAttachment) -> dict[str, Any]:
    return {
        "id": str(adj.id),
        "paso": adj.paso,
        "nombre": adj.nombre_original or "captura",
        "mimetype": adj.mimetype,
        "tamano": adj.tamano,
        "subido_en": _iso(adj.subido_en),
    }


def _evento(
    session: AsyncSession,
    *,
    campo: str,
    admin_id: uuid.UUID | None,
    cycle_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    item_slug: str | None = None,
    paso: int | None = None,
    antes: str | None = None,
    despues: str | None = None,
) -> None:
    session.add(
        QaEvent(
            cycle_id=cycle_id,
            run_id=run_id,
            item_slug=item_slug,
            paso=paso,
            campo=campo,
            antes=antes,
            despues=despues,
            admin_id=admin_id,
        )
    )


# ------------------------------------------------------------------ consultas base
async def _ciclo_abierto(session: AsyncSession) -> QaCycle | None:
    return (
        await session.execute(select(QaCycle).where(QaCycle.cerrado_en.is_(None)))
    ).scalar_one_or_none()


async def _ultima_version(session: AsyncSession) -> QaCatalogVersion | None:
    return (
        await session.execute(
            select(QaCatalogVersion).order_by(QaCatalogVersion.importado_en.desc()).limit(1)
        )
    ).scalar_one_or_none()


async def _items(
    session: AsyncSession, version_id: uuid.UUID, seccion: str | None = None
) -> list[QaItem]:
    query = select(QaItem).where(QaItem.catalog_version_id == version_id)
    if seccion:
        query = query.where(QaItem.seccion == seccion)
    return list((await session.execute(query.order_by(QaItem.orden))).scalars().all())


async def _requerir_ciclo(session: AsyncSession) -> QaCycle:
    ciclo = await _ciclo_abierto(session)
    if ciclo is None:
        raise QaError("no_encontrado", "No hay un ciclo de pruebas abierto: importa el catálogo primero.")
    return ciclo


async def _prueba_del_ciclo(session: AsyncSession, ciclo: QaCycle, slug: str) -> QaItem:
    item = (
        await session.execute(
            select(QaItem).where(
                QaItem.catalog_version_id == ciclo.catalog_version_id,
                QaItem.slug == slug,
                QaItem.seccion == "pruebas",
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise QaError("no_encontrado", "Esa prueba no está en el catálogo del ciclo.")
    return item


async def _mi_run(session: AsyncSession, ciclo: QaCycle, admin_id: uuid.UUID) -> QaRun | None:
    return (
        await session.execute(
            select(QaRun).where(QaRun.cycle_id == ciclo.id, QaRun.admin_id == admin_id)
        )
    ).scalar_one_or_none()


async def _run_editable(session: AsyncSession, admin: Admin) -> tuple[QaCycle, QaRun]:
    ciclo = await _requerir_ciclo(session)
    run = await _mi_run(session, ciclo, admin.id)
    if run is None:
        raise QaError("no_encontrado", "Todavía no empezaste tu ejecución en este ciclo.")
    if run.estado != "en_curso":
        raise QaError("conflicto", "Tu ejecución ya fue enviada: queda en solo lectura.")
    return ciclo, run


async def _resultados_de_runs(
    session: AsyncSession, run_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, str]]:
    """`{run_id: {slug: estado}}` de varias ejecuciones."""
    if not run_ids:
        return {}
    filas = (
        await session.execute(
            select(QaItemResult.run_id, QaItemResult.item_slug, QaItemResult.estado).where(
                QaItemResult.run_id.in_(run_ids)
            )
        )
    ).all()
    out: dict[uuid.UUID, dict[str, str]] = defaultdict(dict)
    for run_id, slug, estado in filas:
        out[run_id][slug] = estado
    return out


async def _detalle_run(session: AsyncSession, run: QaRun) -> dict[str, dict[str, Any]]:
    """Resultados de una ejecución por slug, con pasos, notas y capturas."""
    detalle: dict[str, dict[str, Any]] = {}

    def entrada(slug: str) -> dict[str, Any]:
        return detalle.setdefault(
            slug, {"estado": "sin_correr", "nota": None, "pasos": {}, "adjuntos": []}
        )

    for res in (
        await session.execute(select(QaItemResult).where(QaItemResult.run_id == run.id))
    ).scalars():
        e = entrada(res.item_slug)
        e["estado"], e["nota"] = res.estado, res.nota
    for paso in (
        await session.execute(select(QaStepResult).where(QaStepResult.run_id == run.id))
    ).scalars():
        entrada(paso.item_slug)["pasos"][str(paso.paso)] = {"estado": paso.estado, "nota": paso.nota}
    for adj in (
        await session.execute(
            select(QaAttachment).where(QaAttachment.run_id == run.id).order_by(QaAttachment.subido_en)
        )
    ).scalars():
        entrada(adj.item_slug)["adjuntos"].append(_adjunto_a_dict(adj))
    return detalle


async def _run_a_dict(
    session: AsyncSession, run: QaRun, ciclo: QaCycle, *, con_detalle: bool = True
) -> dict[str, Any]:
    pruebas = await _items(session, ciclo.catalog_version_id, "pruebas")
    slugs = {p.slug for p in pruebas}
    detalle = await _detalle_run(session, run) if con_detalle else {}
    if con_detalle:
        estados = {s: d["estado"] for s, d in detalle.items() if s in slugs}
    else:
        estados = {
            s: e for s, e in (await _resultados_de_runs(session, [run.id])).get(run.id, {}).items() if s in slugs
        }
    tester = await session.get(Admin, run.admin_id)
    out: dict[str, Any] = {
        "id": str(run.id),
        "cycle_id": str(run.cycle_id),
        "ciclo_nombre": ciclo.nombre,
        "tester": {"id": str(run.admin_id), "nombre": _nombre(tester), "email": tester.email if tester else ""},
        "estado": run.estado,
        "editable": run.estado == "en_curso" and ciclo.cerrado_en is None,
        "comentario": run.comentario,
        "iniciada_en": _iso(run.iniciada_en),
        "actualizada_en": _iso(run.actualizada_en),
        "enviada_en": _iso(run.enviada_en),
        "progreso": progreso(estados, len(pruebas)),
    }
    if con_detalle:
        out["resultados"] = detalle
    return out


# ------------------------------------------------------------------ catálogo
@dataclass(slots=True)
class ResultadoImportacion:
    version_id: uuid.UUID
    version: int
    items: int
    creada: bool
    ciclo_id: uuid.UUID
    ciclo_nombre: str


async def importar_catalogo(
    contenido: str | bytes, *, admin_id: uuid.UUID | None, commit_docs: str | None = None
) -> ResultadoImportacion:
    """Importa el YAML. Idempotente por contenido; el ciclo abierto pasa a la versión nueva.

    Si no hay ciclo abierto, abre el primero. Los ciclos cerrados conservan su versión.
    """
    catalogo: Catalogo = parse_catalogo(contenido)
    async with get_session() as session:
        existente = (
            await session.execute(
                select(QaCatalogVersion).where(QaCatalogVersion.contenido_sha256 == catalogo.sha256)
            )
        ).scalar_one_or_none()
        creada = existente is None
        if existente is None:
            existente = QaCatalogVersion(
                version=catalogo.version,
                fecha=catalogo.fecha,
                commit_docs=(commit_docs or "").strip() or None,
                contenido_sha256=catalogo.sha256,
                importado_por=admin_id,
            )
            session.add(existente)
            await session.flush()
            for it in catalogo.items:
                session.add(
                    QaItem(
                        catalog_version_id=existente.id,
                        slug=it.slug,
                        seccion=it.seccion,
                        orden=it.orden,
                        modulo=it.modulo,
                        grupo=it.grupo,
                        titulo=it.titulo,
                        resumen=it.resumen,
                        contexto=it.contexto,
                        prioridad=it.prioridad,
                        plataforma=it.plataforma,
                        precondiciones=it.precondiciones,
                        datos=it.datos,
                        pasos=it.pasos,
                        esperado=it.esperado,
                        porque=it.porque,
                        como=it.como,
                        vars=it.vars,
                        quien=it.quien,
                        hu=it.hu,
                        origen=it.origen,
                    )
                )
        ciclo = await _ciclo_abierto(session)
        if ciclo is None:
            ciclo = QaCycle(
                nombre=f"Ciclo {catalogo.fecha} · catálogo v{catalogo.version}",
                catalog_version_id=existente.id,
                abierto_por=admin_id,
            )
            session.add(ciclo)
            await session.flush()
            _evento(session, campo="ciclo.abierto", admin_id=admin_id, cycle_id=ciclo.id, despues=ciclo.nombre)
        elif ciclo.catalog_version_id != existente.id:
            ciclo.catalog_version_id = existente.id
        if creada:
            _evento(
                session,
                campo="catalogo.importado",
                admin_id=admin_id,
                cycle_id=ciclo.id,
                despues=f"v{catalogo.version} ({len(catalogo.items)} ítems)",
            )
        log.info("qa_catalogo_importado", version=catalogo.version, items=len(catalogo.items), creada=creada)
        return ResultadoImportacion(
            version_id=existente.id,
            version=existente.version,
            items=len(catalogo.items),
            creada=creada,
            ciclo_id=ciclo.id,
            ciclo_nombre=ciclo.nombre,
        )


async def catalogo_con_estado(seccion: str | None = None) -> dict[str, Any]:
    """Catálogo del ciclo abierto con el estado consolidado de cada prueba y el de la config."""
    async with get_session() as session:
        ciclo = await _ciclo_abierto(session)
        if ciclo is None:
            return {"ciclo": None, "catalogo": None, "progreso": progreso({}, 0), "items": []}
        version = await session.get(QaCatalogVersion, ciclo.catalog_version_id)
        items = await _items(session, ciclo.catalog_version_id, seccion)
        run_ids = list(
            (await session.execute(select(QaRun.id).where(QaRun.cycle_id == ciclo.id))).scalars().all()
        )
        por_run = await _resultados_de_runs(session, run_ids)
        por_slug: dict[str, list[str]] = defaultdict(list)
        for estados in por_run.values():
            for slug, estado in estados.items():
                por_slug[slug].append(estado)
        config = {
            c.item_slug: c
            for c in (await session.execute(select(QaConfigStatus))).scalars().all()
        }
        salida: list[dict[str, Any]] = []
        consolidados: dict[str, str] = {}
        for item in items:
            fila = item_a_dict(item)
            if item.seccion == "pruebas":
                estados = por_slug.get(item.slug, [])
                fila["estado"] = consolidar(estados)
                conteo: dict[str, int] = defaultdict(int)
                for estado in estados:
                    conteo[estado] += 1
                fila["ejecuciones"] = dict(conteo)
                consolidados[item.slug] = fila["estado"]
            else:
                cfg = config.get(item.slug)
                fila["estado"] = cfg.estado if cfg else "pendiente"
                fila["nota"] = cfg.nota if cfg else None
                fila["actualizado_en"] = _iso(cfg.actualizado_en) if cfg else None
            salida.append(fila)
        total_pruebas = (
            sum(1 for i in items if i.seccion == "pruebas")
            if seccion in (None, "pruebas")
            else 0
        )
        return {
            "ciclo": {"id": str(ciclo.id), "nombre": ciclo.nombre, "abierto_en": _iso(ciclo.abierto_en)},
            "catalogo": {
                "version": version.version if version else 0,
                "fecha": version.fecha if version else "",
                "commit_docs": version.commit_docs if version else None,
            },
            "progreso": progreso(consolidados, total_pruebas),
            "items": salida,
        }


# ------------------------------------------------------------------ mi ejecución
async def mi_ejecucion(admin: Admin) -> dict[str, Any] | None:
    async with get_session() as session:
        ciclo = await _ciclo_abierto(session)
        if ciclo is None:
            return None
        run = await _mi_run(session, ciclo, admin.id)
        return await _run_a_dict(session, run, ciclo) if run else None


async def iniciar_ejecucion(admin: Admin) -> dict[str, Any]:
    async with get_session() as session:
        ciclo = await _requerir_ciclo(session)
        run = await _mi_run(session, ciclo, admin.id)
        if run is None:
            stmt = (
                pg_insert(QaRun)
                .values(id=uuid.uuid4(), cycle_id=ciclo.id, admin_id=admin.id, estado="en_curso",
                        iniciada_en=utcnow(), actualizada_en=utcnow())
                .on_conflict_do_nothing(constraint="uq_qa_runs_cycle_admin")
            )
            await session.execute(stmt)
            run = await _mi_run(session, ciclo, admin.id)
            assert run is not None
            _evento(session, campo="ejecucion.iniciada", admin_id=admin.id, cycle_id=ciclo.id, run_id=run.id)
        return await _run_a_dict(session, run, ciclo)


async def _recalcular_prueba(
    session: AsyncSession, run: QaRun, item: QaItem
) -> str:
    pasos = {
        p.paso: p.estado
        for p in (
            await session.execute(
                select(QaStepResult).where(
                    QaStepResult.run_id == run.id, QaStepResult.item_slug == item.slug
                )
            )
        ).scalars()
    }
    estado = estado_de_prueba(len(item.pasos or []), pasos)
    # Por el ORM (no un upsert crudo): el `QaItemResult` ya puede estar en la identity map
    # y un upsert lo dejaría desactualizado para el resto del request.
    resultado = await session.get(QaItemResult, (run.id, item.slug))
    if resultado is None:
        resultado = QaItemResult(run_id=run.id, item_slug=item.slug)
        session.add(resultado)
    resultado.estado = estado
    resultado.actualizado_en = utcnow()
    await session.flush()
    return estado


async def marcar_paso(
    admin: Admin,
    slug: str,
    paso: int,
    *,
    estado: str | None,
    nota: str | None,
    cambiar_nota: bool,
) -> dict[str, Any]:
    """Marca (o desmarca con `estado=None`) un paso y recalcula la prueba."""
    if estado is not None and estado not in ESTADOS_PASO:
        raise QaError("invalido", f"Estado de paso inválido: {estado}.")
    nota_limpia = _nota(nota) if cambiar_nota else None
    async with get_session() as session:
        ciclo, run = await _run_editable(session, admin)
        item = await _prueba_del_ciclo(session, ciclo, slug)
        if not 1 <= paso <= len(item.pasos or []):
            raise QaError("invalido", f"La prueba tiene {len(item.pasos or [])} pasos.")
        actual = await session.get(QaStepResult, (run.id, slug, paso))
        antes_estado = actual.estado if actual else None
        antes_nota = actual.nota if actual else None
        if actual is None:
            actual = QaStepResult(run_id=run.id, item_slug=slug, paso=paso)
            session.add(actual)
        actual.estado = estado
        if cambiar_nota:
            actual.nota = nota_limpia
        actual.actualizado_en = utcnow()
        await session.flush()
        if antes_estado != estado:
            _evento(session, campo="paso.estado", admin_id=admin.id, cycle_id=ciclo.id, run_id=run.id,
                    item_slug=slug, paso=paso, antes=antes_estado, despues=estado)
        if cambiar_nota and antes_nota != nota_limpia:
            _evento(session, campo="paso.nota", admin_id=admin.id, cycle_id=ciclo.id, run_id=run.id,
                    item_slug=slug, paso=paso, antes=antes_nota, despues=nota_limpia)
        anterior = await session.get(QaItemResult, (run.id, slug))
        antes_prueba = anterior.estado if anterior else "sin_correr"
        estado_prueba = await _recalcular_prueba(session, run, item)
        if antes_prueba != estado_prueba:
            _evento(session, campo="prueba.estado", admin_id=admin.id, cycle_id=ciclo.id, run_id=run.id,
                    item_slug=slug, antes=antes_prueba, despues=estado_prueba)
        run.actualizada_en = utcnow()
        return await _run_a_dict(session, run, ciclo)


async def nota_de_prueba(admin: Admin, slug: str, nota: str | None) -> dict[str, Any]:
    nota_limpia = _nota(nota)
    async with get_session() as session:
        ciclo, run = await _run_editable(session, admin)
        item = await _prueba_del_ciclo(session, ciclo, slug)
        actual = await session.get(QaItemResult, (run.id, slug))
        antes = actual.nota if actual else None
        if actual is None:
            actual = QaItemResult(run_id=run.id, item_slug=item.slug, estado="sin_correr")
            session.add(actual)
        actual.nota = nota_limpia
        actual.actualizado_en = utcnow()
        if antes != nota_limpia:
            _evento(session, campo="prueba.nota", admin_id=admin.id, cycle_id=ciclo.id, run_id=run.id,
                    item_slug=slug, antes=antes, despues=nota_limpia)
        run.actualizada_en = utcnow()
        await session.flush()
        return await _run_a_dict(session, run, ciclo)


# ------------------------------------------------------------------ capturas
async def subir_adjunto(
    admin: Admin,
    slug: str,
    paso: int,
    *,
    nombre_original: str | None,
    mimetype: str | None,
    contenido: bytes,
) -> dict[str, Any]:
    async with get_session() as session:
        ciclo, run = await _run_editable(session, admin)
        item = await _prueba_del_ciclo(session, ciclo, slug)
        if not 1 <= paso <= len(item.pasos or []):
            raise QaError("invalido", f"La prueba tiene {len(item.pasos or [])} pasos.")
        cantidad = (
            await session.execute(
                select(func.count()).select_from(QaAttachment).where(
                    QaAttachment.run_id == run.id,
                    QaAttachment.item_slug == slug,
                    QaAttachment.paso == paso,
                )
            )
        ).scalar_one()
        if cantidad >= evidencias.MAX_POR_PASO:
            raise QaError("invalido", f"Cada paso admite hasta {evidencias.MAX_POR_PASO} capturas.")
        try:
            ruta, tipo, tamano = evidencias.guardar(run.id, mimetype, contenido)
        except evidencias.EvidenciaInvalida as exc:
            raise QaError("invalido", str(exc)) from exc
        nombre = (nombre_original or "").replace("/", "_").replace("\\", "_").strip()[:200] or None
        adj = QaAttachment(
            run_id=run.id,
            item_slug=slug,
            paso=paso,
            ruta=ruta,
            nombre_original=nombre,
            mimetype=tipo,
            tamano=tamano,
            subido_por=admin.id,
        )
        session.add(adj)
        await session.flush()
        _evento(session, campo="adjunto.agregado", admin_id=admin.id, cycle_id=ciclo.id, run_id=run.id,
                item_slug=slug, paso=paso, despues=nombre or "captura")
        run.actualizada_en = utcnow()
        return _adjunto_a_dict(adj)


async def borrar_adjunto(admin: Admin, adjunto_id: uuid.UUID) -> None:
    async with get_session() as session:
        ciclo, run = await _run_editable(session, admin)
        adj = await session.get(QaAttachment, adjunto_id)
        if adj is None or adj.run_id != run.id:
            raise QaError("no_encontrado", "Captura no encontrada en tu ejecución.")
        ruta = adj.ruta
        _evento(session, campo="adjunto.quitado", admin_id=admin.id, cycle_id=ciclo.id, run_id=run.id,
                item_slug=adj.item_slug, paso=adj.paso, antes=adj.nombre_original or "captura")
        await session.delete(adj)
        run.actualizada_en = utcnow()
    evidencias.borrar(ruta)


async def adjunto_para_servir(admin: Admin, adjunto_id: uuid.UUID) -> tuple[QaAttachment, Any]:
    """La captura y su ruta en disco, si el admin puede verla (dueño de la ejecución o master)."""
    async with get_session() as session:
        adj = await session.get(QaAttachment, adjunto_id)
        if adj is None:
            raise QaError("no_encontrado", "Captura no encontrada.")
        run = await session.get(QaRun, adj.run_id)
        if run is None or (run.admin_id != admin.id and not ve_todas_las_ejecuciones(admin)):
            raise QaError("no_encontrado", "Captura no encontrada.")
    ruta = evidencias.resolver(adj.ruta)
    if ruta is None:
        raise QaError("no_encontrado", "El archivo de la captura ya no está disponible.")
    return adj, ruta


# ------------------------------------------------------------------ informe
async def enviar_ejecucion(admin: Admin, comentario: str | None) -> dict[str, Any]:
    """Congela la ejecución con su informe y avisa por correo a los masters activos."""
    comentario_limpio = _nota(comentario)
    async with get_session() as session:
        ciclo, run = await _run_editable(session, admin)
        version = await session.get(QaCatalogVersion, ciclo.catalog_version_id)
        pruebas = await _items(session, ciclo.catalog_version_id, "pruebas")
        detalle = await _detalle_run(session, run)
        slugs = {p.slug for p in pruebas}
        estados = {s: d["estado"] for s, d in detalle.items() if s in slugs}
        ahora = utcnow()
        resultados: dict[str, dict[str, Any]] = {}
        for slug, d in detalle.items():
            adjuntos: dict[int, list[dict[str, Any]]] = defaultdict(list)
            for adj in d["adjuntos"]:
                adjuntos[adj["paso"]].append({"id": adj["id"], "nombre": adj["nombre"], "mimetype": adj["mimetype"]})
            resultados[slug] = {
                "estado": d["estado"],
                "nota": d["nota"],
                "pasos": {int(n): v for n, v in d["pasos"].items()},
                "adjuntos": adjuntos,
            }
        informe = armar_informe(
            ciclo={"id": str(ciclo.id), "nombre": ciclo.nombre},
            catalogo={"version": version.version if version else 0, "fecha": version.fecha if version else ""},
            tester={"id": str(admin.id), "nombre": _nombre(admin), "email": admin.email},
            iniciada_en=_iso(run.iniciada_en) or "",
            enviada_en=ahora.isoformat(),
            comentario=comentario_limpio,
            progreso=progreso(estados, len(pruebas)),
            items=[item_a_dict(p) for p in pruebas],
            resultados=resultados,
        )
        run.estado = "enviada"
        run.enviada_en = ahora
        run.actualizada_en = ahora
        run.comentario = comentario_limpio
        run.informe = informe
        _evento(session, campo="ejecucion.enviada", admin_id=admin.id, cycle_id=ciclo.id, run_id=run.id,
                antes="en_curso", despues="enviada")
        destinatarios = list(
            (
                await session.execute(
                    select(Admin.email).where(Admin.role == "master", Admin.active.is_(True))
                )
            ).scalars().all()
        )
        salida = await _run_a_dict(session, run, ciclo)
    await _avisar_informe(destinatarios, informe, run.id)
    return salida


async def _avisar_informe(destinatarios: list[str], informe: dict[str, Any], run_id: uuid.UUID) -> None:
    from core.gateway.system_email import send_system_email

    base = get_settings().panel_base_url.rstrip("/")
    enlace = f"{base}/pruebas/informes/{run_id}" if base else None
    from core.shared.texto_seguro import enmascarar_correo

    tester = informe.get("tester") or {}
    # El asunto puede terminar en un log (correo sin SMTP): nunca el correo completo.
    quien = tester.get("nombre") or ""
    if "@" in quien:
        quien = enmascarar_correo(quien)
    asunto = f"Informe de pruebas de {quien} · {(informe.get('ciclo') or {}).get('nombre')}"
    cuerpo = cuerpo_correo(informe, enlace)
    for destino in destinatarios:
        try:
            await send_system_email(destino, asunto, cuerpo)
        except Exception as exc:  # el informe ya quedó guardado; el correo es solo aviso
            log.warning("qa_informe_correo_fallo", error=type(exc).__name__)


async def listar_ejecuciones(admin: Admin, cycle_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
    async with get_session() as session:
        query = select(QaRun, QaCycle).join(QaCycle, QaCycle.id == QaRun.cycle_id)
        if cycle_id is not None:
            query = query.where(QaRun.cycle_id == cycle_id)
        if not ve_todas_las_ejecuciones(admin):
            query = query.where(QaRun.admin_id == admin.id)
        filas = (await session.execute(query.order_by(QaRun.actualizada_en.desc()).limit(200))).all()
        return [await _run_a_dict(session, run, ciclo, con_detalle=False) for run, ciclo in filas]


async def obtener_ejecucion(admin: Admin, run_id: uuid.UUID) -> dict[str, Any]:
    """Una ejecución con su detalle y, si fue enviada, su informe congelado."""
    async with get_session() as session:
        run = await session.get(QaRun, run_id)
        if run is None or (run.admin_id != admin.id and not ve_todas_las_ejecuciones(admin)):
            raise QaError("no_encontrado", "Ejecución no encontrada.")
        ciclo = await session.get(QaCycle, run.cycle_id)
        assert ciclo is not None
        salida = await _run_a_dict(session, run, ciclo)
        salida["informe"] = run.informe
        return salida


# ------------------------------------------------------------------ historial y ciclos
async def historial(
    admin: Admin, slug: str, cycle_id: uuid.UUID | None = None, limite: int = 100
) -> list[dict[str, Any]]:
    """Historial de una prueba. El master ve el de todos; el resto, el de su ejecución y los
    eventos sin ejecución (ciclo y configuración): las notas ajenas no se leen por aquí."""
    async with get_session() as session:
        if cycle_id is None:
            ciclo = await _ciclo_abierto(session)
            if ciclo is None:
                return []
            cycle_id = ciclo.id
        propio = None
        if not ve_todas_las_ejecuciones(admin):
            propio = (
                await session.execute(
                    select(QaRun.id).where(QaRun.cycle_id == cycle_id, QaRun.admin_id == admin.id)
                )
            ).scalar_one_or_none()
        # La config de test y de producción no se reinicia por ciclo: su historial es completo.
        filas = (
            await session.execute(
                select(QaEvent, Admin)
                .outerjoin(Admin, Admin.id == QaEvent.admin_id)
                .where(
                    QaEvent.item_slug == slug,
                    (QaEvent.cycle_id == cycle_id) | QaEvent.campo.like("config.%"),
                    True
                    if ve_todas_las_ejecuciones(admin)
                    else (QaEvent.run_id.is_(None) | (QaEvent.run_id == propio)),
                )
                .order_by(QaEvent.en.desc())
                .limit(min(max(limite, 1), 500))
            )
        ).all()
        return [
            {
                "campo": ev.campo,
                "paso": ev.paso,
                "antes": ev.antes,
                "despues": ev.despues,
                "por": _nombre(adm),
                "en": _iso(ev.en),
                "run_id": str(ev.run_id) if ev.run_id else None,
            }
            for ev, adm in filas
        ]


async def _foto_de_ciclo(session: AsyncSession, ciclo: QaCycle) -> dict[str, Any]:
    version = await session.get(QaCatalogVersion, ciclo.catalog_version_id)
    pruebas = await _items(session, ciclo.catalog_version_id, "pruebas")
    slugs = {p.slug for p in pruebas}
    runs = list((await session.execute(select(QaRun).where(QaRun.cycle_id == ciclo.id))).scalars().all())
    por_run = await _resultados_de_runs(session, [r.id for r in runs])
    por_slug: dict[str, list[str]] = defaultdict(list)
    for estados in por_run.values():
        for slug, estado in estados.items():
            if slug in slugs:
                por_slug[slug].append(estado)
    consolidados = {slug: consolidar(estados) for slug, estados in por_slug.items()}
    ejecuciones = []
    for run in runs:
        tester = await session.get(Admin, run.admin_id)
        propios = {s: e for s, e in por_run.get(run.id, {}).items() if s in slugs}
        ejecuciones.append(
            {
                "run_id": str(run.id),
                "tester": _nombre(tester),
                "estado": run.estado,
                "enviada_en": _iso(run.enviada_en),
                "progreso": progreso(propios, len(pruebas)),
            }
        )
    return {
        "catalogo": {"version": version.version if version else 0, "fecha": version.fecha if version else ""},
        "progreso": progreso(consolidados, len(pruebas)),
        "por_prueba": {p.slug: consolidados.get(p.slug, "sin_correr") for p in pruebas},
        "ejecuciones": ejecuciones,
    }


async def listar_ciclos() -> list[dict[str, Any]]:
    async with get_session() as session:
        ciclos = list(
            (await session.execute(select(QaCycle).order_by(QaCycle.abierto_en.desc()).limit(100))).scalars().all()
        )
        salida = []
        for ciclo in ciclos:
            foto = ciclo.resumen if ciclo.cerrado_en and ciclo.resumen else await _foto_de_ciclo(session, ciclo)
            salida.append(
                {
                    "id": str(ciclo.id),
                    "nombre": ciclo.nombre,
                    "abierto_en": _iso(ciclo.abierto_en),
                    "cerrado_en": _iso(ciclo.cerrado_en),
                    "abierto": ciclo.cerrado_en is None,
                    **foto,
                }
            )
        return salida


async def cerrar_ciclo(admin: Admin, nombre_nuevo: str) -> dict[str, Any]:
    """Cierra el ciclo abierto con su foto y abre otro con la última versión del catálogo."""
    nombre = (nombre_nuevo or "").strip()
    if not nombre or len(nombre) > 120:
        raise QaError("invalido", "El ciclo nuevo necesita un nombre de hasta 120 caracteres.")
    async with get_session() as session:
        ciclo = await _requerir_ciclo(session)
        version = await _ultima_version(session)
        assert version is not None
        ciclo.resumen = await _foto_de_ciclo(session, ciclo)
        ciclo.cerrado_en = utcnow()
        ciclo.cerrado_por = admin.id
        _evento(session, campo="ciclo.cerrado", admin_id=admin.id, cycle_id=ciclo.id, despues=ciclo.nombre)
        await session.flush()
        nuevo = QaCycle(nombre=nombre, catalog_version_id=version.id, abierto_por=admin.id)
        session.add(nuevo)
        await session.flush()
        _evento(session, campo="ciclo.abierto", admin_id=admin.id, cycle_id=nuevo.id, despues=nombre)
        return {"cerrado_id": str(ciclo.id), "abierto_id": str(nuevo.id), "nombre": nombre}


# ------------------------------------------------------------------ configuración de test y prod
async def actualizar_config(
    admin: Admin, slug: str, *, estado: str | None, nota: str | None, cambiar_nota: bool
) -> dict[str, Any]:
    if estado is not None and estado not in ESTADOS_CONFIG:
        raise QaError("invalido", f"Estado inválido: {estado}.")
    nota_limpia = _nota(nota) if cambiar_nota else None
    async with get_session() as session:
        ciclo = await _requerir_ciclo(session)
        item = (
            await session.execute(
                select(QaItem).where(
                    QaItem.catalog_version_id == ciclo.catalog_version_id,
                    QaItem.slug == slug,
                    QaItem.seccion.in_(("test", "prod")),
                )
            )
        ).scalar_one_or_none()
        if item is None:
            raise QaError("no_encontrado", "Ese punto no está en el catálogo.")
        actual = await session.get(QaConfigStatus, slug)
        if actual is None:
            actual = QaConfigStatus(item_slug=slug, estado="pendiente")
            session.add(actual)
        if estado is not None and estado != actual.estado:
            _evento(session, campo="config.estado", admin_id=admin.id, cycle_id=ciclo.id, item_slug=slug,
                    antes=actual.estado, despues=estado)
            actual.estado = estado
        if cambiar_nota and nota_limpia != actual.nota:
            _evento(session, campo="config.nota", admin_id=admin.id, cycle_id=ciclo.id, item_slug=slug,
                    antes=actual.nota, despues=nota_limpia)
            actual.nota = nota_limpia
        actual.actualizado_por = admin.id
        actual.actualizado_en = utcnow()
        await session.flush()
        return {
            "slug": slug,
            "estado": actual.estado,
            "nota": actual.nota,
            "actualizado_en": _iso(actual.actualizado_en),
        }
