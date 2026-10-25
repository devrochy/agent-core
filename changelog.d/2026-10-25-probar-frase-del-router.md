## Probar una frase contra el router de una conexión (HU-AOS-26)

`POST /api/v1/admin/a2a/route-preview` recibe una conexión y una frase. Responde qué agente y qué procedimiento la atenderían con la release activa, y por qué: una regla, un disparador con su puntaje y sus candidatos, o el agente por defecto. Lo usa el panel de colaboración.

- **Cómo funciona:**
  - Usa el mismo router del turno (`router.decidir`), que es léxico y sin LLM, así que probar no tiene costo.
  - No escribe nada ni registra la frase.
- **Qué devuelve:**
  - En `mode`, si hoy el router decide en esa conexión (`off`, `shadow` o `active`).
  - En `not_evaluated_rules`, las reglas que no se pueden evaluar sin conversación (por datos del cliente, etapa u horario).
- **Errores:** 409 `sin_release_activa` si la conexión no tiene versión activa y 404 si la conexión es de otro negocio. Requiere la sección `settings`.
