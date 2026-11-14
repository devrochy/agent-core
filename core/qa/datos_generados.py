"""Datos de prueba por agente según la descripción del negocio (HU-QA-10, ADR-0146).

Solo en test (`QA_TESTERS_ENABLED`; la API responde 404 sin la bandera). Por agente:

1. Elige la forma de datos: la de menor prioridad entre las que declaran los packs habilitados
   para alguna habilidad del agente (`formas_de_datos_de_prueba`). El General, el Coordinador y
   los agentes sin una habilidad con forma no tienen datos que generar.
2. Genera con una llamada al LLM fuera del turno: instrucciones en el playbook del kernel
   `datos_de_prueba`, contexto del negocio y esquema JSON del pack como datos. La salida es dato
   no confiable: se valida con el esquema del pack. Si el proveedor falla, no responde a tiempo o
   la salida no valida, se usa el respaldo del pack (`origen = 'plantilla'`).
3. Retira el lote anterior del agente (el pack lo borra lógicamente) y siembra el nuevo; cada
   registro creado queda en `test_data_records` para el próximo retiro. Lo que el negocio creó a
   mano no se toca: solo se retira lo que la siembra devolvió como creado.
4. Mide la llamada en `usage_ledger` (`kind = 'generacion'`) con créditos de plataforma, con el
   helper de IA estructurada del core (`core.ia.estructurado`, ADR-0194).

Un lock de sesión por negocio y agente (`pg_try_advisory_xact_lock`) evita dos generaciones a la
vez del mismo agente (doble clic): la segunda recibe 409.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select, text, update

from core.db.models import AppSettings, TestDataRecord, utcnow
from core.db.session import get_session
from core.mcp.types import BaseModule
from core.qa.contrato_datos import (
    ContextoDeNegocio,
    FormaDeDatos,
    PlanParaPrueba,
    RegistroDescrito,
    RegistroSembrado,
)
from core.shared.logger import get_logger

log = get_logger(__name__)

PLAYBOOK = "datos_de_prueba"
# Lo importado de un CSV (HU-QA-12) es del dueño: generar y borrar datos de prueba no lo tocan.
ORIGEN_IMPORTACION = "importacion"
TIMEOUT_LLM_S = 90
MAX_TOKENS = 4000
TEMPERATURA = 0.4
# Filas de «Lo cargado» (HU-QA-16): un lote generado trae decenas; lo importado puede traer cientos.
TOPE_REGISTROS = 200


class ErrorDatosDePrueba(Exception):
    """Error apto para el cliente, con status y código estables."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Agente:
    id: uuid.UUID
    key: str
    kind: str
    nombre: str
    descripcion: str
    habilidades: frozenset[str]


@dataclass(frozen=True)
class Eleccion:
    pack_id: str
    modulo: BaseModule
    forma: FormaDeDatos


@dataclass(frozen=True)
class Negocio:
    descripcion: str
    voz: str
    moneda: str
    zona_horaria: str
    idioma: str

    @property
    def falta_descripcion(self) -> bool:
        from core.qa.datos import DESCRIPCION_QA

        descripcion = self.descripcion.strip()
        return not descripcion or descripcion == DESCRIPCION_QA


@dataclass(frozen=True)
class Generado:
    datos: BaseModel
    origen: str  # llm | plantilla
    motivo: str | None = None  # por qué se usó la plantilla


@dataclass(frozen=True)
class Modulo:
    """Un módulo del negocio con sus formas de datos de prueba."""

    id: str
    titulo: str
    seccion: str
    formas: tuple[tuple[str, str], ...]  # (pack_id, modo)


MODULOS: dict[str, Modulo] = {
    "ventas": Modulo("ventas", "Ventas", "pack_commerce", (("pack_commerce", "catalogo"),)),
    "agenda": Modulo("agenda", "Agenda", "pack_calendar", (("pack_calendar", "agenda"),)),
    "cobranza": Modulo("cobranza", "Cobranza", "pack_cobranza", (("pack_cobranza", "cartera"),)),
    "suscripciones": Modulo(
        "suscripciones",
        "Suscripciones",
        "pack_subscriptions:suscripciones",
        (("pack_subscriptions", "planes"), ("pack_subscriptions", "suscripciones")),
    ),
}


