## El verificador de afirmaciones reconoce `precio` y otras claves de consulta como montos (2026-11-03)

- HU-ARCH-29, ADR-0119/0185. El listado de planes de `pack_subscriptions` devuelve el precio bajo `precio`, que no estaba
  en `MONEY_KEYS`: con el verificador real la respuesta «$200.000» se bloqueaba como `monto_sin_evidencia` aunque el precio
  saliera de la tool (afectaba a «informar planes» y a las etapas de `contratar_plan` que listan planes).
- `MONEY_KEYS` suma `precio`, `saldo`, `saldo_total` (consulta y promesa de cobranza) y `total_vigente` (cotización en curso
  de comercio). `valor` y `valorPeriodo` quedan fuera a propósito (genéricos). Solo se amplía la evidencia: un monto que
  ninguna tool devolvió sigue bloqueado.
- Limitación conocida, sin cambio: el detector solo reconoce `$` o `COP` ANTES de la cifra, así que «200.000 COP» no se
  verifica. Ampliarlo es un cambio de comportamiento aparte.
