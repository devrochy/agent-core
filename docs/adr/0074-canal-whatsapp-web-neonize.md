# ADR-0074 — Canal WhatsApp Web no oficial (`waweb`) vía Neonize, coexistente con Meta

- **Estado:** propuesto
- **Fecha:** 2026-09-27

## Contexto

El cliente quiere conectar números reales de WhatsApp a demos y despliegues de forma
simple (QR, minutos), sin depender de la WhatsApp Business Cloud API de Meta (que exige
número dedicado y verificación de negocio). La vía que cumple eso es el protocolo **no
oficial** de WhatsApp Web (multi-device), que **viola los ToS de WhatsApp** y conlleva
riesgo real de suspensión de la cuenta.

El runtime es Python (ADR-0011) y los canales ya se descubren por entry_points
(`agent_core.channels`, ADR-0052), con adaptadores por proveedor
(`core/gateway/channels/base.py`) y credenciales por tenant en `channel_connections`.
El canal oficial Meta ya está en producción y **no se reemplaza**.

## Decisión

Se agrega un **segundo proveedor de canal, `waweb`**, que habla el protocolo de WhatsApp
Web mediante **Neonize** (bindings Python sobre el Go `whatsmeow`, mismo protocolo que
Baileys), **coexistente con `meta`**: cada conexión elige un proveedor y un número no
puede estar en ambos a la vez.

1. **Proveedor por conexión, no reemplazo.** `ChannelConnection.provider = "waweb"`;
   `resolve_credentials`/`outbound_adapter_for` ya despachan por proveedor. El canal
   Meta (incluida la Coexistence oficial ya implementada en `meta_onboarding.py`) queda
   intacto.
2. **Rol de proceso `connector`.** Los sockets vivos de Neonize no caben en el request
   del webhook ni en el worker Celery (prefork, sin loop asyncio). Corren en un proceso
   del mismo binario con `AGENT_CORE_ROLE=connector`, según el criterio de separación de
   ADR-0021. Inbound → `InboundMessage` → stream `agent_core:inbound`; outbound → cola
   Redis request/reply que el connector drena con delay anti-baneo, preservando el
   reintento/DLQ de `core/tasks/outbound_send.py`.
3. **Sesión persistente y cifrada.** La sesión (credenciales multi-device) vive cifrada
   por tenant, sobrevive reinicios de contenedor y NO se guarda en el sistema de
   archivos suelto. Reconexión automática; QR solo la primera vez.
4. **Riesgo advertido y gobernado.** La conexión `waweb` exige aceptación explícita de
   riesgo (persistida) antes de mostrar el QR; arranca en modo **solo-respuesta**
   (contesta a quien escribió primero), con delays humanos y tope diario configurables
   por tenant; el envío no solicitado queda bloqueado por defecto. Alertas ante
   `auth_failure`/`logged_out`/desconexiones repetidas.
5. **Historial solo como backfill.** `getContacts`/`getChatHistory` se usan únicamente
   al vincular para importar; el contexto del LLM sigue viniendo de `messages`/
   `customers` (la memoria del core es la fuente de verdad).

**No hay un umbral de mensajes que garantice no ser baneado**: el detonante es el
comportamiento (no solicitado + reportes de los destinatarios), no un contador. La
política es mitigación, no garantía; el riesgo se documenta y se acepta por escrito.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| `whatsapp-web.js` (Chromium/Puppeteer) | ~400-500 MB por sesión; Docker pesado; inviable multi-instancia |
| Baileys o un sidecar Node | Añade un segundo runtime a un repo Python (ADR-0011) y superficie de supply-chain |
| Solo Coexistence oficial | No sirve para "demo con número personal en minutos"; requiere Business App |
| No hacer nada (solo Meta) | No cubre el caso de números/negocios sin Business API |

## Consecuencias

**Positivas:** demos y clientes sin Business API con conexión en minutos; canal nuevo
sin tocar el core (entry_points); el canal oficial sigue disponible; elección por
conexión.

**Negativas / lo que aceptamos pagar:** es un canal **no oficial** con **riesgo de
baneo** que se traslada al cliente; dependencia externa (`neonize`/`whatsmeow`) con
churn de protocolo y mantenimiento recurrente; un proceso/contenedor más; la sesión
cifrada hay que custodiarla como secreto.

**Qué haría que revisáramos esto:** que Neonize no sostenga sesión/reconexión en el
spike de F0; que WhatsApp cambie el protocolo y `whatsmeow` tarde en actualizar; o que
un baneo real en producción obligue a migrar tenants a Coexistence/Meta.

## Impacto en el código

`packages/channel-waweb/**` (nuevo, entry_point `agent_core.channels`),
`core/waweb/manager.py` (lifecycle del rol connector), migración `waweb_sessions`
(tenant_id + RLS + `deleted_at`), `core/gateway/connections.py` (`CREDENTIAL_KEYS`),
`core/gateway/sender.py` (ventana de servicio provider-aware), `core/config.py`,
`docker-compose.yml`/Coolify (servicio connector), panel `/admin/conexiones` (QR +
badge + aceptación de riesgo). **Sin cambio de contrato congelado.**

## Enmienda (2026-10-28)

El modo «solo respuesta» (`waweb_mode = reply_only`) se mostraba en el panel pero ningún camino de envío lo aplicaba. Los avisos
por evento por QR (ADR-0108, adenda D2 de ADR-0178, tras bandera) son el primer camino que lo hace cumplir: solo salen con
`allow_outbound`. Los demás envíos siguen como estaban.

