## Onboarding obligatorio pero saltable y primer agente listo para probar (2026-10-27)

- HU-SH-10. El negocio guarda el **recorrido** del asistente de primer uso en `tenants.settings.onboarding`
  (`pendiente` → `en_curso` → `omitido` | `completo`, sin migración): `GET /admin/onboarding` trae `recorrido`
  (`abrir`: solo al propietario, `avisar`: mientras esté empezado o saltado) y se mueve con
  `POST /admin/onboarding/{avanzar,omitir,reanudar,finalizar}`. `finalizar` lo valida el servidor (409
  `faltan_pasos`). Los pasos del checklist siguen calculándose del estado real (adenda al ADR-0124). Los
  negocios anteriores a `ONBOARDING_DESDE` (vacía = función apagada) quedan `no_aplica`.
- HU-SH-10. `POST /admin/onboarding/dejar-listo` deja el agente del guion listo para probar sin abrirlo a
  clientes: chat web cerrado (`enabled=false`), publicación y activación de su versión. Idempotente; con el gate
  de pass^k cerrado la activación queda `pendiente_de_revision`; 409 `canal_en_vivo` si el chat web ya atiende.
- HU-AOS-57. `POST /admin/playground/message`, `GET /admin/playground` y `POST /admin/playground/reset` aceptan
  `connection_id`: la conversación de prueba (canal `test`, fuera de métricas) se ata a esa conexión y el turno corre
  por su release, es decir, por el agente publicado ahí. Sin él, igual que antes.
- HU-AOS-58. Nueva opción `business_hours_mode` (`bloquea` por defecto | `informativo`): con `informativo` el
  horario es el del equipo humano, el asistente responde 24/7 y lo conoce por una línea del prompt (playbook del
  kernel `horario-de-servicio`). Los negocios existentes no cambian.
