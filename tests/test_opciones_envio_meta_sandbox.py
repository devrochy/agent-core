"""SPEC-40 · PRUEBA REAL contra el sandbox de Meta del payload de un QUICK_REPLY (HU-OPC-02, ADR-0172 §3).

**OMITIDA POR DEFECTO.** Solo corre con `OPCIONES_META_SANDBOX=1` y las variables de abajo, las pone el dueño en su
terminal; nada de esto se commitea ni se corre en el CI. Sin la variable no hace red ni lee credenciales.

Qué verifica (lo que el diseño SUPONE y no se pudo confirmar contra la documentación oficial):
1. que Meta ACEPTA un envío de plantilla con un componente `button` / `quick_reply` / `index` / parámetro `payload`
   con la forma de `kernel/opciones/envio.componentes_con_payload` (si Graph responde 400, la forma está mal);
2. que el payload acepta el largo máximo que genera el módulo (67 caracteres) y los 10 botones.

Lo que este test NO puede verificar solo (paso manual, anotado en la HU-OPC-02 y en el PR):
- que al tocar el botón desde el teléfono el webhook entregue `type: "button"` con `button.payload` igual al
  enviado (mirar el log del webhook de `inbound.py`);
- los límites de 25 caracteres del texto del botón y de 10 botones al CREAR la plantilla.
Si algo difiere, se reabre ADR-0172 §3 y se cambia `componentes_con_payload` y su golden en
`tests/test_opciones_envio_cascada.py`.

Variables (todas obligatorias con el flag): `OPCIONES_META_PHONE_NUMBER_ID`, `OPCIONES_META_ACCESS_TOKEN`,
`OPCIONES_META_TO` (el celular del dueño, ya agregado como destinatario del número de prueba), `OPCIONES_META_TEMPLATE`
(nombre de una plantilla APROBADA en `es` con 1 a 3 botones QUICK_REPLY y SIN variables) y `OPCIONES_META_LANGUAGE`
(opcional, `es` por defecto).
"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("OPCIONES_META_SANDBOX") != "1",
        reason="prueba real contra el sandbox de Meta: solo con OPCIONES_META_SANDBOX=1 (la corre el dueño)",
    ),
]


def _env(nombre: str, defecto: str | None = None) -> str:
    valor = os.environ.get(nombre, defecto)
    if not valor:
        pytest.fail(f"falta la variable {nombre}")
    return valor


async def test_meta_acepta_el_quick_reply_con_payload_al_enviar() -> None:
    from core.gateway.channels.whatsapp.outbound import WhatsAppMetaOutbound
    from core.kernel.opciones import envio, payload

    tenant, conv, send = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    secreto = "secreto-de-la-prueba-sandbox"
    botones = int(os.environ.get("OPCIONES_META_BOTONES", "1"))
    payloads = [payload.construir(secreto, "opt", tenant, conv, send, f"o{i}") for i in range(1, botones + 1)]
    assert max(len(p) for p in payloads) <= payload.MAX_PAYLOAD

    wamid = await WhatsAppMetaOutbound().send_template(
        {"phone_number_id": _env("OPCIONES_META_PHONE_NUMBER_ID"), "access_token": _env("OPCIONES_META_ACCESS_TOKEN")},
        _env("OPCIONES_META_TO"),
        _env("OPCIONES_META_TEMPLATE"),
        _env("OPCIONES_META_LANGUAGE", "es"),
        envio.componentes_con_payload(payloads),
    )

    assert wamid, "Meta aceptó el envío y devolvió el id del mensaje; falta tocar el botón y mirar el webhook"
