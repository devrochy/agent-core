"""Operación del enrutamiento (HU-AOS-17, ADR-0138): la bandera `tenants.aos_routing` y el reporte
del enrutamiento. Solo el master (CLI `agent-core db ruteo` y `db reporte-ruteo`). Sin LLM.

El reporte tiene **dos fuentes**, y cada una dice qué mide:

1. **Golden de ruteo** (`reporte_de_golden`, verdad esperada por escenario): cada frase de
   `evals/ruteo/golden.jsonl` (o de un archivo propio del negocio) se pasa por el router de la
   **release activa** de cada conexión, y la predicción se compara con el procedimiento esperado.
   No depende del tráfico ni de que el agente inicie nada, y vale igual con el ruteo en `off`,
   `shadow` o `active`. Es la medida de la **configuración de ruteo** (ejemplos, reglas, umbrales).
2. **Tráfico real** (`reporte_de_sombra`): lo que el router predijo en turnos reales frente al
   primer procedimiento que se inició en esa conversación entre ese turno y el siguiente
   (`conversation_tasks.created_at`). Dos orígenes de la predicción:
   - `sombra`: `core_decisions.shadow_route` (el ruteo en `shadow` decide y no cambia quién atiende);
   - `activo`: con `aos_routing = active` no se escribe `shadow_route`; la predicción es un
     disparador (`route_source = 'trigger'`, su procedimiento es el primer candidato) y lo real es lo
     que inicia el especialista al que se enrutó. Las reglas no se miden en activo: la `start` la
     inicia el motor por construcción y la de agente no predice un procedimiento.

   La **verdad** es un inicio del agente (`iniciar_procedimiento`), no una etiqueta: si el agente no
   inicia nada, la predicción queda **sin verdad** (no se puede saber si fue falsa) y no cuenta como
   un fallo. Solo cuenta como falso positivo el turno donde se inició otro procedimiento. Hay verdad
   solo con `aos_procedures` encendida, y como `iniciar_procedimiento` ofrece solo los
   procedimientos del agente en foco, la medida es justa si el agente que atiende los declara todos.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import text

from core.kernel.router import MODOS

MAX_DIAS = 31
_PREDICEN = ("rule", "trigger")


@dataclass(frozen=True)
class Metrica:
    procedure_id: str
    verdaderos: int
    predichos: int
    reales: int
    # Predichos en un turno donde no se inició nada: sin verdad, ni acierto ni falso positivo.
    sin_verdad: int = 0

    @property
    def con_verdad(self) -> int:
        return self.predichos - self.sin_verdad

    @property
    def precision(self) -> float | None:
        """Aciertos entre las predicciones que tienen verdad (None = no medible)."""
        return self.verdaderos / self.con_verdad if self.con_verdad else None

    @property
    def recall(self) -> float | None:
        return self.verdaderos / self.reales if self.reales else None


@dataclass
class Reporte:
    turnos: int = 0
    metricas: list[Metrica] = field(default_factory=list)
    # Tráfico real: turnos por origen de la predicción (`sombra` | `activo`) y reglas del ruteo
    # activo que no se miden (ver el docstring del módulo).
    por_origen: dict[str, int] = field(default_factory=dict)
    reglas_activas: int = 0

    @property
    def sin_verdad(self) -> int:
        return sum(m.sin_verdad for m in self.metricas)

    @property
    def reales(self) -> int:
        return sum(m.reales for m in self.metricas)


def metricas(
    pares: Iterable[tuple[str | None, str | None]],
    *,
    verdad_completa: bool = False,
    sin_dictamen: Iterable[str] = (),
) -> Reporte:
    """`(predicho, real)` por turno (None = ninguno) → métricas por procedimiento. Puro.

    `verdad_completa=False` (tráfico real): `real = None` es «no se inició nada», que no prueba que
    la predicción fuera falsa → cuenta como `sin_verdad`. `True` (golden): `None` es una etiqueta
    («no debe enrutar a ningún procedimiento») y una predicción ahí sí es un falso positivo.
    `sin_dictamen`: procedimientos predichos en un caso donde el autor no se pronunció: cuentan como
    predichos y «sin verdad», nunca como falso positivo."""
    tp: dict[str, int] = {}
    pred: dict[str, int] = {}
    real: dict[str, int] = {}
    sv: dict[str, int] = {}
    reporte = Reporte()
    for predicho, verdadero in pares:
        reporte.turnos += 1
        if predicho:
            pred[predicho] = pred.get(predicho, 0) + 1
            if verdadero is None and not verdad_completa:
                sv[predicho] = sv.get(predicho, 0) + 1
        if verdadero:
            real[verdadero] = real.get(verdadero, 0) + 1
        if predicho and predicho == verdadero:
            tp[predicho] = tp.get(predicho, 0) + 1
    for predicho in sin_dictamen:
        reporte.turnos += 1
        pred[predicho] = pred.get(predicho, 0) + 1
        sv[predicho] = sv.get(predicho, 0) + 1
    for pid in sorted(set(pred) | set(real)):
        reporte.metricas.append(Metrica(pid, tp.get(pid, 0), pred.get(pid, 0), real.get(pid, 0), sv.get(pid, 0)))
    return reporte


async def reporte_de_sombra(tenant_id: uuid.UUID, dias: int = 7) -> Reporte:
    """Tráfico real del negocio, con ruteo en sombra o activo (bajo su `tenant_scope`; RLS y filtro
    por `tenant_id`)."""
    from core.db.session import get_session
    from core.shared.tenant import get_current_tenant_id

    if get_current_tenant_id() != tenant_id:
        raise PermissionError("ruteo: fuera del tenant_scope del negocio")
    dias = max(1, min(int(dias), MAX_DIAS))
    async with get_session() as session:
        filas = (
            await session.execute(
                text(
                    "WITH d AS ("
                    "  SELECT conversation_id, created_at, shadow_route, candidates, route_source AS fuente_real, "
                    "         lead(created_at) OVER (PARTITION BY conversation_id ORDER BY created_at) AS siguiente "
                    "    FROM core_decisions "
                    "   WHERE tenant_id = :t AND outcome = 'routed' "
                    "     AND created_at >= now() - make_interval(days => :dias)"
                    "), o AS ("
                    # Sombra: la ruta que el router habría tomado. Activo: no hay `shadow_route` de ruta (las
                    # filas que solo traen `pila`, HU-AOS-20, no son una ruta) pero sí `candidates` (el router
                    # corrió; con el ruteo apagado queda NULL) y la fuente real; un disparador predice su
                    # primer candidato (ADR-0138: el elegido es el de mayor puntaje).
                    "  SELECT d.*, "
                    "    CASE WHEN d.shadow_route ? 'route_source' THEN 'sombra' "
                    "         WHEN d.candidates IS NOT NULL THEN 'activo' END AS origen, "
                    "    CASE WHEN d.shadow_route ? 'route_source' THEN d.shadow_route->>'route_source' "
                    "         ELSE d.fuente_real END AS fuente, "
                    "    CASE WHEN d.shadow_route ? 'route_source' THEN d.shadow_route->>'procedure_id' "
                    "         WHEN d.fuente_real = 'trigger' THEN d.candidates->0->>'procedure_id' END AS predicho "
                    "    FROM d"
                    ") "
                    "SELECT o.origen, o.fuente, o.predicho, "
                    "  (SELECT ct.procedure_id FROM conversation_tasks ct "
                    "    WHERE ct.tenant_id = :t AND ct.conversation_id = o.conversation_id "
                    "      AND ct.parent_task_id IS NULL AND ct.created_at >= o.created_at "
                    "      AND (o.siguiente IS NULL OR ct.created_at < o.siguiente) "
                    "    ORDER BY ct.created_at LIMIT 1) "
                    "FROM o WHERE o.origen IS NOT NULL AND o.fuente <> 'task'"
                ),
                {"t": tenant_id, "dias": dias},
            )
        ).all()
    pares: list[tuple[str | None, str | None]] = []
    por_origen: dict[str, int] = {}
    reglas_activas = 0
    for origen, fuente, predicho, real in filas:
        if origen == "activo" and fuente == "rule":
            reglas_activas += 1
            continue
        por_origen[origen] = por_origen.get(origen, 0) + 1
        pares.append((str(predicho) if fuente in _PREDICEN and predicho else None, str(real) if real else None))
    reporte = metricas(pares)
    reporte.por_origen = por_origen
    reporte.reglas_activas = reglas_activas
    return reporte


# ------------------------------------------------------------------ golden de ruteo (verdad por escenario)
GOLDEN_POR_DEFECTO = Path(__file__).resolve().parents[2] / "evals" / "ruteo" / "golden.jsonl"


@dataclass(frozen=True)
class CasoGolden:
    texto: str
    esperado: str | None  # None = ningún procedimiento
    catalogo: str = ""    # los procedimientos con que se escribió el caso (el golden del repo)


def cargar_golden(ruta: Path | str | None = None) -> list[CasoGolden]:
    """Los casos del golden (JSON por línea: `texto`, `esperado`, `catalogo` opcional)."""
    archivo = Path(ruta) if ruta else GOLDEN_POR_DEFECTO
    casos: list[CasoGolden] = []
    for n, linea in enumerate(archivo.read_text(encoding="utf-8").splitlines(), 1):
        if not linea.strip():
            continue
        try:
            crudo = json.loads(linea)
            casos.append(
                CasoGolden(str(crudo["texto"]), crudo.get("esperado") or None, str(crudo.get("catalogo") or ""))
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"{archivo}:{n}: caso de golden inválido ({exc})") from exc
    return casos


@dataclass
class ReporteGolden:
    """El golden contra la release activa de una conexión."""

    connection_id: uuid.UUID | None = None
    release_id: uuid.UUID | None = None
    canal: str = ""
    reporte: Reporte = field(default_factory=Reporte)
    no_aplicables: int = 0
    # Casos sin procedimiento esperado donde el router eligió uno que el catálogo del caso no tenía:
    # el autor del golden no se pronunció, no cuentan como falso positivo.
    sin_dictamen: int = 0
    # Los casos donde la predicción no fue la esperada: (texto, esperado, predicho).
    fallos: list[tuple[str, str | None, str | None]] = field(default_factory=list)
    # Reglas que el router no evalúa sin una conversación (dato del cliente, etapa, horario).
    no_evaluadas: tuple[str, ...] = ()


def _catalogos(casos: Sequence[CasoGolden]) -> dict[str, set[str]]:
    """Los procedimientos con que se escribió cada catálogo del golden (los que aparecen como esperados)."""
    catalogo: dict[str, set[str]] = {}
    for c in casos:
        if c.esperado:
            catalogo.setdefault(c.catalogo, set()).add(c.esperado)
    return catalogo


def evaluar_golden(release: Any, casos: Sequence[CasoGolden], canal: str = "") -> ReporteGolden:
    """Pasa cada frase por el router de la release (sin LLM, sin tarea en foco, sin dueño) y la
    compara con lo esperado. Puro respecto a la base: `release` es una `ReleaseCongelada`.

    Un caso con procedimiento esperado aplica si la release lo tiene (si no, es «no aplicable»). Uno sin
    procedimiento esperado («esto no es ningún procedimiento») aplica siempre, pero el autor lo escribió
    con un catálogo: si el router elige un procedimiento que ese catálogo no tenía (uno propio del
    negocio), el autor no se pronunció y queda «sin verdad», no como falso positivo. Un golden sin
    catálogo (`catalogo` vacío) es del negocio y se pronuncia sobre todo."""
    from core.kernel import router

    routing = release.ruteo()
    propios = set(release.procedimientos().keys())
    catalogos = _catalogos(casos)
    salida = ReporteGolden(canal=canal)
    salida.no_evaluadas = tuple(
        r.id for r in routing.rules if r.when.customerFact or r.when.flowStage or r.when.withinHours
    )
    pares: list[tuple[str | None, str | None]] = []
    sin_dictamen: list[str] = []
    for caso in casos:
        if caso.esperado and caso.esperado not in propios:
            salida.no_aplicables += 1
            continue
        ruta = router.decidir(
            release, routing, router.Entrada(texto=caso.texto[: router.MAX_TEXTO], channel=canal), release.foco(None)
        )
        predicho = ruta.procedure_id if ruta.source in _PREDICEN else None
        if caso.esperado is None and predicho and caso.catalogo and predicho not in catalogos.get(caso.catalogo, set()):
            sin_dictamen.append(predicho)
            continue
        pares.append((predicho, caso.esperado))
        if predicho != caso.esperado:
            salida.fallos.append((caso.texto, caso.esperado, predicho))
    salida.sin_dictamen = len(sin_dictamen)
    salida.reporte = metricas(pares, verdad_completa=True, sin_dictamen=sin_dictamen)
    return salida


async def reporte_de_golden(tenant_id: uuid.UUID, casos: Sequence[CasoGolden]) -> list[ReporteGolden]:
    """El golden contra la release activa de cada conexión conversacional viva del negocio (bajo su
    `tenant_scope`). No escribe nada."""
    from core.db.session import get_session
    from core.releases.cargar import cargar_release
    from core.shared.tenant import get_current_tenant_id

    if get_current_tenant_id() != tenant_id:
        raise PermissionError("ruteo: fuera del tenant_scope del negocio")
    salida: list[ReporteGolden] = []
    async with get_session() as session:
        conexiones = (
            await session.execute(
                text(
                    "SELECT cc.id, cc.active_release_id, cc.channel FROM channel_connections cc "
                    "JOIN connection_types ct ON ct.id = cc.type_id "
                    "WHERE cc.tenant_id = :t AND cc.deleted_at IS NULL AND ct.produces_conversations "
                    "  AND cc.active_release_id IS NOT NULL ORDER BY cc.id"
                ),
                {"t": tenant_id},
            )
        ).all()
        for conexion_id, release_id, canal in conexiones:
            release = await cargar_release(session, tenant_id, release_id)
            if release is None:
                continue
            r = evaluar_golden(release, casos, str(canal or ""))
            r.connection_id, r.release_id = conexion_id, release_id
            salida.append(r)
    return salida


# ------------------------------------------------------------------ lectura del reporte
META_PRECISION = 0.90  # meta de R3 (design §8)
META_RECALL = 0.85


def cumple_meta(reporte: Reporte) -> bool | None:
    """¿Todos los procedimientos medibles cumplen la meta de R3? None si no hay nada medible."""
    medibles = [m for m in reporte.metricas if m.precision is not None or m.recall is not None]
    if not medibles:
        return None
    return all(
        (m.precision is None or m.precision >= META_PRECISION) and (m.recall is None or m.recall >= META_RECALL)
        for m in medibles
    )


def leyenda_trafico(modo: str, con_procedimientos: bool, reporte: Reporte) -> list[str]:
    """Qué mide el tráfico real y por qué puede salir vacío o sin verdad. Puro: el CLI lo imprime."""
    notas = [
        "Qué mide: la predicción del router en turnos reales (sin tarea en foco) frente al primer procedimiento "
        "que se inició en esa conversación hasta el turno siguiente. La verdad es un inicio del agente, no una "
        "etiqueta: «Sin verdad» = se predijo y no se inició nada (no prueba que fuera falso)."
    ]
    if modo == "off":
        notas.append("El ruteo está apagado (aos_routing = off): no se registran predicciones. Usa el golden (fuente 1).")
    elif modo == "active":
        notas.append(
            "El ruteo está activo: el router no escribe shadow_route, así que este reporte usa solo los turnos "
            "enrutados por disparador (su procedimiento es el primer candidato) frente a lo que inicia el "
            "especialista al que se enrutó. Las reglas no se miden en activo (la `start` la inicia el motor por "
            f"construcción; la de agente no predice un procedimiento): {reporte.reglas_activas} en la ventana."
        )
        if not reporte.turnos:
            notas.append(
                "Sin turnos medibles: no hubo disparadores en la ventana (o todo fue por regla o con tarea en "
                "foco). Los turnos de antes de pasar a activo, en sombra, sí cuentan si caen en la ventana."
            )
    if not con_procedimientos and reporte.turnos:
        notas.append(
            "aos_procedures está apagada: nadie puede iniciar un procedimiento, así que no hay verdad en este "
            "reporte. Mide con el golden (fuente 1)."
        )
    elif reporte.turnos and not reporte.reales:
        notas.append(
            "Ninguna conversación inició un procedimiento: en sombra el router no inicia nada, lo inicia el agente "
            "por defecto con `iniciar_procedimiento`, y aquí no lo hizo. Las predicciones quedan sin verdad (no es "
            "una precisión de 0). Para medir la configuración sin depender de eso, usa el golden (fuente 1)."
        )
    return notas


# ------------------------------------------------------------------ pila de tareas (HU-AOS-20)
VENTANA_PILA_HORAS = 12


@dataclass
class ReportePila:
    """Lo que la pila habría abierto (`shadow_route.pila`, turnos sin foco) frente a los
    procedimientos que de verdad se iniciaron en las `VENTANA_PILA_HORAS` siguientes."""

    turnos: int = 0
    predichos: int = 0
    reales: int = 0
    aciertos: int = 0
    completos: int = 0  # turnos en que se inició todo lo que habría abierto

    @property
    def precision(self) -> float | None:
        return self.aciertos / self.predichos if self.predichos else None

    @property
    def recall(self) -> float | None:
        return self.aciertos / self.reales if self.reales else None


def metricas_de_pila(filas: Iterable[tuple[Iterable[str], Iterable[str]]]) -> ReportePila:
    """`(lo que habría abierto, lo que se inició)` por turno → métricas. Puro."""
    reporte = ReportePila()
    for predichos, reales in filas:
        p, r = set(predichos), set(reales)
        reporte.turnos += 1
        reporte.predichos += len(p)
        reporte.reales += len(r)
        reporte.aciertos += len(p & r)
        reporte.completos += int(bool(p) and p <= r)
    return reporte


async def reporte_de_pila(tenant_id: uuid.UUID, dias: int = 7) -> ReportePila:
    """El bloque «pila» del reporte de sombra (bajo el `tenant_scope` del negocio). Solo turnos sin
    tarea en foco: con foco, el modelo no puede iniciar otra tarea y no hay con qué comparar."""
    from core.db.session import get_session
    from core.shared.tenant import get_current_tenant_id

    if get_current_tenant_id() != tenant_id:
        raise PermissionError("ruteo: fuera del tenant_scope del negocio")
    dias = max(1, min(int(dias), MAX_DIAS))
    async with get_session() as session:
        filas = (
            await session.execute(
                text(
                    "SELECT d.shadow_route->'pila', "
                    "  coalesce((SELECT array_agg(DISTINCT ct.procedure_id) FROM conversation_tasks ct "
                    "    WHERE ct.tenant_id = :t AND ct.conversation_id = d.conversation_id "
                    "      AND ct.parent_task_id IS NULL AND ct.created_at >= d.created_at "
                    "      AND ct.created_at < d.created_at + make_interval(hours => :horas)), '{}') "
                    "FROM core_decisions d "
                    "WHERE d.tenant_id = :t AND d.outcome = 'routed' AND d.focus_task_id IS NULL "
                    "  AND d.shadow_route ? 'pila' AND d.created_at >= now() - make_interval(days => :dias)"
                ),
                {"t": tenant_id, "dias": dias, "horas": VENTANA_PILA_HORAS},
            )
        ).all()

    def predichos(pila: object) -> list[str]:
        d = pila if isinstance(pila, dict) else {}
        return [str(x) for x in [*(d.get("abre") or []), *(d.get("pausa") or [])]]

    return metricas_de_pila((predichos(f[0]), [str(x) for x in (f[1] or [])]) for f in filas)


async def escribir_modo(tenant_id: uuid.UUID, modo: str, database_url: str | None = None) -> None:
    """Fija `tenants.aos_routing` con el rol dueño y `aos_fijar_banderas` (migración 0097; el
    panel del master usa la misma función con su `admin_id`). Exige `aos_runtime = 'v3'` para
    encenderla (la función y el CHECK de 0094 también)."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from core.config import get_settings
    from core.kernel.banderas import BanderasAOS, fijar_banderas

    if modo not in MODOS:
        raise ValueError(f"modo inválido: {modo}")
    url = database_url or get_settings().migrations_database_url
    if not url:
        raise PermissionError("falta MIGRATIONS_DATABASE_URL (rol dueño) para escribir las banderas")
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            fila = (
                await conn.execute(
                    text("SELECT aos_runtime, aos_procedures FROM tenants WHERE id = :t AND deleted_at IS NULL FOR UPDATE"),
                    {"t": tenant_id},
                )
            ).first()
            if fila is None:
                raise LookupError("negocio no encontrado")
            if modo != "off" and fila[0] != "v3":
                raise ValueError("el enrutamiento exige aos_runtime = 'v3' (migrar-a-v3 primero)")
            await fijar_banderas(
                conn,
                tenant_id,
                BanderasAOS(runtime=str(fila[0]), procedimientos=bool(fila[1]), ruteo=modo),
                actor="system:ruteo",
            )
    finally:
        await engine.dispose()


__all__ = [
    "GOLDEN_POR_DEFECTO",
    "MAX_DIAS",
    "META_PRECISION",
    "META_RECALL",
    "CasoGolden",
    "Metrica",
    "Reporte",
    "ReporteGolden",
    "ReportePila",
    "cargar_golden",
    "cumple_meta",
    "escribir_modo",
    "evaluar_golden",
    "leyenda_trafico",
    "metricas",
    "metricas_de_pila",
    "reporte_de_golden",
    "reporte_de_pila",
    "reporte_de_sombra",
]
