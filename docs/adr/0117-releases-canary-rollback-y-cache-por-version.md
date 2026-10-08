# ADR-0117 — Releases por conexión, canary sticky por episodio, rollback en un clic y caché por versión

- **Estado:** propuesto
- **Fecha:** 2026-10-08
- **Contexto:** HU-AOS-14, HU-AOS-32 y HU-AOS-35 (SPEC-26). Generaliza ADR-0009; reemplaza en parte ADR-0016;
  enmienda ADR-0051 §1 (rollback por *flip* de `is_active`), ADR-0026 y ADR-0109 (overrides y secciones del negocio).

## Contexto

- ADR-0009 pidió releases versionadas con canary al 5 % y rollback en un paso.
- ADR-0016 redefinió el canary como gate de evals, negocio de staging y rollback por *flip* de `is_active` (llevado al panel por ADR-0051 §1), porque `load_active_spec` elige una sola versión por negocio y no hay reparto de tráfico.
- Los overrides de playbooks del negocio (ADR-0026) y sus secciones de flujo (ADR-0109) viven en `TENANTS_ROOT`, fuera de cualquier versión.
- En producción ninguna conversación se cierra: solo el playground la pasa a `closed` (`core/admin/playground.py:115`).
- Con agentes, procedimientos, permisos y políticas por conexión, un rollback por partes deja combinaciones nunca probadas.

## Decisión

Cada conexión publica releases inmutables, con canary sticky por episodio, rollback en un clic y la versión dentro de cada clave de caché.

1. **Versiones inmutables:** `agent_versions`, `policy_versions`, `a2a_graph_versions` y el contenido de las habilidades (`pack_versions`), con `prevent_mutation` y hash del contenido.
2. **Release por conexión:**
   - `connection_releases` guarda el manifiesto, `release_components` las versiones de cada instancia y `release_policies` sus políticas.
   - El build congela también lo que hoy vive en el disco: overrides de playbooks (ADR-0026) y secciones `tenant` del flujo (ADR-0109). El manifiesto guarda su texto y hash, y en runtime se leen de la release, no de `TENANTS_ROOT`.
   - El ciclo de vida queda en `release_events`, append-only.
   - La conexión guarda punteros `active_release_id` y `canary_release_id`. Las FKs compuestas impiden apuntar a la release de otra conexión.
3. **Sticky por episodio:**
   - Un episodio es un tramo de conversación sin pausas mayores que `episode.idleMinutes` (KernelSpec; 720 min por defecto, a validar).
   - La conversación conserva su release durante el episodio. Al empezar el siguiente, el Coordinador asigna la release activa o la candidata.
   - El **canary** reparte por `hash(conversación) % 100` (≤ 50 %), estable entre episodios. El reparto lo hace el Coordinador, sin balanceador: se levanta la objeción de ADR-0016.
   - Un rollback, o un canary detenido, reasigna en su siguiente turno las conversaciones de la release retirada.
   - Al cambiar de release, cada tarea abierta se vuelve a validar contra la versión del procedimiento de la release nueva: conserva los datos válidos, su etapa se recalcula y queda un evento `revalidated` con la versión de origen y la de destino, así la proyección sigue siendo reconstruible (ADR-0114). Si el procedimiento ya no está, la tarea se cancela con el motivo `release_changed`.
   - Una release sin episodios en curso, que no está activa ni en canary, se puede retirar.
4. **Gate:** no se promueve sin el pass^k aprobado (ADR-0119). En canary, si no hay no-inferioridad (completitud, escalamiento, costo y p95), el rollback es automático.
5. **Rollback en un clic:** se cambia el puntero; todas las conversaciones usan la versión anterior desde su siguiente turno (≤ 5 s tras el clic).
6. **Caché por versión:**
   - Claves siempre con el negocio, más el hash de release, agente, procedimiento y acción.
   - *Epochs* de datos que suben con los eventos de dominio que declare la habilidad. Hoy ninguna emite cambios de precio o catálogo: mientras tanto, esas lecturas usan un TTL corto.
   - Warm-up al promover. Nunca se interpolan variables en los bloques fijos del prompt.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| `is_active` por agente | Rollback por partes, sin atomicidad entre agentes, permisos y políticas |
| Mantener ADR-0016 sin porcentaje | Una regresión desconocida llega al 100 % |
| Sticky hasta cerrar la conversación | En producción nada cierra una conversación: la release quedaría fija para siempre |
| GitOps de la configuración | No apto para dueños no técnicos |

## Consecuencias

**Positivas:** rollback atómico por conexión, incluidos los textos del negocio; canary real; las releases viejas se drenan solas; caché sin contenido viejo; cada turno queda atribuido a su release.

**Negativas / lo que aceptamos pagar:**
- Más objetos que gestionar.
- El canary necesita tráfico para decidir.
- Un override del negocio deja de aplicarse en caliente: entra con la siguiente release.
- Una tarea a medias puede perder datos que ya no validan tras un cambio de release.

**Qué haría que revisáramos esto:** que el canary no detecte regresiones reales en 10 releases, o que `episode.idleMinutes` deje a los clientes frecuentes demasiado tiempo en una versión vieja.

## Impacto en el código

- `core/releases/` (nuevo): build, validación, promoción, canary, rollback.
- `core/kernel/`: selección de release y episodios.
- `core/agents/`: playbooks y flujo leen overrides y secciones de la release.
- `core/cache/` (nuevo).
- API `/connections/{id}/releases` y `/rollback`.
