## Depurador del turno y sección de observabilidad (HU-AOS-28, parte 1)

El diseño está en ADR-0169.

- **`GET /api/v1/admin/turns/{turn_id}`** junta en una lectura lo que pasó en un turno: decisión, pasos (comandos, acciones, motor, escalación) con su resultado sin valores, cambios de estado de las tareas, comprobaciones del verificador, operaciones entre agentes, mensajes enviados y costo. `MessageItem` gana `turn_id` (mensajes del agente) para abrirlo desde el chat. Los turnos del playground no se depuran aquí.
- **Sección `observability`:** las entradas y salidas de las acciones solo se ven con ella y siempre con los datos personales enmascarados (`core/shared/redaccion.py`, redacción al leer). El dueño la tiene; el colaborador no la hereda y solo la recibe con un permiso explícito. El costo sigue exigiendo `costs`.
