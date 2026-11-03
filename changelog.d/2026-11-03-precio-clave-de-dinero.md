## El precio del listado de planes respalda lo que el asistente dice (2026-11-03)

- ADR-0185 (seguimiento), HU-SUB-37. `precio` pasa a ser clave de dinero del verificador (`MONEY_KEYS`): `sub_listar_planes` devuelve el precio del plan bajo esa clave y, sin ella, un «$200.000» tomado del listado se bloqueaba con `monto_sin_evidencia` y el cliente recibía un escalamiento en vez del precio (solo pasaba «200.000 COP»). Un monto que la tool no devolvió se sigue bloqueando. Hoy solo esa tool devuelve `precio`; no cambia ningún prompt ni cassette.
