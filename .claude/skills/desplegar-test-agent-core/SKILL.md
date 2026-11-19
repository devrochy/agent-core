---
name: desplegar-test-agent-core
description: >
  Mergea un PR a develop y despliega el resultado en el entorno de TEST de
  Coolify (app `tr0vpdei5av6szcgwpe1vwcg`, test.solvershub.site), verificando
  que quedó sano (contenedor healthy, migraciones al día, API y panel
  respondiendo). Úsala cuando el usuario quiera "desplegar a test", "subir esto
  a test", "validar en test" o mergear un PR y ver el resultado corriendo — en
  el repo agent-core / solvershub-backend (Python/FastAPI). Nunca toca producción.
---

# Desplegar a test (agent-core)

Ciclo completo: PR → `develop` → despliegue en Coolify → verificación.
La fuente de verdad de UUIDs y puertos es `docs/despliegue-coolify.md`: si
algo de esta tabla no coincide con ese documento, gana el documento y se
corrige esta skill.

> ⚠️ **Producción no se toca desde esta skill.** En Coolify, la app de
> producción (`ggzjdyc7zsemt6kzafx5rg7m`, entorno `production`,
> `127.0.0.1:3003`, rama `main`) **sigue llamándose `agent-core-test`** por
> herencia, y su worker y su beat se llaman `agent-core-test-worker` y
> `agent-core-test-beat`. No confíes en el nombre: confía en el UUID y en la
> etiqueta `coolify.environmentName`. Hasta el 2026-10-01 esta skill apuntaba
> por error a esos UUID de producción. El hook `.claude/hooks/guards.py` pide
> confirmación humana ante cualquier comando que opere sobre ellos.

## Datos del entorno de test

| Qué | Valor |
| --- | --- |
| Coolify | `http://127.0.0.1:8000` · proyecto SolversHub `xqrxaj7by6yclupym5u9rcvj` · entorno `test` `vs22dwtxgnmlfelvkeft3flt` |
| App test | `agent-core-test`, UUID **`tr0vpdei5av6szcgwpe1vwcg`** (rama `develop`, `3013:8000`, health `/healthz`) |
| Worker / Beat | `uqfcdd4a3cf5d4iqa0uosrp0` / `set9kgdxianzvsmprklzyym7` (se llaman `clone-of-agent-core-test-*`) |
| Panel test | `panel-web-test`, UUID `eyfamlms7ipmba4h6cct2i4a` (repo `solvershub-panel`, `3014:3000`, health `/login`) |
| Postgres test | contenedor **`48h2r2laml64eyrggfg6eqld`**, db `agent_core`: `docker exec 48h2r2laml64eyrggfg6eqld psql -U postgres -d agent_core` |
| Redis test | `rqkxxnen7ysp7xmqhocrbidw` |
| URL pública | `https://test.solvershub.site` (Cloudflare Access; `/healthz`, `/webhooks` y la API de campo en bypass) |
| Token de deploy | Token de la API de Coolify (`Authorization: Bearer <id>\|<texto>`). El texto plano no queda en ningún archivo: **pedírselo al usuario** y no guardarlo. |

El nombre del contenedor de la app cambia en cada deploy (sufijo numérico
nuevo): resolver con `docker ps --format '{{.Names}}' | grep tr0vpdei`.

No confundir con `agent-sale`: ese es OTRO proyecto (Node/TypeScript,
`agent_sale` DB, puerto `:3000`) y tiene su propia skill de usuario
`desplegar-test`. Esta skill se llama `desplegar-test-agent-core` para evitar
la colisión de nombres.

## 0. Confirmar que el destino es test

Antes de lanzar nada, comprobar la etiqueta de entorno del contenedor actual:

```bash
docker inspect --format '{{index .Config.Labels "coolify.environmentName"}}' \
  "$(docker ps -q --filter name=tr0vpdei5av6szcgwpe1vwcg | head -1)"
# debe imprimir exactamente: test
```

Si imprime otra cosa, **parar** y avisar al usuario: el inventario cambió y
hay que actualizar `docs/despliegue-coolify.md` y esta skill.

## 1. Mergear el PR

Antes de mergear:
- Comprobar que el CI de la PR está verde (o que lo que falla es una falla ya
  conocida y documentada — el "Gate de evals" tiene 2-3 escenarios flaky por
  criterio del LLM, no por infraestructura; confirmarlo leyendo el log del
  job antes de asumir que es lo mismo de siempre).
