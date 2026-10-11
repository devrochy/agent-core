## Agent OS: Coordinador y General por conexión (backfill B2) (2026-10-10)

- HU-AOS-13 (parte 1, #374). **Backfill de agentes** `agent-core db backfill-agentes [--dry-run]
  [--tenant] [--lote]` (paso B2 de SPEC-26 §4.4). Por negocio: un agente `coordinador` (core,
  KernelSpec derivado de la `persona` v2) y un `general` (specialist, el AgentSpec v2 activo
  convertido por el lector tolerante, con sus procedimientos abiertos), sus versiones y una
  instancia de cada uno en cada conexión conversacional (el General, por defecto). Las
  integraciones no reciben agentes.
  - Idempotente por `legacy_agent_spec_id`: re-correrlo no crea nada y cubre las conexiones
    nuevas; si el v2 activo cambió, publica la versión siguiente.
  - Exige el backfill de conexiones de HU-AOS-11 hecho antes. Si falta, el negocio queda
    bloqueado con el comando a correr y el CLI sale con código 1.
  - Los secretos literales de las `declarativeTools` v2 se copian a la instalación `core` antes
    de la primera versión. Ninguna versión ni borrador lleva literales. Un conflicto con un
    secreto existente es un error del negocio, sin escrituras. El backfill nunca pisa un secreto
    (lo comprueba con la fila bloqueada) ni le suma dominios por su cuenta: si la tool sumó
    dominios, pide reescribirlo en el editor.
  - No se corrió en ningún entorno. Sin cambio de conducta: el runtime sigue en
    `aos_runtime = legacy` y `agent_specs` no cambia, salvo la siembra del spec por defecto que
    haría el loop.
- **API de instancias** (lectura): `GET /api/v1/admin/connections/{id}/agents`, con el
  Coordinador primero y la última versión publicada de cada agente. Las escrituras quedan para la
  parte 2.
- `core/agents/instancias.py::asegurar_instancias_base`: Coordinador + General en una conexión,
  idempotente. Nunca pisa otro Coordinador ni otro agente por defecto. La usarán el alta
  posterior de una conexión, la promoción de ADR-0127 §3 y las releases (HU-AOS-14).
- Lector tolerante: el header sensible de una tool sin `allowed_domains` se descarta con aviso.
  Esa tool hoy no llama a nadie. Adenda de HU-AOS-13 en ADR-0113.
- **Credenciales fuera de la lista también por referencia** (decisión del dueño 2026-10-11, adenda en
  ADR-0113). `core/agents/credenciales.py` detecta como sensible un nombre de header, parámetro de
  query o clave del body con patrón de credencial: `auth`, `token`, `key`, `secret`, `password`,
  `session`, `cookie`, `signature`, `credential`…. Los ambiguos cuentan como sensibles. El userinfo de
  la URL también es credencial.
  - El linter v3 rechaza esos literales (`credencial_literal`, sin el valor).
  - El lector tolerante y el backfill los mueven a la instalación `core`: el userinfo pasa a
    `Authorization: Basic`, la query a `{secret:<nombre>}` y el body a `{"secret": ...}`.
  - El editor de tools declarativas v2 aplica el patrón a los headers.
  - El loop legacy no cambia. Las referencias de URL y body las resolverá el runtime v3.
