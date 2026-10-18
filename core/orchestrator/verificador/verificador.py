"""Verificador de afirmaciones con modelo de evidencia (ADR-0119, HU-ARCH-29).

Antes de enviar una respuesta, compara lo que el borrador afirma con lo que el
sistema hizo (`Evidencia`). Es determinista: sin LLM y sin E/S propia.

- **Links:** una URL que no salió de una tool del turno ni del contexto (prompt
  del sistema o mensajes del cliente) se quita del texto. No se reintenta ni se
  reemplaza: el link exacto lo agrega el core con `_safe_append`.
- **Montos:** cada cifra en $/COP tiene que estar en la evidencia (turno actual o
  anteriores). Si no, reintento con los montos verificados del turno.
- **Cierres:** una afirmación de cierre que declara un pack ("tu pedido quedó
  confirmado", "te envío el link de pago") exige una tool que la respalde y que
  haya terminado bien en el turno. Las preguntas, las negaciones ("todavía no
  generé el link") y los condicionales ("para generarte el link necesito…") no
  afirman nada.
- **Referencias y fechas:** se extraen y se cuentan en el veredicto, pero todavía
  no bloquean (modo sombra): no hubo incidentes que las justifiquen y un parser de
  fechas en español que bloquee cuesta falsos positivos.
- **Con el estado de las tareas** (motor activo, HU-AOS-06): un cierre también lo respalda
  una acción que fijó un dato de la tarea en foco (o de una recién completada), y una
  referencia que el borrador afirma y es de una familia que el servidor fijó en las tareas
  (`OC` de `pedido_id = OC-0042`) tiene que estar en la evidencia. Las demás referencias
  siguen en sombra. Sin estado, todo es idéntico a antes.

Ante un fallo, el loop aplica el mismo mecanismo que los guardrails: reintento
correctivo (`guardrail_max_retries`) y luego `guardrail_fail_action` (ADR-0092).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from core.mcp.types import Afirmacion
from core.orchestrator.verificador.evidencia import (
    REFERENCIA_RE,
    EstadoTareas,
    Evidencia,
    clave_url,
    familia,
    urls_con_posicion,
)
from core.shared.dinero import MONEY_IN_TEXT, normalized_amount_key, parse_money
from core.shared.logger import get_logger

log = get_logger(__name__)

# Interruptores por negocio, en `behavior_config["disabled_guardrails"]` como los
# guardrails de los packs. Los nombres de los guardrails que el verificador
# reemplazó siguen valiendo (un negocio que los había apagado los mantiene apagados).
TOGGLES = ("verificador_cierre", "verificador_links", "verificador_montos")
ALIAS = {
    "crm_amount_check": "verificador_montos",
    "crm_completion_check": "verificador_cierre",
}

# Respaldo si el playbook `respuestas` del negocio no trae la sección
# `verificador_<motivo>` (misma idea que `VERIFYING_REPLY`).
INSTRUCCION_GENERICA = (
    "Tu respuesta anterior contenía información no verificada. "
    "Corrige usando SOLO datos devueltos por las herramientas."
)

_PREGUNTA = re.compile(r"¿[^¿?]*\?")
_CORTE_ORACION = re.compile(r"(?<=[.!?…])\s+|\n+")
_CORTE_CLAUSULA = re.compile("[,;:\u2014\u2013]")  # coma, punto y coma, dos puntos y rayas
# La negación se busca en la cláusula de la afirmación ("No te preocupes, tu pedido
# quedó confirmado" sí afirma); el condicional, en toda la oración ("Con tu sí, te
# envío el link de pago" no afirma).
_NEGACION = re.compile(r"\b(?:no|nunca|tampoco|ning[úu]n\w*|sin)\b", re.IGNORECASE)
_CONDICION = re.compile(
    r"\b(?:para\s+(?:que|poder|\w+(?:ar|er|ir)(?:te|le|les|lo|la|los|las|nos|me)?)|"
    r"con\s+(?:tu|tus|eso|esto|esos|estos|esa|esta|ese|este)\b|"
    r"cuando|apenas|en\s+cuanto|una\s+vez\s+(?:que|confirm\w*)|"
    r"si\s+(?:me\s+)?(?:confirm|quer|dese|prefer|est[áa]s|necesit)\w*|"
    r"despu[ée]s\s+de|antes\s+de|en\s+caso\s+de)\b",
    re.IGNORECASE,
)
# "Me faltan dos datos para cerrar el pedido": el verbo de la afirmación en infinitivo.
_PROPOSITO_AL_FINAL = re.compile(r"\b(?:para|a\s+fin\s+de)\s*$", re.IGNORECASE)
# "$1,8 millones", "$200 mil": cifras abreviadas que no se verifican.
_ESCALA = re.compile(r"\s*(?:mil\b|mill[oó]n\w*|k\b)", re.IGNORECASE)
_FECHA = re.compile(
    r"\b\d{1,2}:\d{2}\b|\b\d{1,2}\s+de\s+(?:enero|febrero|marzo|abril|mayo|junio|julio|"
    r"agosto|septiembre|setiembre|octubre|noviembre|diciembre)\b",
    re.IGNORECASE,
)


def normalizar_desactivados(nombres: Iterable[Any] | None) -> list[str]:
    """Nombres de `disabled_guardrails` con los alias del verificador resueltos.

    La usan el loop, la API y el panel: así el nombre viejo se reescribe solo la
    próxima vez que el negocio guarda la configuración."""
    return sorted({ALIAS.get(n, n) for n in nombres or [] if isinstance(n, str) and n})


@dataclass
class Veredicto:
    texto: str
    passed: bool = True
    # Código del motivo: `monto_sin_evidencia`, `status_sin_evidencia` o (con estado)
    # `referencia_sin_evidencia`.
    motivo: str = ""
    # Etiqueta de la afirmación de cierre que falló (para métricas).
    afirmacion: str = ""
    montos_turno: list[float] = field(default_factory=list)
    urls_quitadas: int = 0
    conteo: dict[str, int] = field(default_factory=dict)
    sombra: dict[str, int] = field(default_factory=dict)
    error: bool = False

    @property
    def codigo(self) -> str:
        """Identificador sin `:` (el loop corta el motivo de la escalación en el primero)."""
        return f"verificador.{self.motivo}" if self.motivo else "verificador"

    @property
    def extrajo_algo(self) -> bool:
        return any(self.conteo.values()) or bool(self.urls_quitadas)


# Tope de coincidencias por oración: el costo de `_afirma` crece con el prefijo y un
# texto armado a propósito no debe bloquear el event loop.
_MAX_COINCIDENCIAS = 20


def oraciones_afirmativas(texto: str) -> list[str]:
    """Oraciones del texto sin las preguntas (`¿…?` o terminadas en `?`), con los
    espacios repetidos colapsados (algunos patrones de los packs son cuadráticos
    ante rachas de espacios)."""
    sin_preguntas = _PREGUNTA.sub(" ", texto or "")
    oraciones = []
    for oracion in _CORTE_ORACION.split(sin_preguntas):
        oracion = re.sub(r"[ \t]{2,}", " ", oracion).strip()
        if oracion and not oracion.endswith("?"):
            oraciones.append(oracion)
    return oraciones


def _afirma(oracion: str, match: re.Match[str]) -> bool:
    antes = oracion[: match.start()]
    if _CONDICION.search(antes) or _PROPOSITO_AL_FINAL.search(antes):
        return False
    # La negación cuenta en la cláusula previa y dentro de la propia frase
    # ("el pedido todavía no quedó creado").
    clausula = _CORTE_CLAUSULA.split(antes)[-1]
    return not _NEGACION.search(clausula + " " + match.group(0))


def _quitar_urls(texto: str, respaldada) -> tuple[str, int]:  # type: ignore[no-untyped-def]
    """Quita por posición las URLs sin respaldo y repite hasta que no quede ninguna:
    quitar una puede juntar dos pedazos en una URL nueva (`httpshttps://a.co://x`)."""
    quitadas = 0
    while True:
        sin_respaldo = [(i, f) for i, f, url in urls_con_posicion(texto) if not respaldada(url)]
        if not sin_respaldo:
            return texto, quitadas
        for inicio, fin in reversed(sin_respaldo):
            texto = texto[:inicio] + texto[fin:]
        quitadas += len(sin_respaldo)


def _limpiar(texto: str) -> str:
    texto = re.sub(r"[ \t]{2,}", " ", texto)
    texto = re.sub(r"[ \t]+([.,;:!?])", r"\1", texto)
    texto = "\n".join(linea.rstrip() for linea in texto.splitlines())
    return re.sub(r"\n{3,}", "\n\n", texto).strip()


def verificar(
    borrador: str,
    evidencia: Evidencia,
    afirmaciones: Iterable[Afirmacion],
    desactivados: Iterable[str] = (),
) -> Veredicto:
    """Compara el borrador con la evidencia. Puro: no lee base ni red."""
    apagados = set(desactivados)
    veredicto = Veredicto(texto=borrador, montos_turno=evidencia.montos_turno[:3])
    texto = borrador or ""

    # 1. Links: los que no tienen respaldo se quitan (no consume reintento).
    veredicto.conteo["links"] = len(urls_con_posicion(texto))
    if "verificador_links" not in apagados:
        respaldadas = evidencia.urls_turno | evidencia.urls_contexto
        texto, veredicto.urls_quitadas = _quitar_urls(texto, lambda url: clave_url(url) in respaldadas)
        if veredicto.urls_quitadas:
            texto = _limpiar(texto)
    veredicto.texto = texto

    # 2. Montos, en todo el texto (una cifra inventada en una pregunta también engaña).
    montos = [m for m in MONEY_IN_TEXT.finditer(texto) if not _ESCALA.match(texto, m.end())]
    sin_evidencia = []
    for match in montos:
        valor = parse_money(match.group(0))
        if valor is not None and normalized_amount_key(valor) not in evidencia.montos:
            sin_evidencia.append(match.group(0))
    veredicto.conteo["montos"] = len(montos)

    # 3. Afirmaciones de cierre, solo en oraciones que afirman.
    oraciones = oraciones_afirmativas(texto)
    cierres = 0
    cierre_fallido = ""
    for afirmacion in afirmaciones:
        for oracion in oraciones:
            hallada = any(
                _afirma(oracion, m)
                for _n, m in zip(range(_MAX_COINCIDENCIAS), afirmacion.patron.finditer(oracion), strict=False)
            )
            if not hallada:
                continue
            cierres += 1
            respaldada = (
                bool(afirmacion.tools & evidencia.acciones_ok)
                or (afirmacion.basta_url and bool(evidencia.urls_enlace))
                # Con estado: la acción ya fijó su dato en la tarea en foco (HU-AOS-06).
                or bool(afirmacion.tools & evidencia.acciones_estado)
                # Con estado: el motor fijó en este turno un dato que la afirmación declara.
                or bool(afirmacion.datos & evidencia.datos_fijados)
            )
            if not respaldada and not cierre_fallido:
                cierre_fallido = afirmacion.label
            break
    veredicto.conteo["cierres"] = cierres

    # 4. Modo sombra: se cuentan, no bloquean.
    referencias = set(REFERENCIA_RE.findall(texto))
    veredicto.conteo["referencias"] = len(referencias)
    veredicto.conteo["fechas"] = len(_FECHA.findall(texto))
    veredicto.sombra["referencias_sin_evidencia"] = len(referencias - evidencia.referencias)
    referencia_fallida = _referencia_sin_evidencia(oraciones, evidencia) if evidencia.con_estado else ""

    if cierre_fallido and "verificador_cierre" not in apagados:
        veredicto.afirmacion = cierre_fallido
    if sin_evidencia and "verificador_montos" not in apagados:
        veredicto.passed = False
        veredicto.motivo = "monto_sin_evidencia"
    elif cierre_fallido and "verificador_cierre" not in apagados:
        veredicto.passed = False
        veredicto.motivo = "status_sin_evidencia"
        veredicto.afirmacion = cierre_fallido
    elif referencia_fallida:
        veredicto.passed = False
        veredicto.motivo = "referencia_sin_evidencia"
    return veredicto


def _referencia_sin_evidencia(oraciones: list[str], evidencia: Evidencia) -> str:
    """Primera referencia que el borrador AFIRMA, de una familia que el servidor fijó en las
    tareas, y que no está en la evidencia ("" si no hay). Las preguntas, negaciones y
    condicionales no afirman; las referencias de otras familias (un SKU) siguen en sombra."""
    if not evidencia.familias_estado:
        return ""
    for oracion in oraciones:
        for _n, match in zip(range(_MAX_COINCIDENCIAS), REFERENCIA_RE.finditer(oracion), strict=False):
            referencia = match.group(0)
            if (
                familia(referencia) in evidencia.familias_estado
                and referencia not in evidencia.referencias
                and _afirma(oracion, match)
            ):
                return referencia
    return ""


def verificar_seguro(
    borrador: str,
    resultados: list[dict[str, Any]],
    contexto: str,
    afirmaciones: Iterable[Afirmacion],
    desactivados: Iterable[str] = (),
    estado: EstadoTareas | None = None,
) -> Veredicto:
    """Arma la evidencia y verifica, sin tumbar nunca el turno (mismo criterio que un
    guardrail que lanza, `run_guardrails`). Si algo falla, montos y cierres pasan
    (fail-open), pero los links se quitan todos: un link inventado no llega al cliente
    porque el verificador se rompió. El link exacto lo sigue entregando `_safe_append`."""
    try:
        return verificar(borrador, Evidencia.desde(resultados, contexto, estado), afirmaciones, desactivados)
    except Exception:
        log.exception("verificador_fallo")
    texto = borrador
    if "verificador_links" not in set(desactivados):
        try:
            texto, _ = _quitar_urls(borrador, lambda _url: False)
            texto = _limpiar(texto)
        except Exception:
            log.exception("verificador_fallo_links")
    return Veredicto(texto=texto, error=True)


async def cargar_afirmaciones(habilitados: set[str]) -> list[Afirmacion]:
    """Afirmaciones de cierre de los packs habilitados para el negocio (ADR-0019).

    Una fuente que falla se salta con un log: el resto sigue verificándose."""
    from core.mcp.host import mcp_host

    afirmaciones: list[Afirmacion] = []
    for plugin_id, fuente in mcp_host.get_afirmaciones().items():
        if plugin_id not in habilitados:
            continue
        try:
            afirmaciones.extend(await fuente())
        except Exception:
            log.exception("afirmaciones_fallaron", plugin=plugin_id)
    return afirmaciones


def _formato_monto(valor: float) -> str:
    entero = round(valor)
    if abs(valor - entero) < 0.005:
        return "$" + f"{entero:,}".replace(",", ".")
    return "$" + f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# Instrucciones de los motivos que llegan con el estado de las tareas (HU-AOS-06): viven en un
# playbook del kernel para no cambiar el playbook `respuestas`, que el modelo puede leer entero.
_PLAYBOOK_ESTADO = "verificador-estado"


def _instruccion_del_kernel(clave: str) -> str:
    try:
        from core.agents.playbooks import load_core_playbook

        contenido = load_core_playbook(_PLAYBOOK_ESTADO)
    except Exception:
        log.exception("verificador_playbook_estado_fallo")
        return ""
    lineas: list[str] = []
    dentro = False
    for linea in contenido.splitlines():
        if linea.startswith("## "):
            dentro = linea[3:].strip() == clave
        elif dentro and linea.strip():
            lineas.append(linea.strip())
    return " ".join(lineas)


async def instruccion_correctiva(spec: Any, veredicto: Veredicto) -> str:
    """Instrucción del reintento: sección `verificador_<motivo>` del playbook
    `respuestas` (cero prompts en código, invariante 3). `{montos}` se llena con
    los montos verificados del turno; nada que escriba el cliente o el negocio se
    interpola."""
    from core.agents.responses import load_response

    clave = f"verificador_{veredicto.motivo}"
    texto = await load_response(spec, clave) or _instruccion_del_kernel(clave) or INSTRUCCION_GENERICA
    if "{montos}" in texto:
        montos = ", ".join(_formato_monto(m) for m in veredicto.montos_turno) or "ninguno"
        texto = texto.replace("{montos}", montos)
    return texto


async def registrar_veredicto(
    conversation_id: uuid.UUID,
    veredicto: Veredicto,
    *,
    resultado: str,
    intentos: int,
    motivos: list[str],
) -> None:
    """Una fila por turno en `audit_log` con el veredicto, solo códigos y conteos
    (sin texto del borrador: puede traer datos del cliente). Desde HU-AOS-10 va también a
    `turn_outcomes.verifier`; la fila de `audit_log` sigue de forma transitoria (la leen
    tests y journeys) hasta retirarla en una HU siguiente."""
    from core.kernel.decisions import anotar_veredicto
    from core.shared.audit import record_audit

    try:
        anotar_veredicto(veredicto_para_turno(veredicto, resultado, motivos), len(motivos))
    except Exception:  # observabilidad: nunca impide el registro ni el turno
        pass

    await record_audit(
        actor="core:verificador",
        action="verificacion",
        conversation_id=conversation_id,
        input_data={
            "resultado": resultado,
            "intentos": intentos,
            "motivos": motivos,
            "urls_quitadas": veredicto.urls_quitadas,
            "conteo": veredicto.conteo,
            "sombra": veredicto.sombra,
            "error": veredicto.error,
        },
    )


def veredicto_para_turno(veredicto: Veredicto, resultado: str, motivos: list[str]) -> list[dict[str, Any]]:
    """`turn_outcomes.verifier`: un elemento por tipo de afirmación, solo códigos y conteos
    (`[{kind, verdict, count}]`, sin texto). `verdict`: `pass`, `fail` (motivos de los
    intentos rechazados y del veredicto final), `unverifiable` (modo sombra) o `error`."""
    if veredicto.error:
        return [{"kind": "verificador", "verdict": "error", "count": 1}]
    salida: list[dict[str, Any]] = []
    fallidos: dict[str, int] = {}
    for motivo in motivos:
        fallidos[motivo] = fallidos.get(motivo, 0) + 1
    for kind, count in sorted(veredicto.conteo.items()):
        if count:
            salida.append({"kind": kind, "verdict": "pass", "count": int(count)})
    if veredicto.urls_quitadas:
        salida.append({"kind": "links_sin_respaldo", "verdict": "fail", "count": int(veredicto.urls_quitadas)})
    for motivo, count in sorted(fallidos.items()):
        salida.append({"kind": motivo, "verdict": "fail", "count": count})
    for kind, count in sorted(veredicto.sombra.items()):
        if count:
            salida.append({"kind": kind, "verdict": "unverifiable", "count": int(count)})
    if resultado == "fallo" and not fallidos and veredicto.motivo:
        salida.append({"kind": veredicto.motivo, "verdict": "fail", "count": 1})
    return salida
