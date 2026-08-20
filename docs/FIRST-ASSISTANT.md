# Tu primer asistente en 15 pasos

Tutorial completo: montar un asistente de ventas para una barbería ficticia
("Barbería El Corte") con agendamiento de citas y CRM. Asume que ya seguiste
[INSTALLATION.md](INSTALLATION.md) hasta el paso 2 (infra levantada).

## Parte 1 — El core conversando (pasos 1-6)

**1.** Inicializa:
```bash
agent-core init
# Nombre: Barber Bot
# Negocio: Barbería El Corte, cortes y arreglo de barba en Bogotá. Citas de 45 min.
# Idioma: es · Tono: calido · LLM: deepseek · pega tu API key de platform.deepseek.com
# Embeddings: none (por ahora) · Velocidad: natural
```

**2.** Arranca: `docker compose up -d --build`

**3.** Entra a http://localhost:8000/admin con las credenciales del wizard.

**4.** Ve a **Configuración** y completa la **Voz de marca**:
```
Somos Barbería El Corte. Precios: corte $25.000, barba $15.000, combo $35.000.
Horario: martes a sábado 9am-7pm. Dirección: Calle 45 #12-34, Chapinero.
No manejamos domicilios. Los domingos y lunes no abrimos.
```

**5.** Conecta WhatsApp (INSTALLATION.md §6) con el túnel de cloudflared.

**6.** Escríbele "Hola, ¿cuánto vale el corte?" — debe responder con el precio de la
voz de marca, en tono cálido, dividido en burbujas. **Ya tienes un asistente.**

## Parte 2 — CRM: capturar clientes (pasos 7-10)

**7.** Instala el CRM:
```bash
agent-core plugin install mod-crm-engine
# ¿Datos? → Esquema base vacío
# ¿crm_leads en panel? Sí · ¿tools? Sí
# ¿crm_products / crm_orders en panel? Sí · ¿tools? Sí
```

**8.** En el panel → Plugins → **CRM** → **Configurar**:
- Pipeline: `Ventas` · Auto-crear lead: ✓ · Follow-up: 24h · Scoring: auto
→ **Guardar y activar**.
   - Menú del plugin: Dashboard · Leads · Productos · Categorías · Promociones · Pedidos.

**9.** Escríbele por WhatsApp: *"Hola, soy Andrés Pardo, quiero una hamburguesa"*.
El agente llamará `crear_lead` (nombre + interés) y podrá consultar el menú con
`consultar_inventario`. Verifícalo en /admin/conversaciones → abre el hilo → verás la
tool ejecutada.

**10.** Revisa `sdd/skills.md`: el SDD ya documenta las skills del CRM automáticamente.

## Parte 3 — Google Calendar: agendar de verdad (pasos 11-14)

**11.** Crea credenciales OAuth2 en [console.cloud.google.com](https://console.cloud.google.com):
APIs & Services → Credentials → OAuth client ID (tipo Web) → Redirect URI:
`http://localhost:8000/admin/plugins/mod_google_calendar/oauth/callback`
(habilita también la Google Calendar API).

**12.** Instala y configura:
```bash
agent-core plugin install mod-google-calendar
```
Panel → Plugins → Google Calendar → Configurar → pega client_id y client_secret,
timezone `America/Bogota`, duración `45` → Guardar y activar.

**13.** Conecta la cuenta: menú lateral → **Calendar · Conectar** → autoriza con la
cuenta Google de la barbería.

**14.** Prueba el flujo completo por WhatsApp:
> *"¿Tienes espacio el viernes en la tarde?"* → el agente usa `check_availability`
> *"Perfecto, resérvame a las 4pm, soy Andrés"* → usa `create_calendar_event`
> y el link del evento llega adjuntado por el SafeRenderer (el LLM no lo escribe a mano).

## Parte 4 — Operación (paso 15)

**15.** Conoce tus controles del día a día:
- **/admin/escalaciones**: si el cliente dice "quiero hablar con una persona", el bot
  se pausa para ese hilo y aparece aquí. Respondes desde el detalle de la conversación
  y pulsas "Marcar resuelta" para reactivar el bot.
- **/admin/costos**: cuánto llevas gastado en DeepSeek hoy/mes, por modelo.
- **Kill-switch**: Configuración → Pausar bot (global), o por conversación en la tabla.
- **Cron**: el follow-up del CRM corre a las 9:00 UTC vía Celery Beat
  (`docker compose logs celery-beat`).
- **SDD vivo**: `sdd/project.md`, `sdd/skills.md`, `sdd/changelog.md` reflejan todo
  lo que hiciste. Si usas Claude/OpenCode para personalizar más, dale
  `sdd/skills-config.md` como contexto inicial.

## ¿Y ahora?

- Crea tu propio plugin: `agent-core plugin scaffold mod-lo-que-sea` +
  [PLUGIN-CONTRACT.md](PLUGIN-CONTRACT.md)
- Personaliza con IA: usa los agentes de `.opencode/agents/` (customizer, plugin-dev)
- Antes de producción: corre `python evals/runner.py` (golden scenarios)
