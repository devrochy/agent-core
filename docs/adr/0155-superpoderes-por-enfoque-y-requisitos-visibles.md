# ADR-0155 — Superpoderes por enfoque y requisitos visibles al crear un agente

- **Estado:** propuesto (falta que lo acepte el dueño).
- **Fecha:** 2026-10-23
- **Contexto:** HU-AOS-50. **Enmienda:** ADR-0139 (plantillas y editor, adenda HU-AOS-24) y usa ADR-0118
  (oferta fail-closed por agente), ADR-0138 (enrutamiento léxico) y ADR-0151 §6 (las fotos solo se describen).

## Contexto

El editor de agentes (`GET /admin/agent-superpowers`) lista todos los superpoderes de los packs que el negocio
tiene habilitados, sin mirar el enfoque del agente. Un agente de Cobranza puede marcar «Tomar un pedido» y el
dueño, que no es técnico, no sabe qué es de qué agente. «Enfoque» no es un campo del agente: es la plantilla
(`ventas`, `agenda`, `cobranza`, `suscripciones`) con la que se creó, y el agente guarda esa `key`.

Marcar un superpoder en el editor **no agrega tools** al agente (`derivar_contenido` las filtra solo al crear),
pero el superpoder ajeno corre a medias: `permitida_para_el_agente` deja pasar las acciones de la etapa en foco.
El daño real es de **enrutamiento**: con ADR-0138 el dueño de un procedimiento es el agente que declara menos
procedimientos, así que un Cobranza con dos poderes puede quitarle tráfico a Ventas.

## Decisión

1. **El enfoque de un agente son los `procedimientos` de su plantilla.** Es el único origen de verdad; no se
   duplica en otro catálogo. `procedimientos_del_enfoque(key)` devuelve ese conjunto, o `None` si la `key` no es
   de una plantilla (`general`, `coordinador`, especialistas por API o presets v1): esos no se restringen.
2. **La API ofrece solo lo del enfoque.** `GET /admin/agent-superpowers?agent_id=` devuelve los superpoderes del
   negocio que pertenecen a la plantilla del agente. El `agent_id` se resuelve dentro del negocio del principal
   (404 si es de otro). Sin `agent_id` el comportamiento no cambia.
3. **Publicar valida el enfoque.** `problemas_de_superpoderes` emite `superpoder_fuera_de_enfoque` (422 por
   campo, `procedures[i].id`) si el agente declara un procedimiento que existe pero no es de su plantilla.
4. **El editor deja quitarlo.** Un superpoder marcado que el catálogo del agente ya no ofrece (de otro enfoque o de
   una habilidad que el negocio dejó de tener) se muestra aparte con un botón «Quitar»; sin él, el 422 sería
   imposible de corregir.
5. **No se toca `procedure/1`, los packs ni `pack-versions.lock.json`.** Las plantillas siguen siendo
   configuración del core.

## Brecha conocida

La guarda corre en `publish` (`publicar_en_conexiones`). `POST /admin/agents/{id}/versions` y los agentes
armados por CLI no pasan por ella; un agente creado a mano con la `key` de una plantilla queda sujeto a la regla.
Es aceptable: el editor es el camino del dueño no técnico.

## Agentes que ya existen

No se republican ni se migran en esta decisión (la adenda de ADR-0139 del 2026-10-19 descartó republicar agentes
existentes). Un agente con un superpoder fuera de su enfoque sigue funcionando hasta su siguiente publicación, que
fallará con `superpoder_fuera_de_enfoque` hasta que el dueño lo quite. Una limpieza automática exigiría derogar
esa adenda y se decide solo después de medir cuántos agentes están afectados.

## Siguiente (fuera de este ADR)

- **Requisitos visibles** (HU-AOS-50, entrega 2): avisar al elegir plantilla, en el editor y al publicar qué falta
  configurar (cobro en línea, calendario, catálogo o cartera cargados, proveedor de suscripciones). Se apoya en
  `pack_installations.status = 'pending_config'` y en el hook `contenido_cargado` de cada pack, sin lógica por pack
  en el core. Este ADR se enmienda cuando se decida.
- **Catálogo en los packs:** cuando HU-AOS-16 parte 2 declare capacidades en los packs, el enfoque pasará de
  la lista de la plantilla a esa declaración.

## Consecuencias

- Tres de las cuatro plantillas tienen un solo procedimiento: el filtro les deja una casilla. El valor para el
  dueño está en no ver lo ajeno y, en la entrega 2, en saber qué le falta configurar.
- Quitar un procedimiento de un agente cambia el desempate de enrutamiento en vivo (ADR-0138).
- Se agrega el parámetro `agent_id` al OpenAPI (aditivo, retrocompatible).
