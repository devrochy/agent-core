"""Claves reservadas de los agentes del Agent OS (ADR-0113, HU-AOS-12).

Viven aparte del lector tolerante para que el camino v3 no dependa de él: el lector se retira con
ADR-0191 y estas claves siguen (el Coordinador siempre; el General hasta ADR-0192)."""

from __future__ import annotations

KEY_GENERAL = "general"
KEY_COORDINADOR = "coordinador"
KEYS_RESERVADAS = frozenset({KEY_GENERAL, KEY_COORDINADOR})
