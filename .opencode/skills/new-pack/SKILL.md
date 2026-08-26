---
name: new-pack
description: >
  Crea un vertical pack nuevo en agent-core (barberías, comercio, encuestas). Usar SOLO cuando
  el usuario pida "un pack de", "vertical nuevo", "CRM para <nicho>" o "adaptar el sistema a <sector>".
---

Antes de generar nada, responde estas tres preguntas y muéstramelas:

1. ¿Cuál es la **entidad central** del nicho? (producto, cita, respuesta, póliza…)
   Si es la misma que la de un pack existente, esto no es un pack nuevo: es una extensión.
2. ¿Qué del núcleo canónico reutiliza tal cual? (contact, conversation, deal, payment…)
3. ¿Hay un cliente real esperando este pack? Si no, detente: no se construyen packs
   especulativos (regla de tres, ver ADR-0001).

## Estructura a generar

```
packages/pack-$0/
├── agentpkg.yaml           manifest completo
├── config.schema.json      JSON Schema → el panel genera el formulario
├── migrations/             una por entidad, siguiendo /db-migration
├── src/
│   ├── entities/
│   ├── tools/              una carpeta por tool, siguiendo /new-tool
│   └── views.ts            descriptores de vista (table | kanban | calendar | detail)
├── skills/                 playbooks del agente en formato SKILL.md
├── evals/                  golden set del pack
└── CLAUDE.md               invariantes propias del pack
```

## Manifest mínimo

```yaml
name: pack-$0
version: 0.1.0
kind: pack
coreApi: "^1.0.0"
requires:
  scopes: []
  secrets: []
  network: []
provides:
  entities: []
  migrations: ./migrations
  tools: []
  views: []
  workflows: []
  playbooks: []
config:
  $schema: ./config.schema.json
runtime: in-process
```

## Reglas

- Ningún import desde otro pack.
- Toda entidad del pack referencia `contact(id)` del core, no duplica contactos.
- Las vistas se declaran, no se escriben en React.
- Los playbooks van en `skills/` como `SKILL.md`, nunca embebidos en código.