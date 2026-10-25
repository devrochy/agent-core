"""Grabar y reproducir las llamadas al LLM de las evals (adenda 2026-10-10 de ADR-0119).

Las evals (`evals/runner.py`, `evals/journeys.py` y el cliente simulado) corren el loop real.
Con este módulo un runner puede **grabar** cada llamada (request normalizado → respuesta) en un
JSON por escena y después **reproducirla** sin red ni `LLM_API_KEY`.

- El modo es un estado del proceso que activa **solo un runner de evals** (`activar`). El servidor
  no lo lee de ninguna variable, y `activar` falla fuera de `APP_ENV` development/test.
- `replay` nunca construye el proveedor real (`ProveedorGrabado` sin `inner`): una llamada que no
  está grabada falla con `GrabacionDesactualizada` y un diff de lo que cambió; jamás cae a la red.
- `record` llama al proveedor real y guarda la respuesta. Gasta saldo: requiere OK del dueño.
- Las llamadas se agrupan por **escena** (un escenario del golden o un journey, con sus k
  intentos), abierta por el runner con `escena(ruta)`. Una llamada sin escena falla.
- Clave = sha256 del request normalizado. Qué se normaliza: ver `_VOLATIL` y `evals/README.md`.
  Los valores volátiles se reemplazan por marcadores numerados (`<uuid:1>`); al grabar, la
  respuesta guarda los marcadores en lugar de los valores del request, y al reproducir se
  rehidratan con los valores de la corrida actual (un `order_id` nuevo sigue siendo válido).
- Intentos repetidos de un mismo request se guardan como secuencia y se reproducen en orden; la
  semilla del intento (`intento(semilla)`) es parte de la clave.

Las grabaciones son datos de prueba, nunca prompts: nada las carga como instrucciones
(invariante 3).
"""

from __future__ import annotations

import contextlib
import contextvars
import difflib
import hashlib
import json
import os
import re
import tempfile
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from core.llm.base import BaseLLMProvider
from core.llm.prompt_builder import CURRENT_DATETIME_HEADER
from core.llm.types import LLMMessage, LLMResponse, LLMToolCall, LLMToolSchema, LLMUsageInfo

Modo = Literal["off", "record", "replay"]
MODOS: tuple[str, ...] = ("off", "record", "replay")
ENTORNOS_PERMITIDOS = frozenset({"development", "test"})
FORMATO = 1
MAX_BYTES = 20_000_000


class ErrorGrabacion(RuntimeError):
    """Error del modo grabar/reproducir. No hereda de `LLMProviderError` a propósito: el loop no
    debe convertirlo en fallback ni en escalamiento; el runner corta la corrida con él."""


class GrabacionProhibida(ErrorGrabacion):
    """El modo no está permitido en este entorno (producción)."""


class SinGrabacion(ErrorGrabacion):
    """Replay sin archivo de grabación para la escena."""


class GrabacionDesactualizada(ErrorGrabacion):
    """Replay de un request que no coincide con lo grabado (cambió el prompt, las tools…)."""


# ------------------------------------------------------------------ estado del proceso
_modo: Modo = "off"
_escena: contextvars.ContextVar[_Escena | None] = contextvars.ContextVar("llm_cassette_escena", default=None)
_semilla: contextvars.ContextVar[int | None] = contextvars.ContextVar("llm_cassette_semilla", default=None)


def activar(modo: str) -> None:
    """Enciende el modo para este proceso. Solo lo llaman los runners de evals."""
    global _modo
    if modo not in MODOS:
        raise ErrorGrabacion(f"Modo de grabación desconocido: {modo!r} (off, record o replay)")
    if modo != "off":
        from core.config import get_settings

        entorno = (get_settings().app_env or "").lower()
        if entorno not in ENTORNOS_PERMITIDOS:
            raise GrabacionProhibida(
                f"El modo {modo!r} de grabación del LLM no está permitido con APP_ENV={entorno!r} "
                "(solo development o test)."
            )
    _modo = modo  # type: ignore[assignment]


def modo_activo() -> Modo:
    return _modo


