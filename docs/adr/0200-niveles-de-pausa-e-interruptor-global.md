# ADR-0200 — Niveles de pausa e interruptor global

- **Estado:** propuesto
- **Fecha:** 2026-11-16
- **Contexto:** HU-ARCH-37 y HU-ARCH-38 (SPEC-01, piso de control). D-76 (docs): D-SPEC-01-4 y D-SPEC-01-5, opción A, y las decisiones del dueño del 2026-11-16.
- **Enmienda:** ADR-0183 (un cron programado que escribe a clientes no corre con el interruptor global).
- **Usa:** ADR-0122 (pausa de la conexión), ADR-0107 (outbox), ADR-0188 (observabilidad).

## Contexto

Había pausa por negocio (`app_settings.bot_paused`), cliente y conversación, que solo frenaban el turno. Lo proactivo (seguimientos, recordatorios, avisos de cobro, envíos a segmento) seguía saliendo. Tampoco existía un nivel global: el comentario de `core/db/models.py` lo anunciaba en «Redis/env» y nunca se construyó. Ante un modelo que responde mal o una inyección en curso, había que pausar negocio por negocio.

## Decisión

**Hay seis niveles de pausa. Cada uno se lee en caliente, sin publicar una release ni desplegar. Lo automático hacia el cliente se frena en un solo guardián; lo que escribe una persona sigue saliendo, salvo con la conexión pausada.**

1. **Niveles y precedencia** (el primero que aplica decide el motivo):

   | Nivel | Dónde vive | Frena | Motivo |
   |---|---|---|---|
   | Global | `platform_kill_switch` (tabla de sistema, una fila) | turnos y todo lo automático de todos los negocios | `kill_switch` |
   | Conexión | ADR-0122 | también a las personas (no hay por dónde enviar) | el de ADR-0122 |
   | Negocio | `app_settings.bot_paused` | turnos y todo lo automático del negocio | `negocio_pausado` |
   | Cliente | `customers.bot_paused` | turnos y lo automático hacia ese cliente | `cliente_pausado` |
   | Conversación | `conversations.bot_paused` | turnos de esa conversación (la enciende el escalamiento) | `kill_switch` en `core_decisions` |
   | Agente | columna del agente (HU-ARCH-38, segunda parte) | turnos de ese especialista, que pasan al agente por defecto | `kill_switch` |

   En la entrada del turno el global va primero de todo; después la conexión, como ya hacía `politicas_de_entrada`.

2. **Guardián único de lo automático** (`core/gateway/pausa_automatica.py`, HU-ARCH-37). Lo llaman el sender (texto, voz, plantilla e interactivo, antes del canal de prueba), el worker para lo ya encolado (`PausaAutomaticaError`, sin reintentos ni escalamiento), los avisos por evento (resultado propio, sin `NoticeNotSentError`), `egress` (no escala una respuesta frenada), el correo, las opciones y los envíos a segmento. «Automático» es todo lo que no tiene `sender_type="human"`, más los envíos a segmento, que se reconocen por su `client_message_id`. Lo frenado queda `failed` con el motivo y **no se reenvía al reactivar**. No mira `conversations.bot_paused`: el escalamiento la enciende justo antes de mandar su aviso.

3. **Interruptor global.**
   - Lo escribe solo `core/admin/kill_switch_service.py`, desde la ruta `PUT /api/v1/admin/platform/kill-switch` (solo `es_master`; un no master recibe 403 y queda una fila `denied` en el historial) y la CLI `agent-core admin kill-switch`. Activarlo exige un motivo. Cada cambio queda en `platform_kill_switch_events` (solo inserciones), con alerta a Slack y la métrica `agentcore_kill_switch_changes_total`.
   - Lo lee `core/shared/kill_switch.py` con caché por proceso de 5 s: frena en ≤ 5 s sin desplegar. **Falla cerrado**: si no se puede leer, se trata como activo, con evento de error, métrica y alerta. Por eso la migración siembra la fila apagada.
   - Lo que hace con cada camino: la entrada y el preámbulo del loop cierran el turno en silencio con `kill_switch` (el mensaje queda en la bandeja); el consumer no gasta en descargar ni transcribir; el guardián frena lo automático.
   - **Crons** (enmienda a ADR-0183): `CronMeta.writes_to_customers` marca los que escriben a clientes (seguimientos de leads y cotizaciones, recordatorios de cita, avisos de cobro; todo cron así es `sensitive`). Con el interruptor activo no corren programados (`skipped`, `kill_switch`). Los que solo escriben datos (cargos, mora, cierre, renovación) siguen. **A mano sí corren** (decisión del dueño), pero lo que intenten enviar lo frena el guardián.
   - **Al reactivar no hay avalancha.** La recuperación de pendientes no corre con el interruptor activo y, reactivado, no reprograma lo que llegó antes de la última reactivación: eso queda en la bandeja para una persona.

4. **Pausa por agente** (segunda parte de HU-ARCH-38). Es estado vivo fuera de la release (`agent_instances.enabled` queda congelado en ella). Se lee al asignar el turno: un especialista pausado no atiende y el turno pasa al agente por defecto, sin cambiar el dueño de la conversación (al reanudar vuelve al especialista); si el pausado es el de por defecto, el turno se cierra en silencio con `kill_switch`. A2A le niega el handoff y la consulta. No frena jobs: los jobs corren por negocio.

## Consecuencias

- Un incidente se contiene en segundos con un comando, sin desplegar y sin pausar negocio por negocio.
- Durante el interruptor global no salen recordatorios ni avisos de cobro: un cliente puede quedarse sin un aviso. Lo no enviado queda registrado con su motivo.
- Una base caída detiene la plataforma (falla cerrado). Es a propósito: sin la base tampoco corre el turno.
- Riesgos que quedan escritos: las tareas abiertas de un agente pausado no se reasignan; la recuperación de pendientes sigue sin tope de tiempo fuera del interruptor global.

## Alternativas descartadas

- **Interruptor en Redis:** los controles en Redis del proyecto fallan abiertos y se pierden si Redis reinicia sin persistencia.
- **Variable de entorno:** exige reiniciar, y en Coolify reiniciar reconstruye desde la rama.
- **`paused` en `agents.status`:** el estado se copia a la release congelada y pausar no debe archivar.
