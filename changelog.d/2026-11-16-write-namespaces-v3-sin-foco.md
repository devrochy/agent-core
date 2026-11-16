## Memoria fail-closed en todo turno v3 sin agente en foco (2026-11-16)

- `write_namespaces_del_turno()` devuelve `frozenset()` (ninguna escritura) siempre que el turno `exige_concesion()` y no hay agente en foco del negocio actual: release fijada sin foco, negocio v3 sin release o ranura de otro negocio. Antes, los dos últimos casos daban `None` y las tools de memoria trataban el turno como legacy, sin chequear `writeNamespaces`.
- Legacy (ranura sin marcar, bandera apagada o conexión interna del Centro de pruebas) sigue en `None`. El Coordinador y el turno de solo lectura no cambian.
- Hallazgo del auditor multitenant sobre HU-ARCH-35. `update_customer_profile` queda negado de inmediato; `remember_fact` lo queda cuando su chequeo de HU-ARCH-35 (core#815) esté en develop.
