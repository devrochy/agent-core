# agentcore-channel-waweb

Canal WhatsApp Web **no oficial** (ADR-0074) para agent-core.

No ejecuta el socket: eso vive en el proceso `connector` del core
(`core/waweb/`). Este paquete solo aporta los adaptadores `InboundAdapter` /
`OutboundAdapter` que el registry descubre por el entry_point
`agent_core.channels`.

El envío cruza de proceso por Redis (request/reply): el adaptador corre en el
worker Celery y el connector drena la cola con la política anti-baneo del
tenant.
