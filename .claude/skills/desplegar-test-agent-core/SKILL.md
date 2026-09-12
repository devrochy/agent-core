---
name: desplegar-test-agent-core
description: >
  Mergea un PR a develop y despliega el resultado en el entorno de test de
  Coolify (agent-core-test), verificando que quedó sano (contenedor healthy,
  migraciones al día, panel respondiendo). Úsala cuando el usuario quiera
  "desplegar a test", "subir esto a test", "validar en test" o mergear un PR
  y ver el resultado corriendo — en el repo agent-core (Python/FastAPI).
---

# Desplegar a test (agent-core)

Ciclo completo: PR → `develop` → despliegue en Coolify → verificación.

## Datos del entorno

| Qué | Valor |
| --- | --- |
| Coolify | `http://127.0.0.1:8000` |
| App test | `agent-core-test`, UUID `ggzjdyc7zsemt6kzafx5rg7m` (rama `develop`) |
| Postgres test | contenedor `huvhzaxim2notqkcxqa5930a`, DB `agent_core`:
  `docker exec huvhzaxim2notqkcxqa5930a psql -U postgres -d agent_core` |
| Panel admin | solo funciona en `http://127.0.0.1:3003/admin` |
| Tenants reales en esa BD | `tenant-1` (dev) y `yopoldo` (real, WhatsApp
  conectado — restaurante de pollo asado en Manizales) |
| Token de deploy | abilities `["deploy","read","write"]`, guardado en la BD
  de Coolify (`personal_access_tokens` id 3). El texto plano no queda en
  ningún archivo — pedírselo al usuario. |

El nombre del contenedor de la app cambia en cada deploy (sufijo numérico
nuevo): resolver con `docker ps --format '{{.Names}}' | grep ggzjdyc`.

No confundir con `agent-sale`: ese es OTRO proyecto (Node/TypeScript,
`agent_sale` DB, migraciones `pgmigrations`/`npm run migrate`, puerto
`:3000`). Este skill se llamaba antes `desplegar-test` a secas, mismo
nombre que el skill de usuario (`~/.claude/skills/desplegar-test`) para
`agent-sale` — un bug real del Skill tool (reportado a Anthropic, ver
`docs/audit/` o el historial de la sesión 2026-09-12) hacía que el de
usuario ganara pese a estar en el proyecto equivocado, cargando datos de
`agent-sale` estando en `agent-core`. Se renombró a
`desplegar-test-agent-core` específicamente para eliminar la colisión de
nombre mientras Anthropic corrige la resolución real.

## 1. Mergear el PR

Antes de mergear:
- Comprobar que el CI está verde (o que lo que falla es una falla ya
  conocida y documentada — el "Gate de evals" tiene 2-3 escenarios flaky por
  criterio del LLM, no por infraestructura; confirmarlo leyendo el log del
  job antes de asumir que es lo mismo de siempre).
- Si el usuario tiene cambios sin commitear en la rama del PR, preguntarle
  qué hacer con ellos antes de mergear.

```bash
gh pr merge <n> --merge
```

## 2. Lanzar el despliegue

Vía API de Coolify (no la UI — más confiable):

```bash
curl -s -X POST "http://127.0.0.1:8000/api/v1/applications/ggzjdyc7zsemt6kzafx5rg7m/start" \
  -H "Authorization: Bearer <token>" -H "Accept: application/json"
# devuelve {"deployment_uuid": "..."}
```

Esperar a que termine (poll cada 15s, no hay webhook):

```bash
curl -s "http://127.0.0.1:8000/api/v1/deployments/<deployment_uuid>" \
  -H "Authorization: Bearer <token>" -H "Accept: application/json"
# status pasa de in_progress a finished (o failed)
```

## 3. Verificar que quedó sano

**Nunca dar por bueno el estado "finished" de Coolify solo.**

```bash
# 1. Contenedor arriba y healthy (esperar unos segundos tras el deploy).
docker ps --format '{{.Names}}\t{{.Status}}' | grep ggzjdyc

# 2. Migraciones al día: los dos valores tienen que coincidir.
docker exec huvhzaxim2notqkcxqa5930a psql -U postgres -d agent_core -tAc "SELECT version_num FROM alembic_version;"
uv run python -c "
from alembic.config import Config
from alembic.script import ScriptDirectory
cfg = Config('alembic.ini')
print(ScriptDirectory.from_config(cfg).get_current_head())
"

# 3. El código del fix esperado está realmente en el contenedor nuevo
#    (grep puntual de una línea del cambio, no asumir por el deploy status).
docker exec <contenedor-nuevo> grep -n "<algo del cambio>" /app/<ruta/al/archivo.py>

# 4. El panel responde de verdad.
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:3003/admin/login   # 200
```

Para probar lógica real sin credenciales de admin del panel, se puede correr
código directo dentro del contenedor nuevo (útil para repros dirigidas):

```bash
docker exec <contenedor-nuevo> python3 -c "
import asyncio, sys
sys.path.insert(0, '/app')
async def main():
    from core.shared.tenant import tenant_scope
    # ... import de lo que se quiera probar, dentro de tenant_scope(tenant_id)
asyncio.run(main())
"
```

Cualquier llamada así que haga un INSERT/UPDATE/DELETE real cuenta como
escritura en datos reales — pedir permiso antes, igual que con SQL directo.

## Si algo falla

- **El contenedor desaparece y el deploy figura como Success**: revisar los
  logs del contenedor (`docker logs <contenedor>`) buscando el traceback —
  casi siempre es una migración pendiente o un import roto.
- **El "Gate de evals" falla en CI**: antes de asumir que es infra rota,
  mirar el log — si el fallo es "no llamó la tool X" o "respuesta demasiado
  larga" en 1-2 escenarios puntuales, es variabilidad conocida del LLM, no
  bloqueante. Si es un `ModuleNotFoundError`, un `RecursionError` o algo que
  rompe TODOS los escenarios, sí es infraestructura real.

## Al terminar

Reportar al usuario, con datos y no con adjetivos: qué commit quedó
desplegado, el resultado de comparar migraciones, y el resultado de
`/admin/login`. Si algo quedó a medias, decirlo explícitamente.