@contextlib.contextmanager
def escena(ruta: Path, *, regrabar: str = "") -> Iterator[_Escena | None]:
    """Agrupa las llamadas de un escenario o journey (con sus k intentos) en `ruta`.

    Con el modo apagado no hace nada. En `record` escribe el archivo al salir sin error (si hubo
    llamadas), reemplazando el anterior. `regrabar` es el comando que se sugiere en los errores.
    """
    if _modo == "off":
        yield None
        return
    actual = _Escena.cargar(ruta, _modo, regrabar) if _modo == "replay" else _Escena(ruta, _modo, regrabar)
    if _modo == "record":
        actual.secretos.agregar(secretos_de_la_config())
    token = _escena.set(actual)
    try:
        yield actual
    finally:
        _escena.reset(token)
    if _modo == "record" and actual.llamadas:
        actual.escribir()


@contextlib.contextmanager
def intento(semilla: int) -> Iterator[None]:
    """Semilla del intento en curso (journeys y cliente simulado): entra en la clave."""
    token = _semilla.set(semilla)
    try:
        yield
    finally:
        _semilla.reset(token)


# ------------------------------------------------------------------ normalización
# Orden importa: timestamp antes que fecha y UUID antes que hex. Los slugs `journeys-xxxxxxxx`
# los crea `evals/journeys.py` por corrida.
_VOLATIL = re.compile(
    r"(?P<ts>(?<!\d)\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?(?!\d))"
    r"|(?P<uuid>(?<![0-9A-Fa-f])[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}(?![0-9A-Fa-f]))"
    r"|(?P<hex>(?<![0-9A-Fa-f])[0-9a-f]{32}(?![0-9A-Fa-f]))"
    # Tokens de 16 hex con al menos una letra (p. ej. el token de términos del alta, un HMAC de la
    # conversación). Sin letras serían números (teléfonos, tarjetas de prueba) y no se tocan.
    r"|(?P<hex16>(?<![0-9A-Fa-f])(?=[0-9]*[a-f])[0-9a-f]{16}(?![0-9A-Fa-f]))"
    r"|(?P<slug>(?:journeys-[0-9a-f]{8}|gate-eval-[0-9a-f]{10})(?![0-9a-z]))"
    r"|(?P<fecha>(?<!\d)\d{4}-\d{2}-\d{2}(?!\d))"
)
_MARCADOR = re.compile(r"<(ts|uuid|hex16|hex|slug|fecha|call):\d+>")
FECHA_HORA = "<fecha_hora>"


def _sin_fecha_hora(bloque: str) -> str:
    """Quita la fecha y hora de ADR-0091 (y su lista de próximos días) del bloque dinámico."""
    inicio = bloque.find(CURRENT_DATETIME_HEADER)
    if inicio < 0:
        return bloque
    fin = bloque.find("\n\n## ", inicio + len(CURRENT_DATETIME_HEADER))
    resto = bloque[fin:] if fin >= 0 else ""
    return f"{bloque[:inicio]}{CURRENT_DATETIME_HEADER}\n{FECHA_HORA}{resto}"


@dataclass
class _Normalizador:
    """Reemplaza valores volátiles por marcadores numerados por tipo, en orden de aparición."""

    marcadores: dict[str, str] = field(default_factory=dict)  # valor real → marcador
    llamadas: dict[str, str] = field(default_factory=dict)  # id de tool_call → marcador
    _cuentas: dict[str, int] = field(default_factory=dict)

    def _marcador(self, tipo: str, valor: str, tabla: dict[str, str]) -> str:
        if valor not in tabla:
            self._cuentas[tipo] = self._cuentas.get(tipo, 0) + 1
            tabla[valor] = f"<{tipo}:{self._cuentas[tipo]}>"
        return tabla[valor]

    def texto(self, valor: str) -> str:
        def _uno(m: re.Match[str]) -> str:
            tipo = m.lastgroup or "valor"
            return self._marcador(tipo, m.group(0), self.marcadores)

        return _VOLATIL.sub(_uno, valor)

    def dato(self, valor: Any) -> Any:
        if isinstance(valor, str):
            return self.texto(valor)
        if isinstance(valor, list):
            return [self.dato(v) for v in valor]
        if isinstance(valor, dict):
            return {str(k): self.dato(v) for k, v in valor.items()}
        return valor

    def id_llamada(self, valor: str | None) -> str | None:
        return None if valor is None else self._marcador("call", valor, self.llamadas)

    def solo_conocidos(self, valor: Any) -> Any:
        """Para la respuesta al grabar: marca solo los valores que vinieron en el request."""
        if isinstance(valor, str):
            return _VOLATIL.sub(lambda m: self.marcadores.get(m.group(0), m.group(0)), valor)
        if isinstance(valor, list):
            return [self.solo_conocidos(v) for v in valor]
        if isinstance(valor, dict):
            return {k: self.solo_conocidos(v) for k, v in valor.items()}
        return valor

    def rehidratar(self, valor: Any) -> Any:
        """Para la respuesta al reproducir: marcadores → valores de la corrida actual."""
        inverso = {m: v for v, m in self.marcadores.items()}
        if isinstance(valor, str):
            return _MARCADOR.sub(lambda m: inverso.get(m.group(0), m.group(0)), valor)
        if isinstance(valor, list):
            return [self.rehidratar(v) for v in valor]
        if isinstance(valor, dict):
            return {k: self.rehidratar(v) for k, v in valor.items()}
        return valor


