"""Qué recordatorio de cobro toca hoy (HU-SUB-05). Lógica pura, sin BD.

`dias` son desplazamientos respecto al vencimiento (`-3` = tres días antes, `0` = el
día, `3` = tres días después). Cada uno se envía una sola vez: la idempotencia la da
el outbox (`dedupe_key = sub_reminder:<cargo>:<dias>`).
"""

from __future__ import annotations

from datetime import date, timedelta


def recordatorio_de_hoy(vence: date, creado: date, hoy: date, dias: list[int]) -> int | None:
    """El desplazamiento a recordar hoy, o None.

    - Solo el **último** alcanzado: si el cron estuvo caído varios días, no se manda
      una ráfaga de recordatorios viejos.
    - Uno de antes o del día del vencimiento que ya cubrió el aviso del cargo nuevo
      (el cargo se creó ese día o después) no se repite.
    """
    alcanzados = [d for d in dias if vence + timedelta(days=d) <= hoy]
    if not alcanzados:
        return None
    d = max(alcanzados)
    if d <= 0 and creado >= vence + timedelta(days=d):
        return None
    return d
