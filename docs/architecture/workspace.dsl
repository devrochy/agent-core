// Modelo C4 canónico de solvershub (ADR-0189).
// Las vistas de docs/architecture/c4.md y de la wiki se exportan de este archivo.
// Estado leído de origin/develop el 2026-11-08. Los comentarios citan la evidencia.
//
// Quedan fuera a propósito:
// - Stubs sin código: apps/{agentos,gateway,worker}, packages/{core-crm,core-runtime,channel-whatsapp}, plugins/.
// - GitHub y GHCR: solo intervienen en CI (.github/workflows/ci-postgres-image.yml), no en ejecución.
//
// Preguntas abiertas:
// - solvershub-apk/eas.json:49 apunta a app.solvershub.site/api/pack/pack_cobranza, y
//   docs/despliegue-coolify.md:94-96 dice que esa ruta no es pública en producción.
// - Qué túnel publica la landing en test y en producción heredada: el repo no lo documenta.
//   Sus contenedores existen en ambos (solvershub-infra/observabilidad/alloy/config.alloy:90-95 y 115-120).
// - El salto D-59 (base nueva en la misma máquina) cambia la producción heredada al ejecutarse.

workspace "SolversHub" "Plataforma multi-tenant de agentes de IA para negocios." {

    model {

        // --- Personas (core/admin/roles.py, core/access/roles.py, core/db/models.py) ---
        equipo = person "Equipo de plataforma" "Rol master: opera la plataforma y todos los negocios."
        tester = person "Tester" "Actúa como master, solo con QA_TESTERS_ENABLED."
        negocio = person "Dueño o colaborador del negocio" "Configura sus agentes y atiende sus conversaciones."
        gestor = person "Gestor de cobranza" "Trabaja en campo con la app móvil."
        clienteFinal = person "Cliente final" "Conversa con el agente del negocio. No tiene usuario en el sistema."
        visitante = person "Visitante de la landing" "Público que deja sus datos o usa el chat web."

        // --- Sistemas externos ---
        group "Canales" {
            // core/gateway/channels/{whatsapp,instagram,messenger}/, core/gateway/meta_graph.py
            meta = softwareSystem "Meta" "WhatsApp Cloud API, Instagram y Messenger por Graph API." "Externo"
            // core/gateway/twilio/, packages/channel-twilio/, ADR-0186
            twilio = softwareSystem "Twilio" "WhatsApp por cuenta Twilio del negocio. Backend listo, sin número conectado." "Externo,Sin activar"
            // core/waweb/backend_neonize.py, ADR-0074
            whatsappWeb = softwareSystem "WhatsApp Web" "Solo en test. Canal no oficial por QR, con Neonize." "Externo,Solo test"
        }
        group "Servicios" {
            // core/billing/providers/wompi.py, packages/pack-payments/
            wompi = softwareSystem "Wompi" "Cobro de la plataforma y links de pago de cada negocio." "Externo"
            // core/config.py:128-175, core/llm/, core/media/{stt,vision}.py
            llm = softwareSystem "Proveedores LLM" "DeepSeek por defecto (chat y visión), Groq (transcripción), OpenAI y Anthropic opcionales, también con llave del negocio." "Externo"
            // core/notifications/push.py:68, solvershub-apk/eas.json, solvershub-apk/docs/push-android.md
            expo = softwareSystem "Expo y Firebase" "Expo Push entrega por FCM. EAS compila la app y publica OTA." "Externo"
            // packages/pack-calendar/pack_calendar/google_api.py
            googleCalendar = softwareSystem "Google Calendar" "Agenda externa de cada negocio por OAuth2." "Externo"
            // core/geocoding/config.py:21
            locationiq = softwareSystem "LocationIQ" "Geocodificación de direcciones." "Externo"
            // core/gateway/system_email.py, core/gateway/channels/email.py, solvershub-landing/app/api/leads/route.ts:42-62
            correo = softwareSystem "Correo" "SMTP para el backend y Resend para la landing." "Externo"
            // docs/despliegue-coolify.md:97-114, core/api/routers/web_chat.py:42
            cloudflare = softwareSystem "Cloudflare" "DNS, Tunnel, Access y Turnstile." "Externo"
            // solvershub-landing/lib/analytics.ts
            posthog = softwareSystem "PostHog" "Analítica de la landing." "Externo"
            // core/observability/sentry_setup.py, core/shared/alerts.py:30, solvershub-infra/observabilidad/
            observabilidadExt = softwareSystem "Servicios de observabilidad" "Grafana Cloud (Loki, Mimir, Tempo, Synthetic), Sentry y alertas en Slack." "Externo"
            // docs/runbooks/produccion-vps.md:746-757
            s3 = softwareSystem "Almacenamiento S3" "Respaldos fuera del proveedor principal. Solo en el diseño objetivo." "Externo,Objetivo"
        }

        solvershub = softwareSystem "SolversHub" "Atiende a los clientes de cada negocio con agentes de IA y da las herramientas para operarlos." {

            group "Clientes" {
                // solvershub-landing/Dockerfile
                landing = container "landing" "Sitio público, formularios de leads y chat web." "Next.js 16" "Web"
                // solvershub-panel/apps/panel-web/Dockerfile, lib/api/server.ts
                panelWeb = container "panel-web" "Panel de administración. Su BFF /api/* guarda el token y llama al backend." "Next.js 16, React 19" "Web"
                // solvershub-apk/eas.json, src/data/db/conexion.ts
                apk = container "App móvil" "Cobranza en campo para gestores y seguimiento del negocio para dueños y administradores, con datos locales cifrados." "Expo, React Native, SQLCipher" "Movil"
            }

            group "Backend (una imagen, cuatro roles por AGENT_CORE_ROLE)" {
                // docker-compose.yml:49, core/main.py:72-73, ADR-0021
                coreApi = container "core-api" "API /api/v1, webhooks y panel clásico. En el mismo proceso consume la cola y ejecuta los turnos del agente." "Python 3.12, FastAPI" {

                    group "Entrada y canales" {
                        gateway = component "Gateway de canales" "Valida webhooks, deduplica, publica en la cola, administra conexiones y encola envíos." "core/gateway, core/waweb" "Entrada"
                        apiV1 = component "API /api/v1" "Unos 60 routers JSON para panel y app. Publica el chat web." "core/api, FastAPI" "Entrada"
                        adminSsr = component "Panel clásico" "Panel SSR /admin, que se retira por secciones (ADR-0076)." "core/admin, Jinja" "Entrada,En retiro"
                    }

                    group "Turno y agente" {
                        orquestador = component "Orquestador" "Consume la cola, agrupa mensajes (debounce) y ejecuta el ciclo LLM y tools." "core/orchestrator, asyncio" "Turno"
                        kernel = component "Kernel del Coordinador" "Políticas de entrada, decisiones, procedimientos y salida (ADR-0112)." "core/kernel" "Turno"
                        agentesReleases = component "Agentes y releases" "AgentSpec, playbooks, versiones y canary." "core/agents, core/playbooks, core/releases" "Turno"
                        proveedoresLlm = component "Abstracción LLM" "Interfaz común a los proveedores, sin SDK." "core/llm, httpx" "Turno"
                        hostTools = component "Host de tools" "Ejecuta skills y tools con contexto del tenant y HTTP con allowlist." "core/mcp, core/tools, core/datos_cliente" "Turno"
                        memoria = component "Memoria y búsqueda" "Memoria de trabajo y recuerdo vectorial." "core/memory, core/search" "Turno"
                        media = component "Media" "Descarga, transcribe y describe audios e imágenes." "core/media" "Turno"
                    }

                    group "Plataforma" {
                        pluginManager = component "Plugin manager" "Descubre packs por entry points y administra su ciclo de vida por tenant." "core/plugin_manager.py" "Plataforma"
                        facturacion = component "Facturación" "Planes, cuotas y cobro de la plataforma." "core/billing" "Plataforma"
                        accesoSeguridad = component "Acceso y seguridad" "Sesiones, dispositivos, rate limit y bloqueo." "core/access, core/security" "Plataforma"
                        eventosAvisos = component "Eventos y avisos" "Outbox de eventos de dominio y centro de notificaciones." "core/events, core/notifications" "Plataforma"
                        plataformaDatos = component "Datos y operación" "RLS por tenant, alta de negocios, QA y observabilidad." "core/db, core/provisioning, core/qa, core/observability" "Plataforma"
                        registros = component "Registros de capacidades" "Los packs publican y consumen capacidades sin importarse entre sí." "core/payments, core/subscriptions" "Plataforma"
                        almacenamiento = component "Almacenamiento" "Controlador único para guardar y abrir archivos del negocio (ADR-0085)." "core/almacenamiento" "Plataforma"
                        contratos = component "contracts" "Tipos congelados: AgentSpec, KernelSpec y procedimientos." "agentcore-contracts, pydantic" "Contrato"
                        canalesInstalables = component "Canales instalables" "Adaptadores twilio, waweb y web por el entry point agent_core.channels. También los usa el worker." "packages/channel-*" "Canal"
                    }

                    group "Packs" {
                        // packages/pack-*: entry points agent_core.plugins. .importlinter prohíbe imports entre packs.
                        // Estos packs también corren en celery-worker; ver el componente Jobs de packs.
                        packCobranza = component "pack-cobranza" "Cartera, préstamos y rutas. API de la app en /api/pack/pack_cobranza." "Python, 18 migraciones" "Pack"
                        packCommerce = component "pack-commerce" "Leads, catálogo y pedidos. Recibe leads en /api/pack/pack_commerce." "Python, 19 migraciones" "Pack"
                        packSubscriptions = component "pack-subscriptions" "Planes e inscripción de clientes finales." "Python, 13 migraciones" "Pack"
                        packCalendar = component "pack-calendar" "Citas con agenda interna o Google." "Python, 6 migraciones" "Pack"
                        packPayments = component "pack-payments" "Links de pago. Se registra en core.payments.payment_registry." "Python, 3 migraciones" "Pack"
                        packKnowledge = component "pack-knowledge" "Documentos por agente con búsqueda vectorial." "Python, 2 migraciones, pgvector" "Pack"
                    }
                }

                // core/tasks/celery_app.py:36-44
                celeryWorker = container "celery-worker" "Envía los mensajes salientes y ejecuta push, crons y evaluaciones." "Python, Celery" {
                    wkEnvio = component "Envío saliente" "Tarea core.outbound_send. Elige el adaptador del canal, incluidos los instalables." "core/tasks/outbound_send.py" "Worker"
                    wkSaludMeta = component "Salud de Meta" "Suscripciones, tokens, plantillas y precios." "core/tasks" "Worker"
                    wkTwilio = component "Tareas Twilio" "Sondeo, sincronización, salud y costo por mensaje." "core/tasks" "Worker"
                    wkOutbox = component "Despachador de eventos" "Reemite domain_events cada 30 segundos a sus handlers." "core/tasks/domain_events_dispatch.py" "Worker"
                    wkPush = component "Push" "Notificaciones a la app móvil." "core/tasks/push_tasks.py" "Worker"
                    wkFacturacion = component "Ciclo de facturación" "Ciclo de vida, renovación y conciliación." "core/billing, Celery" "Worker"
                    wkMantenimiento = component "Mantenimiento y Agent OS" "Retenciones, memoria, esperas, canary, latido y evaluación de candidatas." "core/tasks, core/releases" "Worker"
                    wkPacks = component "Jobs de packs" "Crons y tareas registrados por los packs de core-api con @register_cron." "packages/pack-*/jobs.py" "Worker"
                }

                // docker-compose.yml:110, core/tasks/cron_scheduler.py
                celeryBeat = container "celery-beat" "Programa los crons. Instancia única." "Python, Celery Beat"

                // docker-compose.yml:133, core/waweb/connector.py
                wawebConnector = container "waweb-connector" "Solo en test. Mantiene un socket de WhatsApp Web por tenant." "Python, Neonize" "Solo test"
            }

            group "Datos" {
                // docker-compose.yml:11, ADR-0058
                postgres = container "Postgres" "Datos de negocio aislados por RLS y vectores." "PostgreSQL 17, pgvector, PostGIS" "Base de datos"
                // docker-compose.yml:39
                redis = container "Redis" "Cola de entrada, debounce, locks, memoria de trabajo y broker de Celery." "Redis 7" "Base de datos"
                // docker-compose.yml:159, ADR-0085
                tenantsVolume = container "Volumen tenants" "Artefactos por negocio: media, documentos, evidencias, flow.json y overrides de playbooks." "Volumen Docker" "Volumen"
            }

            group "Borde" {
                // docs/despliegue-coolify.md:76
                cloudflared = container "cloudflared" "Conecta los hostnames públicos con los servicios internos, sin puertos abiertos." "Cloudflare Tunnel"
            }

            group "Telemetría" {
                // solvershub-infra/observabilidad/docker-compose.yml, ADR-0188
                alloy = container "alloy" "Recibe OTLP del backend, lee los logs de Docker de todos los contenedores, los redacta y los reenvía." "Grafana Alloy" "Observabilidad,Solo test"
                otelcolEs = container "otelcol-es" "Escribe los logs en Elasticsearch." "OpenTelemetry Collector" "Observabilidad,Solo test"
                elasticsearch = container "Elasticsearch" "Almacén de logs." "Elasticsearch, un nodo" "Observabilidad,Base de datos,Solo test"
                kibana = container "Kibana" "Consulta de logs. Se publica en kibana.solvershub.site por el túnel solvershub, detrás de Access." "Kibana" "Observabilidad,Solo test"
            }
        }

        // --- Nivel 1. Se declara primero para fijar el texto de las relaciones de sistema. ---
        equipo -> solvershub "Opera la plataforma y los negocios" "Navegador"
        tester -> solvershub "Ejecuta pruebas con permisos de master" "Navegador"
        negocio -> solvershub "Configura agentes y atiende conversaciones" "Navegador, app móvil"
        gestor -> solvershub "Registra cobros y gestiones en campo" "App móvil"
        visitante -> solvershub "Deja sus datos o chatea" "Navegador"
        clienteFinal -> meta "Escribe por WhatsApp, Instagram o Messenger" "App de mensajería"
        clienteFinal -> twilio "Escribe por WhatsApp" "App de mensajería"
        clienteFinal -> whatsappWeb "Escribe al número conectado por QR" "App de mensajería"
        meta -> solvershub "Entrega mensajes" "Webhook HTTPS firmado"
        solvershub -> meta "Envía respuestas y vigila la conexión" "Graph API"
        twilio -> solvershub "Entrega mensajes" "Webhook HTTPS"
        solvershub -> twilio "Envía respuestas y consulta costos" "REST"
        solvershub -> whatsappWeb "Mantiene la sesión, recibe y envía" "Protocolo WhatsApp Web"
        wompi -> solvershub "Notifica pagos" "Webhook HTTPS"
        solvershub -> wompi "Cobra y crea links de pago" "HTTPS"
        solvershub -> llm "Genera respuestas, transcribe y describe media" "HTTPS"
        solvershub -> expo "Envía push y descarga actualizaciones OTA" "HTTPS"
        expo -> negocio "Avisa en el teléfono" "FCM"
        solvershub -> googleCalendar "Sincroniza citas" "OAuth2, HTTPS"
        solvershub -> locationiq "Geocodifica direcciones" "HTTPS"
        solvershub -> correo "Envía accesos, avisos y correos del canal" "SMTP, HTTPS"
        cloudflare -> solvershub "Entrega el tráfico público" "Tunnel"
        solvershub -> cloudflare "Valida Turnstile" "HTTPS"
        solvershub -> posthog "Envía analítica de la landing" "HTTPS"
        solvershub -> observabilidadExt "Envía métricas, trazas, logs, errores y alertas" "OTLP, HTTPS"

        // --- Nivel 2 ---
        visitante -> landing "Llena formularios y usa el chat web" "HTTPS"
        equipo -> panelWeb "Administra la plataforma" "HTTPS"
        tester -> panelWeb "Ejecuta el catálogo de pruebas" "HTTPS"
        negocio -> panelWeb "Administra su negocio" "HTTPS"
        gestor -> apk "Registra cobros y gestiones" "Android"
        negocio -> apk "Consulta el negocio y recibe avisos" "Android"
        equipo -> apk "Administra negocios y cobranza" "Android"
        equipo -> coreApi "Usa las secciones que quedan del panel clásico" "HTTPS"
        equipo -> kibana "Consulta logs" "HTTPS, Access"
        equipo -> observabilidadExt "Ve tableros y alertas" "HTTPS"

        landing -> coreApi "Envía leads y reenvía el chat web" "HTTP; X-Intake-Secret solo en leads"
        landing -> correo "Notifica leads si no hay webhook configurado" "Resend, HTTPS"
        landing -> cloudflare "Muestra el widget Turnstile en el navegador" "HTTPS"
        landing -> posthog "Envía eventos desde el navegador" "HTTPS"
        panelWeb -> coreApi "Llama a /api/v1 desde el BFF" "HTTP, Bearer, X-Tenant-Id"
        apk -> coreApi "Usa la API de campo y /api/v1" "HTTPS, Bearer"
        apk -> expo "Registra el token y descarga OTA" "HTTPS"
        expo -> apk "Entrega notificaciones" "FCM"

        cloudflare -> cloudflared "Entrega el tráfico de los hostnames públicos" "Tunnel"
        cloudflared -> coreApi "Enruta webhooks, /healthz y las rutas públicas de API" "HTTP"
        cloudflared -> panelWeb "Enruta el resto de los hostnames del panel" "HTTP"

        meta -> coreApi "Entrega mensajes" "Webhook HTTPS firmado"
        twilio -> coreApi "Entrega mensajes" "Webhook HTTPS"
        wompi -> coreApi "Notifica pagos" "Webhook HTTPS"

        coreApi -> celeryWorker "Encola envíos, push y evaluaciones" "Celery sobre Redis"
        celeryBeat -> celeryWorker "Dispara los crons" "Celery sobre Redis"
        celeryWorker -> wawebConnector "Pide envíos por WhatsApp Web" "Redis: waweb:outbox, waweb:reply"
        wawebConnector -> coreApi "Publica mensajes entrantes, QR y estado" "Redis Streams y claves waweb:*"
        coreApi -> wawebConnector "Pide reiniciar la sesión" "Redis: waweb:reset"
        wawebConnector -> whatsappWeb "Mantiene un socket por tenant" "Protocolo WhatsApp Web"

        coreApi -> postgres "Lee y escribe con tenant_scope" "SQLAlchemy async, rol app_runtime"
        celeryWorker -> postgres "Lee y escribe con tenant_scope" "SQLAlchemy async, rol app_runtime"
        celeryBeat -> postgres "Lee horarios de cron_job_configs" "SQLAlchemy"
        wawebConnector -> postgres "Guarda credenciales de sesión cifradas" "SQLAlchemy"
        coreApi -> redis "Cola de entrada, debounce, memoria de trabajo, caché de equipos y límites" "Redis"
        celeryWorker -> redis "Broker, locks de cron y latido" "Redis"
        celeryBeat -> redis "Publica tareas programadas" "Redis"
        wawebConnector -> redis "Colas de envío, QR y frenos antibaneo" "Redis"
        coreApi -> tenantsVolume "Escribe media y documentos, lee flow.json y overrides" "Sistema de archivos"
        celeryWorker -> tenantsVolume "Lee documentos y artefactos" "Sistema de archivos"

        coreApi -> meta "Descarga media y administra conexiones" "Graph API"
        coreApi -> twilio "Descarga media y administra conexiones" "REST"
        celeryWorker -> meta "Envía mensajes y vigila la conexión" "Graph API"
        celeryWorker -> twilio "Envía mensajes y consulta costos" "REST"
        coreApi -> wompi "Cobra y crea links de pago" "HTTPS"
        celeryWorker -> wompi "Crea links de cobro de suscripciones" "HTTPS"
        coreApi -> llm "Genera respuestas, transcribe, describe y crea embeddings" "HTTPS"
        celeryWorker -> llm "Corre evaluaciones y embeddings de documentos" "HTTPS"
        coreApi -> googleCalendar "Sincroniza citas" "OAuth2, HTTPS"
        coreApi -> locationiq "Geocodifica direcciones" "HTTPS"
        coreApi -> correo "Envía correos de acceso" "SMTP"
        celeryWorker -> correo "Envía correos del canal, de facturación y de acceso" "SMTP"
        coreApi -> cloudflare "Valida Turnstile" "HTTPS"
        celeryWorker -> expo "Envía notificaciones push" "HTTPS"

        coreApi -> alloy "Envía métricas y trazas" "OTLP/HTTP"
        celeryWorker -> alloy "Envía métricas y trazas" "OTLP/HTTP"
        celeryBeat -> alloy "Envía métricas" "OTLP/HTTP"
        wawebConnector -> alloy "Envía métricas" "OTLP/HTTP"
        coreApi -> observabilidadExt "Envía errores y alertas" "Sentry, Slack"
        celeryWorker -> observabilidadExt "Envía errores y alertas" "Sentry, Slack"
        celeryBeat -> observabilidadExt "Envía errores" "Sentry"
        wawebConnector -> observabilidadExt "Envía errores" "Sentry"
        alloy -> observabilidadExt "Reenvía logs, métricas y trazas" "Loki, Mimir, Tempo"
        alloy -> otelcolEs "Reenvía logs redactados" "OTLP/HTTP"
        otelcolEs -> elasticsearch "Indexa logs" "HTTP"
        kibana -> elasticsearch "Consulta logs" "HTTP"
        observabilidadExt -> coreApi "Comprueba disponibilidad" "Synthetic, /healthz"

        postgres -> s3 "Envía respaldos" "S3" "Objetivo"
        tenantsVolume -> s3 "Envía copia diaria versionada" "S3" "Objetivo"

        // --- Nivel 3: core-api ---
        meta -> gateway "Entrega mensajes" "Webhook HTTPS firmado"
        twilio -> gateway "Entrega mensajes" "Webhook HTTPS"
        wompi -> gateway "Notifica pagos de la plataforma" "Webhook HTTPS"
        wompi -> packPayments "Notifica pagos de los negocios" "Webhook HTTPS"
        wawebConnector -> orquestador "Publica mensajes entrantes" "Redis Stream agent_core:inbound"
        wawebConnector -> gateway "Publica QR y estado" "Redis: waweb:qr, waweb:ready"
        panelWeb -> apiV1 "Llama a /api/v1" "HTTP"
        apk -> apiV1 "Llama a /api/v1" "HTTPS"
        landing -> apiV1 "Reenvía el chat web" "HTTP"
        apk -> packCobranza "Usa la API de campo" "HTTPS"
        landing -> packCommerce "Envía leads" "HTTP"
        equipo -> adminSsr "Usa las secciones que quedan" "HTTPS"

        gateway -> orquestador "Publica el mensaje entrante" "Redis Stream agent_core:inbound"
        gateway -> redis "Deduplica y publica" "SET NX, XADD"
        gateway -> postgres "Audita webhooks y guarda salientes" "SQLAlchemy"
        gateway -> meta "Administra conexiones" "Graph API"
        gateway -> twilio "Administra conexiones" "REST"
        gateway -> correo "Envía correos de acceso" "SMTP"
        gateway -> canalesInstalables "Resuelve el adaptador del canal" "entry points"
        gateway -> facturacion "Aplica el pago" "En proceso"
        gateway -> packPayments "Reenvía el evento si la cuenta es compartida" "En proceso"
        gateway -> wkEnvio "Encola core.outbound_send" "Celery sobre Redis"
        canalesInstalables -> twilio "Usa REST y Content API" "REST"

        apiV1 -> gateway "Administra conexiones" "En proceso"
        apiV1 -> orquestador "Publica el chat web" "Redis Stream agent_core:inbound"
        apiV1 -> agentesReleases "Edita y publica agentes" "En proceso"
        apiV1 -> facturacion "Consulta planes y cuotas" "En proceso"
        apiV1 -> accesoSeguridad "Autentica y limita" "En proceso"
        apiV1 -> plataformaDatos "Da de alta y consulta datos" "En proceso"
        apiV1 -> cloudflare "Valida Turnstile" "HTTPS"
        apiV1 -> locationiq "Geocodifica direcciones" "HTTPS"
        adminSsr -> plataformaDatos "Lee y escribe datos" "En proceso"

        orquestador -> redis "Lee la cola y agrupa mensajes" "XREADGROUP, debounce"
        orquestador -> postgres "Registra eventos y mensajes" "SQLAlchemy"
        orquestador -> kernel "Aplica políticas y registra decisiones" "En proceso"
        orquestador -> agentesReleases "Carga el AgentSpec y playbooks" "En proceso"
        orquestador -> proveedoresLlm "Genera la respuesta" "En proceso"
        orquestador -> hostTools "Ejecuta tools" "En proceso"
        orquestador -> memoria "Lee y guarda contexto" "En proceso"
        orquestador -> media "Procesa audios e imágenes" "En proceso"
        kernel -> gateway "Entrega la respuesta" "En proceso"
        kernel -> postgres "Guarda decisiones y resultados" "SQLAlchemy"
        kernel -> contratos "Usa KernelSpec y procedimientos" "pydantic"
        agentesReleases -> contratos "Valida el AgentSpec" "pydantic"
        agentesReleases -> postgres "Guarda versiones y releases congeladas" "SQLAlchemy"
        agentesReleases -> tenantsVolume "Lee overrides y flow.json al construir la release" "Sistema de archivos"
        agentesReleases -> wkMantenimiento "Encola la evaluación de candidatas" "Celery sobre Redis"
        proveedoresLlm -> llm "Completions" "HTTPS"
        memoria -> redis "Memoria de trabajo" "Redis"
        memoria -> postgres "Recuerdo vectorial" "pgvector"
        memoria -> llm "Crea embeddings, opcional" "HTTPS"
        media -> meta "Descarga media" "Graph API"
        media -> twilio "Descarga media" "REST"
        media -> llm "Transcribe y describe" "HTTPS"
        media -> tenantsVolume "Guarda archivos" "Sistema de archivos"

        pluginManager -> hostTools "Registra skills, tools y guardrails" "En proceso"
        pluginManager -> postgres "Lee packs activos y su configuración" "SQLAlchemy"
        almacenamiento -> tenantsVolume "Guarda y abre archivos" "Sistema de archivos"
        facturacion -> wompi "Cobra la suscripción" "HTTPS"
        facturacion -> postgres "Guarda planes y cuotas" "SQLAlchemy"
        accesoSeguridad -> redis "Caché de vigencia de equipos y límites" "Redis"
        accesoSeguridad -> postgres "Usuarios, sesiones y dispositivos" "SQLAlchemy"
        eventosAvisos -> postgres "Guarda eventos y avisos" "SQLAlchemy"
        eventosAvisos -> wkPush "Encola notificaciones" "Celery sobre Redis"
        plataformaDatos -> postgres "Aplica tenant_scope y RLS" "SQLAlchemy"
        plataformaDatos -> correo "Envía accesos" "SMTP"
        plataformaDatos -> llm "Genera datos de prueba" "HTTPS"
        plataformaDatos -> alloy "Envía métricas y trazas" "OTLP/HTTP"
        plataformaDatos -> observabilidadExt "Envía errores y alertas" "Sentry, Slack"

        // Packs
        pluginManager -> packCobranza "Activa por tenant" "entry points"
        pluginManager -> packCommerce "Activa por tenant" "entry points"
        pluginManager -> packSubscriptions "Activa por tenant" "entry points"
        pluginManager -> packCalendar "Activa por tenant" "entry points"
        pluginManager -> packPayments "Activa por tenant" "entry points"
        pluginManager -> packKnowledge "Activa por tenant" "entry points"
        hostTools -> packCobranza "Ejecuta sus tools" "ToolContext"
        hostTools -> packCommerce "Ejecuta sus tools" "ToolContext"
        hostTools -> packSubscriptions "Ejecuta sus tools" "ToolContext"
        hostTools -> packCalendar "Ejecuta sus tools" "ToolContext"
        hostTools -> packPayments "Ejecuta sus tools" "ToolContext"
        hostTools -> packKnowledge "Ejecuta sus tools" "ToolContext"
        packKnowledge -> contratos "Usa sus tipos" "pydantic"
        packCobranza -> postgres "Guarda cartera y cobros" "SQLAlchemy, RLS"
        packCommerce -> postgres "Guarda leads y pedidos" "SQLAlchemy, RLS"
        packSubscriptions -> postgres "Guarda planes e inscripciones" "SQLAlchemy, RLS"
        packCalendar -> postgres "Guarda citas" "SQLAlchemy, RLS"
        packPayments -> postgres "Guarda links y pagos" "SQLAlchemy, RLS"
        packKnowledge -> postgres "Guarda fragmentos y vectores" "SQLAlchemy, pgvector"
        packCobranza -> tenantsVolume "Guarda evidencias" "Sistema de archivos"
        packCommerce -> almacenamiento "Guarda fotos" "En proceso"
        packKnowledge -> almacenamiento "Guarda documentos" "En proceso"
        packCommerce -> cloudflare "Valida Turnstile en leads" "HTTPS"
        packCalendar -> googleCalendar "Sincroniza citas" "OAuth2, HTTPS"
        packPayments -> wompi "Crea links de pago" "HTTPS"
        packPayments -> registros "Registra su capacidad de pago" "En proceso"
        packSubscriptions -> registros "Registra su capacidad y pide links" "En proceso"
        packCobranza -> registros "Pide links de pago" "En proceso"
        packCommerce -> registros "Pide links de pago" "En proceso"

        // --- Nivel 3: celery-worker ---
        celeryBeat -> wkSaludMeta "Dispara" "Celery sobre Redis"
        celeryBeat -> wkTwilio "Dispara" "Celery sobre Redis"
        celeryBeat -> wkOutbox "Dispara" "Celery sobre Redis"
        celeryBeat -> wkFacturacion "Dispara" "Celery sobre Redis"
        celeryBeat -> wkMantenimiento "Dispara" "Celery sobre Redis"
        celeryBeat -> wkPacks "Dispara" "Celery sobre Redis"
        wkEnvio -> meta "Envía mensajes" "Graph API"
        wkEnvio -> twilio "Envía mensajes" "REST"
        wkEnvio -> correo "Envía correos del canal" "SMTP"
        wkEnvio -> wawebConnector "Pide envíos" "Redis: waweb:outbox, waweb:reply"
        wkEnvio -> postgres "Actualiza el estado del mensaje" "SQLAlchemy"
        wkSaludMeta -> meta "Vigila tokens, plantillas y precios" "Graph API"
        wkTwilio -> twilio "Sincroniza estado y costos" "REST"
        wkOutbox -> postgres "Lee domain_events" "SQLAlchemy"
        wkOutbox -> wkEnvio "Envía avisos resultantes" "Celery sobre Redis"
        wkOutbox -> correo "Envía el acceso del alta automática" "SMTP"
        wkPush -> expo "Envía notificaciones" "HTTPS"
        wkFacturacion -> postgres "Actualiza suscripciones" "SQLAlchemy"
        wkFacturacion -> correo "Avisa renovaciones" "SMTP"
        wkFacturacion -> observabilidadExt "Alerta diferencias de conciliación" "Slack"
        wkMantenimiento -> postgres "Aplica retenciones y esperas" "SQLAlchemy"
        wkMantenimiento -> redis "Publica el latido" "Redis"
        wkMantenimiento -> llm "Corre evaluaciones" "HTTPS"
        wkPacks -> postgres "Ejecuta las rutinas de cada pack" "SQLAlchemy, RLS"
        wkPacks -> wkEnvio "Envía seguimientos" "Celery sobre Redis"
        wkPacks -> wompi "Crea links de cobro de suscripciones" "HTTPS"
        wkPacks -> tenantsVolume "Lee documentos a indexar" "Sistema de archivos"
        wkPacks -> llm "Crea embeddings de documentos" "HTTPS"

        // --- Despliegue. Fuente: docs/despliegue-coolify.md y docs/runbooks/produccion-vps.md ---
        deploymentEnvironment "Producción heredada" {
            // despliegue-coolify.md:344-375, alloy/config.alloy:115-120. Sin panel nuevo. D-59 la reemplaza por una base nueva en la misma máquina.
            deploymentNode "Máquina de desarrollo" "Excepción temporal a producción fuera de la máquina de desarrollo (D-59)." "Linux, Docker" {
                deploymentNode "Coolify local, entorno production" "Atiende app.solvershub.site." "Coolify" {
                    containerInstance coreApi
                    containerInstance landing
                    containerInstance celeryWorker
                    containerInstance celeryBeat
                    containerInstance postgres
                    containerInstance redis
                }
                deploymentNode "Túnel solvershub" "Iniciado por crontab @reboot." "cloudflared" {
                    containerInstance cloudflared
                }
            }
        }

        deploymentEnvironment "Test" {
            // despliegue-coolify.md:376-434. Observabilidad: solvershub-infra/observabilidad/docker-compose.yml.
            deploymentNode "Máquina de desarrollo" "Comparte máquina con la producción heredada." "Linux, Docker" {
                deploymentNode "Coolify local, entorno test" "Atiende test.solvershub.site." "Coolify" {
                    containerInstance coreApi
                    containerInstance panelWeb
                    containerInstance landing
                    containerInstance celeryWorker
                    containerInstance celeryBeat
                    containerInstance wawebConnector
                    containerInstance postgres
                    containerInstance redis
                    containerInstance tenantsVolume
                }
                deploymentNode "Túnel solvershub-test" "Publica pack_cobranza, /api/v1, /webhooks y /healthz en core-api, y el resto en el panel." "cloudflared" {
                    containerInstance cloudflared
                }
                deploymentNode "Stack de observabilidad" "Docker Compose en el host, fuera de Coolify." "Docker Compose" {
                    containerInstance alloy
                    containerInstance otelcolEs
                    containerInstance elasticsearch
                    containerInstance kibana
                }
            }
        }

        deploymentEnvironment "Producción objetivo" {
            // despliegue-coolify.md:21-114, HU-ARCH-01. Diseño: el VPS aún no está contratado.
            deploymentNode "prod-app" "VPS de aplicación. OBJETIVO: aún no existe." "Linux, Coolify" "Objetivo" {
                deploymentNode "Coolify" "API en 3003, panel en 3004 y landing en 3005." "Coolify" "Objetivo" {
                    containerInstance coreApi
                    containerInstance panelWeb
                    containerInstance landing
                    containerInstance celeryWorker
                    containerInstance celeryBeat
                    containerInstance tenantsVolume
                }
                deploymentNode "Túnel solvershub-prod" "Sobre systemd. Publica api., app., el dominio raíz y www." "cloudflared" "Objetivo" {
                    containerInstance cloudflared
                }
            }
            deploymentNode "prod-data" "VPS de datos. OBJETIVO: aún no existe." "Linux" "Objetivo" {
                containerInstance postgres
                containerInstance redis
            }
            deploymentNode "Proveedor S3" "Otro proveedor, para que un fallo no afecte también a los respaldos." "S3 compatible" "Objetivo" {
                softwareSystemInstance s3
            }
        }
    }

    views {
        systemContext solvershub "contexto" "Nivel 1: quién usa solvershub y con qué sistemas se integra." {
            include *
            include clienteFinal
            exclude "element.tag==Objetivo"
            autoLayout lr
        }

        container solvershub "contenedores" "Nivel 2: procesos, almacenes y quién los usa." {
            include landing panelWeb apk coreApi celeryWorker celeryBeat wawebConnector postgres redis tenantsVolume cloudflared
            include equipo tester negocio gestor visitante
            autoLayout lr
        }

        container solvershub "contenedores-integraciones" "Nivel 2: con qué sistema externo habla cada contenedor." {
            include coreApi celeryWorker wawebConnector landing apk cloudflared
            include meta twilio whatsappWeb wompi llm expo googleCalendar locationiq correo cloudflare posthog
            autoLayout lr
        }

        container solvershub "telemetria" "Nivel 2: cómo sale la telemetría (ADR-0188)." {
            include "element.tag==Observabilidad"
            include coreApi celeryWorker celeryBeat wawebConnector observabilidadExt equipo
            autoLayout lr
        }

        component coreApi "core-api-bloques" "Nivel 3: bloques internos de core-api." {
            include "element.parent==coreApi"
            exclude "element.tag==Pack"
            exclude "element.tag==Contrato"
            exclude "element.tag==Canal"
            autoLayout lr
        }

        component coreApi "core-api-entrada" "Nivel 3: entrada y canales." {
            include "element.tag==Entrada"
            include orquestador canalesInstalables wkEnvio
            include meta twilio wompi correo cloudflare locationiq facturacion
            include panelWeb apk landing wawebConnector redis
            autoLayout lr
        }

        component coreApi "core-api-turno" "Nivel 3: de la cola a la respuesta." {
            include "element.tag==Turno"
            include gateway contratos wkMantenimiento
            include redis tenantsVolume llm
            autoLayout lr
        }

        component coreApi "core-api-plataforma" "Nivel 3: servicios transversales." {
            include "element.tag==Plataforma"
            include apiV1 adminSsr hostTools wkPush
            include wompi correo llm tenantsVolume observabilidadExt
            autoLayout lr
        }

        component celeryWorker "worker" "Nivel 3: familias de tareas de celery-worker. celery-beat dispara las programadas." {
            include "element.parent==celeryWorker"
            include wawebConnector meta twilio correo expo llm wompi observabilidadExt
            autoLayout lr
        }

        component coreApi "packs" "Nivel 3: packs verticales y su carga. También se ejecutan en celery-worker." {
            include "element.tag==Pack"
            include contratos canalesInstalables pluginManager registros almacenamiento gateway kernel agentesReleases
            include apk landing wompi cloudflare googleCalendar twilio
            autoLayout lr
        }

        deployment solvershub "Producción heredada" "despliegue-heredada" "Despliegue actual de producción, documentado en despliegue-coolify.md." {
            include *
            autoLayout lr
        }

        deployment solvershub "Test" "despliegue-test" "Entorno de test." {
            include *
            autoLayout lr
        }

        deployment * "Producción objetivo" "despliegue-objetivo" "OBJETIVO, no es el estado actual: dos VPS pendientes de contratar (HU-ARCH-01)." {
            include *
            autoLayout lr
        }

        styles {
            element "Person" {
                shape Person
                background #1f4e79
                color #ffffff
            }
            element "Software System" {
                background #2e6da4
                color #ffffff
            }
            element "Externo" {
                background #7f8c8d
                color #ffffff
            }
            element "Container" {
                background #3f8fd2
                color #ffffff
            }
            element "Component" {
                background #8cc4ee
                color #10253a
            }
            element "Web" {
                shape WebBrowser
            }
            element "Movil" {
                shape MobileDevicePortrait
            }
            element "Base de datos" {
                shape Cylinder
            }
            element "Volumen" {
                shape Folder
            }
            element "Observabilidad" {
                background #5b7f6b
                color #ffffff
            }
            element "Pack" {
                background #d9b36c
                color #2b2110
            }
            element "Solo test" {
                stroke #c77700
                strokeWidth 5
                border dashed
            }
            element "Sin activar" {
                stroke #c77700
                border dashed
            }
            element "En retiro" {
                opacity 60
                border dotted
            }
            element "Objetivo" {
                border dashed
            }
            relationship "Objetivo" {
                style dashed
                color #c77700
            }
        }
    }

    configuration {
        scope softwaresystem
    }
}