def _hash(dato: Any) -> str:
    crudo = json.dumps(dato, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(crudo.encode("utf-8")).hexdigest()


@dataclass
class Pedido:
    """Request normalizado: lo que define la clave."""

    proveedor: str
    modelo: str
    thinking: bool | None
    max_tokens: int
    temperature: float | None
    semilla: int | None
    sistema: list[str]
    herramientas: list[dict[str, Any]] | None
    mensajes: list[dict[str, Any]]
    normalizador: _Normalizador

    def completo(self) -> dict[str, Any]:
        return {
            "proveedor": self.proveedor,
            "modelo": self.modelo,
            "thinking": self.thinking,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "semilla": self.semilla,
            "sistema": self.sistema,
            "herramientas": self.herramientas,
            "mensajes": self.mensajes,
        }

    @property
    def clave(self) -> str:
        return _hash(self.completo())


def normalizar(
    *,
    proveedor: str,
    modelo: str,
    thinking: bool | None,
    system_blocks: list[str],
    messages: list[LLMMessage],
    tools: list[LLMToolSchema] | None,
    max_tokens: int,
    temperature: float | None,
    semilla: int | None = None,
) -> Pedido:
    norm = _Normalizador()
    sistema = [norm.texto(_sin_fecha_hora(b)) for b in system_blocks]
    herramientas = (
        [norm.dato({"name": t.name, "description": t.description, "parameters": t.parameters}) for t in tools]
        if tools
        else None
    )
    mensajes: list[dict[str, Any]] = []
    for m in messages:
        item: dict[str, Any] = {"role": m.role, "content": norm.texto(m.content or "")}
        if m.tool_calls:
            item["tool_calls"] = [
                {"id": norm.id_llamada(tc.id), "name": tc.name, "arguments": norm.dato(tc.arguments)}
                for tc in m.tool_calls
            ]
        if m.tool_call_id is not None:
            item["tool_call_id"] = norm.id_llamada(m.tool_call_id)
        if m.reasoning_content:  # solo con la política de riesgo alto (HU-AOS-09)
            item["reasoning_content"] = norm.texto(m.reasoning_content)
        mensajes.append(item)
    return Pedido(
        proveedor=proveedor,
        modelo=modelo,
        thinking=thinking,
        max_tokens=max_tokens,
        temperature=temperature,
        semilla=semilla,
        sistema=sistema,
        herramientas=herramientas,
        mensajes=mensajes,
        normalizador=norm,
    )


# ------------------------------------------------------------------ escena (archivo)
# Formas conocidas de credenciales: si aparecen, la grabación no se escribe.
_SECRETO = re.compile(
    r"\bsk-[A-Za-z0-9_-]{16,}"  # OpenAI, DeepSeek, Anthropic
    r"|\bEAA[A-Za-z0-9]{20,}"  # token de Meta
    r"|\b(?:pub|prv)_(?:test|prod)_[A-Za-z0-9]{8,}"  # llaves de Wompi
    r"|\b(?:test|prod)_(?:integrity|events)_[A-Za-z0-9]{8,}"  # secretos de Wompi
    r"|\beyJ[\w-]{10,}\.[\w-]{10,}\."  # JWT
    r"|\bAKIA[0-9A-Z]{16}\b"  # AWS
    r"|\bxox[abp]-[A-Za-z0-9-]{10,}"  # Slack
    r"|\bya29\.[\w-]{10,}"  # Google OAuth
    r"|\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"
    r"|[?&](?:access_token|api_key|apikey)=[^&\s\"]{8,}"
    r"|\b[a-z][a-z0-9+.-]*://[^/\s:@\"]+:[^@\s/\"]+@"  # URL con usuario:clave
)
_PALABRAS_SECRETAS = ("key", "secret", "token", "password", "dsn")
_CLAVE_EN_URL = re.compile(r"://[^/\s:@]+:([^@\s/]+)@")


def secretos_de_la_config() -> dict[str, str]:
    """Valores de la config que no pueden terminar en una grabación (valor → de dónde sale): todo
    campo cuyo nombre sugiere un secreto, las claves de un JSON de claves y las contraseñas de las URLs."""
    from core.config import get_settings

    secretos: dict[str, str] = {}
    for nombre, valor in get_settings().model_dump().items():
        if not isinstance(valor, str) or not valor:
            continue
        for clave in _CLAVE_EN_URL.findall(valor):
            secretos[clave] = f"contraseña de {nombre}"
        if any(p in nombre.lower() for p in _PALABRAS_SECRETAS):
            secretos[valor] = nombre
            with contextlib.suppress(ValueError, TypeError, AttributeError):
                anidado = json.loads(valor)
                secretos.update({str(v): nombre for v in anidado.values() if v})
    return {v: o for v, o in secretos.items() if len(v) >= 8}


class Secretos:
    """Valores que no pueden terminar en una grabación (valor → de dónde sale).

    No se puede imprimir: `repr`, `str` y rich muestran solo cuántos hay. Un traceback con
    variables locales (rich, `show_locals`) que pase por una escena o por el proveedor nunca
    muestra la clave. Los valores solo salen por `motivo_en`, que devuelve el ORIGEN, no el valor.
    """

    __slots__ = ("_valores",)

    def __init__(self) -> None:
        self._valores: dict[str, str] = {}

    def agregar(self, valores: dict[str, str]) -> None:
        self._valores.update({v: o for v, o in valores.items() if v and len(v) >= 8})

    def agregar_de(self, otros: Secretos) -> None:
        self._valores.update(otros._valores)

    def motivo_en(self, texto: str) -> str | None:
        """Por qué `texto` no se puede escribir (sin el valor), o None si está limpio."""
        origen = next((o for v, o in self._valores.items() if v in texto), None)
        if origen is not None:
            return f"contiene una credencial de la configuración ({origen})"
        if _SECRETO.search(texto):
            return "contiene algo con forma de credencial (API key, token)"
        return None

    def __len__(self) -> int:
        return len(self._valores)

    def __repr__(self) -> str:
        return f"<Secretos: {len(self._valores)} ocultos>"

    __str__ = __repr__

    def __rich_repr__(self) -> Iterator[tuple[str, int]]:
        yield "ocultos", len(self._valores)


def _respuesta_a_dict(respuesta: LLMResponse, norm: _Normalizador) -> dict[str, Any]:
    uso = respuesta.usage
    return {
        "content": norm.solo_conocidos(respuesta.content or ""),
        "stop_reason": respuesta.stop_reason,
        "tool_calls": [
            {"id": tc.id, "name": tc.name, "arguments": norm.solo_conocidos(tc.arguments)}
            for tc in respuesta.tool_calls
        ],
        "usage": {
            "input_tokens": uso.input_tokens,
            "output_tokens": uso.output_tokens,
            "cache_read_tokens": getattr(uso, "cache_read_tokens", 0),
            "cache_write_tokens": getattr(uso, "cache_write_tokens", 0),
        },
        "model": respuesta.model,
        # Solo si lo hay (DeepSeek con thinking): las grabaciones existentes no cambian.
        **(
            {"reasoning_content": norm.solo_conocidos(respuesta.reasoning_content)}
            if respuesta.reasoning_content
            else {}
        ),
    }


_STOP_REASONS = frozenset({"end_turn", "tool_use", "max_tokens", "refusal"})
_CAMPOS_USO = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def _respuesta_de_dict(dato: dict[str, Any], norm: _Normalizador) -> LLMResponse:
    """La grabación es un dato no confiable (invariante 7): se valida campo por campo."""
    dato = norm.rehidratar(dato)
    stop = dato.get("stop_reason", "end_turn")
    if not isinstance(dato.get("content", ""), str) or stop not in _STOP_REASONS:
        raise ValueError("respuesta con content o stop_reason inválido")
    llamadas = []
    for tc in dato.get("tool_calls") or []:
        if not (isinstance(tc.get("id"), str) and isinstance(tc.get("name"), str) and isinstance(tc.get("arguments", {}), dict)):
            raise ValueError("tool_call inválido")
        llamadas.append(LLMToolCall(id=tc["id"], name=tc["name"], arguments=tc.get("arguments") or {}))
    uso = dato.get("usage") or {}
    return LLMResponse(
        content=dato.get("content", ""),
        stop_reason=stop,
        tool_calls=llamadas,
        usage=LLMUsageInfo(**{c: int(uso.get(c, 0) or 0) for c in _CAMPOS_USO}),
        model=str(dato.get("model", "")),
        latency_ms=0,
        reasoning_content=razonamiento if isinstance(razonamiento := dato.get("reasoning_content", ""), str) else "",
    )


@dataclass
class _Escena:
    ruta: Path
    modo: str
    regrabar: str = ""
    llamadas: dict[str, dict[str, Any]] = field(default_factory=dict)  # clave → llamada (orden de aparición)
    bloques: dict[str, str] = field(default_factory=dict)
    herramientas: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    secretos: Secretos = field(default_factory=Secretos, repr=False)
    existe: bool = True
    _consumo: dict[str, int] = field(default_factory=dict)

    @classmethod
    def cargar(cls, ruta: Path, modo: str, regrabar: str) -> _Escena:
        if not ruta.exists():
            return cls(ruta, modo, regrabar, existe=False)
        if ruta.stat().st_size > MAX_BYTES:
            raise GrabacionDesactualizada(f"{ruta}: la grabación pesa más de {MAX_BYTES // 1_000_000} MB.{cls._pie(regrabar)}")
        try:
            dato = json.loads(ruta.read_text(encoding="utf-8"))
            if dato.get("formato") != FORMATO:
                raise ValueError(f"formato {dato.get('formato')!r}, se esperaba {FORMATO}")
            llamadas = {str(ll["clave"]): ll for ll in dato.get("llamadas", [])}
            for ll in llamadas.values():
                if not isinstance(ll["respuestas"], list) or not isinstance(ll["pedido"], dict):
                    raise ValueError("llamada sin pedido o respuestas")
            bloques = {str(k): str(v) for k, v in dict(dato.get("bloques", {})).items()}
            herramientas = dict(dato.get("herramientas", {}))
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise GrabacionDesactualizada(f"{ruta}: grabación ilegible ({exc}).{cls._pie(regrabar)}") from exc
        return cls(ruta, modo, regrabar, llamadas=llamadas, bloques=bloques, herramientas=herramientas)

    @staticmethod
    def _pie(regrabar: str) -> str:
        comando = f" `{regrabar}`" if regrabar else " el runner con --grabacion record"
        return f"\nPara regrabar (llama al LLM real y gasta saldo: requiere OK del dueño):{comando}"

    # ---- formato en disco
    def _guardar_pedido(self, pedido: Pedido) -> dict[str, Any]:
        sistema = []
        for bloque in pedido.sistema:
            h = _hash(bloque)[:16]
            self.bloques[h] = bloque
            sistema.append(h)
        tools = None
        if pedido.herramientas is not None:
            tools = _hash(pedido.herramientas)[:16]
            self.herramientas[tools] = pedido.herramientas
        return {
            "proveedor": pedido.proveedor,
            "modelo": pedido.modelo,
            "thinking": pedido.thinking,
            "max_tokens": pedido.max_tokens,
            "temperature": pedido.temperature,
            "sistema": sistema,
            "herramientas": tools,
            "mensajes": pedido.mensajes,
        }

    def grabar(self, pedido: Pedido, respuesta: LLMResponse, crudo: str) -> None:
        # Sobre el texto CRUDO (antes de normalizar): un secreto con un UUID adentro no se
        # escaparía partido en marcadores.
        motivo = self.secretos.motivo_en(crudo)
        if motivo:
            del crudo  # que el texto con la credencial no quede en las variables del traceback
            raise ErrorGrabacion(f"{self.ruta}: la grabación {motivo}; no se escribe.")
        clave = pedido.clave
        llamada = self.llamadas.get(clave)
        if llamada is None:
            llamada = {"clave": clave, "semilla": pedido.semilla, "pedido": self._guardar_pedido(pedido), "respuestas": []}
            self.llamadas[clave] = llamada
        llamada["respuestas"].append(_respuesta_a_dict(respuesta, pedido.normalizador))

    def serializar(self) -> str:
        usados_b = {h for ll in self.llamadas.values() for h in ll["pedido"]["sistema"]}
        usados_t = {ll["pedido"]["herramientas"] for ll in self.llamadas.values()} - {None}
        dato = {
            "formato": FORMATO,
            "escena": self.ruta.stem,
            "nota": "Grabación de evals con datos de prueba (adenda de ADR-0119). No es un prompt ni configuración.",
            "llamadas": list(self.llamadas.values()),
            "bloques": {h: t for h, t in self.bloques.items() if h in usados_b},
            "herramientas": {h: t for h, t in self.herramientas.items() if h in usados_t},
        }
        return json.dumps(dato, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    def escribir(self) -> None:
        texto = self.serializar()
        motivo = self.secretos.motivo_en(texto)
        if motivo:
            del texto  # que el texto con la credencial no quede en las variables del traceback
            raise ErrorGrabacion(f"{self.ruta}: la grabación {motivo}; no se escribe.")
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.ruta.parent, prefix=f".{self.ruta.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(texto)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.ruta)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    # ---- reproducir
    def reproducir(self, pedido: Pedido) -> LLMResponse:
        if not self.existe:
            raise SinGrabacion(f"No hay grabación en {self.ruta} y el modo es replay (no se llama al LLM real).{self._pie(self.regrabar)}")
        clave = pedido.clave
        llamada = self.llamadas.get(clave)
        if llamada is None:
            raise GrabacionDesactualizada(self._diagnostico(pedido))
        usadas = self._consumo.get(clave, 0)
        respuestas = llamada["respuestas"]
        if usadas >= len(respuestas):
            raise GrabacionDesactualizada(
                f"Grabación desactualizada ({self.ruta}): el mismo request se pidió {usadas + 1} veces y hay "
                f"{len(respuestas)} respuesta(s) grabada(s). Graba con el mismo k (y los mismos escenarios) que vas a reproducir."
                f"{self._pie(self.regrabar)}"
            )
        self._consumo[clave] = usadas + 1
        try:
            respuesta = _respuesta_de_dict(respuestas[usadas], pedido.normalizador)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise GrabacionDesactualizada(f"{self.ruta}: respuesta grabada ilegible ({exc}).{self._pie(self.regrabar)}") from exc
        # La grabación no puede pedir una tool que no se ofreció en este request.
        ofrecidas = {t["name"] for t in pedido.herramientas or []}
        ajenas = sorted({tc.name for tc in respuesta.tool_calls} - ofrecidas)
        if ajenas:
            raise GrabacionDesactualizada(f"{self.ruta}: la respuesta grabada llama tools no ofrecidas: {ajenas}.{self._pie(self.regrabar)}")
        return respuesta

    def _diagnostico(self, pedido: Pedido) -> str:
        cabeza = f"Grabación desactualizada ({self.ruta}): la llamada al LLM no coincide con ninguna grabada"
        candidatas = [ll for ll in self.llamadas.values() if ll.get("semilla") == pedido.semilla] or list(self.llamadas.values())
        if not candidatas:
            return f"{cabeza} (la grabación no tiene llamadas).{self._pie(self.regrabar)}"
        ahora = self._guardar_pedido_sin_registrar(pedido)
        mejor = max(candidatas, key=lambda ll: _parecido(ll["pedido"], ahora))
        numero = list(self.llamadas.values()).index(mejor) + 1
        cambios = self._cambios(mejor["pedido"], pedido)
        detalle = "\n".join(f"  - {c}" for c in cambios) or "  - (sin diferencias visibles: revisa la semilla o el orden de los intentos)"
        semilla = f", semilla {pedido.semilla}" if pedido.semilla is not None else ""
        return f"{cabeza}{semilla}. Lo más parecido es la llamada #{numero}; qué cambió:\n{detalle}{self._pie(self.regrabar)}"

    def _guardar_pedido_sin_registrar(self, pedido: Pedido) -> dict[str, Any]:
        return {
            "proveedor": pedido.proveedor,
            "modelo": pedido.modelo,
            "thinking": pedido.thinking,
            "max_tokens": pedido.max_tokens,
            "temperature": pedido.temperature,
            "sistema": [_hash(b)[:16] for b in pedido.sistema],
            "herramientas": _hash(pedido.herramientas)[:16] if pedido.herramientas is not None else None,
            "mensajes": pedido.mensajes,
        }

    def _cambios(self, grabado: dict[str, Any], pedido: Pedido) -> list[str]:
        cambios: list[str] = []
        for campo in ("proveedor", "modelo", "thinking", "max_tokens", "temperature"):
            antes, ahora = grabado.get(campo), getattr(pedido, campo)
            if antes != ahora:
                cambios.append(f"{campo}: grabado {antes!r}, ahora {ahora!r}")
        antes_s = [self.bloques.get(h, "") for h in grabado.get("sistema", [])]
        if len(antes_s) != len(pedido.sistema):
            cambios.append(f"prompt del sistema: {len(antes_s)} bloques grabados, ahora {len(pedido.sistema)}")
        for i, (a, b) in enumerate(zip(antes_s, pedido.sistema, strict=False)):
            if a != b:
                cambios.append(f"prompt del sistema, bloque {i + 1} ({_titulo(b)}):\n{_diff(a, b)}")
        cambios += _cambios_tools(self.herramientas.get(grabado.get("herramientas") or "", None), pedido.herramientas)
        antes_m, ahora_m = grabado.get("mensajes", []), pedido.mensajes
        for i, (a, b) in enumerate(zip(antes_m, ahora_m, strict=False)):
            if a != b:
                cambios.append(f"mensaje {i + 1} ({b.get('role')}): grabado {_corto(a)}, ahora {_corto(b)}")
                break
        if len(antes_m) != len(ahora_m):
            cambios.append(f"mensajes: {len(antes_m)} grabados, ahora {len(ahora_m)}")
        return cambios


def _parecido(a: dict[str, Any], b: dict[str, Any]) -> int:
    puntos = sum(a.get(c) == b.get(c) for c in ("proveedor", "modelo", "thinking", "max_tokens", "temperature", "herramientas"))
    puntos += sum(x == y for x, y in zip(a.get("sistema", []), b.get("sistema", []), strict=False))
    comunes = 0
    for x, y in zip(a.get("mensajes", []), b.get("mensajes", []), strict=False):
        if x != y:
            break
        comunes += 1
    misma_longitud = len(a.get("mensajes", [])) == len(b.get("mensajes", []))
    return puntos + 2 * comunes + 3 * misma_longitud


def _titulo(bloque: str) -> str:
    primera = next((linea.strip() for linea in bloque.splitlines() if linea.strip()), "")
    return primera[:60] or "vacío"


def _diff(antes: str, ahora: str, maximo: int = 12) -> str:
    lineas = [
        linea
        for linea in difflib.unified_diff(antes.splitlines(), ahora.splitlines(), "grabado", "ahora", n=0, lineterm="")
        if not linea.startswith(("---", "+++", "@@"))
    ]
    extra = f"\n      … ({len(lineas) - maximo} líneas más)" if len(lineas) > maximo else ""
    # El bloque viene de un JSON no confiable: sin caracteres de control (ANSI) en los logs del CI.
    return "\n".join(f"      {_CONTROL.sub('?', linea[:160])}" for linea in lineas[:maximo]) + extra


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _corto(mensaje: dict[str, Any]) -> str:
    texto = json.dumps(mensaje, ensure_ascii=False, sort_keys=True)
    return repr(texto[:160] + ("…" if len(texto) > 160 else ""))


def _cambios_tools(antes: list[dict[str, Any]] | None, ahora: list[dict[str, Any]] | None) -> list[str]:
    a = {t["name"]: t for t in antes or []}
    b = {t["name"]: t for t in ahora or []}
    cambios = []
    if sorted(set(b) - set(a)):
        cambios.append(f"herramientas agregadas: {sorted(set(b) - set(a))}")
    if sorted(set(a) - set(b)):
        cambios.append(f"herramientas quitadas: {sorted(set(a) - set(b))}")
    distintas = sorted(n for n in set(a) & set(b) if a[n] != b[n])
    if distintas:
        cambios.append(f"herramientas con descripción o parámetros distintos: {distintas}")
    if not cambios and [t["name"] for t in antes or []] != [t["name"] for t in ahora or []]:
        cambios.append("herramientas en otro orden")
    return cambios


def _texto_crudo(system_blocks: list[str], messages: list[LLMMessage], respuesta: LLMResponse) -> str:
    """Todo el texto del request y la respuesta, sin normalizar, para buscar credenciales."""
    return json.dumps(
        [
            system_blocks,
            [[m.content, [tc.arguments for tc in m.tool_calls]] for m in messages],
            respuesta.content,
            [tc.arguments for tc in respuesta.tool_calls],
            # El razonamiento de DeepSeek también se escanea (HU-AOS-09): puede repetir datos.
            [m.reasoning_content for m in messages if m.reasoning_content],
            respuesta.reasoning_content,
        ],
        ensure_ascii=False,
        default=str,
    )


# ------------------------------------------------------------------ proveedor
class ProveedorGrabado(BaseLLMProvider):
    """Envoltorio del proveedor resuelto (`core.llm.resolve_provider`).

    `inner` es `None` en replay: no hay proveedor real que pueda salir a la red.
    """

    def __init__(
        self,
        *,
        modo: str,
        inner: Any | None,
        provider_name: str,
        model: str,
        thinking: bool | None,
        pricing_per_mtok: tuple[float, float],
        secretos: tuple[str, ...] = (),
    ) -> None:
        if modo not in ("record", "replay"):
            raise ErrorGrabacion(f"ProveedorGrabado no aplica al modo {modo!r}")
        if modo == "replay" and inner is not None:
            raise ErrorGrabacion("En replay no se construye el proveedor real")
        if modo == "record" and inner is None:
            raise ErrorGrabacion("record necesita el proveedor real")
        self.modo = modo
        self.inner = inner
        self.provider_name = provider_name
        self.model = model
        self.thinking = thinking
        self.pricing_per_mtok = pricing_per_mtok
        # Nunca un atributo con la clave en claro: `Secretos` no se puede imprimir.
        self._secretos = Secretos()
        self._secretos.agregar(dict.fromkeys(secretos, "API key del proveedor (LLM_API_KEY o BYOK)"))

    async def generate(
        self,
        system_blocks: list[str],
        messages: list[LLMMessage],
        tools: list[LLMToolSchema] | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> LLMResponse:
        actual = _escena.get()
        if actual is None:
            raise ErrorGrabacion(
                f"Llamada al LLM fuera de una escena con el modo {self.modo!r}: el runner debe abrir "
                "`cassette.escena(ruta)` por escenario o journey."
            )
        pedido = normalizar(
            proveedor=self.provider_name,
            modelo=self.model,
            thinking=self.thinking,
            system_blocks=system_blocks,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            semilla=_semilla.get(),
        )
        if self.modo == "replay":
            return actual.reproducir(pedido)
        assert self.inner is not None
        actual.secretos.agregar_de(self._secretos)
        respuesta = await self.inner.generate(
            system_blocks=system_blocks, messages=messages, tools=tools, max_tokens=max_tokens, temperature=temperature
        )
        actual.grabar(pedido, respuesta, _texto_crudo(system_blocks, messages, respuesta))
        return respuesta

    async def stream(  # type: ignore[override]
        self,
        system_blocks: list[str],
        messages: list[LLMMessage],
        tools: list[LLMToolSchema] | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        raise ErrorGrabacion("El streaming no se graba ni se reproduce (las evals usan generate).")
        yield ""  # pragma: no cover - hace de esto un generador async
