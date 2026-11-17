"""Los leads viven en el core (ADR-0193, HU-AOS-70).

El lead es el enriquecimiento de embudo de un `Customer` (ADR-0022): etapa, puntaje, interés, seguimiento, motivo de
pérdida, valor y origen. Lo capta cualquier agente con la habilidad Captar leads (grupo de acciones `leads`), tenga o
no comercio el negocio. Ningún pack importa los modelos (`core.db.models.Lead*`): leen y escriben por este paquete.
"""