- Si el usuario tiene cambios sin commitear en la rama del PR, preguntarle
  qué hacer con ellos antes de mergear.
- El merge es una acción visible para terceros: confirmarlo con el usuario.

```bash
gh pr merge <n> --merge
```

Después del merge, **esperar el CI del push a develop** antes de desplegar. La PR
corre el nivel rápido del CI (ADR-0120): solo el subconjunto `aislamiento` de
integración, sin evals ni journeys. La integración completa y los gates de evals
corren en el push a develop:

```bash
gh run list --workflow ci.yml --branch develop --event push --limit 1
gh run watch <run-id> --exit-status
```

Si sale en rojo, **no desplegar**. Avisar al usuario con el paso que falló y el
log del job `integracion`; las fallas flaky conocidas del gate de evals se tratan
igual que arriba.

## 2. Lanzar el despliegue

Vía API de Coolify (no la UI — más confiable):

```bash
curl -s -X POST "http://127.0.0.1:8000/api/v1/applications/tr0vpdei5av6szcgwpe1vwcg/start" \
  -H "Authorization: Bearer <token>" -H "Accept: application/json"
# devuelve {"deployment_uuid": "..."}
```

Si el cambio toca crons o tareas Celery, desplegar también el worker y el
beat de test (`uqfcdd4a3cf5d4iqa0uosrp0`, `set9kgdxianzvsmprklzyym7`): usan
la misma imagen y, si no se redespliegan, siguen con el código anterior.

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
docker ps --format '{{.Names}}\t{{.Status}}' | grep tr0vpdei

# 2. Migraciones al día: core y cada pack tocado por el cambio.
docker exec 48h2r2laml64eyrggfg6eqld psql -U postgres -d agent_core -tAc "SELECT version_num FROM alembic_version;"
docker exec 48h2r2laml64eyrggfg6eqld psql -U postgres -d agent_core -tAc "SELECT version_num FROM alembic_version_pack_<pack>;"
uv run alembic heads                                   # head del core en el repo
ls packages/pack-<pack>/alembic/versions | sort | tail -1   # último de cada pack

# 3. RLS: ninguna tabla con RLS habilitada y sin FORCE (lo mismo que mira el
#    checklist de producción, `rls_forced`).
docker exec 48h2r2laml64eyrggfg6eqld psql -U postgres -d agent_core -tAc "
  select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
  where n.nspname = 'public' and c.relkind = 'r'
    and c.relrowsecurity and not c.relforcerowsecurity;"

# 4. El código del fix esperado está realmente en el contenedor nuevo
#    (grep puntual de una línea del cambio, no asumir por el deploy status).
docker exec <contenedor-nuevo> grep -n "<algo del cambio>" /app/<ruta/al/archivo.py>

# 5. API y panel responden (por los puertos locales: por el dominio,
#    Cloudflare Access redirige si no hay sesión).
curl -s http://127.0.0.1:3013/healthz                                   # {"status":"ok"}
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:3014/login    # 200
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
escritura en datos del entorno — pedir permiso antes, igual que con SQL
directo. Los datos de demo de test se siembran con
`docker exec "$APP" python scripts/seed_demo_login.py` (ver
`docs/despliegue-coolify.md`), también con permiso.

## Si algo falla

- **El contenedor desaparece y el deploy figura como Success**: revisar los
  logs del contenedor (`docker logs <contenedor>`) buscando el traceback —
  casi siempre es una migración pendiente o un import roto.
- **Un contenedor atascado en crash-loop bloquea el siguiente deploy**
  (`No such container` al limpiar): pedir permiso para `docker rm -f
  <contenedor>` y redesplegar.
- **El "Gate de evals" falla en CI**: antes de asumir que es infra rota,
  mirar el log — si el fallo es "no llamó la tool X" o "respuesta demasiado
  larga" en 1-2 escenarios puntuales, es variabilidad conocida del LLM, no
  bloqueante. Si es un `ModuleNotFoundError`, un `RecursionError` o algo que
  rompe TODOS los escenarios, sí es infraestructura real.

## Al terminar

Reportar al usuario, con datos y no con adjetivos: qué commit quedó
desplegado en test, el resultado de comparar migraciones (core y packs), el
chequeo de FORCE y el resultado de `/healthz` y `/login`. Si algo quedó a
medias, decirlo explícitamente. A producción se llega con la skill `release`.
