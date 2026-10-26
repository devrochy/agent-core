## El canary se retira solo si empeora y avisa (HU-AOS-32, parte 2)

Con esto, «Probar con una parte» queda disponible (`CANARY_DISPONIBLE = True`). El diseño está en la adenda HU-AOS-32 de ADR-0117 (§5 y §6).

- **Job** (`core/releases/canary_job.py`), dos entradas en el beat:
  - **Cada 5 min** (`core.canary_senal_dura`): si la versión en prueba usa una habilidad retirada, se detiene (`habilidad_retirada`, no bloquea la versión). Si hay señal dura, se retira (`senal_dura`, bloquea la versión).
  - **Cada hora** (`core.canary_no_inferioridad`): el veredicto se decide una sola vez.
    - **Peor:** se retira la versión (`no_inferioridad`).
    - **Listo:** avisa que se puede publicar para todos, más los avisos de costo y lentitud si corresponden.
    - **A los 7 días sin muestra:** avisa que se puede publicar a mano.
  - **Aislamiento y concurrencia:** recorre solo negocios v3, cada uno en su `tenant_scope` y con un `try` por negocio. Por conexión, todo pasa bajo su `FOR UPDATE` y se vuelve a leer el canary. Es idempotente.
- **Avisos** en el centro de notificaciones (categoría «sistema», sección Conexiones, deduplicados por versión y motivo, en la misma transacción que la acción):
  - `core.canary_revertido` (crítico);
  - `core.canary_listo`;
  - `core.canary_sin_muestra`;
  - `core.canary_costo`;
  - `core.canary_lentitud`.
