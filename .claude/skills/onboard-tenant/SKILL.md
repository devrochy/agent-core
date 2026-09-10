---
name: onboard-tenant
description: >
  Da de alta un cliente nuevo en agent-core: tenant, canales, packs, agente y publicación.
  Usar SOLO cuando el usuario diga "nuevo cliente", "dar de alta a", "onboarding de" o
  "conectar el WhatsApp de <negocio>".
---

Onboarding de `$ARGUMENTS`. Ejecuta en orden y reporta el tiempo total al final.

1. **Tenant**: crear registro, plan, límites (conversaciones/mes, canales, asientos),
   modo de aislamiento (pooled por defecto), zona horaria y locale.
2. **Canales**: registrar instancias de canal. Guardar credenciales cifradas, nunca en `.env`.
   Verificar webhook con un mensaje de prueba antes de continuar.
3. **Packs**: instalar los que apliquen. Consentir scopes explícitamente. Correr migraciones
   del pack para este tenant. Validar la config contra su JSON Schema.
4. **AgentSpec**: persona, tono, emojis, longitud, frases prohibidas, modelo, política de
   escalamiento, presupuesto por conversación. Nada de esto se escribe en código.
5. **Conocimiento**: cargar catálogo / servicios / FAQ. Indexar para RAG si aplica.
6. **Evals**: 10 conversaciones representativas de ESTE cliente en `evals/tenants/<slug>/`.
   No se publica sin este paso.
7. **Canary**: publicar al 5% del tráfico. Revisar a las 24 h: tasa de escalamiento,
   costo por conversación, latencia p95.
8. **Publicación** al 100% + entrega al cliente: accesos al panel, cómo escalar a humano,
   cómo pedir cambios.

## Criterio de aceptación

Si algún paso requirió escribir código, **no es onboarding: es un pack incompleto**.
Anótalo como deuda en `docs/ROADMAP.md` y dilo explícitamente en el reporte final.