def _modulo_de_forma(pack_id: str, modo: str) -> str | None:
    for modulo in MODULOS.values():
        if (pack_id, modo) in modulo.formas:
            return modulo.id
    return None


# ------------------------------------------------------------------ lectura
async def _agente(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> Agente:
    from core.agents.editor import habilidades_del_contenido

    async with get_session() as session:
        fila = (
            await session.execute(
                text(
                    "SELECT a.key, a.kind, a.name, a.description, "
                    "  COALESCE(d.content, (SELECT v.content FROM agent_versions v "
                    "    WHERE v.tenant_id = a.tenant_id AND v.agent_id = a.id "
                    "    ORDER BY v.version DESC LIMIT 1)) "
                    "FROM agents a LEFT JOIN agent_drafts d "
                    "  ON d.tenant_id = a.tenant_id AND d.agent_id = a.id "
                    "WHERE a.tenant_id = :t AND a.id = :a AND a.deleted_at IS NULL"
                ),
                {"t": tenant_id, "a": agent_id},
            )
        ).first()
    if fila is None:
        raise ErrorDatosDePrueba(404, "not_found", "Agente no encontrado.")
    contenido = fila[4] if isinstance(fila[4], Mapping) else {}
    return Agente(
        id=agent_id,
        key=str(fila[0]),
        kind=str(fila[1]),
        nombre=str(fila[2]),
        descripcion=str(fila[3] or ""),
        habilidades=frozenset(habilidades_del_contenido(contenido)),
    )


async def _eleccion(tenant_id: uuid.UUID, agente: Agente) -> Eleccion | str:
    """La forma de datos del agente, o el motivo por el que no tiene."""
    from core.agents.editor import habilitados_del_negocio
    from core.plugin_manager import plugin_manager

    if agente.kind != "specialist":
        return "El Coordinador no usa datos de un pack: genera los de cada especialista."
    habilitados = await habilitados_del_negocio(tenant_id)
    nombrada = _forma_de_plantilla(agente.key)
    if nombrada is not None:
        pack_id, modo = nombrada
        modulo = plugin_manager.get(pack_id) if pack_id in habilitados else None
        forma = next((f for f in modulo.formas_de_datos_de_prueba() if f.modo == modo), None) if modulo else None
        if modulo is None or forma is None:
            return "Este agente necesita una habilidad que tu negocio no tiene activa para generar sus datos."
        return Eleccion(pack_id=pack_id, modulo=modulo, forma=forma)
    candidatas: list[tuple[int, str, BaseModule, FormaDeDatos]] = []
    for pack_id in sorted(habilitados):
        modulo = plugin_manager.get(pack_id)
        if modulo is None:
            continue
        for forma in modulo.formas_de_datos_de_prueba():
            if forma.habilidad in agente.habilidades and forma.habilidad in habilitados:
                candidatas.append((forma.prioridad, pack_id, modulo, forma))
    if not candidatas:
        return "Las habilidades de este agente no tienen datos de prueba que generar."
    _, pack_id, modulo, forma = min(candidatas, key=lambda c: (c[0], c[1], c[3].modo))
    return Eleccion(pack_id=pack_id, modulo=modulo, forma=forma)


def _forma_de_plantilla(key: str) -> tuple[str, str] | None:
    """`(pack, modo)` que nombra la plantilla del agente (su key es la de la plantilla)."""
    from core.agents.editor import keys_de_plantillas
    from core.presets import PresetError, load_plantilla

    if key not in keys_de_plantillas():
        return None
    try:
        nombre = load_plantilla(key).datos_de_prueba
    except PresetError:
        return None
    if not nombre:
        return None
    pack_id, modo = nombre.split(".", 1)
    return pack_id, modo


async def _negocio(tenant_id: uuid.UUID) -> Negocio:
    async with get_session() as session:
        fila = (
            await session.execute(select(AppSettings).where(AppSettings.tenant_id == tenant_id))
        ).scalar_one_or_none()
    if fila is None:
        return Negocio(descripcion="", voz="", moneda="COP", zona_horaria="America/Bogota", idioma="es")
    return Negocio(
        descripcion=fila.business_description or "",
        voz=fila.brand_voice or "",
        moneda=fila.currency or "COP",
        zona_horaria=fila.timezone or "America/Bogota",
        idioma=fila.language or "es",
    )


async def _registros_vivos(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> list[TestDataRecord]:
    async with get_session() as session:
        return list(
            (
                await session.execute(
                    select(TestDataRecord)
                    .where(
                        TestDataRecord.tenant_id == tenant_id,
                        TestDataRecord.agent_id == agent_id,
                        TestDataRecord.origen != ORIGEN_IMPORTACION,
                        TestDataRecord.deleted_at.is_(None),
                    )
                    .order_by(TestDataRecord.created_at)
                )
            ).scalars()
        )


async def _registros_modulo(tenant_id: uuid.UUID, modulo: str) -> list[TestDataRecord]:
    async with get_session() as session:
        return list(
            (
                await session.execute(
                    select(TestDataRecord)
                    .where(
                        TestDataRecord.tenant_id == tenant_id,
                        TestDataRecord.modulo == modulo,
                        TestDataRecord.origen != ORIGEN_IMPORTACION,
                        TestDataRecord.deleted_at.is_(None),
                    )
                    .order_by(TestDataRecord.created_at)
                )
            ).scalars()
        )


def _lote(registros: list[TestDataRecord]) -> dict[str, Any] | None:
    if not registros:
        return None
    ultimo = registros[-1]
    return {
        "lote_id": ultimo.lote_id,
        "modo": ultimo.modo,
        "origen": ultimo.origen,
        "creado_en": ultimo.created_at,
        "conteos": dict(Counter(r.kind for r in registros)),
    }


async def _formas_de_modulo(
    tenant_id: uuid.UUID, modulo: Modulo
) -> list[tuple[str, BaseModule, FormaDeDatos]] | str:
    """Las formas del módulo, o el motivo por el que no está disponible."""
    from core.agents.editor import habilitados_del_negocio
    from core.plugin_manager import plugin_manager

    habilitados = await habilitados_del_negocio(tenant_id)
    salida: list[tuple[str, BaseModule, FormaDeDatos]] = []
    for pack_id, modo in modulo.formas:
        if pack_id not in habilitados:
            return f"Activa la habilidad necesaria para generar los datos de {modulo.titulo}."
        pack = plugin_manager.get(pack_id)
        if pack is None:
            return f"La habilidad de {modulo.titulo} no está instalada."
        forma = next((f for f in pack.formas_de_datos_de_prueba() if f.modo == modo), None)
        if forma is None:
            return f"La habilidad de {modulo.titulo} no tiene datos de prueba que generar."
        salida.append((pack_id, pack, forma))
    return salida


async def _agente_del_modulo(tenant_id: uuid.UUID, modulo: Modulo) -> tuple[Agente, uuid.UUID | None]:
    """El especialista del módulo si existe; si no (onboarding), un contexto para el prompt."""
    async with get_session() as session:
        fila = (
            await session.execute(
                text(
                    "SELECT id FROM agents WHERE tenant_id = :t AND key = :k "
                    "AND kind = 'specialist' AND deleted_at IS NULL ORDER BY created_at LIMIT 1"
                ),
                {"t": tenant_id, "k": modulo.id},
            )
        ).first()
    if fila is not None:
        agente = await _agente(tenant_id, fila[0])
        return agente, agente.id
    return (
        Agente(
            id=uuid.uuid4(),
            key=modulo.id,
            kind="module",
            nombre=f"Asistente de {modulo.titulo}",
            descripcion=f"Atiende el módulo de {modulo.titulo} del negocio.",
            habilidades=frozenset(),
        ),
        None,
    )


async def estado_modulo(tenant_id: uuid.UUID, modulo_id: str) -> dict[str, Any]:
    modulo = MODULOS.get(modulo_id)
    if modulo is None:
        raise ErrorDatosDePrueba(404, "not_found", "Módulo no encontrado.")
    formas = await _formas_de_modulo(tenant_id, modulo)
    negocio = await _negocio(tenant_id)
    disponible = not isinstance(formas, str)
    return {
        "modulo": modulo.id,
        "titulo": modulo.titulo,
        "disponible": disponible,
        "motivo": None if disponible else formas,
        "falta_descripcion": negocio.falta_descripcion,
        "lote": _lote(await _registros_modulo(tenant_id, modulo.id)),
    }


async def estado(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> dict[str, Any]:
    agente = await _agente(tenant_id, agent_id)
    eleccion = await _eleccion(tenant_id, agente)
    negocio = await _negocio(tenant_id)
    disponible = isinstance(eleccion, Eleccion)
    return {
        "disponible": disponible,
        "motivo": None if disponible else eleccion,
        "falta_descripcion": negocio.falta_descripcion,
        "forma": {"modo": eleccion.forma.modo, "titulo": eleccion.forma.titulo} if disponible else None,
        "lote": _lote(await _registros_vivos(tenant_id, agent_id)),
    }


# ------------------------------------------------------------------ lo cargado (HU-QA-16)
def _seccion(registro: TestDataRecord) -> str:
    modulo = MODULOS.get(registro.modulo or "")
    return modulo.seccion if modulo is not None else registro.pack_id


async def registros(
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    *,
    puede_ver: Callable[[str], Awaitable[bool]] | None = None,
) -> dict[str, Any]:
    """Lo que el agente tiene cargado para probarlo: el último lote generado más lo importado (vivo),
    en palabras de cada pack (`describir_registros`), por lote en el orden en que se cargó y, dentro
    de cada lote, por tipo y nombre.

    - `puede_ver(seccion)`: lo de un módulo cuya sección el usuario no ve no se describe (ni se cuenta).
    - `sin_detalle`: registros de un pack que no se describe (sin el hook o desinstalado). Lo que un
      pack elige no listar (variantes, cuotas, citas) no cuenta: va en el detalle de otra fila.
    """
    from core.plugin_manager import plugin_manager

    await _agente(tenant_id, agent_id)  # 404 sin eco si no es de este negocio
    async with get_session() as session:
        vivos = list(
            (
                await session.execute(
                    select(TestDataRecord)
                    .where(
                        TestDataRecord.tenant_id == tenant_id,
                        TestDataRecord.agent_id == agent_id,
                        TestDataRecord.deleted_at.is_(None),
                    )
                    .order_by(TestDataRecord.created_at, TestDataRecord.id)
                )
            ).scalars()
        )
    generados = [r for r in vivos if r.origen != ORIGEN_IMPORTACION]
    lote = _lote(generados)
    if lote is not None:  # generar retira los lotes anteriores; por si quedó alguno, solo el último
        generados = [r for r in generados if r.lote_id == lote["lote_id"]]
        lote["conteos"] = dict(Counter(r.kind for r in generados))
    elegidos = generados + [r for r in vivos if r.origen == ORIGEN_IMPORTACION]
    if puede_ver is not None:
        permitidas = {s for s in {_seccion(r) for r in elegidos} if await puede_ver(s)}
        elegidos = [r for r in elegidos if _seccion(r) in permitidas]
        if generados and _seccion(generados[0]) not in permitidas:
            lote = None  # ni sus conteos: el lote es de un módulo que no ve

    por_pack: dict[str, list[TestDataRecord]] = {}
    for r in elegidos:
        por_pack.setdefault(r.pack_id, []).append(r)
    descritos: dict[tuple[str, uuid.UUID], RegistroDescrito] = {}
    sin_detalle = 0
    for pack_id, filas in por_pack.items():
        pack = plugin_manager.get(pack_id)
        if pack is None or type(pack).describir_registros is BaseModule.describir_registros:
            sin_detalle += len(filas)
            continue
        for d in await pack.describir_registros(
            tenant_id, [RegistroSembrado(kind=r.kind, ref=r.record_ref) for r in filas]
        ):
            descritos[(d.kind, d.ref)] = d

    # Un lote comparte `created_at`: dentro de él, por tipo y nombre (orden estable para el panel).
    filas: list[tuple[Any, str, str, dict[str, Any]]] = []
    for r in elegidos:
        d = descritos.pop((r.kind, r.record_ref), None)
        if d is not None:
            fila = {"tipo": d.kind, "nombre": d.nombre, "detalle": d.detalle, "origen": r.origen}
            filas.append((r.created_at, d.kind, d.nombre.casefold(), fila))
    salida = [f[3] for f in sorted(filas, key=lambda f: f[:3])]
    return {
        "lote": lote,
        "registros": salida[:TOPE_REGISTROS],
        "total": len(salida),
        "sin_detalle": sin_detalle,
    }


# ------------------------------------------------------------------ generación
def _entrada(negocio: Negocio, agente: Agente, forma: FormaDeDatos) -> str:
    return json.dumps(
        {
            "negocio": {
                "descripcion": negocio.descripcion,
                "voz_de_marca": negocio.voz,
                "moneda": negocio.moneda,
                "idioma": negocio.idioma,
            },
            "agente": {"nombre": agente.nombre, "descripcion": agente.descripcion},
            "forma": forma.titulo,
            "esquema": forma.esquema.model_json_schema(),
        },
        ensure_ascii=False,
    )


def _proveedor() -> Any:
    from core.llm import resolve_provider

    return resolve_provider(thinking=False)


async def _generar(
    tenant_id: uuid.UUID,
    negocio: Negocio,
    agente: Agente,
    forma: FormaDeDatos,
    modulo: str | None = None,
) -> Generado:
    from core.ia.estructurado import ErrorFormato, ErrorProveedor, generar_estructurado

    respaldo = forma.esquema.model_validate(dict(forma.respaldo))
    try:
        generado = await generar_estructurado(
            tenant_id,
            playbook=PLAYBOOK,
            esquema=forma.esquema,
            entrada=_entrada(negocio, agente, forma),
            kind="generacion",
            result=(
                {"proposito": "datos_de_prueba", "agent_id": str(agente.id)}
                if modulo is None
                else {"proposito": "datos_de_prueba", "modulo": modulo}
            ),
            # Créditos de plataforma: generar datos de prueba es de test, no usa la llave del negocio.
            proveedor=_proveedor,
            key_mode="platform",
            max_tokens=MAX_TOKENS,
            temperatura=TEMPERATURA,
            timeout_s=TIMEOUT_LLM_S,
        )
    except ErrorProveedor as exc:  # proveedor caído, sin saldo, timeout: igual quedan datos
        log.warning("datos_de_prueba_llm_fallo", error=exc.causa, modo=forma.modo)
        return Generado(datos=respaldo, origen="plantilla", motivo="proveedor")
    except ErrorFormato as exc:
        log.warning("datos_de_prueba_salida_invalida", error=exc.causa, modo=forma.modo)
        return Generado(datos=respaldo, origen="plantilla", motivo="formato")
    return Generado(datos=generado.datos, origen="llm")


# ------------------------------------------------------------------ siembra y retiro
async def _retirar_registros(tenant_id: uuid.UUID, vivos: list[TestDataRecord]) -> int:
    """Retira los registros vivos por pack y marca sus filas."""
    from core.plugin_manager import plugin_manager

    if not vivos:
        return 0
    por_pack: dict[str, list[RegistroSembrado]] = {}
    for r in vivos:
        por_pack.setdefault(r.pack_id, []).append(
            RegistroSembrado(kind=r.kind, ref=r.record_ref)
        )
    total = 0
    for pack_id, registros in por_pack.items():
        pack = plugin_manager.get(pack_id)
        if pack is None:  # pack desinstalado: sus filas ya no se ven; solo se marca el lote
            continue
        total += await pack.retirar_datos_de_prueba(tenant_id, registros)
    async with get_session() as session:
        await session.execute(
            update(TestDataRecord)
            .where(
                TestDataRecord.tenant_id == tenant_id,
                TestDataRecord.id.in_([r.id for r in vivos]),
            )
            .values(deleted_at=utcnow())
        )
    return total


async def _retirar(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> int:
    return await _retirar_registros(tenant_id, await _registros_vivos(tenant_id, agent_id))


async def _retirar_modulo(tenant_id: uuid.UUID, modulo: str) -> int:
    return await _retirar_registros(tenant_id, await _registros_modulo(tenant_id, modulo))


async def _con_lock(
    tenant_id: uuid.UUID, clave: str, trabajo: Any, mensaje: str
) -> Any:
    async with get_session() as session:
        tomado = (
            await session.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": f"datos_de_prueba:{tenant_id}:{clave}"},
            )
        ).scalar_one()
        if not tomado:
            raise ErrorDatosDePrueba(409, "en_curso", mensaje)
        return await trabajo()


async def generar(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> dict[str, Any]:
    agente = await _agente(tenant_id, agent_id)
    eleccion = await _eleccion(tenant_id, agente)
    if not isinstance(eleccion, Eleccion):
        raise ErrorDatosDePrueba(409, "sin_datos", eleccion)
    negocio = await _negocio(tenant_id)
    if negocio.falta_descripcion:
        raise ErrorDatosDePrueba(
            422,
            "descripcion_requerida",
            "Escribe la descripción de tu negocio en Configuración > Identidad para generar datos parecidos a él.",
        )

    # La IA va antes del lock: no se retiene una conexión del pool mientras responde.
    generado = await _generar(tenant_id, negocio, agente, eleccion.forma)

    async def trabajo() -> dict[str, Any]:
        retirados = await _retirar(tenant_id, agent_id)
        lote_id = uuid.uuid4()
        contexto = ContextoDeNegocio(
            moneda=negocio.moneda, zona_horaria=negocio.zona_horaria, sufijo=lote_id.hex[:6].upper()
        )
        creados = await eleccion.modulo.sembrar_datos_de_prueba(
            tenant_id, eleccion.forma.modo, generado.datos, contexto
        )
        ahora = utcnow()
        async with get_session() as session:
            session.add_all(
                TestDataRecord(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    modulo=_modulo_de_forma(eleccion.pack_id, eleccion.forma.modo),
                    lote_id=lote_id,
                    pack_id=eleccion.pack_id,
                    modo=eleccion.forma.modo,
                    kind=r.kind,
                    record_ref=r.ref,
                    origen=generado.origen,
                    created_at=ahora,
                )
                for r in creados
            )
        log.info(
            "datos_de_prueba_generados",
            tenant_id=str(tenant_id),
            agent_id=str(agent_id),
            modo=eleccion.forma.modo,
            origen=generado.origen,
            creados=len(creados),
            retirados=retirados,
        )
        return {
            "lote_id": lote_id,
            "modo": eleccion.forma.modo,
            "origen": generado.origen,
            "motivo_plantilla": generado.motivo,
            "creado_en": ahora,
            "conteos": dict(Counter(r.kind for r in creados)),
            "retirados": retirados,
        }

    return await _con_lock(
        tenant_id,
        f"agente:{agent_id}",
        trabajo,
        "Ya se están generando los datos de este agente.",
    )


def _origen_del_lote(partes: list[Generado]) -> tuple[str, str | None]:
    for generado in partes:
        if generado.origen != "llm":
            return generado.origen, generado.motivo
    return "llm", None


async def _guardar_registros(
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    modulo: str,
    lote_id: uuid.UUID,
    creados: list[tuple[str, str, RegistroSembrado]],
    origen: str,
    creado_en: Any,
) -> None:
    async with get_session() as session:
        session.add_all(
            TestDataRecord(
                tenant_id=tenant_id,
                agent_id=agent_id,
                modulo=modulo,
                lote_id=lote_id,
                pack_id=pack_id,
                modo=modo,
                kind=registro.kind,
                record_ref=registro.ref,
                origen=origen,
                created_at=creado_en,
            )
            for pack_id, modo, registro in creados
        )


async def _generar_modulo_simple(
    tenant_id: uuid.UUID,
    modulo: Modulo,
    formas: list[tuple[str, BaseModule, FormaDeDatos]],
) -> dict[str, Any]:
    pack_id, pack, forma = formas[0]
    agente, agent_id = await _agente_del_modulo(tenant_id, modulo)
    negocio = await _negocio(tenant_id)
    generado = await _generar(tenant_id, negocio, agente, forma, modulo.id)

    async def trabajo() -> dict[str, Any]:
        retirados = await _retirar_modulo(tenant_id, modulo.id)
        lote_id = uuid.uuid4()
        contexto = ContextoDeNegocio(
            moneda=negocio.moneda,
            zona_horaria=negocio.zona_horaria,
            sufijo=lote_id.hex[:6].upper(),
        )
        creados = await pack.sembrar_datos_de_prueba(tenant_id, forma.modo, generado.datos, contexto)
        ahora = utcnow()
        await _guardar_registros(
            tenant_id,
            agent_id,
            modulo.id,
            lote_id,
            [(pack_id, forma.modo, registro) for registro in creados],
            generado.origen,
            ahora,
        )
        log.info(
            "datos_de_prueba_modulo_generados",
            tenant_id=str(tenant_id),
            modulo=modulo.id,
            origen=generado.origen,
            creados=len(creados),
            retirados=retirados,
        )
        return {
            "lote_id": lote_id,
            "modo": forma.modo,
            "origen": generado.origen,
            "motivo_plantilla": generado.motivo,
            "creado_en": ahora,
            "conteos": dict(Counter(r.kind for r in creados)),
            "retirados": retirados,
        }

    return await _con_lock(
        tenant_id,
        f"modulo:{modulo.id}",
        trabajo,
        f"Ya se están generando los datos de {modulo.titulo}.",
    )


async def _generar_modulo_suscripciones(
    tenant_id: uuid.UUID,
    modulo: Modulo,
    formas: list[tuple[str, BaseModule, FormaDeDatos]],
) -> dict[str, Any]:
    """Planes de prueba y, con su snapshot, clientes/cargos, los dos en Suscripciones."""
    (pack_planes_id, pack_planes, forma_planes), (pack_subs_id, pack_subs, forma_subs) = formas
    agente, agent_id = await _agente_del_modulo(tenant_id, modulo)
    negocio = await _negocio(tenant_id)
    generado_planes = await _generar(
        tenant_id, negocio, agente, forma_planes, modulo.id
    )
    generado_clientes = await _generar(
        tenant_id, negocio, agente, forma_subs, modulo.id
    )
    origen, motivo = _origen_del_lote([generado_planes, generado_clientes])

    async def trabajo() -> dict[str, Any]:
        retirados = await _retirar_modulo(tenant_id, modulo.id)
        lote_id = uuid.uuid4()
        sufijo = lote_id.hex[:6].upper()
        contexto = ContextoDeNegocio(
            moneda=negocio.moneda, zona_horaria=negocio.zona_horaria, sufijo=sufijo
        )
        creados_planes = await pack_planes.sembrar_datos_de_prueba(
            tenant_id, forma_planes.modo, generado_planes.datos, contexto
        )
        planes = tuple(
            PlanParaPrueba(plan_id=r.ref, plan=dict(r.metadata["plan"]))
            for r in creados_planes
            if r.kind == "plan" and r.metadata and r.metadata.get("plan")
        )
        if not planes:
            raise ErrorDatosDePrueba(
                409,
                "sin_datos",
                "No se pudieron crear los planes de suscripción de prueba.",
            )
        contexto_subs = ContextoDeNegocio(
            moneda=negocio.moneda,
            zona_horaria=negocio.zona_horaria,
            sufijo=sufijo,
            planes=planes,
        )
        creados_subs = await pack_subs.sembrar_datos_de_prueba(
            tenant_id, forma_subs.modo, generado_clientes.datos, contexto_subs
        )
        ahora = utcnow()
        await _guardar_registros(
            tenant_id,
            agent_id,
            modulo.id,
            lote_id,
            [(pack_planes_id, forma_planes.modo, registro) for registro in creados_planes]
            + [(pack_subs_id, forma_subs.modo, registro) for registro in creados_subs],
            origen,
            ahora,
        )
        todos = [*creados_planes, *creados_subs]
        log.info(
            "datos_de_prueba_modulo_generados",
            tenant_id=str(tenant_id),
            modulo=modulo.id,
            origen=origen,
            creados=len(todos),
            retirados=retirados,
        )
        return {
            "lote_id": lote_id,
            "modo": modulo.id,
            "origen": origen,
            "motivo_plantilla": motivo,
            "creado_en": ahora,
            "conteos": dict(Counter(r.kind for r in todos)),
            "retirados": retirados,
        }

    return await _con_lock(
        tenant_id,
        f"modulo:{modulo.id}",
        trabajo,
        f"Ya se están generando los datos de {modulo.titulo}.",
    )


async def generar_modulo(tenant_id: uuid.UUID, modulo_id: str) -> dict[str, Any]:
    modulo = MODULOS.get(modulo_id)
    if modulo is None:
        raise ErrorDatosDePrueba(404, "not_found", "Módulo no encontrado.")
    formas = await _formas_de_modulo(tenant_id, modulo)
    if isinstance(formas, str):
        raise ErrorDatosDePrueba(409, "sin_datos", formas)
    negocio = await _negocio(tenant_id)
    if negocio.falta_descripcion:
        raise ErrorDatosDePrueba(
            422,
            "descripcion_requerida",
            "Escribe la descripción de tu negocio en Configuración > Identidad para generar datos parecidos a él.",
        )
    if modulo.id == "suscripciones":
        return await _generar_modulo_suscripciones(tenant_id, modulo, formas)
    return await _generar_modulo_simple(tenant_id, modulo, formas)


async def borrar_modulo(tenant_id: uuid.UUID, modulo_id: str) -> dict[str, Any]:
    modulo = MODULOS.get(modulo_id)
    if modulo is None:
        raise ErrorDatosDePrueba(404, "not_found", "Módulo no encontrado.")

    async def trabajo() -> dict[str, Any]:
        return {"retirados": await _retirar_modulo(tenant_id, modulo.id)}

    return await _con_lock(
        tenant_id,
        f"modulo:{modulo.id}",
        trabajo,
        f"Ya se están generando los datos de {modulo.titulo}.",
    )


async def borrar(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> dict[str, Any]:
    await _agente(tenant_id, agent_id)

    async def trabajo() -> dict[str, Any]:
        return {"retirados": await _retirar(tenant_id, agent_id)}

    return await _con_lock(
        tenant_id,
        f"agente:{agent_id}",
        trabajo,
        "Ya se están generando los datos de este agente.",
    )


__all__ = [
    "MODULOS",
    "ErrorDatosDePrueba",
    "borrar",
    "borrar_modulo",
    "estado",
    "estado_modulo",
    "generar",
    "generar_modulo",
]
