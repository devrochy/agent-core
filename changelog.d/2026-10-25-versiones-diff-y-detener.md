## Versiones de una conexión: diferencias, detener y permisos en el gate (HU-AOS-27, backend)

Es la base del panel de versiones. El diseño está en la adenda HU-AOS-27 de ADR-0117.

- **`GET /api/v1/admin/connections/{id}/releases/{rid}/diff?against=`.** Compara contra otra versión de la misma conexión (por defecto, la activa) y agrupa los cambios en:
  - agentes: alta, baja, otra versión o cambio de agente por defecto, con las secciones del contenido que cambiaron (nunca textos) y las herramientas y procedimientos añadidos o quitados;
  - superpoderes, con su nombre;
  - permisos A2A;
  - otros: ajustes, playbooks y flujo;
  - qué medirá el gate.

  Si la otra versión es de otra conexión, responde 404.
- **`POST .../releases/{rid}/stop`.** El dueño detiene una candidata y deja de poder activarse. Errores 409: `ya_activa`, `es_canary` o `ya_detenida`. Como el build es idempotente, reconstruir el mismo contenido responde 409 `version_detenida`.
- **Gate:**
  - Cambiar un permiso A2A ahora toca los procedimientos de los agentes del enlace (`permiso:a>b`).
  - Un veredicto aprobado solo vale si cubrió todo lo que se toca hoy.
- **Listado y detalle:** suman `lifecycle` (último evento de ciclo de vida) y `detenida`.
