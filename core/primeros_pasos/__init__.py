"""Primeros pasos de un negocio y embudo de activación (HU-SH-06, ADR-0124).

El checklist del Resumen se **calcula del estado real** cada vez (no hay casillas que alguien
marque a mano): datos del negocio, WhatsApp conectado y verificado, catálogo o servicios,
probar el asistente, equipo invitado, las habilidades que quedaron pendientes de configurar y
el tipo de negocio que espera un plan más alto. Lo único que se guarda es la **primera vez**
que se cumplió cada paso, con los segundos desde el alta (`onboarding_hitos`): eso es el
embudo de activación.

Módulos:

- `contrato`: lo que una habilidad aporta al paso «catálogo o servicios»
  (`BaseModule.contenido_cargado`). Es lo ÚNICO de este paquete que puede importar un pack.
- `reglas`: funciones puras que deciden el estado de cada paso con lo que se leyó.
- `calculo`: lee el estado real del negocio (siempre bajo su `tenant_scope` y filtrando por
  `tenant_id`) y arma los pasos.
- `hitos`: registra la primera vez de cada paso (idempotente) y arma el embudo.
"""

from __future__ import annotations

# Actor de la auditoría de los hitos: los registra el core al evaluar, no una persona.
ACTOR = "core:primeros_pasos"
