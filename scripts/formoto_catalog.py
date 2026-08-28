"""Catálogo de FORMOTO portado de agent-sale/scripts/seed-catalogo-prueba.ts.

Precios en COP. Port mecánico 1:1 — no editar a mano (regenerar desde la fuente).
"""


FORMOTO_CATEGORY_PATHS: list[list[str]] = [
    # UN nodo por categoryPath distinto usado en los productos, en orden de aparición.
    # Incluye TODOS los nodos del árbol (raíces e hijos). Ej:
    # ["Protección personal"],
    # ["Protección personal", "Cascos"],
    # ...
    ['Protección personal', 'Cascos'],
    ['Protección personal', 'Guantes'],
    ['Protección personal', 'Chaquetas'],
    ['Repuestos y mantenimiento', 'Llantas y neumáticos'],
    ['Repuestos y mantenimiento', 'Aceites y lubricantes'],
    ['Repuestos y mantenimiento', 'Filtros'],
    ['Repuestos y mantenimiento', 'Frenos'],
    ['Repuestos y mantenimiento', 'Cadenas y kits de arrastre'],
    ['Repuestos y mantenimiento', 'Baterías'],
    ['Para motos', 'Otros para motos', 'Iluminación'],
    ['Para motos', 'Otros para motos', 'Iluminación', 'Exploradoras'],
    ['Accesorios y equipamiento', 'Espejos'],
    ['Accesorios y equipamiento', 'Escapes'],
    ['Accesorios y equipamiento', 'Accesorios'],
    ['Accesorios y equipamiento', 'Herramientas'],
    ['Repuestos y mantenimiento', 'Amortiguadores y suspensión'],
    ['Repuestos y mantenimiento', 'Bujías y eléctrico'],
]


