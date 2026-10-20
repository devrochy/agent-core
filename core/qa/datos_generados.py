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
4. Mide la llamada en `usage_ledger` (`kind = 'generacion'`) con créditos de plataforma.

Un lock de sesión por negocio y agente (`pg_try_advisory_xact_lock`) evita dos generaciones a la
vez del mismo agente (doble clic): la segunda recibe 409.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import select, text, update

from core.db.models import AppSettings, TestDataRecord, UsageLedger, utcnow
from core.db.session import get_session
from core.mcp.types import BaseModule
from core.qa.contrato_datos import ContextoDeNegocio, FormaDeDatos, RegistroSembrado
from core.shared.logger import get_logger

log = get_logger(__name__)

PLAYBOOK = "datos_de_prueba"
TIMEOUT_LLM_S = 90
MAX_TOKENS = 4000
TEMPERATURA = 0.4


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
    from core.agents.tolerante import KEY_GENERAL
    from core.plugin_manager import plugin_manager

    if agente.kind != "specialist" or agente.key == KEY_GENERAL:
        return "El agente General y el Coordinador no usan datos de un pack: genera los de cada especialista."
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


# ------------------------------------------------------------------ generación
_JSON = re.compile(r"\{.*\}", re.S)


def _parsear(contenido: str, esquema: type[BaseModel]) -> BaseModel:
    """El primer objeto JSON de la respuesta, validado. Lanza ValueError o ValidationError."""
    hallado = _JSON.search(contenido or "")
    if hallado is None:
        raise ValueError("la respuesta no trae un objeto JSON")
    return esquema.model_validate(json.loads(hallado.group(0)))


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


async def _registrar_consumo(tenant_id: uuid.UUID, proveedor: Any, respuesta: Any, agente: Agente) -> None:
    from core.billing.tarifario import TARIFARIO_VIGENTE, creditos_por_evento, trm_vigente

    uso = respuesta.usage
    costo = Decimal(str(proveedor.estimate_cost_usd(uso.input_tokens, uso.output_tokens)))
    trm = trm_vigente()
    try:
        async with get_session() as session:
            session.add(
                UsageLedger(
                    tenant_id=tenant_id,
                    conversation_id=None,
                    turn_id=None,
                    kind="generacion",
                    model=respuesta.model or getattr(proveedor, "model", ""),
                    input_tokens=uso.input_tokens,
                    output_tokens=uso.output_tokens,
                    latency_ms=respuesta.latency_ms,
                    cost_usd=costo,
                    result={"proposito": "datos_de_prueba", "agent_id": str(agente.id)},
                    credits=creditos_por_evento(costo, "llm_turn", "platform", trm),
                    key_mode="platform",
                    tarifario_version=TARIFARIO_VIGENTE.version,
                    trm_cop=trm,
                )
            )
    except Exception as exc:
        log.warning("usage_ledger_generacion_write_failed", error=type(exc).__name__)


def _proveedor() -> Any:
    from core.llm import resolve_provider

    return resolve_provider(thinking=False)


async def _generar(tenant_id: uuid.UUID, negocio: Negocio, agente: Agente, forma: FormaDeDatos) -> Generado:
    from core.agents.playbooks import load_core_playbook
    from core.llm.types import LLMMessage

    respaldo = forma.esquema.model_validate(dict(forma.respaldo))
    try:
        proveedor = _proveedor()
        respuesta = await asyncio.wait_for(
            proveedor.generate(
                system_blocks=[load_core_playbook(PLAYBOOK)],
                messages=[LLMMessage(role="user", content=_entrada(negocio, agente, forma))],
                max_tokens=MAX_TOKENS,
                temperature=TEMPERATURA,
            ),
            timeout=TIMEOUT_LLM_S,
        )
    except Exception as exc:  # proveedor caído, sin saldo, timeout: igual quedan datos
        log.warning("datos_de_prueba_llm_fallo", error=type(exc).__name__, modo=forma.modo)
        return Generado(datos=respaldo, origen="plantilla", motivo="proveedor")
    await _registrar_consumo(tenant_id, proveedor, respuesta, agente)
    try:
        return Generado(datos=_parsear(respuesta.content, forma.esquema), origen="llm")
    except (ValueError, ValidationError) as exc:
        log.warning("datos_de_prueba_salida_invalida", error=type(exc).__name__, modo=forma.modo)
        return Generado(datos=respaldo, origen="plantilla", motivo="formato")


# ------------------------------------------------------------------ siembra y retiro
async def _retirar(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> int:
    """Retira todos los lotes vivos del agente (por pack) y marca sus filas."""
    from core.plugin_manager import plugin_manager

    vivos = await _registros_vivos(tenant_id, agent_id)
    if not vivos:
        return 0
    por_pack: dict[str, list[RegistroSembrado]] = {}
    for r in vivos:
        por_pack.setdefault(r.pack_id, []).append(RegistroSembrado(kind=r.kind, ref=r.record_ref))
    total = 0
    for pack_id, registros in por_pack.items():
        modulo = plugin_manager.get(pack_id)
        if modulo is None:  # pack desinstalado: sus filas ya no se ven; solo se marca el lote
            continue
        total += await modulo.retirar_datos_de_prueba(tenant_id, registros)
    async with get_session() as session:
        await session.execute(
            update(TestDataRecord)
            .where(
                TestDataRecord.tenant_id == tenant_id,
                TestDataRecord.agent_id == agent_id,
                TestDataRecord.id.in_([r.id for r in vivos]),
            )
            .values(deleted_at=utcnow())
        )
    return total


async def _con_lock(tenant_id: uuid.UUID, agent_id: uuid.UUID, trabajo: Any) -> Any:
    async with get_session() as session:
        tomado = (
            await session.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": f"datos_de_prueba:{tenant_id}:{agent_id}"},
            )
        ).scalar_one()
        if not tomado:
            raise ErrorDatosDePrueba(409, "en_curso", "Ya se están generando los datos de este agente.")
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

    return await _con_lock(tenant_id, agent_id, trabajo)


async def borrar(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> dict[str, Any]:
    await _agente(tenant_id, agent_id)

    async def trabajo() -> dict[str, Any]:
        return {"retirados": await _retirar(tenant_id, agent_id)}

    return await _con_lock(tenant_id, agent_id, trabajo)


__all__ = ["ErrorDatosDePrueba", "borrar", "estado", "generar"]
