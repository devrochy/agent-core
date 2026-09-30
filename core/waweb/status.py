"""Estados de la sesión `waweb` (ADR-0074), alineados con `WaWebSession.status`."""

from __future__ import annotations

PENDING_QR = "pending_qr"
CONNECTING = "connecting"
CONNECTED = "connected"
DISCONNECTED = "disconnected"
LOGGED_OUT = "logged_out"
ERROR = "error"

ALL = (PENDING_QR, CONNECTING, CONNECTED, DISCONNECTED, LOGGED_OUT, ERROR)
