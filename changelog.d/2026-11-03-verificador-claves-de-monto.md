## El verificador reconoce `saldo`, `saldo_total` y `total_vigente` como montos (2026-11-03)

- HU-ARCH-29, ADR-0119. Sigue a «`precio` es clave de dinero»: se revisaron las demás tools de los packs y `MONEY_KEYS` suma
  las que son claramente dinero y hoy no respaldaban lo que el asistente dice: `saldo` y `saldo_total` (consultar saldo y
  opciones de promesa de pack-cobranza) y `total_vigente` (cotización en curso de pack-commerce). `valor` y `valorPeriodo`
  quedan fuera a propósito (genéricos). Solo se amplía la evidencia: un monto que ninguna tool devolvió sigue bloqueado.
- Limitación conocida, sin cambio: el detector solo reconoce `$` o `COP` ANTES de la cifra, así que «200.000 COP» no se
  verifica (pasa con o sin evidencia). Ampliarlo es un cambio de comportamiento aparte; un test lo documenta.
