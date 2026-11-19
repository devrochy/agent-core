# ADR-0052 — Extensibilidad de capacidades: tools declarativas y canales por entry_points

- **Estado:** aceptado
- **Fecha:** 2026-09-23

## Contexto

Dos capacidades nuevas exigen escribir Python en el core y desplegar (violando el
espíritu de la invariante #2, "nunca fork por cliente; config → pack → plugin"):

1. **Tools**: toda tool es una clase `BaseSkill` en Python (`core/mcp/types.py`);
   no hay forma de declarar una tool HTTP/JSON-Schema por tenant. Además
   `ToolContext` (el contrato, `packages/contracts/.../contracts.py`) promete
   `db`/`secrets`/`http` con allowlist, pero el `ToolContext` del runtime solo lleva
   `conversation_id`/`customer_id`/`idempotency_key`/`channel` — **sin** acceso a red
   ni recursos acotados. Una tool declarativa necesitaría exactamente eso, de forma
   segura (invariante #6: sin pool ni `environ`).
2. **Canales**: el registry está **hardcodeado** (`core/gateway/channels/registry.py`
   con un solo proveedor `"meta"`), y la entrada fija `inbound_adapter_for("meta")`
   (`core/gateway/webhooks.py`). Sumar Telegram/Instagram exige editar el core.

Los plugins ya se descubren por **entry_points** (`ENTRY_POINT_GROUP =
"agent_core.plugins"`, `core/plugin_manager.py`): ese es el patrón a generalizar.

## Decisión

1. **Canales por entry_points.** Nuevo grupo `agent_core.channels`. Un paquete de
   canal expone una clase que provee adaptadores `InboundAdapter`/`OutboundAdapter`
   por `provider`. `registry.py` escanea ese grupo **además** de los adaptadores
   built-in (`meta`), y resuelve por proveedor. La entrada deja de asumir `meta`:
   `webhooks.py` resuelve el proveedor de la conexión (o por el slug). Los canales
   nuevos se instalan sin tocar el core.
2. **Tools declarativas por tenant.** Dos piezas:
   - **`ToolContext` con `http` acotado:** el protocolo del contrato ya declaraba
     `http`/`db`/`secrets`; lo que faltaba era el **runtime**. Se implementa
     `ScopedHttp` (allowlist de dominios, `httpx`, sin acceso a red arbitraria) y se
     pasa en el `ToolContext` del runtime. No cambia la forma del contrato congelado
     (sin bump); el runtime nunca entrega el pool crudo ni `environ` (invariante #6).
     `db`/`secrets` quedan reservados para una extensión posterior.
   - **Registro declarativo:** `AgentSpec.extensions["tools"]` (JSON) declara tools
     HTTP (`{name, description, method, url, headers?, body?, parameters}`) con
     JSON-Schema de parámetros. El core las registra por-tenant (no en el `mcp_host`
     global, que revienta con nombres duplicados) y las ejecuta con el `http`
     acotado. Quedan sujetas al mismo `_tool_denial`/scopes/`maxCalls` de ADR-0050.
3. **Seguridad primero.** El `http` declarativo respeta una **allowlist de dominios
   por tenant** (declarada en `extensions["tools"][].allowed_domains` y validada
   contra `settings` si hace falta). Nada de URLs arbitrarias desde el LLM: la URL
   base la fija el admin, los parámetros se interpolan de forma segura.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Tools declarativas con acceso total a `httpx` | Rompe la invariante #6 (sin control de red); vector SSRF. Se acota con allowlist. |
| Registrar tools declarativas en el `mcp_host` global | Nombres por-tenant chocan ("Skill duplicada"); el host es singleton de proceso. |
| Canales en un dict del core (status quo) | Obliga a desplegar el core por cada canal nuevo (fork de facto). |
| Completar `ToolContext` solo con `http` y dejar `db`/`secrets` fuera | Suficiente para tools HTTP; `db`/`secrets` quedan para una extensión posterior. Se documenta como reservado. |

## Consecuencias

**Positivas:** un canal nuevo es un paquete instalable (entry_points); un negocio
declara tools HTTP en su spec sin fork; `ToolContext` deja de mentir en el runtime.

**Negativas / lo que aceptamos pagar:** `ScopedHttp` con allowlist agrega
superficie de red al runtime (mitigada por dominios fijos y sin `environ`); las tools
declarativas son más simples que una tool Python (sin lógica arbitraria); `db`/`secrets`
en `ToolContext` quedan reservados (no implementados). Sin cambio de contrato.

**Qué haría que revisáramos esto:** que la allowlist de dominios sea insuficiente
(hace falta auth OAuth por tool → tool Python o MCP externo), o que un canal necesite
ruteo de entrada propio (webhook dedicado).

## Impacto en el código
channels/base.py` (contrato de registro), `core/gateway/webhooks.py` (resolver por
proveedor), `core/mcp/types.py` (`ScopedHttp`) + `core/mcp/host.py` (ToolContext),
`core/mcp/declarative.py` (nuevo: tools declarativas), `core/orchestrator/loop.py`
(`_tool_denial`/ejecución), `core/agents/spec.py` (`extensions["tools"]`), UI (tab
Habilidades). Tests por pieza. **Sin cambio de contrato congelado.**

## Correcciones post-auditoría (`auditor-multitenant`)

- **SSRF (alto)**: `ScopedHttp` bloquea IPs internas/loopback/link-local/rangos
  reservados y metadata de cloud, **aunque** estén en la allowlist; solo `http(s)`
  y el host debe resolver a la allowlist. ~~(DNS que resuelve a IP privada queda como
  limitación documentada.)~~ Cerrado el 2026-10-08: el host debe tener forma de
  dominio público (sin hosts de una etiqueta como `redis`, sin formas numéricas no
  canónicas como `127.1`/`2130706433`, sin `*.internal`/`*.local`/`localhost`); se
  resuelve el DNS y se rechaza si **alguna** IP no es pública (privada, loopback,
  link-local, reservada, multicast, CGNAT `100.64/10`, IPv4 embebida en IPv6); y se
  conecta a la IP ya validada con `Host`/SNI del dominio (sin segunda resolución,
  así no hay DNS rebinding), sin redirects ni proxies del entorno. Las tools
  declarativas solo guardan `allowed_domains` que sean FQDN públicos.
- **Allowlist por turno (medio)**: el `ScopedHttp` del turno se acota a la allowlist
  de **la tool que se ejecuta**, no a la unión de todas.
- **Shadowing (medio)**: una tool declarativa no puede pisar una tool ya registrada
  (prioridad al host).
