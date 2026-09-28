"""F0 — Spike de Neonize: validar el canal `waweb` (ADR-0074) antes de invertir.

Script MANUAL, fuera del runtime (nada lo importa). Sirve para responder las
preguntas go/no-go del spike:

  1. ¿Instala `neonize` (Go/FFI) en el entorno objetivo?
  2. ¿Parea por QR y sostiene la sesión tras reiniciar el proceso (sin QR otra vez)?
  3. ¿La sesión sobrevive a recrear el contenedor?
  4. ¿Round-trip de texto y media?
  5. ¿Reconecta sola tras una caída de red?

Uso:
    # El venv del proyecto puede no traer `_ctypes` (Python fuente sin libffi).
    # Por eso el spike usa un venv dedicado con un Python con libffi:
    python3.12 -m venv .spike/venv && .spike/venv/bin/pip install neonize
    .spike/venv/bin/python scripts/spike_neonize.py --check   # verifica instalación/versión
    .spike/venv/bin/python scripts/spike_neonize.py --pair    # parea/escuha y hace eco

La sesión de prueba se guarda en `.spike/neonize/` (gitignored). NO usar el
número personal de nadie: por el protocolo no oficial, ese número puede ser
suspendido. Usar un número de repuesto.

Ojo: el contenedor de despliegue (`python:3.12-slim`) SÍ trae `_ctypes`/libffi
(verificado); el problema de `_ctypes` es solo del venv local.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
from pathlib import Path

SESSION_DIR = Path(".spike") / "neonize"


def check() -> int:
    """Verifica que `neonize` importe y reporta su versión (criterio de F0 #1)."""
    try:
        neonize = importlib.import_module("neonize")
    except Exception as exc:  # pragma: no cover - manual
        print(f"[F0] neonize NO está instalado: {exc}")
        print("[F0] instalalo con: pip install neonize")
        return 1
    version = getattr(neonize, "__version__", "desconocida")
    print(f"[F0] neonize OK (versión {version})")
    return 0


def _message_text(message: object) -> str:
    """Extrae el texto de un mensaje entrante (tolerante a la forma del protobuf)."""
    for attr in ("conversation", "extendedTextMessage"):
        value = getattr(message, attr, None)
        if isinstance(value, str) and value:
            return value
        text = getattr(value, "text", None)
        if isinstance(text, str) and text:
            return text
    return ""


async def pair() -> None:  # pragma: no cover - manual, requiere un número real
    """Parea por QR, persiste la sesión y hace eco de los mensajes entrantes.

    Requiere un número de repuesto. Al reiniciar el script, la sesión guardada en
    `.spike/neonize/` debe reconectar SIN pedir QR otra vez.
    """
    from neonize.aioze.client import NewAClient
    from neonize.aioze.events import ConnectedEv, MessageEv, PairStatusEv

    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    client = NewAClient(str(SESSION_DIR / "spike.db"))

    @client.event(ConnectedEv)
    async def _on_connected(_client, _event):
        print("[F0] conectado y listo")

    @client.event(MessageEv)
    async def _on_message(_client, event):
        message = getattr(event, "Message", None)
        text = _message_text(message) if message is not None else ""
        if not text:
            return
        chat = getattr(getattr(event, "Info", None), "Chat", None)
        print(f"[F0] recibido: {text!r}")
        if chat is not None:
            await client.send_message(chat, f"eco: {text}")

    @client.event(PairStatusEv)
    async def _on_pair(_client, _event):
        print("[F0] dispositivo vinculado (sesión persistida)")

    print("[F0] escaneá el QR que aparece abajo con el número de repuesto...")
    await client.connect()
    await client.idle()


def main() -> int:
    parser = argparse.ArgumentParser(description="Spike de Neonize (F0, ADR-0074)")
    parser.add_argument("--check", action="store_true", help="solo verifica la instalación")
    parser.add_argument("--pair", action="store_true", help="parea por QR y hace eco")
    args = parser.parse_args()

    if args.check:
        return check()
    if args.pair:
        asyncio.run(pair())
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
