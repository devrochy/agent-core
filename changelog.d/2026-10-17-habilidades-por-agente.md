## Habilidades por conexión y procedimientos por agente, parte 1 (2026-10-17)

- **HU-AOS-16 (parte 1), adenda de ADR-0118.** Cada agente de una release ve solo los procedimientos
  que declara su versión, resueltos en el build a una versión exacta contra lo que publica la versión
  congelada de cada habilidad (`release/2`: `components[].procedures` e `is_default`).
- **API nueva** `/api/v1/admin/skills/installations` (GET/POST/DELETE): instala una habilidad solo en
  una conexión; esa conexión congela en su próxima release solo sus instalaciones. El pack tiene que
  estar habilitado en el negocio (409 `pack_not_enabled`); el scope de negocio sigue siendo la
  habilitación de siempre.
- **El General del lector tolerante declara** los procedimientos publicados de los packs habilitados
  (`^M.0`): con él por defecto, el modelo ve lo mismo que sin release.
- **Oferta fail-closed por agente y por scope** con el motor activo: nada de un pack fuera de la
  release; `iniciar_procedimiento` solo con los procedimientos del agente en foco.
- **Cambio de release sin escalar:** una tarea sigue si la release nueva trae el mismo procedimiento
  con el mismo major.
- El detalle de una versión de agente (`GET /admin/agents/{id}/versions/{vid}`) trae sus
  `procedures` resueltos.
- **Sin cambio** con las banderas apagadas; sin migración. El primer build tras el deploy crea una
  release nueva por conexión (`release/2` entra al hash).
- **Un solo modelo de agentes de la release** con HU-AOS-15: los procedimientos por agente cuelgan del
  `AgenteCongelado` y se aplican al especialista en foco que elige `elegir_foco`; con `release/2`, el
  `is_default` de la conexión gana sobre el `routing.defaultAgent` del KernelSpec.
