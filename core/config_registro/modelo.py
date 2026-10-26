"""Forma de una opción de configuración del negocio (SPEC-37, ADR-0166).

El registro describe cada opción para las personas (panel y APK): qué es, qué valores acepta,
qué cambia y quién la edita. **No guarda nada**: el valor sigue viviendo donde ya vivía
(`almacen`). Las etiquetas y explicaciones son texto de interfaz y nunca entran al contexto
del LLM (invariante 3): ningún módulo del runtime de turnos importa este paquete, y una
prueba lo vigila.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

Grupo = Literal[
    "identidad",
    "voz",
    "respuesta",
    "escalamiento",
    "horario",
    "calidad",
    "bajas",
    "whatsapp_web",
    # HU-CFG-07: el Coordinador de la conexión (KernelSpec) y las banderas del Agent OS.
    "coordinador",
    "agent_os",
]
# De quién es el valor: del negocio o de una conexión (un negocio tiene N). Sale del almacén.
Ambito = Literal["negocio", "conexion"]
Tipo = Literal[
    "texto",
    "texto_largo",
    "entero",
    "decimal",
    "booleano",
    "elegir_una",
    "elegir_varias",
    "lista_de_textos",
    "franjas",
]
EditablePor = Literal["dueno", "master"]

_ALMACENES = frozenset({"columna", "behavior_config", "escalation_config", "spec", "kernelspec", "tenants"})
_TIPOS_CON_VALORES = frozenset({"elegir_una", "elegir_varias"})
_TIPOS_CON_RANGO = frozenset({"entero", "decimal"})


class ValorPosible(BaseModel):
    """Un valor que el usuario puede escoger, con su etiqueta en español."""

    model_config = ConfigDict(extra="forbid")

    valor: str
    etiqueta: str
    # Aclaración corta bajo la etiqueta (por ejemplo «espera 15 s»).
    nota: str = ""


class Dependencia(BaseModel):
    """La opción solo tiene efecto si se cumple una condición sobre otra."""

    model_config = ConfigDict(extra="forbid")

    clave: str
    # `igual` compara contra `valor`; las otras dos solo miran la otra opción.
    condicion: Literal["no_vacia", "verdadera", "igual"]
    valor: str | None = None
    motivo: str


class Opcion(BaseModel):
    """Una opción de configuración. `extra="forbid"`: un campo mal escrito falla al importar."""

    model_config = ConfigDict(extra="forbid")

    clave: str
    grupo: Grupo
    tipo: Tipo
    etiqueta: str
    explicacion: str
    ejemplo: str = ""
    # Unidad que se muestra junto al número («segundos», «caracteres», «ms»).
    unidad: str = ""
    valores: list[ValorPosible] = Field(default_factory=list)
    minimo: float | None = None
    maximo: float | None = None
    # Referencia a la constante del runtime, no un número copiado a mano.
    por_defecto: Any = None
    depende_de: Dependencia | None = None
    editable_por: EditablePor = "dueno"
    solo_lectura: bool = False
    # Dónde vive el valor: `columna:<nombre>`, `behavior_config:<clave>`,
    # `escalation_config:<clave>[.<subclave>]`, `spec:persona.<campo>`, `kernelspec:<ruta.punteada>`
    # (versión del agente Coordinador de la conexión) o `tenants:<columna>` (banderas del Agent OS).
    almacen: str

    @model_validator(mode="after")
    def _coherente(self) -> Opcion:
        if self.depende_de and (self.depende_de.condicion == "igual") != (self.depende_de.valor is not None):
            raise ValueError(f"{self.clave}: `valor` va con la condición `igual` y solo con ella")
        if self.tipo in _TIPOS_CON_VALORES and not self.valores and not self.valores_calculados:
            raise ValueError(f"{self.clave}: una opción de elegir necesita sus valores")
        if self.tipo in _TIPOS_CON_RANGO and (self.minimo is None or self.maximo is None):
            raise ValueError(f"{self.clave}: un número necesita mínimo y máximo")
        if self.tipo not in _TIPOS_CON_RANGO and (self.minimo is not None or self.maximo is not None):
            raise ValueError(f"{self.clave}: solo los números llevan rango")
        prefijo = self.almacen.split(":", 1)[0]
        if prefijo not in _ALMACENES:
            raise ValueError(f"{self.clave}: almacén desconocido «{self.almacen}»")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ambito(self) -> Ambito:
        """Derivado del almacén (una sola fuente): lo del KernelSpec es de la conexión."""
        return "conexion" if self.almacen.startswith("kernelspec:") else "negocio"

    @property
    def valores_calculados(self) -> bool:
        """Las opciones cuya lista depende del negocio (hoy, los guardrails que se pueden apagar)."""
        return self.clave in OPCIONES_CALCULADAS_POR_NEGOCIO


# Claves cuyos `valores` los completa el servidor por negocio (`catalogo.py`).
OPCIONES_CALCULADAS_POR_NEGOCIO = frozenset({"disabled_guardrails"})
