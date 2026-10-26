## Dos pedidos en un mensaje: detección y pila en sombra (HU-AOS-20, parte 1)

El diseño está en la adenda HU-AOS-20 de ADR-0116. Esta parte solo **mide**: no cambia cómo se atiende ningún turno.

- **Detección:** el Coordinador parte el mensaje por conectores (sección `conectores` del playbook `disparadores`) y signos, sin romper «1.500» ni «p. m.». Puntúa cada parte con los disparadores de la release. Hay un pedido por parte cuando su ganador es fuerte y también es candidato del mensaje completo, sin repetir lo que ya tiene una tarea abierta. Solo en español.
- **Sombra:** con la bandera `pila` en `shadow` y el ruteo encendido, lo que la pila habría abierto queda en `core_decisions.shadow_route.pila` (`abre`, `pausa` y `sobrantes`, según `procedures.maxStack`). Se registra también con el ruteo activo. Con una tarea en foco, un pedido nuevo se anota en pausa (digresión).
- **Reporte:** `agent-core db reporte-ruteo` suma el bloque «Pila en sombra», que compara lo que habría abierto con lo que se inició en las 12 horas siguientes. El reporte del ruteo ignora las filas que solo traen `pila`.
- **Banderas** en `tenants.settings.agent_os`: `pila` (`off` por defecto), `verificar_horarios` y `verificar_preguntas` (`shadow`).
  - Endpoint del master: `GET/PUT /api/v1/admin/platform/tenants/{id}/colaboracion`, auditado como `agent_os_colaboracion`.
  - Lo que todavía no está construido (`pila = active`, verificación `active`) responde 409 `no_disponible`.
  - Las tres banderas están registradas en el catálogo de opciones (grupo «Agent OS»).
- `ReleaseCongelada.procedimientos_kernel()` lee `procedures` del KernelSpec congelado.
- Golden nuevo `tests/fixtures/ruteo/golden_pila.jsonl`, con dos pedidos y negativos con «y»: precisión 1,00 y 14 de 15 aciertos exactos.
