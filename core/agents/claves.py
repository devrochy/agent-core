"""Claves reservadas de los agentes del Agent OS (ADR-0113, HU-AOS-12).

Solo el Coordinador tiene clave reservada: el General se retiró con ADR-0192 y `general` es una
clave más para un agente nuevo."""

from __future__ import annotations

KEY_COORDINADOR = "coordinador"
KEYS_RESERVADAS = frozenset({KEY_COORDINADOR})
