"""Proveedor de métricas OTel (ADR-0188): apagado por defecto, una vez por proceso, en el hijo
del prefork y nunca rompe el arranque.

Las pruebas con el SDK real se saltan donde no carga (el Python del runner de CI no trae
`_ctypes`); corren en un intérprete aparte para no dejar fijado el `MeterProvider` global del
proceso de pytest.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

import core.observability.metricas as metricas
from core.config import get_settings


@pytest.fixture(autouse=True)
def _sin_proveedor(monkeypatch):
    monkeypatch.setattr(metricas, "_proveedor", None)


def test_apagado_por_defecto_no_importa_el_sdk(monkeypatch):
    monkeypatch.setattr(get_settings(), "otel_metrics_enabled", False)
    # Si se intentara importar el SDK, esto fallaría.
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk.metrics", None)

    assert metricas.configure_metrics() is False
    assert metricas._proveedor is None


def test_encendido_sin_endpoint_no_configura(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "otel_metrics_enabled", True)
    monkeypatch.setattr(settings, "otel_metrics_endpoint", "")

    assert metricas.configure_metrics() is False


def test_sin_sdk_el_proceso_arranca_igual(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "otel_metrics_enabled", True)
    monkeypatch.setattr(settings, "otel_metrics_endpoint", "http://alloy:4318/v1/metrics")
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk.metrics", None)  # import → ImportError

    assert metricas.configure_metrics() is False


def test_idempotente_y_shutdown_sin_proveedor(monkeypatch):
    llamadas: list[int] = []

    class _Falso:
        def shutdown(self, timeout_millis: int) -> None:
            llamadas.append(timeout_millis)

    monkeypatch.setattr(metricas, "_proveedor", _Falso())
    assert metricas.configure_metrics() is True  # ya configurado: no vuelve a fijar nada
    metricas.shutdown_metrics()
    metricas.shutdown_metrics()  # segunda vez: no-op
    assert llamadas == [5_000]


def test_instancia_lleva_host_y_pid():
    import os
    import socket

    assert metricas.instancia() == f"{socket.gethostname()}-{os.getpid()}"


def test_el_worker_configura_en_el_hijo_y_no_en_el_padre():
    from celery.signals import worker_init, worker_process_init, worker_process_shutdown

    import core.tasks.celery_app as app

    assert app._metricas_en_el_hijo in [r() for _, r in worker_process_init.receivers]
    assert app._metricas_al_salir_el_hijo in [r() for _, r in worker_process_shutdown.receivers]
    assert app._metricas_en_el_hijo not in [r() for _, r in worker_init.receivers]


def _correr(codigo: str, **env: str) -> str:
    import os

    entorno = {**os.environ, **env}
    salida = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(codigo)], capture_output=True, text=True, env=entorno, timeout=60
    )
    assert salida.returncode == 0, salida.stderr
    # Solo lo que imprime la prueba (marcado con "R "): los logs del core también van a stdout.
    return "\n".join(linea[2:] for linea in salida.stdout.splitlines() if linea.startswith("R "))


def test_los_contadores_declarados_antes_salen_por_el_proveedor():
    pytest.importorskip("opentelemetry.sdk.metrics")
    salida = _correr(
        """
        from opentelemetry import metrics
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader

        # Como los módulos del core: el instrumento se crea al importar, antes del proveedor.
        contador = metrics.get_meter("agent-core").create_counter("option_taps_total")

        from core.observability import metricas
        lector = InMemoryMetricReader()
        assert metricas.configure_metrics(lector) is True
        assert metricas.configure_metrics() is True  # idempotente
        contador.add(2, {"outcome": "ok", "via": "boton"})

        datos = lector.get_metrics_data()
        (rm,) = datos.resource_metrics
        attrs = dict(rm.resource.attributes)
        print("R", attrs["service.name"], attrs["deployment.environment"], attrs["service.instance.id"])
        (metrica,) = [m for sm in rm.scope_metrics for m in sm.metrics if m.name == "option_taps_total"]
        (punto,) = metrica.data.data_points
        print("R", punto.value, metrica.data.aggregation_temporality.name)
        metricas.shutdown_metrics()
        """,
        OTEL_METRICS_ENABLED="true",
        OTEL_SERVICE_NAME="agent-core-api",
        OTEL_RESOURCE_ATTRIBUTES="deployment.environment=test,service.name=pisado",
    )
    nombre, entorno, instancia_id = salida.splitlines()[0].split()
    assert (nombre, entorno) == ("agent-core-api", "test")  # el atributo explícito gana
    assert "-" in instancia_id
    assert salida.splitlines()[1] == "2 CUMULATIVE"


def test_el_exportador_usa_la_url_completa_y_acumulada():
    pytest.importorskip("opentelemetry.sdk.metrics")
    salida = _correr(
        """
        from opentelemetry.sdk.metrics import Counter, Histogram
        from opentelemetry.sdk.metrics.export import AggregationTemporality
        from core.observability import metricas

        lector = metricas._lector_otlp("http://alloy:4318/v1/metrics")
        exp = lector._exporter
        print("R", exp._endpoint)
        print("R", exp._preferred_temporality[Counter] is AggregationTemporality.CUMULATIVE,
              exp._preferred_temporality[Histogram] is AggregationTemporality.CUMULATIVE)
        print("R", lector._export_interval_millis)
        lector.shutdown()
        """,
        # Ni la base de trazas ni la preferencia del entorno cambian lo fijado en código.
        OTEL_EXPORTER_OTLP_ENDPOINT="http://alloy:4318/v1/traces",
        OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE="DELTA",
    )
    assert salida.splitlines() == ["http://alloy:4318/v1/metrics", "True True", "60000"]
