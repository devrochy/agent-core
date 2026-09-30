# Tu primer tenant en 15 pasos

Tutorial completo: dar de alta un cliente ("Barbería El Corte") como tenant de
agent-core — ventas por WhatsApp con CRM. Asume que ya instalaste la plataforma
([INSTALLATION.md](INSTALLATION.md), pasos 1-4): un despliegue multi-tenant listo
para recibir clientes.

## Parte 1 — El asistente conversando (pasos 1-6)

**1.** Da de alta al cliente con el wizard:
```bash
agent-core tenant create
# Nombre: Barber Bot
# Slug: barberia-el-corte
# Negocio: Barbería El Corte, cortes y arreglo de barba en Bogotá. Citas de 45 min.
# Idioma: es · Tono: calido
# Packs: (ninguno por ahora)
# WhatsApp: omitir por ahora
```
Equivalente en el panel: `/admin/tenants` → **+ Nuevo tenant**.

**2.** El wizard deja al tenant listo: `tenant`, `agent_specs`, `app_settings` y
acceso del admin. Cambia al tenant en el selector del panel (arriba a la izquierda).

**3.** Ve a **Configuración** y completa la **Voz de marca** del tenant:
```
Somos Barbería El Corte. Precios: corte $25.000, barba $15.000, combo $35.000.
Horario: martes a sábado 9am-7pm. Dirección: Calle 45 #12-34, Chapinero.
No manejamos domicilios. Los domingos y lunes no abrimos.
```

**4.** En **/admin/spec** revisa el AgentSpec del tenant: persona, tono, triggers
de escalación, packs.

**5.** Conecta WhatsApp desde **/admin/conexiones** (credenciales de la WABA) y
registra el webhook del tenant en Meta — la URL es `.../webhooks/whatsapp/<slug>`:
```bash
agent-core tenant webhook barberia-el-corte   # imprime Callback URL + verify token
```
Guía completa (WABA de prueba + túnel): INSTALLATION.md §6.

**6.** Escríbele "Hola, ¿cuánto vale el corte?" — debe responder con el precio de la
voz de marca, en tono cálido. **Ya tienes un asistente por tenant.**

## Parte 2 — CRM: capturar clientes (pasos 7-10)

**7.** Instala el pack (una sola vez, sirve para todos los tenants):
```bash
agent-core plugin install pack-commerce
```

**8.** Habilita el pack PARA ESTE TENANT: Panel → **/admin/spec** → Packs →
marca `pack_commerce` → Guardar. (El wizard de alta también lo permite con un
checkbox.) Configura el pack en Plugins → **CRM** → **Configurar**:
- Pipeline: `Ventas` · Auto-crear lead: ✓ · Follow-up: 24h · Scoring: auto
→ **Guardar y activar**.

**9.** Si el cliente tiene catálogo, créalo como dato en
`tenants/barberia-el-corte/catalog.json` e importa:
```bash
agent-core tenant seed barberia-el-corte
```
Escríbele por WhatsApp: *"Hola, soy Andrés Pardo, quiero una hamburguesa"*.
El agente llamará `crear_lead` (nombre + interés). Verifícalo en
`/admin/conversaciones` → abre el hilo → verás la tool ejecutada.

**10.** La documentación del tenant vive en `tenants/barberia-el-corte/docs/`.
Anota ahí decisiones de configuración y cambios. No va en `docs/` del framework.

## Parte 3 — Agendar citas (pasos 11-14)

**11.** Crea credenciales OAuth2 en [console.cloud.google.com](https://console.cloud.google.com):
Redirect URI `http://localhost:8000/admin/plugins/pack_calendar/oauth/callback`.

**12.** Instala el pack:
```bash
agent-core plugin install pack-calendar
```
Panel → Plugins → Google Calendar → Configurar → client_id y client_secret,
timezone `America/Bogota`, duración `45` → Guardar y activar.

**13.** Conecta la cuenta: menú lateral → **Calendar · Conectar** → autoriza con
la cuenta Google de la barbería.

**14.** Prueba el flujo completo por WhatsApp:
> *"¿Tienes espacio el viernes en la tarde?"* → el agente usa `check_availability`
> *"Perfecto, resérvame a las 4pm, soy Andrés"* → usa `create_calendar_event`.

## Parte 4 — Operación (paso 15)

**15.** Conoce tus controles del día a día:
- **/admin/escalaciones**: si el cliente dice "quiero hablar con una persona", el bot
  se pausa para ese hilo y aparece aquí.
- **/admin/costos**: cuánto llevas gastado en LLM hoy/mes, por tenant.
- **/admin/tenants**: da de alta al siguiente cliente. El mismo framework, otro tenant.
- **Kill-switch**: Configuración → Pausar bot (por tenant).
- **Evals del tenant**: crea `tenants/<slug>/evals/` (spec + scenarios) con el
  agente `eval-author` y corre `python evals/runner.py --tenant <slug>` antes de publicar.

## ¿Y ahora?

- Crea tu propio pack: `agent-core plugin scaffold mod-lo-que-sea` +
  [PLUGIN-CONTRACT.md](PLUGIN-CONTRACT.md)
- Antes de producción por cliente: golden set (`--tenant`) + canary 5% → 24 h → 100%
  (ADR-0016).
- Ver `docs/MULTITENANT.md` para el modelo de tenants completo.