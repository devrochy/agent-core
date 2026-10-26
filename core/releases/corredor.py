"""Corredor del gate de pass^k (HU-AOS-21 parte 3b, adenda de ADR-0117).

`medir` mide una release candidata contra la activa y deja el veredicto que lee `gate_aprobado`:

1. **Reclamo** (una transacción, `pg_advisory_xact_lock` por negocio): relee la activa, calcula qué
   procedimientos tocó la candidata (`diferencias`), elige sus escenarios (`escenarios`), revisa el tope
   del mes y escribe el evento `evaluated` con `estado = en_curso`. Un reclamo vivo (menos de
   `EVAL_TIMEOUT_SEGUNDOS`) o una medición ya hecha contra esa misma base devuelven sin gastar nada: es la
   idempotencia por `(release_id, base_release_id)`. Las cinco fallidas del día cortan los reintentos.
2. **Medición** (fuera de la transacción): un SUBPROCESO (`evals/gate_release.py`) con entorno propio, contra
   `agentcore_eval`, sobre una foto de la candidata (`snapshot`). Si no responde en el tiempo, se mata.
3. **Cierre** (una transacción): valida la salida del subproceso (es dato, no instrucción) y, si la activa
   sigue siendo la misma, `veredicto.calcular` + `veredicto.registrar` (corrida, resultados, gasto y evento
   `approved`/`rejected`). Si la medición falló, NO hay veredicto: queda el gasto parcial (`kind = eval`, cuenta
   contra el tope) y un `evaluated` con `estado = fallida`. Una falla de infraestructura nunca rechaza.

El gate lo cierra la parte 4 (`core/releases/gate.py`): con la bandera del negocio encendida, una candidata
sin veredicto responde 409 `gate_pendiente` y encola esta medición en el worker
(`core.releases.evaluar_candidata`).
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import secrets
import shutil
import signal
import sys
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import make_url

from core.releases import escenarios as esc
from core.releases import snapshot, veredicto
from core.releases.build import registrar_evento
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

GATE = Path(__file__).resolve().parents[2] / "evals" / "gate_release.py"
K = veredicto.K
# Fallidas por `(release, base)` en 24 h antes de dejar de reintentar solas.
MAX_FALLIDAS_DIA = 5
# Variables que SÍ llegan al subproceso (lista blanca): las del proceso, el LLM y los modos de
# ejecución. Nada de canales (Meta, WhatsApp, Instagram), cobro (Wompi), correo, push ni alertas.
ENTORNO_PERMITIDO = frozenset(
    {
        "PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR", "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHASHSEED",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
        "APP_NAME", "APP_ENV",
        "LLM_PROVIDER", "LLM_API_KEY", "LLM_MODEL", "LLM_MODEL_REASONING", "LLM_MODEL_FALLBACK", "LLM_BASE_URL",
        "LLM_THINKING_DEFAULT", "LLM_THINKING_REASONING", "LLM_MAX_TOKENS", "DIFFICULTY_ROUTING",
        "OPENAI_API_KEY",
        "EMBEDDING_PROVIDER", "EMBEDDING_MODEL", "EMBEDDING_DIMENSIONS", "KNOWLEDGE_EMBEDDING_PROVIDER",
        "MAX_AGENT_ITERATIONS", "MESSAGE_SPLIT_STYLE", "DEBOUNCE_MODE", "KERNEL_LEDGER_ENABLED",
        "TURN_TIMEOUT_S", "TURN_QUEUE_TIMEOUT_S", "BILLING_TRM_COP", "BILLING_CURRENCY", "LOG_LEVEL",
        "EVAL_GATE_EJECUTOR", "EVAL_GATE_TRAZA", "EVAL_GATE_GUION", "EVAL_GATE_EXPLOTA", "EVAL_GATE_COSTO_INTENTO",
    }
)


class MedicionInvalida(ValueError):
    """La salida del subproceso no cumple el esquema (se descarta, no hay veredicto)."""


@dataclass
class ResultadoMedicion:
    """`estado`: aprobada, rechazada, exento, sin_cambios, ya_medida, en_curso, tope_alcanzado,
    sin_escenarios, demasiados_intentos, activa_cambio o fallida."""

    estado: str
    detalle: str = ""
    run_id: uuid.UUID | None = None
    costo_usd: float = 0.0
    por_procedimiento: dict[str, dict[str, Any]] = field(default_factory=dict)
    sin_escenarios: list[str] = field(default_factory=list)
    no_medibles: list[str] = field(default_factory=list)


Ejecutor = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def _exigir_scope(tenant_id: uuid.UUID) -> None:
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("corredor: fuera del tenant_scope del negocio")


async def mediciones_contra(
    session: Any, tenant_id: uuid.UUID, release_id: uuid.UUID, base_release_id: uuid.UUID, timeout: float
) -> list[Any]:
    """Los eventos `evaluated` de la candidata contra esa base, del más nuevo al más viejo, con `vencido`
    (un reclamo `en_curso` más viejo que el tiempo máximo). La usan el corredor y el gate (parte 4)."""
    return list(
        (
            await session.execute(
                text(
                    "select data, created_at, now() - created_at > make_interval(secs => :s) as vencido "
                    "from release_events where tenant_id = :t and release_id = :r and type = 'evaluated' "
                    "and data->>'base_release_id' = :b order by created_at desc"
                ),
                {"t": tenant_id, "r": release_id, "b": str(base_release_id), "s": timeout},
            )
        )
        .mappings()
        .all()
    )


# ------------------------------------------------------------------ entorno y subproceso
def entorno_del_subproceso(
    base: Mapping[str, str], *, url_eval: str, url_eval_dueno: str, redis_url: str, redis_db: int, tenants_root: str
) -> dict[str, str]:
    """Entorno del subproceso, armado desde cero: lista blanca, las URLs de eval (nunca la del negocio),
    otra base lógica de Redis y el runtime por releases encendido."""
    entorno = {k: v for k, v in base.items() if k in ENTORNO_PERMITIDO}
    if entorno.get("APP_ENV", "").lower() not in ("development", "ci", "test"):
        for k in [k for k in entorno if k.startswith("EVAL_GATE_")]:
            del entorno[k]
    if str(redis_db) == str(make_url(redis_url).database or "0"):
        raise MedicionInvalida("EVAL_REDIS_DB no puede ser el índice de Redis del negocio.")
    redis = make_url(redis_url).set(database=str(redis_db)).render_as_string(hide_password=False)
    raiz = str(GATE.parents[1])
    entorno.update(
        {
            # Claves desechables: el subproceso no recibe las de la plataforma (firman tokens y descifran canales).
            "APP_SECRET_KEY": secrets.token_hex(32),
            "TOKEN_MASTER_KEYS": json.dumps({"gate": base64.b64encode(secrets.token_bytes(32)).decode()}),
            "TOKEN_ACTIVE_KEY_ID": "gate",
            "PYTHONPATH": os.pathsep.join(p for p in (raiz, entorno.get("PYTHONPATH", "")) if p),
            "DATABASE_URL": url_eval,
            "MIGRATIONS_DATABASE_URL": url_eval_dueno,
            "REDIS_URL": redis,
            "TENANTS_ROOT": tenants_root,
            "AOS_CONNECTION_ROUTING": "true",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return entorno


async def lanzar_subproceso(entrada: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
    """Corre `evals/gate_release.py` con la entrada y devuelve su salida (o una falla sin gasto
    conocido si no respondió). Entrada y salida van en un directorio 0700 que se borra al terminar."""
    from core.config import get_settings

    settings = get_settings()
    if not settings.eval_database_url or not settings.eval_migrations_database_url:
        return {"ok": False, "codigo": "sin_base_de_eval", "mensaje": "EVAL_DATABASE_URL y EVAL_MIGRATIONS_DATABASE_URL no están configuradas."}
    if not GATE.is_file():
        return {"ok": False, "codigo": "sin_evals_en_la_imagen", "mensaje": "evals/gate_release.py no está en la imagen."}
    plazo = float(timeout if timeout is not None else settings.eval_timeout_segundos)
    carpeta = Path(tempfile.mkdtemp(prefix="gate-eval-"))
    try:
        os.chmod(carpeta, 0o700)
        ruta_in, ruta_out, raiz_tenants = carpeta / "entrada.json", carpeta / "salida.json", carpeta / "tenants"
        raiz_tenants.mkdir()
        fd = os.open(ruta_in, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({**entrada, "negocio_db": make_url(settings.database_url).database or ""}, f, ensure_ascii=False)
        entorno = entorno_del_subproceso(
            os.environ,
            url_eval=settings.eval_database_url,
            url_eval_dueno=settings.eval_migrations_database_url,
            redis_url=settings.redis_url,
            redis_db=settings.eval_redis_db,
            tenants_root=str(raiz_tenants),
        )
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(GATE),
            str(ruta_in),
            str(ruta_out),
            env=entorno,
            cwd=str(carpeta),  # fuera del repo: así no se recarga un archivo de entorno con los secretos quitados
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=plazo)
        except TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
            return {"ok": False, "codigo": "tiempo_agotado", "mensaje": "La medición pasó el tiempo máximo.", "gasto_desconocido": True}
        if not ruta_out.is_file():
            return {"ok": False, "codigo": "sin_salida", "mensaje": f"El proceso terminó ({proc.returncode}) sin salida.", "gasto_desconocido": True}
        try:
            return json.loads(ruta_out.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
        except ValueError:
            return {"ok": False, "codigo": "salida_ilegible", "mensaje": "La salida del proceso no es JSON.", "gasto_desconocido": True}
    finally:
        shutil.rmtree(carpeta, ignore_errors=True)


# ------------------------------------------------------------------ validación de la salida
def validar_salida(
    salida: Mapping[str, Any], pedidos: Mapping[tuple[str, str], list[str]], *, presupuesto_usd: float
) -> tuple[dict[str, veredicto.Medicion], float, str, int, int]:
    """Valida la salida de una medición exitosa contra lo pedido. Devuelve `(Medicion por procedimiento
    sin el sha, costo, modelo, tokens_in, tokens_out)`; `MedicionInvalida` si algo no cuadra."""
    if salida.get("ok") is not True:
        raise MedicionInvalida("la salida no es exitosa")
    resultados = salida.get("resultados")
    if not isinstance(resultados, list):
        raise MedicionInvalida("resultados no es una lista")
    costo, tin, tout = salida.get("costo_usd"), salida.get("input_tokens"), salida.get("output_tokens")
    if (
        isinstance(costo, bool)
        or not isinstance(costo, int | float)
        or not math.isfinite(costo)
        or not 0 <= costo <= max(presupuesto_usd * 1.5, 1.0)
    ):
        raise MedicionInvalida("costo fuera de rango")
    for n in (tin, tout):
        if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= 10**9:
            raise MedicionInvalida("tokens fuera de rango")
    modelo = salida.get("modelo")
    if not isinstance(modelo, str) or not 0 < len(modelo) <= 80:
        raise MedicionInvalida("modelo inválido")
    vistos: set[tuple[str, str]] = set()
    por_pid: dict[str, list[veredicto.Escenario]] = {}
    for r in resultados:
        if not isinstance(r, dict):
            raise MedicionInvalida("resultado inválido")
        clave = (str(r.get("set")), str(r.get("nombre")))
        if clave not in pedidos or clave in vistos:
            raise MedicionInvalida(f"escenario no pedido o repetido: {clave[0]}/{clave[1]}")
        vistos.add(clave)
        intentos = r.get("intentos")
        if not isinstance(intentos, list) or len(intentos) != K:
            raise MedicionInvalida(f"{clave[1]}: se esperaban {K} intentos")
        oks: list[bool] = []
        detalles: list[str] = []
        for i in intentos:
            if not isinstance(i, dict) or not isinstance(i.get("ok"), bool) or not isinstance(i.get("detalle", ""), str):
                raise MedicionInvalida(f"{clave[1]}: intento inválido")
            oks.append(i["ok"])
            if not i["ok"]:
                detalles.append(i.get("detalle", "")[:300])
        pasaron = sum(oks)
        escenario = veredicto.Escenario(
            f"{clave[0]}/{clave[1]}", K, pasaron / K, 1.0 if pasaron == K else 0.0, pasaron == K, " | ".join(detalles[:2])
        )
        for pid in pedidos[clave]:
            por_pid.setdefault(pid, []).append(escenario)
    if vistos != set(pedidos):
        raise MedicionInvalida("faltan escenarios en la salida")
    return (
        {pid: veredicto.Medicion(pid, tuple(lista), "") for pid, lista in por_pid.items()},
        float(costo),
        modelo,
        int(tin or 0),
        int(tout or 0),
    )


# ------------------------------------------------------------------ reclamo y cierre
async def _evento_fallida(
    session: Any, tenant_id: uuid.UUID, connection_id: uuid.UUID, release_id: uuid.UUID, actor: str, datos: dict[str, Any]
) -> None:
    await registrar_evento(session, tenant_id, connection_id, release_id, "evaluated", actor, datos)


async def _cerrar_fallida(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    release_id: uuid.UUID,
    base: uuid.UUID,
    solicitud: str,
    actor: str,
    salida: Mapping[str, Any],
    codigo: str,
) -> ResultadoMedicion:
    """Sin veredicto: el gasto parcial sí se registra (el LLM se pagó) y queda `evaluated/fallida`."""
    from core.db.session import get_session

    costo = salida.get("costo_usd")
    costo_f = float(costo) if isinstance(costo, int | float) and not isinstance(costo, bool) and math.isfinite(costo) else 0.0
    costo_f = max(0.0, min(costo_f, 1000.0))
    tin = salida.get("input_tokens") if isinstance(salida.get("input_tokens"), int) else 0
    tout = salida.get("output_tokens") if isinstance(salida.get("output_tokens"), int) else 0
    modelo = salida.get("modelo") if isinstance(salida.get("modelo"), str) else "desconocido"
    async with get_session() as session:
        if costo_f > 0 or tin or tout:
            session.add(
                veredicto._fila_de_gasto(
                    tenant_id, connection_id, release_id, costo_f, str(modelo)[:80], int(tin or 0), int(tout or 0), None, "gate_fallido"
                )
            )
        await _evento_fallida(
            session,
            tenant_id,
            connection_id,
            release_id,
            actor,
            {
                "estado": "fallida",
                "codigo": codigo[:60],
                "solicitud": solicitud,
                "base_release_id": str(base),
                "costo_usd": round(costo_f, 6),
                "gasto_desconocido": bool(salida.get("gasto_desconocido")),
            },
        )
    log.warning("release_medicion_fallida", release_id=str(release_id), codigo=codigo, costo_usd=costo_f)
    return ResultadoMedicion("fallida", codigo, costo_usd=costo_f)


async def medir(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    release_id: uuid.UUID,
    *,
    actor: str = "system:gate",
    ejecutor: Ejecutor | None = None,
    grabacion: str | None = None,
) -> ResultadoMedicion:
    """Mide la candidata y registra el veredicto. Exige el `tenant_scope` del negocio.

    `ejecutor`: la medición (por defecto el subproceso); los tests inyectan uno guionado.
    `grabacion`: `None` (LLM real) o `replay` (desde `evals/cassettes/`)."""
    from core.db.session import get_session
    from core.releases.cargar import cargar_release
    from core.releases.diferencias import procedimientos_tocados

    _exigir_scope(tenant_id)
    from core.config import get_settings

    timeout = get_settings().eval_timeout_segundos
    solicitud = uuid.uuid4().hex
    # --- 1. reclamo
    async with get_session() as session:
        await session.execute(text("select pg_advisory_xact_lock(hashtext(:k))"), {"k": f"gate:{tenant_id}"})
        activa = (
            await session.execute(
                text("select active_release_id from channel_connections where tenant_id = :t and id = :c and deleted_at is null"),
                {"t": tenant_id, "c": connection_id},
            )
        ).scalar_one_or_none()
        if activa is None:
            return ResultadoMedicion("exento", "Sin release activa: la release 1 no pasa por el gate.")
        if activa == release_id:
            return ResultadoMedicion("sin_cambios", "Es la release activa.")
        candidata = await cargar_release(session, tenant_id, release_id)
        base = await cargar_release(session, tenant_id, activa)
        if candidata is None or base is None or candidata.connection_id != connection_id:
            raise LookupError(str(release_id))
        tocados = procedimientos_tocados(candidata, base)
        if not tocados:
            return ResultadoMedicion("sin_cambios", "La candidata no tocó procedimientos.")
        if await veredicto.ultimo(session, tenant_id, release_id, activa) is not None:
            return ResultadoMedicion("ya_medida", "Ya hay un veredicto contra la release activa de ahora.")
        eventos = await mediciones_contra(session, tenant_id, release_id, activa, timeout)
        ultimo = eventos[0] if eventos else None
        if ultimo is not None and (ultimo["data"] or {}).get("estado") == "en_curso" and not ultimo["vencido"]:
            return ResultadoMedicion("en_curso", "Ya hay una medición en curso para esta release.")
        hoy = datetime.now(UTC).timestamp() - 86400
        fallidas = sum(
            1 for e in eventos if (e["data"] or {}).get("estado") == "fallida" and e["created_at"].timestamp() > hoy
        )
        if fallidas >= MAX_FALLIDAS_DIA:
            return ResultadoMedicion("demasiados_intentos", f"{fallidas} mediciones fallidas en 24 h: revisa antes de reintentar.")
        gastado, tope = await veredicto.tope_del_mes(session, tenant_id)
        restante = tope - gastado
        elegidos, sin = esc.elegir(tocados)
        medibles, no_medibles = _medibles_por_foco(candidata, elegidos)
        sin_total = sorted(set(sin))
        if not medibles:
            datos = {
                "estado": "sin_escenarios",
                "solicitud": solicitud,
                "base_release_id": str(activa),
                "sin_escenarios": sin_total,
                "no_medibles": sorted(no_medibles),
            }
            await _evento_fallida(session, tenant_id, connection_id, release_id, actor, datos)
            return ResultadoMedicion("sin_escenarios", "Ningún procedimiento tocado tiene escenarios medibles.", sin_escenarios=sin_total, no_medibles=sorted(no_medibles))
        if restante <= 0:
            await _evento_fallida(
                session, tenant_id, connection_id, release_id, actor,
                {"estado": "tope_alcanzado", "solicitud": solicitud, "base_release_id": str(activa), "gastado_usd": round(gastado, 4), "tope_usd": tope},
            )
            return ResultadoMedicion("tope_alcanzado", f"Gastado {gastado:.2f} de {tope:.2f} USD este mes.")
        foto = await snapshot.exportar(session, tenant_id, release_id)
        previo_activa = await veredicto.de_la_activa(session, tenant_id, activa)
        await _evento_fallida(
            session, tenant_id, connection_id, release_id, actor,
            {"estado": "en_curso", "solicitud": solicitud, "base_release_id": str(activa), "tocados": dict(sorted(tocados.items())[:200]), "presupuesto_usd": round(restante, 4)},
        )
    # --- 2. medición
    pedidos = esc.por_set(medibles)
    por_clave: dict[tuple[str, str], list[str]] = {(s, n): pids for s, nombres in pedidos.items() for n, pids in nombres.items()}
    entrada = {
        "foto": foto,
        "sets": {s: sorted(nombres) for s, nombres in pedidos.items()},
        "k": K,
        "presupuesto_usd": restante,
        "grabacion": grabacion,
    }
    try:
        salida = await (ejecutor or lanzar_subproceso)(entrada)
        if salida.get("traza"):
            log.error("release_medicion_traza", traza=str(salida["traza"]))
    except Exception as exc:  # el ejecutor inyectado o el lanzamiento: nunca un veredicto
        log.error("release_medicion_error", release_id=str(release_id), error=type(exc).__name__)
        salida = {"ok": False, "codigo": "error_del_ejecutor", "gasto_desconocido": True}
    # --- 3. cierre
    async with get_session() as session:
        await session.execute(text("select pg_advisory_xact_lock(hashtext(:k))"), {"k": f"gate:{tenant_id}"})
        activa_ahora = (
            await session.execute(
                text("select active_release_id from channel_connections where tenant_id = :t and id = :c"),
                {"t": tenant_id, "c": connection_id},
            )
        ).scalar_one_or_none()
    if activa_ahora != activa:
        r = await _cerrar_fallida(tenant_id, connection_id, release_id, activa, solicitud, actor, salida, "activa_cambio")
        return ResultadoMedicion("activa_cambio", "La release activa cambió durante la medición: hay que volver a medir.", costo_usd=r.costo_usd)
    if salida.get("ok") is not True:
        return await _cerrar_fallida(tenant_id, connection_id, release_id, activa, solicitud, actor, salida, str(salida.get("codigo") or "fallida"))
    try:
        por_pid, costo, modelo, tin, tout = validar_salida(salida, por_clave, presupuesto_usd=restante)
    except MedicionInvalida as exc:
        return await _cerrar_fallida(tenant_id, connection_id, release_id, activa, solicitud, actor, salida, f"salida_invalida:{exc}"[:60])
    mediciones = [
        veredicto.Medicion(pid, m.escenarios, esc.sha256_de(medibles[pid])) for pid, m in sorted(por_pid.items())
    ]
    resultado = veredicto.calcular(mediciones, previo_activa)
    sin_medir = sorted(set(sin_total) | set(no_medibles))
    run_id = await veredicto.registrar(
        tenant_id,
        connection_id,
        release_id,
        base_release_id=activa,
        tocados=tocados,
        mediciones=mediciones,
        veredicto=resultado,
        costo_usd=costo,
        modelo=modelo,
        input_tokens=tin,
        output_tokens=tout,
        actor=actor,
        sin_medir=sin_medir,
    )
    return ResultadoMedicion(
        "aprobada" if resultado.aprobado else "rechazada",
        resultado.motivo,
        run_id=run_id,
        costo_usd=costo,
        por_procedimiento=resultado.por_procedimiento,
        sin_escenarios=sin_total,
        no_medibles=sorted(no_medibles),
    )


def _medibles_por_foco(
    candidata: Any, elegidos: Mapping[str, list[esc.EscenarioDeProcedimiento]]
) -> tuple[dict[str, list[esc.EscenarioDeProcedimiento]], list[str]]:
    """Sin ruteo, el turno lo atiende el especialista en foco: un procedimiento que no es suyo nunca se
    ejercita, y no cuenta como medido ni como reprobado (el golden de ruteo es el seguimiento)."""
    from core.releases.cargar import ErrorDeRelease

    try:
        foco = candidata.foco()
    except ErrorDeRelease:
        return {}, sorted(elegidos)
    propios = set(candidata.procedimientos_de(foco.instance_id))
    medibles = {pid: lista for pid, lista in elegidos.items() if pid in propios}
    return medibles, sorted(set(elegidos) - set(medibles))


__all__ = [
    "ENTORNO_PERMITIDO",
    "MAX_FALLIDAS_DIA",
    "MedicionInvalida",
    "ResultadoMedicion",
    "entorno_del_subproceso",
    "lanzar_subproceso",
    "medir",
    "validar_salida",
]
