"""Formas de pago del agente de Ventas (SPEC-48, HU-COM-21, ADR-0203).

El dominio «Formas de pago» tiene tres formas fijas: contra entrega (sin procedimiento),
transferencia (con cuentas y comprobante) y pago en línea (con el proveedor del negocio). El dueño
las activa o apaga por agente en `CRMConfig.payment_forms`; la entrega y los datos que se le piden
al cliente siguen en la política de datos del agente (HU-CRM-28), no aquí.

Reglas puras, sin base ni red: el estado de cada forma («Activa», «Falta configurar», «Apagada») y
los métodos que el agente puede ofrecer. Sin configuración guardada (`payment_forms is None`) se
reproduce el comportamiento de antes: contra entrega siempre, transferencia si hay cuentas y pago
en línea si el negocio cobra en línea. Las cuentas nunca salen de aquí hacia el modelo: solo ids.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

CONTRA_ENTREGA = "efectivo_contraentrega"
TRANSFERENCIA = "transferencia"
PAGO_EN_LINEA = "pago_en_linea"
METODOS: tuple[str, ...] = (CONTRA_ENTREGA, TRANSFERENCIA, PAGO_EN_LINEA)
ETIQUETAS = {
    CONTRA_ENTREGA: "Contra entrega",
    TRANSFERENCIA: "Transferencia",
    PAGO_EN_LINEA: "Pago en línea",
}
# Superpoder del dominio que cada forma necesita (contra entrega no se comprueba: no tiene).
SUPERPODER = {
    TRANSFERENCIA: "pack_commerce.comprobar_transferencia",
    PAGO_EN_LINEA: "pack_commerce.pago_en_linea",
}
MAX_INSTRUCCIONES = 500
MAX_CUENTAS = 10
_CAMPOS_DE_CUENTA = ("entity", "accountType", "accountNumber", "holderName", "holderDocument")
_SOLO_DIGITOS = re.compile(r"\D")

Estado = Literal["activa", "falta_configurar", "apagada"]


@dataclass(frozen=True)
class EstadoDeForma:
    method: str
    label: str
    enabled: bool
    estado: Estado
    # Qué falta, en palabras del dueño («Agrega una cuenta…»); vacío si nada.
    falta: str
    instructions: str
    superpoder: str | None


def normalizar_formas(crudo: Any) -> list[dict[str, Any]] | None:
    """`payment_forms` saneado: las tres formas en orden fijo, sin repetidos ni métodos inventados.
    `None` se conserva (negocio sin configurar). Levanta `ValueError` con un mensaje para el dueño."""
    if crudo is None:
        return None
    if not isinstance(crudo, list):
        raise ValueError("Las formas de pago tienen que ser una lista.")
    por_metodo: dict[str, dict[str, Any]] = {}
    for i, forma in enumerate(crudo):
        if not isinstance(forma, dict):
            raise ValueError(f"La forma de pago {i + 1} no es un objeto.")
        metodo = str(forma.get("method") or "")
        if metodo not in METODOS:
            raise ValueError(f"La forma de pago {i + 1} no es una forma conocida.")
        if metodo in por_metodo:
            raise ValueError(f"La forma «{ETIQUETAS[metodo]}» está repetida.")
        instrucciones = str(forma.get("instructions") or "").strip()
        if len(instrucciones) > MAX_INSTRUCCIONES:
            raise ValueError(f"Las instrucciones de «{ETIQUETAS[metodo]}» pasan de {MAX_INSTRUCCIONES} caracteres.")
        por_metodo[metodo] = {"method": metodo, "enabled": bool(forma.get("enabled", False)), "instructions": instrucciones}
    salida = [por_metodo.get(m, {"method": m, "enabled": False, "instructions": ""}) for m in METODOS]
    if not any(f["enabled"] for f in salida):
        raise ValueError("Tiene que quedar al menos una forma de pago activa.")
    return salida


def normalizar_cuentas(crudo: Any) -> list[dict[str, str]]:
    """Cuentas de transferencia con los cinco campos de texto; entidad y número son obligatorios."""
    if not crudo:
        return []
    if not isinstance(crudo, list):
        raise ValueError("Las cuentas de transferencia tienen que ser una lista.")
    if len(crudo) > MAX_CUENTAS:
        raise ValueError(f"Puedes registrar hasta {MAX_CUENTAS} cuentas de transferencia.")
    salida: list[dict[str, str]] = []
    for i, cuenta in enumerate(crudo):
        if not isinstance(cuenta, dict):
            raise ValueError(f"La cuenta {i + 1} no es un objeto.")
        limpia = {c: str(cuenta.get(c) or "").strip()[:80] for c in _CAMPOS_DE_CUENTA}
        if not limpia["entity"] or not limpia["accountNumber"]:
            raise ValueError(f"La cuenta {i + 1} necesita la entidad y el número.")
        salida.append(limpia)
    return salida


def sanear_cuentas(crudo: Any) -> list[dict[str, str]]:
    """Versión tolerante para cargar lo guardado: nunca levanta; descarta lo que no es una cuenta."""
    if not isinstance(crudo, list):
        return []
    return [
        {c: str(cuenta.get(c) or "").strip()[:80] for c in _CAMPOS_DE_CUENTA}
        for cuenta in crudo[:MAX_CUENTAS]
        if isinstance(cuenta, dict) and (cuenta.get("entity") or cuenta.get("accountNumber"))
    ]


def ultimos_4(numero: str) -> str:
    """Los últimos 4 dígitos de un número de cuenta (lo único que se muestra o se compara fuera)."""
    return _SOLO_DIGITOS.sub("", numero or "")[-4:]


def estados(
    payment_forms: list[dict[str, Any]] | None, cuentas: list[dict[str, Any]] | None, pago_en_linea_disponible: bool
) -> list[EstadoDeForma]:
    """Estado de las tres formas. Una forma activa a la que le falta algo no se ofrece."""
    hay_cuentas = bool(cuentas)
    guardadas = {f["method"]: f for f in (payment_forms or [])}
    salida: list[EstadoDeForma] = []
    for metodo in METODOS:
        if payment_forms is None:
            encendida = metodo == CONTRA_ENTREGA or (metodo == TRANSFERENCIA and hay_cuentas) or (
                metodo == PAGO_EN_LINEA and pago_en_linea_disponible
            )
            instrucciones = ""
        else:
            forma = guardadas.get(metodo) or {}
            encendida = bool(forma.get("enabled"))
            instrucciones = str(forma.get("instructions") or "")
        falta = ""
        if metodo == TRANSFERENCIA and not hay_cuentas:
            falta = "Agrega al menos una cuenta para recibir transferencias."
        elif metodo == PAGO_EN_LINEA and not pago_en_linea_disponible:
            falta = "Conecta una cuenta de pagos en línea (Wompi) para enviar links de pago."
        estado: Estado = "apagada" if not encendida else ("falta_configurar" if falta else "activa")
        salida.append(
            EstadoDeForma(metodo, ETIQUETAS[metodo], encendida, estado, falta, instrucciones, SUPERPODER.get(metodo))
        )
    return salida


def metodos_activos(
    payment_forms: list[dict[str, Any]] | None, cuentas: list[dict[str, Any]] | None, pago_en_linea_disponible: bool
) -> list[str]:
    """Los métodos que el agente puede ofrecer y aceptar: encendidos y con todo lo que necesitan."""
    return [e.method for e in estados(payment_forms, cuentas, pago_en_linea_disponible) if e.estado == "activa"]


# ------------------------------------------------------------------ lectura (con I/O)
async def pago_en_linea_disponible() -> bool:
    """El negocio cobra en línea si algún pack habilitado ofrece la capacidad de pagos."""
    from core.agents.spec import enabled_packs, load_active_spec
    from core.payments import payment_registry
    from core.shared.tenant import get_current_tenant_id

    spec = await load_active_spec(get_current_tenant_id())
    return payment_registry.capability_for(enabled_packs(spec)) is not None


async def config_del_agente(agent_id: Any) -> Any:
    """`CRMConfig` del negocio con lo propio del agente encima (ADR-0190 §3); None si no hay."""
    try:
        from core.plugin_manager import plugin_manager

        config = await plugin_manager.load_config("pack_commerce", agent_id=agent_id)
        if config is not None or agent_id is None:
            return config
        # El negocio nunca guardó su config de Comercio: lo propio del agente vale solo.
        from core.agents.instalaciones_agente import config_del_agente as propia
        from core.shared.tenant import get_current_tenant_id
        from pack_commerce.config import CRMConfig

        tenant_id = get_current_tenant_id()
        propios = await propia(tenant_id, agent_id, "pack_commerce") if tenant_id else None
        return CRMConfig.model_validate(propios) if propios else None
    except Exception:
        return None


async def estados_del_agente(agent_id: Any) -> list[EstadoDeForma]:
    config = await config_del_agente(agent_id)
    return estados(
        getattr(config, "payment_forms", None),
        getattr(config, "transfer_accounts", None),
        await pago_en_linea_disponible(),
    )


async def metodos_del_agente(agent_id: Any) -> list[str]:
    return [e.method for e in await estados_del_agente(agent_id) if e.estado == "activa"]


async def metodos_configurados(agent_id: Any) -> list[str] | None:
    """Los métodos activos SOLO si el agente guardó sus formas de pago; `None` si no las guardó.
    Con `None`, `crear_pedido` y el resolver siguen como antes de SPEC-48 (no se endurece nada a un
    negocio que no configuró): la regla nueva aplica cuando el dueño decide sus formas."""
    config = await config_del_agente(agent_id)
    guardadas = getattr(config, "payment_forms", None)
    if guardadas is None:
        return None
    return metodos_activos(guardadas, getattr(config, "transfer_accounts", None), await pago_en_linea_disponible())