FORMOTO_PRODUCTS: list[dict] = [
    {
        "name": 'Casco Integral Thunder Road',
        "description": 'Casco integral con visor antirayas y ventilación regulable, ideal para carretera.',
        "category_path": ['Protección personal', 'Cascos'],
        "image_url": 'https://picsum.photos/seed/CAS-001-S/600/400',
        "variants": [
            {
                "sku": 'CAS-001-S',
                "attributes": {'talla': 'S'},
                "price": 380000,
                "stock": 4,
            },
            {
                "sku": 'CAS-001-M',
                "attributes": {'talla': 'M'},
                "price": 380000,
                "stock": 5,
            },
            {
                "sku": 'CAS-001-L',
                "attributes": {'talla': 'L'},
                "price": 380000,
                "stock": 3,
            },
        ],
    },
    {
        "name": 'Casco Integral Storm GT',
        "description": 'Casco integral de fibra de vidrio, doble densidad de espuma para mayor absorción de impacto.',
        "category_path": ['Protección personal', 'Cascos'],
        "image_url": 'https://picsum.photos/seed/CAS-002/600/400',
        "variants": [
            {
                "sku": 'CAS-002',
                "attributes": {},
                "price": 420000,
                "stock": 8,
            },
        ],
    },
    {
        "name": 'Casco Abierto Urban Rider',
        "description": 'Casco abierto liviano para uso urbano, con visor solar interno.',
        "category_path": ['Protección personal', 'Cascos'],
        "image_url": 'https://picsum.photos/seed/CAS-003/600/400',
        "variants": [
            {
                "sku": 'CAS-003',
                "attributes": {},
                "price": 210000,
                "stock": 15,
            },
        ],
    },
    {
        "name": 'Casco Modular Cross Tech',
        "description": 'Casco modular con mentonera abatible, permite usarlo integral o abierto.',
        "category_path": ['Protección personal', 'Cascos'],
        "image_url": 'https://picsum.photos/seed/CAS-004/600/400',
        "variants": [
            {
                "sku": 'CAS-004',
                "attributes": {},
                "price": 450000,
                "stock": 0,
            },
        ],
    },
    {
        "name": 'Casco Infantil SafeKid',
        "description": 'Casco integral talla infantil, colores llamativos y correa de seguridad reforzada.',
        "category_path": ['Protección personal', 'Cascos'],
        "image_url": 'https://picsum.photos/seed/CAS-005/600/400',
        "variants": [
            {
                "sku": 'CAS-005',
                "attributes": {},
                "price": 150000,
                "stock": 10,
            },
        ],
    },
    {
        "name": 'Casco Integral Racing Pro',
        "description": 'Casco de competición homologado, aerodinámica optimizada para alta velocidad.',
        "category_path": ['Protección personal', 'Cascos'],
        "image_url": 'https://picsum.photos/seed/CAS-006/600/400',
        "variants": [
            {
                "sku": 'CAS-006',
                "attributes": {},
                "price": 480000,
                "stock": 5,
            },
        ],
    },
    {
        "name": 'Casco Abierto Vintage Cafe',
        "description": 'Casco estilo retro para motos café racer, visor incluido.',
        "category_path": ['Protección personal', 'Cascos'],
        "image_url": 'https://picsum.photos/seed/CAS-007/600/400',
        "variants": [
            {
                "sku": 'CAS-007',
                "attributes": {},
                "price": 230000,
                "stock": 9,
            },
        ],
    },
    {
        "name": 'Casco Cross Enduro X',
        "description": 'Casco para motocross con visera larga y ventilación frontal amplia.',
        "category_path": ['Protección personal', 'Cascos'],
        "image_url": 'https://picsum.photos/seed/CAS-008/600/400',
        "variants": [
            {
                "sku": 'CAS-008',
                "attributes": {},
                "price": 260000,
                "stock": 7,
            },
        ],
    },
    {
        "name": 'Guantes de Cuero Touring',
        "description": 'Guantes de cuero genuino con protección en nudillos, para viajes largos.',
        "category_path": ['Protección personal', 'Guantes'],
        "image_url": 'https://picsum.photos/seed/GUA-001-S/600/400',
        "variants": [
            {
                "sku": 'GUA-001-S',
                "attributes": {'talla': 'S'},
                "price": 95000,
                "stock": 6,
            },
            {
                "sku": 'GUA-001-M',
                "attributes": {'talla': 'M'},
                "price": 95000,
                "stock": 9,
            },
            {
                "sku": 'GUA-001-L',
                "attributes": {'talla': 'L'},
                "price": 95000,
                "stock": 5,
            },
        ],
    },
    {
        "name": 'Guantes Racing con Protección',
        "description": 'Guantes con protección rígida en nudillos y palma antideslizante para uso deportivo.',
        "category_path": ['Protección personal', 'Guantes'],
        "image_url": 'https://picsum.photos/seed/GUA-002/600/400',
        "variants": [
            {
                "sku": 'GUA-002',
                "attributes": {},
                "price": 120000,
                "stock": 14,
            },
        ],
    },
    {
        "name": 'Guantes Urbanos Táctiles',
        "description": 'Guantes livianos compatibles con pantalla táctil, uso diario en ciudad.',
        "category_path": ['Protección personal', 'Guantes'],
        "image_url": 'https://picsum.photos/seed/GUA-003/600/400',
        "variants": [
            {
                "sku": 'GUA-003',
                "attributes": {},
                "price": 55000,
                "stock": 25,
            },
        ],
    },
    {
        "name": 'Guantes Impermeables Invierno',
        "description": 'Guantes forrados e impermeables para clima frío y lluvia.',
        "category_path": ['Protección personal', 'Guantes'],
        "image_url": 'https://picsum.photos/seed/GUA-004/600/400',
        "variants": [
            {
                "sku": 'GUA-004',
                "attributes": {},
                "price": 85000,
                "stock": 0,
            },
        ],
    },
    {
        "name": 'Guantes Cross con Nudillera',
        "description": 'Guantes de motocross con nudillera de protección y agarre reforzado.',
        "category_path": ['Protección personal', 'Guantes'],
        "image_url": 'https://picsum.photos/seed/GUA-005/600/400',
        "variants": [
            {
                "sku": 'GUA-005',
                "attributes": {},
                "price": 70000,
                "stock": 11,
            },
        ],
    },
    {
        "name": 'Guantes de Malla Verano',
        "description": 'Guantes transpirables de malla para clima cálido.',
        "category_path": ['Protección personal', 'Guantes'],
        "image_url": 'https://picsum.photos/seed/GUA-006/600/400',
        "variants": [
            {
                "sku": 'GUA-006',
                "attributes": {},
                "price": 45000,
                "stock": 18,
            },
        ],
    },
    {
        "name": 'Guantes Mujer Comfort Fit',
        "description": 'Guantes diseñados para mano femenina, ajuste ergonómico y protección básica.',
        "category_path": ['Protección personal', 'Guantes'],
        "image_url": 'https://picsum.photos/seed/GUA-007/600/400',
        "variants": [
            {
                "sku": 'GUA-007',
                "attributes": {},
                "price": 60000,
                "stock": 13,
            },
        ],
    },
    {
        "name": 'Chaqueta Textil Touring',
        "description": 'Chaqueta textil impermeable con protecciones removibles en hombros y codos.',
        "category_path": ['Protección personal', 'Chaquetas'],
        "image_url": 'https://picsum.photos/seed/CHA-001/600/400',
        "variants": [
            {
                "sku": 'CHA-001',
                "attributes": {},
                "price": 320000,
                "stock": 9,
            },
        ],
    },
    {
        "name": 'Chaqueta de Cuero Classic',
        "description": 'Chaqueta de cuero genuino estilo clásico, forro térmico desmontable.',
        "category_path": ['Protección personal', 'Chaquetas'],
        "image_url": 'https://picsum.photos/seed/CHA-002/600/400',
        "variants": [
            {
                "sku": 'CHA-002',
                "attributes": {},
                "price": 480000,
                "stock": 6,
            },
        ],
    },
    {
        "name": 'Chaqueta Racing con Protecciones',
        "description": 'Chaqueta deportiva con protecciones CE en espalda, hombros y codos.',
        "category_path": ['Protección personal', 'Chaquetas'],
        "image_url": 'https://picsum.photos/seed/CHA-003/600/400',
        "variants": [
            {
                "sku": 'CHA-003',
                "attributes": {},
                "price": 550000,
                "stock": 4,
            },
        ],
    },
    {
        "name": 'Chaqueta Impermeable All Weather',
        "description": 'Chaqueta todo clima con membrana impermeable y forro térmico.',
        "category_path": ['Protección personal', 'Chaquetas'],
        "image_url": 'https://picsum.photos/seed/CHA-004/600/400',
        "variants": [
            {
                "sku": 'CHA-004',
                "attributes": {},
                "price": 280000,
                "stock": 0,
            },
        ],
    },
    {
        "name": 'Chaqueta Mesh Verano',
        "description": 'Chaqueta de malla transpirable con protecciones básicas, ideal para calor.',
        "category_path": ['Protección personal', 'Chaquetas'],
        "image_url": 'https://picsum.photos/seed/CHA-005/600/400',
        "variants": [
            {
                "sku": 'CHA-005',
                "attributes": {},
                "price": 190000,
                "stock": 12,
            },
        ],
    },
    {
        "name": 'Chaleco Reflectivo Seguridad',
        "description": 'Chaleco reflectivo de alta visibilidad para conducción nocturna.',
        "category_path": ['Protección personal', 'Chaquetas'],
        "image_url": 'https://picsum.photos/seed/CHA-006/600/400',
        "variants": [
            {
                "sku": 'CHA-006',
                "attributes": {},
                "price": 35000,
                "stock": 30,
            },
        ],
    },
    {
        "name": 'Chaqueta Térmica Invierno',
        "description": 'Chaqueta térmica acolchada para bajas temperaturas.',
        "category_path": ['Protección personal', 'Chaquetas'],
        "image_url": 'https://picsum.photos/seed/CHA-007/600/400',
        "variants": [
            {
                "sku": 'CHA-007',
                "attributes": {},
                "price": 260000,
                "stock": 8,
            },
        ],
    },
    {
        "name": 'Llanta Delantera Sport 110/70-17',
        "description": 'Llanta deportiva delantera, buen agarre en seco y mojado.',
        "category_path": ['Repuestos y mantenimiento', 'Llantas y neumáticos'],
        "image_url": 'https://picsum.photos/seed/LLA-001/600/400',
        "variants": [
            {
                "sku": 'LLA-001',
                "attributes": {},
                "price": 280000,
                "stock": 10,
            },
        ],
    },
    {
        "name": 'Llanta Trasera Sport 140/70-17',
        "description": 'Llanta deportiva trasera de alto rendimiento.',
        "category_path": ['Repuestos y mantenimiento', 'Llantas y neumáticos'],
        "image_url": 'https://picsum.photos/seed/LLA-002/600/400',
        "variants": [
            {
                "sku": 'LLA-002',
                "attributes": {},
                "price": 320000,
                "stock": 10,
            },
        ],
    },
    {
        "name": 'Llanta Todo Terreno 90/90-19',
        "description": 'Llanta mixta para asfalto y trocha, taco resistente.',
        "category_path": ['Repuestos y mantenimiento', 'Llantas y neumáticos'],
        "image_url": 'https://picsum.photos/seed/LLA-003/600/400',
        "variants": [
            {
                "sku": 'LLA-003',
                "attributes": {},
                "price": 260000,
                "stock": 7,
            },
        ],
    },
    {
        "name": 'Llanta Urbana 80/100-18',
        "description": 'Llanta económica para uso urbano diario.',
        "category_path": ['Repuestos y mantenimiento', 'Llantas y neumáticos'],
        "image_url": 'https://picsum.photos/seed/LLA-004/600/400',
        "variants": [
            {
                "sku": 'LLA-004',
                "attributes": {},
                "price": 190000,
                "stock": 15,
            },
        ],
    },
    {
        "name": 'Llanta Touring 120/70-17',
        "description": 'Llanta de larga duración pensada para viajes de carretera.',
        "category_path": ['Repuestos y mantenimiento', 'Llantas y neumáticos'],
        "image_url": 'https://picsum.photos/seed/LLA-005/600/400',
        "variants": [
            {
                "sku": 'LLA-005',
                "attributes": {},
                "price": 300000,
                "stock": 0,
            },
        ],
    },
    {
        "name": 'Cámara de Aire Reforzada',
        "description": 'Cámara de aire de caucho reforzado, varias medidas disponibles.',
        "category_path": ['Repuestos y mantenimiento', 'Llantas y neumáticos'],
        "image_url": 'https://picsum.photos/seed/LLA-006/600/400',
        "variants": [
            {
                "sku": 'LLA-006',
                "attributes": {},
                "price": 35000,
                "stock": 40,
            },
        ],
    },
    {
        "name": 'Llanta Scooter 120/70-12',
        "description": 'Llanta para scooter con buen agarre urbano.',
        "category_path": ['Repuestos y mantenimiento', 'Llantas y neumáticos'],
        "image_url": 'https://picsum.photos/seed/LLA-007/600/400',
        "variants": [
            {
                "sku": 'LLA-007',
                "attributes": {},
                "price": 175000,
                "stock": 9,
            },
        ],
    },
    {
        "name": 'Aceite Sintético 10W-40 1L',
        "description": 'Aceite 100% sintético para motor 4 tiempos, alta protección térmica.',
        "category_path": ['Repuestos y mantenimiento', 'Aceites y lubricantes'],
        "image_url": 'https://picsum.photos/seed/ACE-001/600/400',
        "variants": [
            {
                "sku": 'ACE-001',
                "attributes": {},
                "price": 58000,
                "stock": 35,
            },
        ],
    },
    {
        "name": 'Aceite Semisintético 20W-50 1L',
        "description": 'Aceite semisintético de uso general para motos de trabajo diario.',
        "category_path": ['Repuestos y mantenimiento', 'Aceites y lubricantes'],
        "image_url": 'https://picsum.photos/seed/ACE-002/600/400',
        "variants": [
            {
                "sku": 'ACE-002',
                "attributes": {},
                "price": 42000,
                "stock": 40,
            },
        ],
    },
    {
        "name": 'Aceite Mineral 20W-50 1L',
        "description": 'Aceite mineral económico para motores de baja exigencia.',
        "category_path": ['Repuestos y mantenimiento', 'Aceites y lubricantes'],
        "image_url": 'https://picsum.photos/seed/ACE-003/600/400',
        "variants": [
            {
                "sku": 'ACE-003',
                "attributes": {},
                "price": 28000,
                "stock": 50,
            },
        ],
    },
    {
        "name": 'Lubricante de Cadena Spray',
        "description": 'Lubricante en spray para cadena, resistente al agua.',
        "category_path": ['Repuestos y mantenimiento', 'Aceites y lubricantes'],
        "image_url": 'https://picsum.photos/seed/ACE-004/600/400',
        "variants": [
            {
                "sku": 'ACE-004',
                "attributes": {},
                "price": 25000,
                "stock": 45,
            },
        ],
    },
    {
        "name": 'Aceite de Horquilla 10W',
        "description": 'Aceite específico para suspensión delantera.',
        "category_path": ['Repuestos y mantenimiento', 'Aceites y lubricantes'],
        "image_url": 'https://picsum.photos/seed/ACE-005/600/400',
        "variants": [
            {
                "sku": 'ACE-005',
                "attributes": {},
                "price": 32000,
                "stock": 0,
            },
        ],
    },
    {
        "name": 'Grasa para Rodamientos',
        "description": 'Grasa multipropósito para rodamientos y bujes.',
        "category_path": ['Repuestos y mantenimiento', 'Aceites y lubricantes'],
        "image_url": 'https://picsum.photos/seed/ACE-006/600/400',
        "variants": [
            {
                "sku": 'ACE-006',
                "attributes": {},
                "price": 18000,
                "stock": 22,
            },
        ],
    },
    {
        "name": 'Aditivo Limpiador de Inyectores',
        "description": 'Aditivo para limpiar inyectores y mejorar el rendimiento del motor.',
        "category_path": ['Repuestos y mantenimiento', 'Aceites y lubricantes'],
        "image_url": 'https://picsum.photos/seed/ACE-007/600/400',
        "variants": [
            {
                "sku": 'ACE-007',
                "attributes": {},
                "price": 30000,
                "stock": 17,
            },
        ],
    },
    {
        "name": 'Filtro de Aceite Universal',
        "description": 'Filtro de aceite compatible con la mayoría de motores 150-250cc.',
        "category_path": ['Repuestos y mantenimiento', 'Filtros'],
        "image_url": 'https://picsum.photos/seed/FIL-001/600/400',
        "variants": [
            {
                "sku": 'FIL-001',
                "attributes": {},
                "price": 22000,
                "stock": 30,
            },
        ],
    },
    {
        "name": 'Filtro de Aire Deportivo',
        "description": 'Filtro de aire de alto flujo lavable y reutilizable.',
        "category_path": ['Repuestos y mantenimiento', 'Filtros'],
        "image_url": 'https://picsum.photos/seed/FIL-002/600/400',
        "variants": [
            {
                "sku": 'FIL-002',
                "attributes": {},
                "price": 38000,
                "stock": 20,
            },
        ],
    },
    {
        "name": 'Filtro de Aire Original',
        "description": 'Filtro de aire de repuesto según especificación de fábrica.',
        "category_path": ['Repuestos y mantenimiento', 'Filtros'],
        "image_url": 'https://picsum.photos/seed/FIL-003/600/400',
        "variants": [
            {
                "sku": 'FIL-003',
                "attributes": {},
                "price": 25000,
                "stock": 25,
            },
        ],
    },
    {
        "name": 'Filtro de Combustible',
        "description": 'Filtro de combustible en línea, evita impurezas en el sistema de inyección.',
        "category_path": ['Repuestos y mantenimiento', 'Filtros'],
        "image_url": 'https://picsum.photos/seed/FIL-004/600/400',
        "variants": [
            {
                "sku": 'FIL-004',
                "attributes": {},
                "price": 15000,
                "stock": 0,
            },
        ],
    },
    {
        "name": 'Filtro de Aire de Alto Flujo',
        "description": 'Filtro deportivo de alto flujo para mayor potencia.',
        "category_path": ['Repuestos y mantenimiento', 'Filtros'],
        "image_url": 'https://picsum.photos/seed/FIL-005/600/400',
        "variants": [
            {
                "sku": 'FIL-005',
                "attributes": {},
                "price": 45000,
                "stock": 12,
            },
        ],
    },
    {
        "name": 'Filtro de Aceite Racing',
        "description": 'Filtro de aceite de alto rendimiento para uso deportivo.',
        "category_path": ['Repuestos y mantenimiento', 'Filtros'],
        "image_url": 'https://picsum.photos/seed/FIL-006/600/400',
        "variants": [
            {
                "sku": 'FIL-006',
                "attributes": {},
                "price": 28000,
                "stock": 14,
            },
        ],
    },
    {
        "name": 'Pastillas de Freno Delanteras Cerámicas',
        "description": 'Pastillas cerámicas de alto rendimiento y baja generación de polvo.',
        "category_path": ['Repuestos y mantenimiento', 'Frenos'],
        "image_url": 'https://picsum.photos/seed/FRE-001/600/400',
        "variants": [
            {
                "sku": 'FRE-001',
                "attributes": {},
                "price": 65000,
                "stock": 18,
            },
        ],
    },
    {
        "name": 'Pastillas de Freno Traseras Orgánicas',
        "description": 'Pastillas orgánicas de frenado suave para uso diario.',
        "category_path": ['Repuestos y mantenimiento', 'Frenos'],
        "image_url": 'https://picsum.photos/seed/FRE-002/600/400',
        "variants": [
            {
                "sku": 'FRE-002',
                "attributes": {},
                "price": 45000,
                "stock": 20,
            },
        ],
    },
    {
        "name": 'Disco de Freno Delantero Flotante',
        "description": 'Disco flotante de alto rendimiento, mejor disipación de calor.',
        "category_path": ['Repuestos y mantenimiento', 'Frenos'],
        "image_url": 'https://picsum.photos/seed/FRE-003/600/400',
        "variants": [
            {
                "sku": 'FRE-003',
                "attributes": {},
                "price": 180000,
                "stock": 6,
            },
        ],
    },
    {
        "name": 'Disco de Freno Trasero',
        "description": 'Disco de freno trasero de repuesto original.',
        "category_path": ['Repuestos y mantenimiento', 'Frenos'],
        "image_url": 'https://picsum.photos/seed/FRE-004/600/400',
        "variants": [
            {
                "sku": 'FRE-004',
                "attributes": {},
                "price": 150000,
                "stock": 0,
            },
        ],
    },
    {
        "name": 'Líquido de Frenos DOT 4',
        "description": 'Líquido de frenos DOT 4 para sistemas hidráulicos.',
        "category_path": ['Repuestos y mantenimiento', 'Frenos'],
        "image_url": 'https://picsum.photos/seed/FRE-005/600/400',
        "variants": [
            {
                "sku": 'FRE-005',
                "attributes": {},
                "price": 22000,
                "stock": 35,
            },
        ],
    },
    {
        "name": 'Kit de Mangueras de Freno Trenzadas',
        "description": 'Mangueras trenzadas de acero para mejor tacto de frenado.',
        "category_path": ['Repuestos y mantenimiento', 'Frenos'],
        "image_url": 'https://picsum.photos/seed/FRE-006/600/400',
        "variants": [
            {
                "sku": 'FRE-006',
                "attributes": {},
                "price": 130000,
                "stock": 8,
            },
        ],
    },
    {
        "name": 'Bomba de Freno Trasera',
        "description": 'Bomba de freno trasera de repuesto.',
        "category_path": ['Repuestos y mantenimiento', 'Frenos'],
        "image_url": 'https://picsum.photos/seed/FRE-007/600/400',
        "variants": [
            {
                "sku": 'FRE-007',
                "attributes": {},
                "price": 95000,
                "stock": 5,
            },
        ],
    },
    {
        "name": 'Kit de Arrastre Completo 428H',
        "description": 'Kit completo (cadena, piñón, corona) para motos de 150-200cc.',
        "category_path": ['Repuestos y mantenimiento', 'Cadenas y kits de arrastre'],
        "image_url": 'https://picsum.photos/seed/CAD-001/600/400',
        "variants": [
            {
                "sku": 'CAD-001',
                "attributes": {},
                "price": 220000,
                "stock": 10,
            },
        ],
    },
    {
        "name": 'Cadena de Transmisión 520',
        "description": 'Cadena reforzada 520 para motos deportivas.',
        "category_path": ['Repuestos y mantenimiento', 'Cadenas y kits de arrastre'],
        "image_url": 'https://picsum.photos/seed/CAD-002/600/400',
        "variants": [
            {
                "sku": 'CAD-002',
                "attributes": {},
                "price": 130000,
                "stock": 12,
            },
        ],
    },
    {
        "name": 'Piñón Delantero',
        "description": 'Piñón delantero de acero de alta resistencia.',
        "category_path": ['Repuestos y mantenimiento', 'Cadenas y kits de arrastre'],
        "image_url": 'https://picsum.photos/seed/CAD-003/600/400',
        "variants": [
            {
                "sku": 'CAD-003',
                "attributes": {},
                "price": 40000,
                "stock": 0,
            },
        ],
    },
    {
        "name": 'Corona Trasera',
        "description": 'Corona trasera de aluminio liviano.',
        "category_path": ['Repuestos y mantenimiento', 'Cadenas y kits de arrastre'],
        "image_url": 'https://picsum.photos/seed/CAD-004/600/400',
        "variants": [
            {
                "sku": 'CAD-004',
                "attributes": {},
                "price": 90000,
                "stock": 9,
            },
        ],
    },
    {
        "name": 'Kit de Arrastre Reforzado Racing',
        "description": 'Kit de arrastre reforzado para uso deportivo o de competición.',
        "category_path": ['Repuestos y mantenimiento', 'Cadenas y kits de arrastre'],
        "image_url": 'https://picsum.photos/seed/CAD-005/600/400',
        "variants": [
            {
                "sku": 'CAD-005',
                "attributes": {},
                "price": 320000,
                "stock": 4,
            },
        ],
    },
    {
        "name": 'Tensor de Cadena Ajustable',
        "description": 'Tensor de cadena ajustable para mantener la tensión correcta.',
        "category_path": ['Repuestos y mantenimiento', 'Cadenas y kits de arrastre'],
        "image_url": 'https://picsum.photos/seed/CAD-006/600/400',
        "variants": [
            {
                "sku": 'CAD-006',
                "attributes": {},
                "price": 55000,
                "stock": 15,
            },
        ],
    },
    {
        "name": 'Batería Gel 12V 9Ah',
        "description": 'Batería de gel libre de mantenimiento, mayor vida útil.',
        "category_path": ['Repuestos y mantenimiento', 'Baterías'],
        "image_url": 'https://picsum.photos/seed/BAT-001/600/400',
        "variants": [
            {
                "sku": 'BAT-001',
                "attributes": {},
                "price": 220000,
                "stock": 14,
            },
        ],
    },
    {
        "name": 'Batería de Litio Ultraligera',
        "description": 'Batería de litio de bajo peso y arranque instantáneo.',
        "category_path": ['Repuestos y mantenimiento', 'Baterías'],
        "image_url": 'https://picsum.photos/seed/BAT-002/600/400',
        "variants": [
            {
                "sku": 'BAT-002',
                "attributes": {},
                "price": 380000,
                "stock": 5,
            },
        ],
    },
    {
        "name": 'Batería Convencional 12V 5Ah',
        "description": 'Batería de plomo-ácido convencional, requiere mantenimiento.',
        "category_path": ['Repuestos y mantenimiento', 'Baterías'],
        "image_url": 'https://picsum.photos/seed/BAT-003/600/400',
        "variants": [
            {
                "sku": 'BAT-003',
                "attributes": {},
                "price": 120000,
                "stock": 0,
            },
        ],
    },
    {
        "name": 'Cargador de Batería Inteligente',
        "description": 'Cargador inteligente con corte automático de carga.',
        "category_path": ['Repuestos y mantenimiento', 'Baterías'],
        "image_url": 'https://picsum.photos/seed/BAT-004/600/400',
        "variants": [
            {
                "sku": 'BAT-004',
                "attributes": {},
                "price": 85000,
                "stock": 18,
            },
        ],
    },
    {
        "name": 'Batería AGM 12V 12Ah',
        "description": 'Batería AGM sellada de alta durabilidad.',
        "category_path": ['Repuestos y mantenimiento', 'Baterías'],
        "image_url": 'https://picsum.photos/seed/BAT-005/600/400',
        "variants": [
            {
                "sku": 'BAT-005',
                "attributes": {},
                "price": 260000,
                "stock": 7,
            },
        ],
    },
    {
        "name": 'Faro Delantero LED',
        "description": 'Faro delantero LED de alta luminosidad y bajo consumo.',
        "category_path": ['Para motos', 'Otros para motos', 'Iluminación'],
        "image_url": 'https://picsum.photos/seed/LUZ-001/600/400',
        "variants": [
            {
                "sku": 'LUZ-001',
                "attributes": {},
                "price": 150000,
                "stock": 10,
            },
        ],
    },
    {
        "name": 'Kit de Luces LED H4',
        "description": 'Kit de bombillas LED H4 plug and play.',
        "category_path": ['Para motos', 'Otros para motos', 'Iluminación'],
        "image_url": 'https://picsum.photos/seed/LUZ-002/600/400',
        "variants": [
            {
                "sku": 'LUZ-002',
                "attributes": {},
                "price": 90000,
                "stock": 20,
            },
        ],
    },
    {
        "name": 'Direccionales LED Ahumadas',
        "description": 'Par de direccionales LED con lente ahumado.',
        "category_path": ['Para motos', 'Otros para motos', 'Iluminación'],
        "image_url": 'https://picsum.photos/seed/LUZ-003/600/400',
        "variants": [
            {
                "sku": 'LUZ-003',
                "attributes": {},
                "price": 45000,
                "stock": 25,
            },
        ],
    },
    {
        "name": 'Luz Trasera LED con Freno',
        "description": 'Luz trasera LED integrada con función de freno.',
        "category_path": ['Para motos', 'Otros para motos', 'Iluminación'],
        "image_url": 'https://picsum.photos/seed/LUZ-004/600/400',
        "variants": [
            {
                "sku": 'LUZ-004',
                "attributes": {},
                "price": 55000,
                "stock": 0,
            },
        ],
    },
    {
        "name": 'Luces Auxiliares Neblineras',
        "description": 'Par de luces auxiliares para neblina y baja visibilidad.',
        "category_path": ['Para motos', 'Otros para motos', 'Iluminación', 'Exploradoras'],
        "image_url": 'https://picsum.photos/seed/LUZ-005/600/400',
        "variants": [
            {
                "sku": 'LUZ-005',
                "attributes": {},
                "price": 110000,
                "stock": 8,
            },
        ],
    },
    {
        "name": 'Luz LED para Placa',
        "description": 'Luz LED pequeña para iluminación de placa.',
        "category_path": ['Para motos', 'Otros para motos', 'Iluminación'],
        "image_url": 'https://picsum.photos/seed/LUZ-006/600/400',
        "variants": [
            {
                "sku": 'LUZ-006',
                "attributes": {},
                "price": 20000,
                "stock": 30,
            },
        ],
    },
    {
        "name": 'Espejos Retrovisores Deportivos',
        "description": 'Par de espejos deportivos de perfil aerodinámico.',
        "category_path": ['Accesorios y equipamiento', 'Espejos'],
        "image_url": 'https://picsum.photos/seed/ESP-001/600/400',
        "variants": [
            {
                "sku": 'ESP-001',
                "attributes": {},
                "price": 65000,
                "stock": 16,
            },
        ],
    },
    {
        "name": 'Espejos Retrovisores Clásicos',
        "description": 'Par de espejos redondos estilo clásico.',
        "category_path": ['Accesorios y equipamiento', 'Espejos'],
        "image_url": 'https://picsum.photos/seed/ESP-002/600/400',
        "variants": [
            {
                "sku": 'ESP-002',
                "attributes": {},
                "price": 55000,
                "stock": 14,
            },
        ],
    },
    {
        "name": 'Espejos Convexos Antivibración',
        "description": 'Espejos convexos que reducen el punto ciego y la vibración.',
        "category_path": ['Accesorios y equipamiento', 'Espejos'],
        "image_url": 'https://picsum.photos/seed/ESP-003/600/400',
        "variants": [
            {
                "sku": 'ESP-003',
                "attributes": {},
                "price": 70000,
                "stock": 0,
            },
        ],
    },
    {
        "name": 'Espejo Retrovisor Universal Cromado',
        "description": 'Espejo universal cromado, encaja en la mayoría de modelos.',
        "category_path": ['Accesorios y equipamiento', 'Espejos'],
        "image_url": 'https://picsum.photos/seed/ESP-004/600/400',
        "variants": [
            {
                "sku": 'ESP-004',
                "attributes": {},
                "price": 35000,
                "stock": 22,
            },
        ],
    },
    {
        "name": 'Espejos Plegables Racing',
        "description": 'Espejos plegables de bajo perfil para uso deportivo.',
        "category_path": ['Accesorios y equipamiento', 'Espejos'],
        "image_url": 'https://picsum.photos/seed/ESP-005/600/400',
        "variants": [
            {
                "sku": 'ESP-005',
                "attributes": {},
                "price": 90000,
                "stock": 9,
            },
        ],
    },
    {
        "name": 'Escape Deportivo Slip-On',
        "description": 'Escape deportivo de instalación simple, mejora sonido y peso.',
        "category_path": ['Accesorios y equipamiento', 'Escapes'],
        "image_url": 'https://picsum.photos/seed/ESC-001/600/400',
        "variants": [
            {
                "sku": 'ESC-001',
                "attributes": {},
                "price": 480000,
                "stock": 6,
            },
        ],
    },
    {
        "name": 'Escape Completo Racing',
        "description": 'Sistema de escape completo de alto rendimiento para competición.',
        "category_path": ['Accesorios y equipamiento', 'Escapes'],
        "image_url": 'https://picsum.photos/seed/ESC-002/600/400',
        "variants": [
            {
                "sku": 'ESC-002',
                "attributes": {},
                "price": 950000,
                "stock": 2,
            },
        ],
    },
    {
        "name": 'Silenciador Universal',
        "description": 'Silenciador universal adaptable a varios modelos.',
        "category_path": ['Accesorios y equipamiento', 'Escapes'],
        "image_url": 'https://picsum.photos/seed/ESC-003/600/400',
        "variants": [
            {
                "sku": 'ESC-003',
                "attributes": {},
                "price": 280000,
                "stock": 8,
            },
        ],
    },
    {
        "name": 'Protector de Escape Carbono',
        "description": 'Protector térmico de fibra de carbono para el escape.',
        "category_path": ['Accesorios y equipamiento', 'Escapes'],
        "image_url": 'https://picsum.photos/seed/ESC-004/600/400',
        "variants": [
            {
                "sku": 'ESC-004',
                "attributes": {},
                "price": 95000,
                "stock": 0,
            },
        ],
    },
    {
        "name": 'Escape Megáfono Clásico',
        "description": 'Escape estilo megáfono para motos clásicas y café racer.',
        "category_path": ['Accesorios y equipamiento', 'Escapes'],
        "image_url": 'https://picsum.photos/seed/ESC-005/600/400',
        "variants": [
            {
                "sku": 'ESC-005',
                "attributes": {},
                "price": 320000,
                "stock": 5,
            },
        ],
    },
    {
        "name": 'Empaque de Escape Universal',
        "description": 'Empaque de repuesto para unión del escape al motor.',
        "category_path": ['Accesorios y equipamiento', 'Escapes'],
        "image_url": 'https://picsum.photos/seed/ESC-006/600/400',
        "variants": [
            {
                "sku": 'ESC-006',
                "attributes": {},
                "price": 12000,
                "stock": 40,
            },
        ],
    },
    {
        "name": 'Maleta Lateral Rígida',
        "description": 'Par de maletas laterales rígidas resistentes al agua.',
        "category_path": ['Accesorios y equipamiento', 'Accesorios'],
        "image_url": 'https://picsum.photos/seed/ACC-001/600/400',
        "variants": [
            {
                "sku": 'ACC-001',
                "attributes": {},
                "price": 420000,
                "stock": 5,
            },
        ],
    },
    {
        "name": 'Baúl Trasero 40L',
        "description": 'Baúl trasero de 40 litros con respaldo acolchado.',
        "category_path": ['Accesorios y equipamiento', 'Accesorios'],
        "image_url": 'https://picsum.photos/seed/ACC-002/600/400',
        "variants": [
            {
                "sku": 'ACC-002',
                "attributes": {},
                "price": 250000,
                "stock": 9,
            },
        ],
    },
    {
        "name": 'Cubre Asiento Impermeable',
        "description": 'Cubre asiento impermeable universal.',
        "category_path": ['Accesorios y equipamiento', 'Accesorios'],
        "image_url": 'https://picsum.photos/seed/ACC-003/600/400',
        "variants": [
            {
                "sku": 'ACC-003',
                "attributes": {},
                "price": 40000,
                "stock": 25,
            },
        ],
    },
    {
        "name": 'Soporte para Celular',
        "description": 'Soporte ajustable para celular con agarre antivibración.',
        "category_path": ['Accesorios y equipamiento', 'Accesorios'],
        "image_url": 'https://picsum.photos/seed/ACC-004/600/400',
        "variants": [
            {
                "sku": 'ACC-004',
                "attributes": {},
                "price": 35000,
                "stock": 0,
            },
        ],
    },
    {
        "name": 'Alarma Antirrobo con Sensor',
        "description": 'Alarma con sensor de movimiento y control remoto.',
        "category_path": ['Accesorios y equipamiento', 'Accesorios'],
        "image_url": 'https://picsum.photos/seed/ACC-005/600/400',
        "variants": [
            {
                "sku": 'ACC-005',
                "attributes": {},
                "price": 130000,
                "stock": 12,
            },
        ],
    },
    {
        "name": 'Cubierta para Moto Impermeable',
        "description": 'Cubierta impermeable con protección UV.',
        "category_path": ['Accesorios y equipamiento', 'Accesorios'],
        "image_url": 'https://picsum.photos/seed/ACC-006/600/400',
        "variants": [
            {
                "sku": 'ACC-006',
                "attributes": {},
                "price": 60000,
                "stock": 20,
            },
        ],
    },
    {
        "name": 'Portaequipaje Trasero',
        "description": 'Portaequipaje trasero de acero para carga adicional.',
        "category_path": ['Accesorios y equipamiento', 'Accesorios'],
        "image_url": 'https://picsum.photos/seed/ACC-007/600/400',
        "variants": [
            {
                "sku": 'ACC-007',
                "attributes": {},
                "price": 75000,
                "stock": 10,
            },
        ],
    },
    {
        "name": 'Candado de Disco con Alarma',
        "description": 'Candado de disco con alarma sonora antirrobo.',
        "category_path": ['Accesorios y equipamiento', 'Accesorios'],
        "image_url": 'https://picsum.photos/seed/ACC-008/600/400',
        "variants": [
            {
                "sku": 'ACC-008',
                "attributes": {},
                "price": 90000,
                "stock": 14,
            },
        ],
    },
    {
        "name": 'Kit de Herramientas Básico',
        "description": 'Kit básico de herramientas para mantenimiento en casa.',
        "category_path": ['Accesorios y equipamiento', 'Herramientas'],
        "image_url": 'https://picsum.photos/seed/HER-001/600/400',
        "variants": [
            {
                "sku": 'HER-001',
                "attributes": {},
                "price": 85000,
                "stock": 16,
            },
        ],
    },
    {
        "name": 'Llave de Cruz para Moto',
        "description": 'Llave de cruz multipropósito para tuercas y pernos.',
        "category_path": ['Accesorios y equipamiento', 'Herramientas'],
        "image_url": 'https://picsum.photos/seed/HER-002/600/400',
        "variants": [
            {
                "sku": 'HER-002',
                "attributes": {},
                "price": 30000,
                "stock": 20,
            },
        ],
    },
    {
        "name": 'Compresor de Aire Portátil',
        "description": 'Compresor portátil 12V para inflar llantas.',
        "category_path": ['Accesorios y equipamiento', 'Herramientas'],
        "image_url": 'https://picsum.photos/seed/HER-003/600/400',
        "variants": [
            {
                "sku": 'HER-003',
                "attributes": {},
                "price": 180000,
                "stock": 0,
            },
        ],
    },
    {
        "name": 'Multímetro Digital',
        "description": 'Multímetro digital para diagnóstico eléctrico básico.',
        "category_path": ['Accesorios y equipamiento', 'Herramientas'],
        "image_url": 'https://picsum.photos/seed/HER-004/600/400',
        "variants": [
            {
                "sku": 'HER-004',
                "attributes": {},
                "price": 65000,
                "stock": 11,
            },
        ],
    },
    {
        "name": 'Elevador Hidráulico para Moto',
        "description": 'Elevador hidráulico para mantenimiento y cambio de llantas.',
        "category_path": ['Accesorios y equipamiento', 'Herramientas'],
        "image_url": 'https://picsum.photos/seed/HER-005/600/400',
        "variants": [
            {
                "sku": 'HER-005',
                "attributes": {},
                "price": 450000,
                "stock": 3,
            },
        ],
    },
    {
        "name": 'Amortiguador Trasero Ajustable',
        "description": 'Amortiguador trasero con ajuste de precarga.',
        "category_path": ['Repuestos y mantenimiento', 'Amortiguadores y suspensión'],
        "image_url": 'https://picsum.photos/seed/AMO-001/600/400',
        "variants": [
            {
                "sku": 'AMO-001',
                "attributes": {},
                "price": 320000,
                "stock": 7,
            },
        ],
    },
    {
        "name": 'Kit de Amortiguadores Delanteros',
        "description": 'Par de amortiguadores delanteros de repuesto.',
        "category_path": ['Repuestos y mantenimiento', 'Amortiguadores y suspensión'],
        "image_url": 'https://picsum.photos/seed/AMO-002/600/400',
        "variants": [
            {
                "sku": 'AMO-002',
                "attributes": {},
                "price": 280000,
                "stock": 6,
            },
        ],
    },
    {
        "name": 'Resortes de Suspensión Progresivos',
        "description": 'Resortes progresivos para mejorar la respuesta de la suspensión.',
        "category_path": ['Repuestos y mantenimiento', 'Amortiguadores y suspensión'],
        "image_url": 'https://picsum.photos/seed/AMO-003/600/400',
        "variants": [
            {
                "sku": 'AMO-003',
                "attributes": {},
                "price": 150000,
                "stock": 0,
            },
        ],
    },
    {
        "name": 'Amortiguador de Dirección',
        "description": 'Amortiguador de dirección para mayor estabilidad en carretera.',
        "category_path": ['Repuestos y mantenimiento', 'Amortiguadores y suspensión'],
        "image_url": 'https://picsum.photos/seed/AMO-004/600/400',
        "variants": [
            {
                "sku": 'AMO-004',
                "attributes": {},
                "price": 260000,
                "stock": 4,
            },
        ],
    },
    {
        "name": 'Bujes de Suspensión Poliuretano',
        "description": 'Bujes de poliuretano de alta durabilidad.',
        "category_path": ['Repuestos y mantenimiento', 'Amortiguadores y suspensión'],
        "image_url": 'https://picsum.photos/seed/AMO-005/600/400',
        "variants": [
            {
                "sku": 'AMO-005',
                "attributes": {},
                "price": 60000,
                "stock": 15,
            },
        ],
    },
    {
        "name": 'Kit de Retenes de Horquilla',
        "description": 'Kit de retenes para sellar la horquilla delantera.',
        "category_path": ['Repuestos y mantenimiento', 'Amortiguadores y suspensión'],
        "image_url": 'https://picsum.photos/seed/AMO-006/600/400',
        "variants": [
            {
                "sku": 'AMO-006',
                "attributes": {},
                "price": 45000,
                "stock": 18,
            },
        ],
    },
    {
        "name": 'Bujía de Iridio',
        "description": 'Bujía de iridio de larga duración y mejor encendido.',
        "category_path": ['Repuestos y mantenimiento', 'Bujías y eléctrico'],
        "image_url": 'https://picsum.photos/seed/BUJ-001/600/400',
        "variants": [
            {
                "sku": 'BUJ-001',
                "attributes": {},
                "price": 45000,
                "stock": 24,
            },
        ],
    },
    {
        "name": 'Bujía Estándar',
        "description": 'Bujía estándar de níquel para uso general.',
        "category_path": ['Repuestos y mantenimiento', 'Bujías y eléctrico'],
        "image_url": 'https://picsum.photos/seed/BUJ-002/600/400',
        "variants": [
            {
                "sku": 'BUJ-002',
                "attributes": {},
                "price": 12000,
                "stock": 40,
            },
        ],
    },
    {
        "name": 'Bobina de Encendido',
        "description": 'Bobina de encendido de repuesto para sistema eléctrico.',
        "category_path": ['Repuestos y mantenimiento', 'Bujías y eléctrico'],
        "image_url": 'https://picsum.photos/seed/BUJ-003/600/400',
        "variants": [
            {
                "sku": 'BUJ-003',
                "attributes": {},
                "price": 65000,
                "stock": 0,
            },
        ],
    },
    {
        "name": 'Regulador de Voltaje',
        "description": 'Regulador/rectificador de voltaje para el sistema de carga.',
        "category_path": ['Repuestos y mantenimiento', 'Bujías y eléctrico'],
        "image_url": 'https://picsum.photos/seed/BUJ-004/600/400',
        "variants": [
            {
                "sku": 'BUJ-004',
                "attributes": {},
                "price": 55000,
                "stock": 10,
            },
        ],
    },
]


FORMOTO_COMPLEMENT_PAIRS: list[tuple[list[str], list[str]]] = [
    (['Protección personal', 'Cascos'], ['Protección personal', 'Guantes']),
    # ... exactamente los pares del source (ambas direcciones NO hace falta: el
    # consumidor las inserta en ambas direcciones)
    (['Protección personal', 'Cascos'], ['Protección personal', 'Chaquetas']),
]
