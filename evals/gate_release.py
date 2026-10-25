"""Proceso de medición del gate de pass^k de una release (HU-AOS-21 parte 3b, adenda de ADR-0117).

`python evals/gate_release.py <entrada.json> <salida.json>`. Lo lanza `core/releases/corredor.py`
con un entorno PROPIO: `DATABASE_URL` y `MIGRATIONS_DATABASE_URL` apuntan a `agentcore_eval`, nunca a la
base del negocio; Redis es otra base lógica y no llegan los secretos de los canales ni del cobro. Vive
en `evals/` y no en `core/` porque reutiliza `journeys.py`, que parchea el proceso (pasarela y correo
simulados, vendedor, bloqueos): dentro de un worker compartido esos parches se filtrarían al tráfico real.

Qué hace, en `agentcore_eval`:

1. Se niega a correr si la base no es de eval (`current_database()` exacto), es la del negocio, o tiene
   clientes o conversaciones de un negocio que no sea un efímero del corredor.
2. Toma un candado de sesión (las corridas se serializan: la foto conserva los UUID de la release) y
   purga los restos de una corrida caída (`gate-eval-*` suspendidos o viejos).
3. Por cada set de escenarios: negocio efímero (`preparar_negocio` del set, sin motor legado), foto de
   la release candidata (`snapshot.importar`), `aos_runtime = v3` y `aos_procedures`, y los escenarios
   `k` veces con `journeys.correr_journey` (canal `test`, pasarela y correo simulados).
4. Tras cada INTENTO suma el gasto (el `usage_ledger` del efímero y el cliente simulado, que no deja fila)
   y corta al pasar el presupuesto.
5. Siempre, al terminar: purga el negocio efímero (`purge_tenant` SQL sobre un negocio suspendido, solo ids
   que creó este proceso, solo en la base de eval) y su estado en Redis.

La salida es un JSON con los intentos por escenario y el gasto (también cuando falla: el LLM se pagó). El
veredicto y la escritura en la base del negocio los hace quien lo lanzó. No imprime la configuración.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any

EVALS = Path(__file__).resolve().parent
if str(EVALS) not in sys.path:
    sys.path.insert(0, str(EVALS))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from core.llm.cassette import ErrorGrabacion  # noqa: E402
from core.shared.logger import get_logger  # noqa: E402

log = get_logger("gate_release")

PREFIJO_SLUG = "gate-eval-"
# Base de eval: `agentcore_eval` (y variantes con sufijo para pruebas: `agentcore_eval_test`).
_BASE_DE_EVAL = re.compile(r"^agentcore_eval(_[a-z0-9]{1,20})?$")
# Candado de sesión que serializa las corridas en la base de eval.
CANDADO = 7_300_121
# Un efímero más viejo que esto es el resto de una corrida caída.
ENTORNOS_DE_PRUEBA = ("development", "ci", "test")
K = 4


class CorridaFallida(RuntimeError):
    """La medición no se pudo hacer (sin veredicto): infra, presupuesto, grabación faltante."""

    def __init__(self, codigo: str, mensaje: str = "") -> None:
        super().__init__(mensaje or codigo)
        self.codigo = codigo


def nombre_de_base(url: str) -> str:
    return (make_url(url).database or "").lower()


def exigir_urls_de_eval(url_runtime: str, url_dueno: str, negocio_db: str) -> None:
    """Fail-closed antes de abrir nada: las dos URLs son de una base de eval y no la del negocio."""
    for url in (url_runtime, url_dueno):
        if not url or not _BASE_DE_EVAL.match(nombre_de_base(url)):
            raise CorridaFallida("base_no_es_de_eval", "El corredor solo corre contra la base agentcore_eval.")
        if negocio_db and nombre_de_base(url) == negocio_db.lower():
            raise CorridaFallida("base_del_negocio", "La base de eval no puede ser la del negocio.")


async def exigir_base_de_eval(dueno: Any) -> None:
    """Fail-closed ya conectado: la base es de eval y solo tiene negocios efímeros del corredor."""
    async with dueno.connect() as conn:
        actual = (await conn.execute(text("select current_database()"))).scalar_one()
        if not _BASE_DE_EVAL.match(str(actual)):
            raise CorridaFallida("base_no_es_de_eval", "El corredor solo corre contra la base agentcore_eval.")
        # Las migraciones siembran un negocio por defecto (`tenant-1`): no es un problema mientras no tenga
        # clientes ni conversaciones. Un negocio ajeno CON datos es una base de negocio mal apuntada.
        ajenos = (
            await conn.execute(
                text(
                    "select (select count(*) from customers c join tenants t on t.id = c.tenant_id where t.slug not like :p) "
                    "+ (select count(*) from conversations v join tenants t on t.id = v.tenant_id where t.slug not like :p)"
                ),
                {"p": PREFIJO_SLUG + "%"},
            )
        ).scalar_one()
        if ajenos:
            raise CorridaFallida("base_con_negocios", "La base de eval tiene datos de negocios que no son del corredor.")


def _ejecutor_de_prueba() -> Any | None:
    """Ejecutor sustituto de un test (`EVAL_GATE_EJECUTOR=modulo:funcion`), solo en entornos de
    prueba: en producción nunca se lee."""
    ruta = os.environ.get("EVAL_GATE_EJECUTOR", "").strip()
    if not ruta:
        return None
    from core.config import get_settings

    if (get_settings().app_env or "").lower() not in ENTORNOS_DE_PRUEBA:
        raise CorridaFallida("ejecutor_de_prueba_prohibido", "EVAL_GATE_EJECUTOR solo existe en entornos de prueba.")
    modulo, _, funcion = ruta.partition(":")
    return getattr(importlib.import_module(modulo), funcion)


async def _purgar(dueno: Any, tenant_id: uuid.UUID) -> None:
    """Borra un negocio efímero de la base de eval. NO es `tenant purge`: no hay frase ni master; el
    id lo calculó este proceso y la base es `agentcore_eval`. Usa la misma función SQL (`purge_tenant`,
    SECURITY DEFINER): exige el negocio suspendido y es transaccional."""
    async with dueno.begin() as conn:
        slug = (await conn.execute(text("select slug from tenants where id = :t"), {"t": tenant_id})).scalar_one_or_none()
        if slug is None:
            return
        if not str(slug).startswith(PREFIJO_SLUG):
            raise CorridaFallida("purga_de_negocio_ajeno", "Solo se purgan negocios efímeros del corredor.")
        await conn.execute(text("update tenants set status = 'suspended' where id = :t"), {"t": tenant_id})
    async with dueno.begin() as conn:
        await conn.execute(text("select purge_tenant(:t)"), {"t": tenant_id})
    try:
        from core.tenants import purge_tenant_redis

        await purge_tenant_redis(tenant_id)
    except Exception:
        log.warning("corredor_redis_sin_limpiar", tenant_id=str(tenant_id))


async def _purgar_restos(dueno: Any) -> int:
    async with dueno.connect() as conn:
        ids = [
            f[0]
            for f in (
                await conn.execute(
                    text(
                        "select id from tenants where slug like :p"
                    ),
                    {"p": PREFIJO_SLUG + "%"},
                )
            ).all()
        ]
    for tid in ids:
        await _purgar(dueno, tid)
    return len(ids)


async def _gastado(tenant_id: uuid.UUID) -> tuple[float, int, int, str]:
    """(costo USD, tokens de entrada, de salida, modelo principal) del `usage_ledger` del efímero."""
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            fila = (
                await session.execute(
                    text(
                        "select coalesce(sum(cost_usd), 0), coalesce(sum(input_tokens), 0), "
                        "coalesce(sum(output_tokens), 0) from usage_ledger where tenant_id = :t"
                    ),
                    {"t": tenant_id},
                )
            ).one()
            modelo = (
                await session.execute(
                    text(
                        "select model from usage_ledger where tenant_id = :t and model is not null "
                        "group by model order by count(*) desc limit 1"
                    ),
                    {"t": tenant_id},
                )
            ).scalar_one_or_none()
    return float(fila[0]), int(fila[1]), int(fila[2]), str(modelo or "")


class ContadorDelSimulador:
    """El cliente simulado llama al proveedor directo y no deja fila de ledger: se cuenta aquí."""

    def __init__(self) -> None:
        self.costo_usd = 0.0
        self.tokens_in = 0
        self.tokens_out = 0

    def instalar(self) -> None:
        import simulador

        original = simulador.ClienteSimulado.siguiente
        contador = self

        class _Proxy:
            def __init__(self, base: Any) -> None:
                self._base = base

            async def generate(self, **kw: Any) -> Any:
                r = await self._base.generate(**kw)
                contador.tokens_in += int(r.usage.input_tokens)
                contador.tokens_out += int(r.usage.output_tokens)
                contador.costo_usd += float(self._base.estimate_cost_usd(r.usage.input_tokens, r.usage.output_tokens))
                return r

            def __getattr__(self, nombre: str) -> Any:
                return getattr(self._base, nombre)

        async def contado(self_cliente: Any, provider: Any) -> str:
            return await original(self_cliente, _Proxy(provider))

        simulador.ClienteSimulado.siguiente = contado  # type: ignore[method-assign]


async def _encender_v3(dueno: Any, tenant_id: uuid.UUID) -> None:
    async with dueno.begin() as conn:
        await conn.execute(
            text("update tenants set aos_runtime = 'v3', aos_procedures = true where id = :t"), {"t": tenant_id}
        )


async def _correr_set(
    entrada: dict[str, Any],
    set_id: str,
    nombres: list[str],
    journeys: Any,
    dueno: Any,
    estado: dict[str, Any],
    ejecutor: Any | None,
) -> list[dict[str, Any]]:
    """Un negocio efímero para el set; devuelve los resultados de sus escenarios."""
    from core.db.session import get_session
    from core.llm import cassette
    from core.releases import snapshot
    from core.shared.tenant import tenant_scope

    carpeta = journeys.JOURNEYS_DIR / set_id
    negocio, rutas = journeys.rutas_del_set(set_id, [""], carpeta)
    pedidos = {r.stem: r for r in rutas if r.stem in nombres}
    k = int(entrada["k"])
    pagos = journeys.PagoSimulado(monto_minimo=float((negocio.get("pagos_simulados") or {}).get("monto_minimo", 0)))
    slug = f"{PREFIJO_SLUG}{uuid.uuid4().hex[:10]}"
    # Se registra antes de crearlo: si `preparar_negocio` falla a medias, el `finally` lo purga.
    tenant_id = await journeys.preparar_negocio(negocio, pagos, slug=slug, replica=False, motor=False)
    estado["efimeros"].append(tenant_id)
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            conexion = await snapshot.importar(session, tenant_id, entrada["foto"])
    await _encender_v3(dueno, tenant_id)
    journeys._CONEXION_V3[tenant_id] = conexion
    resultados: list[dict[str, Any]] = []
    raiz_grabaciones = journeys.EVALS_DIR / "cassettes" / "journeys" / set_id
    for nombre, ruta in sorted(pedidos.items()):
        intentos: list[dict[str, Any]] = []

        async def todos(nombre: str = nombre, ruta: Path = ruta, intentos: list[dict[str, Any]] = intentos) -> None:
            for intento in range(k):
                if ejecutor is not None:
                    r = await ejecutor(
                        tenant_id=tenant_id, set_id=set_id, nombre=nombre, ruta=ruta, intento=intento, pagos=pagos
                    )
                    intentos.append({"ok": bool(r["ok"]), "detalle": str(r.get("detalle", ""))[:500]})
                else:
                    r2 = await journeys.correr_journey(ruta, tenant_id, pagos, semilla=intento)
                    intentos.append({"ok": bool(r2.ok), "detalle": r2.detalle[:500]})
                costo, tin, tout, modelo = await _gastado(tenant_id)
                estado["parcial"] = (costo, tin, tout, modelo)
                total = estado["base"][0] + costo + estado["simulador"].costo_usd
                if total > float(entrada["presupuesto_usd"]):
                    raise CorridaFallida("presupuesto_agotado", "La corrida pasó el presupuesto de evals del negocio.")

        if entrada.get("grabacion") == "replay" and ejecutor is None:
            with cassette.escena(raiz_grabaciones / f"{nombre}.json"):
                await todos()
        else:
            await todos()
        resultados.append({"set": set_id, "nombre": nombre, "intentos": intentos})
    costo, tin, tout, modelo = await _gastado(tenant_id)
    b = estado["base"]
    estado["base"] = (b[0] + costo, b[1] + tin, b[2] + tout, b[3] or modelo)
    estado["parcial"] = (0.0, 0, 0, "")
    return resultados


def _gasto(estado: dict[str, Any]) -> dict[str, Any]:
    costo, tin, tout, modelo = estado["base"]
    pc, pin, pout, pm = estado["parcial"]
    sim: ContadorDelSimulador = estado["simulador"]
    return {
        "costo_usd": round(costo + pc + sim.costo_usd, 6),
        "input_tokens": tin + pin + sim.tokens_in,
        "output_tokens": tout + pout + sim.tokens_out,
        "modelo": modelo or pm or "desconocido",
    }


async def correr(entrada: dict[str, Any]) -> dict[str, Any]:
    """El cuerpo del proceso. Siempre devuelve la salida; en un fallo trae `ok: false`, el código y el gasto."""
    from core.config import get_settings

    estado: dict[str, Any] = {
        "efimeros": [],
        "base": (0.0, 0, 0, ""),
        "parcial": (0.0, 0, 0, ""),
        "simulador": ContadorDelSimulador(),
    }
    dueno = None
    candado: Any = None
    conn: Any = None
    raiz: Path | None = None
    inicio = time.monotonic()
    try:
        settings = get_settings()
        url_runtime, url_dueno = settings.database_url, settings.migrations_database_url or settings.database_url
        exigir_urls_de_eval(url_runtime, url_dueno, str(entrada.get("negocio_db") or ""))
        if entrada.get("grabacion") not in (None, "replay"):
            raise CorridaFallida("grabacion_invalida", "El gate solo corre con el LLM real o en replay.")
        if int(entrada.get("k", 0)) != K:
            raise CorridaFallida("k_invalido", f"El gate mide con k = {K}.")
        ejecutor = _ejecutor_de_prueba()
        import journeys
        if entrada.get("grabacion") == "replay":
            from core.llm import cassette

            cassette.activar("replay")  # sin esto el proveedor real se construiría y gastaría saldo

        dueno = create_async_engine(url_dueno)
        await exigir_base_de_eval(dueno)
        candado = dueno.connect()
        conn = await candado.__aenter__()
        espera = time.monotonic()
        while not (await conn.execute(text("select pg_try_advisory_lock(:k)"), {"k": CANDADO})).scalar_one():
            if time.monotonic() - espera > float(entrada.get("espera_candado_s", 600)):
                raise CorridaFallida("candado_ocupado", "Otra medición sigue corriendo en la base de eval.")
            await asyncio.sleep(2)
        restos = await _purgar_restos(dueno)
        _, raiz = journeys.preparar_replica(None)
        await journeys._preparar_proceso()
        journeys.CORREOS.instalar()
        estado["simulador"].instalar()
        from core.releases import snapshot

        async with dueno.begin() as c2:
            await snapshot.importar_pack_versions(c2, entrada["foto"])
        resultados: list[dict[str, Any]] = []
        for set_id, nombres in sorted(entrada["sets"].items()):
            resultados += await _correr_set(entrada, set_id, list(nombres), journeys, dueno, estado, ejecutor)
        return {
            "ok": True,
            "resultados": resultados,
            **_gasto(estado),
            "restos_purgados": restos,
            "segundos": round(time.monotonic() - inicio, 1),
        }
    except CorridaFallida as exc:
        return {"ok": False, "codigo": exc.codigo, "mensaje": str(exc)[:300], **_gasto(estado)}
    except ErrorGrabacion as exc:  # falta o cambió la grabación: la corrida falla, nunca es un veredicto
        return {"ok": False, "codigo": "grabacion_faltante", "mensaje": type(exc).__name__, **_gasto(estado)}
    except Exception as exc:  # sin traza ni `str(exc)`: pueden traer parámetros de SQL
        log.error("corredor_proceso_error", error=type(exc).__name__)
        salida = {"ok": False, "codigo": "error_inesperado", "mensaje": type(exc).__name__, **_gasto(estado)}
        if os.environ.get("EVAL_GATE_TRAZA"):  # solo para depurar a mano: la traza puede traer parámetros de SQL
            import traceback

            salida["traza"] = traceback.format_exc(limit=-8)[-3000:]
        return salida
    finally:
        if dueno is not None:
            for tid in estado["efimeros"]:
                try:
                    await _purgar(dueno, tid)
                except Exception as exc:  # el próximo arranque purga los restos
                    log.error("corredor_purga_fallida", tenant_id=str(tid), error=type(exc).__name__)
            if conn is not None:
                try:
                    await conn.execute(text("select pg_advisory_unlock(:k)"), {"k": CANDADO})
                except Exception:
                    pass
            if candado is not None:
                await candado.__aexit__(None, None, None)
            await dueno.dispose()
        if raiz is not None:
            shutil.rmtree(raiz, ignore_errors=True)


async def _cerrar() -> None:
    from core.db.session import get_engine
    from core.memory import working

    await get_engine().dispose()
    if getattr(working, "_client", None) is not None:
        await working._client.aclose()


async def _principal(entrada: dict[str, Any]) -> dict[str, Any]:
    try:
        return await correr(entrada)
    finally:
        await _cerrar()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("uso: python evals/gate_release.py <entrada.json> <salida.json>", file=sys.stderr)
        return 2
    entrada = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    from core.shared.logger import configure_console_logging

    configure_console_logging()  # tracebacks sin variables locales: nunca imprimen la config
    salida = asyncio.run(_principal(entrada))
    Path(args[1]).write_text(json.dumps(salida, ensure_ascii=False), encoding="utf-8")
    return 0 if salida.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
