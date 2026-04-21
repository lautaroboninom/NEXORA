from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCREENSHOT_DIR = ROOT / "screenshots"
LOGO_PATH = ROOT.parents[1] / "web" / "public" / "branding" / "logo-app.png"

SCREENSHOTS = {
    "home_qr": {
        "file": SCREENSHOT_DIR / "home_qr.png",
        "route": "/",
        "wait_text": "Busqueda por N/S o MG",
        "delay": 1.0,
        "crop": (0.18, 0.08, 0.98, 0.88),
    },
    "nuevo_ingreso": {
        "file": SCREENSHOT_DIR / "nuevo_ingreso.png",
        "route": "/ingresos/nuevo",
        "wait_text": "Nuevo Ingreso",
        "delay": 1.1,
        "crop": (0.12, 0.05, 0.98, 0.92),
    },
    "pendientes_general": {
        "file": SCREENSHOT_DIR / "pendientes_general.png",
        "route": "/pendientes",
        "wait_text": "Pendientes general",
        "delay": 1.0,
        "crop": (0.15, 0.04, 0.98, 0.78),
    },
    "pendientes_por_tecnico": {
        "file": SCREENSHOT_DIR / "pendientes_por_tecnico.png",
        "route": "/pendientes-por-tecnico",
        "wait_text": "Pendientes por tecnico",
        "delay": 1.0,
        "crop": (0.15, 0.04, 0.98, 0.82),
    },
    "service_principal": {
        "file": SCREENSHOT_DIR / "service_principal.png",
        "route": "/ingresos/7",
        "wait_text": "Hoja de servicio",
        "delay": 1.0,
        "crop": (0.02, 0.06, 0.98, 0.88),
    },
    "service_diagnostico": {
        "file": SCREENSHOT_DIR / "service_diagnostico.png",
        "route": "/ingresos/6?tab=diagnostico",
        "wait_text": "Descripcion del problema",
        "delay": 1.1,
        "crop": (0.02, 0.06, 0.98, 0.9),
    },
    "service_test": {
        "file": SCREENSHOT_DIR / "service_test.png",
        "route": "/ingresos/6?tab=test",
        "wait_text": "Test tecnico",
        "delay": 1.1,
        "crop": (0.02, 0.06, 0.98, 0.9),
    },
    "service_presupuesto": {
        "file": SCREENSHOT_DIR / "service_presupuesto.png",
        "route": "/ingresos/6?tab=presupuesto",
        "wait_text": "Emitir presupuesto",
        "delay": 1.1,
        "crop": (0.02, 0.06, 0.98, 0.9),
    },
    "service_archivos": {
        "file": SCREENSHOT_DIR / "service_archivos.png",
        "route": "/ingresos/6?tab=archivos",
        "wait_text": "Archivos del ingreso",
        "delay": 1.1,
        "crop": (0.02, 0.06, 0.98, 0.86),
    },
    "equipos": {
        "file": SCREENSHOT_DIR / "equipos.png",
        "route": "/equipos",
        "wait_text": "Gestion de equipos",
        "delay": 1.0,
        "crop": (0.02, 0.04, 0.98, 0.9),
    },
    "preventivos": {
        "file": SCREENSHOT_DIR / "preventivos.png",
        "route": "/equipos?tab=preventivos",
        "wait_text": "Mantenimientos preventivos",
        "delay": 1.3,
        "crop": (0.02, 0.04, 0.98, 0.84),
    },
    "instituciones": {
        "file": SCREENSHOT_DIR / "instituciones.png",
        "route": "/equipos?tab=instituciones&institucion_id=2",
        "wait_text": "Instituciones",
        "delay": 1.3,
        "crop": (0.02, 0.04, 0.98, 0.88),
    },
    "catalogo_marcas": {
        "file": SCREENSHOT_DIR / "catalogo_marcas.png",
        "route": "/catalogo/marcas",
        "wait_text": "Marcas",
        "delay": 1.0,
        "crop": (0.15, 0.04, 0.98, 0.9),
    },
    "repuestos": {
        "file": SCREENSHOT_DIR / "repuestos.png",
        "route": "/catalogo/repuestos",
        "wait_text": "Repuestos",
        "delay": 1.4,
        "crop": (0.02, 0.03, 0.98, 0.9),
    },
    "general_cliente": {
        "file": SCREENSHOT_DIR / "general_cliente.png",
        "route": "/clientes?cliente_id=2",
        "wait_text": "General por cliente",
        "delay": 1.4,
        "crop": (0.15, 0.04, 0.98, 0.88),
    },
    "buscar_ns": {
        "file": SCREENSHOT_DIR / "buscar_ns.png",
        "route": "/buscar-ns?serie=NSPRUEBA123",
        "wait_text": "Resultados para N/S o MG",
        "delay": 1.0,
        "crop": (0.04, 0.04, 0.98, 0.86),
    },
    "metricas_tecnicos": {
        "file": SCREENSHOT_DIR / "metricas_tecnicos.png",
        "route": "/metricas",
        "wait_text": "MTTR PROMEDIO",
        "delay": 1.4,
        "crop": (0.02, 0.04, 0.98, 0.92),
    },
    "metricas_finanzas": {
        "file": SCREENSHOT_DIR / "metricas_finanzas.png",
        "route": "/metricas/finanzas",
        "wait_text": "Ingresos (ARS, sin IVA)",
        "delay": 1.4,
        "crop": (0.02, 0.04, 0.98, 0.9),
    },
    "depositos_bajas": {
        "file": SCREENSHOT_DIR / "depositos_bajas.png",
        "route": "/depositos?ubicacion_id=bajas",
        "wait_text": "Depositos/Bajas",
        "delay": 1.0,
        "crop": (0.15, 0.04, 0.98, 0.88),
    },
    "usuarios": {
        "file": SCREENSHOT_DIR / "usuarios.png",
        "route": "/usuarios",
        "wait_text": "Usuarios",
        "delay": 1.1,
        "crop": (0.15, 0.04, 0.98, 0.9),
    },
    "garantias": {
        "file": SCREENSHOT_DIR / "garantias.png",
        "route": "/garantias",
        "wait_text": "Garantias",
        "delay": 1.1,
        "crop": (0.15, 0.04, 0.98, 0.9),
    },
}

PAGES = [
    {
        "id": "cover",
        "layout": "cover",
        "title": "NEXORA",
        "subtitle": "Software de gestión para servicio técnico, mantenimiento y trazabilidad operativa.",
        "summary": [
            "Centraliza recepción, diagnóstico, presupuesto, reparación, entrega y análisis en un mismo circuito.",
            "Sostiene trazabilidad por OS, número de serie, número interno, cliente, equipo y evidencia técnica.",
            "Puede desplegarse en infraestructura interna y rediseñarse según cada operación, rol y necesidad.",
        ],
        "highlights": [
            "Circuito completo",
            "Control técnico documental",
            "Historial por equipo",
            "Adaptable a cada operación",
        ],
        "images": [
            {
                "key": "service_principal",
                "caption": "Hoja de servicio: la ficha central donde se ordenan el trabajo técnico, el estado y la entrega.",
            },
            {
                "key": "metricas_tecnicos",
                "caption": "Métricas técnicas: visibilidad sobre tiempos, SLA, carga y productividad.",
            },
        ],
    },
    {
        "id": "problema",
        "layout": "problem",
        "title": "Qué problema resuelve",
        "subtitle": "NEXORA ordena la operación cuando taller, recepción y jefatura necesitan trabajar sobre la misma verdad.",
        "sections": [
            {
                "heading": "Sin NEXORA",
                "points": [
                    "La información queda repartida entre notas, planillas, mails y mensajes informales.",
                    "Se pierde tiempo buscando antecedentes, repuestos usados, responsables y estados reales.",
                    "La dirección ve resultados tarde y con poco contexto técnico.",
                ],
            },
            {
                "heading": "Con NEXORA",
                "points": [
                    "Cada equipo sigue un circuito claro, con responsables, estados y documentos asociados.",
                    "Cada decisión queda respaldada por datos operativos, técnicos y financieros.",
                    "Cada área trabaja sobre el mismo flujo sin duplicar carga ni reinventar planillas.",
                ],
            },
        ],
        "image": {
            "key": "home_qr",
            "caption": "Ingreso rápido al sistema con búsqueda directa por OS, serie, MG, referencia o lectura de QR.",
        },
    },
    {
        "id": "flujo",
        "layout": "flow",
        "title": "Cómo funciona el circuito completo",
        "subtitle": "El software acompaña el recorrido real del equipo desde que entra hasta que queda entregado y analizado.",
        "flow_steps": [
            ("Recepción", "Alta del ingreso, validación del equipo, motivo, accesorios, garantías y ubicación inicial."),
            ("Diagnóstico", "Registro de falla, trabajos, evidencias, accesorios y comentarios técnicos."),
            ("Presupuesto", "Costos, mano de obra, repuestos, condiciones comerciales y aprobación."),
            ("Reparación y test", "Resolución técnica, control final, protocolos por tipo de equipo e informe."),
            ("Liberación y entrega", "Orden de salida, remito, factura, entrega y cierre del circuito."),
            ("Historial y métricas", "Consulta histórica, tiempos, productividad, finanzas y seguimiento."),
        ],
        "callouts": [
            "Un mismo flujo une recepción, técnicos, depósito, jefatura y administración.",
            "Cada etapa deja trazabilidad concreta y reutilizable para auditoría, servicio y dirección.",
        ],
        "result_title": "Resultado",
        "result_text": "El equipo deja de moverse por memoria informal y pasa a un circuito visible, medible y auditable.",
    },
    {
        "id": "ingreso",
        "layout": "text_image",
        "title": "Ingreso y trazabilidad inicial",
        "subtitle": "Desde recepción, NEXORA captura la información clave para que cada equipo entre ordenado al taller.",
        "sections": [
            {
                "heading": "Qué registra",
                "points": [
                    "Cliente o particular, propietario, contacto, tipo de equipo, marca, modelo y variante.",
                    "Número de serie, número interno, remito de ingreso, motivo, garantía de fábrica y garantía de reparación.",
                    "Accesorios recibidos, informe preliminar, comentarios y técnico asignado.",
                ],
            },
            {
                "heading": "Qué aporta",
                "points": [
                    "Evita ingresos incompletos y reduce retrabajo posterior.",
                    "Deja identificado el equipo desde el primer minuto.",
                    "Ordena automáticamente la hoja de servicio y el seguimiento posterior.",
                ],
            },
        ],
        "image": {
            "key": "nuevo_ingreso",
            "caption": "Nuevo ingreso: recepción guiada con datos técnicos, comerciales y de trazabilidad en una sola pantalla.",
        },
    },
    {
        "id": "colas",
        "layout": "two_images",
        "title": "Gestión de colas operativas",
        "subtitle": "NEXORA convierte la carga diaria en tableros de trabajo claros para cada rol.",
        "sections": [
            {
                "heading": "Tableros operativos",
                "points": [
                    "Pendientes generales para ver el panorama completo del taller.",
                    "Pendientes por técnico para balancear carga, prioridad y asignación.",
                    "Estados visibles para saber qué está ingresado, diagnosticado, reparando, reparado o entregado.",
                ],
            },
            {
                "heading": "Resultado",
                "points": [
                    "Menos seguimiento informal y menos dependencias personales.",
                    "Priorización inmediata de urgencias, equipos detenidos o cuellos de botella.",
                ],
            },
        ],
        "images": [
            {
                "key": "pendientes_general",
                "caption": "Pendientes generales: vista transversal para recepción, jefatura y coordinación diaria.",
            },
            {
                "key": "pendientes_por_tecnico",
                "caption": "Pendientes por técnico: distribución de carga y foco operativo por responsable.",
            },
        ],
    },
    {
        "id": "service",
        "layout": "text_image",
        "title": "Hoja de servicio central",
        "subtitle": "La hoja de servicio concentra toda la vida operativa del equipo en una sola ficha.",
        "sections": [
            {
                "heading": "Funciones principales",
                "points": [
                    "Ficha técnica y comercial del ingreso con cliente, equipo, motivo, garantías, accesorios y comentarios.",
                    "Estado actual, técnico asignado, ubicación, alquiler, datos de entrega y acciones de cierre.",
                    "Acceso directo a diagnóstico, tests, presupuesto, derivaciones, historial y archivos.",
                ],
            },
            {
                "heading": "Para qué sirve",
                "points": [
                    "Evita navegar entre sistemas o documentos separados.",
                    "Da contexto completo al técnico, a recepción y a quien aprueba decisiones.",
                ],
            },
        ],
        "image": {
            "key": "service_principal",
            "caption": "Hoja de servicio: vista central del caso, con todos los datos relevantes para operar y supervisar.",
        },
    },
    {
        "id": "diagnostico",
        "layout": "two_images",
        "title": "Diagnóstico, resolución y evidencias",
        "subtitle": "El software registra la lógica técnica del trabajo y la evidencia que lo respalda.",
        "sections": [
            {
                "heading": "Qué puede documentarse",
                "points": [
                    "Descripción de la falla, fecha de servicio, trabajos realizados y resolución final.",
                    "Accesorios asociados al ingreso y comentarios de trabajo.",
                    "Carga de imágenes, PDF y videos cortos para respaldar el proceso técnico.",
                ],
            },
            {
                "heading": "Valor operativo",
                "points": [
                    "Unifica memoria técnica, evidencia y criterio de cierre.",
                    "Mejora la comunicación interna y reduce discusiones sin respaldo.",
                ],
            },
        ],
        "images": [
            {
                "key": "service_diagnostico",
                "caption": "Diagnóstico y reparación: registro estructurado de falla, trabajos y resolución.",
            },
            {
                "key": "service_archivos",
                "caption": "Archivos del ingreso: evidencia visual y documental vinculada al caso técnico.",
            },
        ],
    },
    {
        "id": "tests",
        "layout": "text_image",
        "title": "Tests técnicos y control de calidad",
        "subtitle": "NEXORA incorpora protocolos por tipo de equipo para normalizar el control final.",
        "sections": [
            {
                "heading": "Funciones",
                "points": [
                    "Planillas de test por categoría de equipo con parámetros medidos y resultado por ítem.",
                    "Referencias técnicas visibles para sostener el criterio aplicado.",
                    "Emisión de informe PDF para respaldo técnico y trazabilidad documental.",
                ],
            },
            {
                "heading": "Impacto",
                "points": [
                    "Estandariza el control de calidad entre técnicos.",
                    "Documenta resultados medibles y evita cierres sin evidencia.",
                ],
            },
        ],
        "image": {
            "key": "service_test",
            "caption": "Tests técnicos: protocolo operativo con parámetros, tolerancias, resultado y firma responsable.",
        },
    },
    {
        "id": "presupuesto",
        "layout": "text_image",
        "title": "Presupuestos, aprobación y salida",
        "subtitle": "El circuito comercial y técnico convive dentro del mismo flujo operativo.",
        "sections": [
            {
                "heading": "Qué resuelve",
                "points": [
                    "Armado de presupuesto con repuestos, mano de obra, IVA y condiciones comerciales.",
                    "Estados de emisión, aprobado, rechazado o no aplica dentro de la misma OS.",
                    "Transición ordenada hacia reparación, liberación y remito de salida.",
                ],
            },
            {
                "heading": "Qué control aporta",
                "points": [
                    "Menos errores entre costo técnico y decisión comercial.",
                    "Más velocidad para aprobar, reparar y dejar el equipo listo para entrega.",
                ],
            },
        ],
        "image": {
            "key": "service_presupuesto",
            "caption": "Presupuesto integrado: costos, términos y aprobación dentro del mismo caso técnico.",
        },
    },
    {
        "id": "logistica",
        "layout": "two_images",
        "title": "Derivaciones y logística interna",
        "subtitle": "NEXORA ordena los movimientos del equipo aun cuando sale del circuito técnico principal.",
        "sections": [
            {
                "heading": "Cobertura operativa",
                "points": [
                    "Derivaciones externas, seguimiento del equipo, devolución y continuidad del trabajo.",
                    "Control de equipos liberados, entregados, en depósito, en baja o bajo alquiler.",
                    "Ubicaciones claras para sostener trazabilidad física además de la técnica.",
                ],
            },
            {
                "heading": "Beneficio",
                "points": [
                    "Reduce pérdidas de ubicación y mejora la coordinación entre taller, depósito y recepción.",
                ],
            },
        ],
        "images": [
            {
                "key": "depositos_bajas",
                "caption": "Depósitos y bajas: trazabilidad física para equipos fuera del flujo operativo normal.",
            },
            {
                "key": "service_principal",
                "caption": "La hoja de servicio mantiene visible ubicación, entrega, alquiler y estado de salida.",
            },
        ],
    },
    {
        "id": "activos",
        "layout": "text_image",
        "title": "Gestión de activos y parque técnico",
        "subtitle": "Además del ingreso puntual, NEXORA conserva la historia del equipo como activo técnico.",
        "sections": [
            {
                "heading": "Funciones",
                "points": [
                    "Listado de equipos con propiedad, último cliente, número de serie, número interno, marca, modelo y ubicación.",
                    "Alta directa de equipos para inventario o tutela técnica institucional.",
                    "Historial reutilizable por cliente y por equipo para continuidad operativa.",
                ],
            },
            {
                "heading": "Para qué sirve",
                "points": [
                    "Permite pensar el servicio no solo como casos aislados, sino como cartera técnica gestionada.",
                ],
            },
        ],
        "image": {
            "key": "equipos",
            "caption": "Gestión de equipos: vista del parque técnico con historia de propiedad, identificación y ubicación.",
        },
    },
    {
        "id": "preventivos",
        "layout": "two_images",
        "title": "Mantenimientos preventivos e instituciones",
        "subtitle": "NEXORA extiende la gestión desde la reparación correctiva hacia el mantenimiento planificado.",
        "sections": [
            {
                "heading": "Qué incluye",
                "points": [
                    "Planes preventivos por equipo o por institución con próximas revisiones y alertas.",
                    "Agenda de vencidos, próximos y equipos sin plan.",
                    "Seguimiento institucional con continuidad entre una visita y la siguiente.",
                ],
            },
            {
                "heading": "Resultado",
                "points": [
                    "Más previsibilidad operativa y mejor servicio sobre carteras institucionales.",
                ],
            },
        ],
        "images": [
            {
                "key": "preventivos",
                "caption": "Agenda preventiva: vencimientos, próximos controles y acción inmediata sobre cada equipo.",
            },
            {
                "key": "equipos",
                "caption": "Base de activos: soporte para cartera institucional y continuidad por equipo.",
            },
        ],
    },
    {
        "id": "catalogos",
        "layout": "two_images",
        "title": "Catálogos técnicos y repuestos",
        "subtitle": "El software ordena la base maestra que sostiene la operación cotidiana.",
        "sections": [
            {
                "heading": "Catálogos",
                "points": [
                    "Marcas, modelos, variantes y tipos de equipo para normalizar el alta y la clasificación.",
                    "Asignación técnica por marca o modelo para ordenar especialización y carga.",
                ],
            },
            {
                "heading": "Repuestos",
                "points": [
                    "Catálogo de repuestos, stock, mínimo, costos, movimientos, compras y permisos de conteo.",
                    "Base común para presupuestos más consistentes y trazabilidad de insumos.",
                ],
            },
        ],
        "images": [
            {
                "key": "catalogo_marcas",
                "caption": "Catálogo técnico: marcas, modelos, variantes y técnico responsable por familia de equipo.",
            },
            {
                "key": "repuestos",
                "caption": "Repuestos: stock, costos, movimientos y control sobre el insumo que impacta en el presupuesto.",
            },
        ],
    },
    {
        "id": "busqueda",
        "layout": "two_images",
        "title": "Búsqueda, clientes y análisis",
        "subtitle": "NEXORA acelera la consulta operativa y transforma los datos diarios en información accionable.",
        "sections": [
            {
                "heading": "Acceso rápido",
                "points": [
                    "Búsqueda por OS, número de serie, número interno, referencia de accesorio o lectura de QR.",
                    "Consulta histórica por cliente con exportaciones para seguimiento y gestión externa.",
                ],
            },
            {
                "heading": "Analítica",
                "points": [
                    "Métricas técnicas y financieras para tiempos, SLA, facturación, ticket promedio y carga de trabajo.",
                ],
            },
        ],
        "images": [
            {
                "key": "general_cliente",
                "caption": "General por cliente: historial operativo consolidado para seguimiento de cuentas e instituciones.",
            },
            {
                "key": "buscar_ns",
                "caption": "Búsqueda directa: acceso inmediato a antecedentes por serie o número interno.",
            },
        ],
        "image_strip": [
            {
                "key": "metricas_tecnicos",
                "caption": "Métricas técnicas",
            },
            {
                "key": "metricas_finanzas",
                "caption": "Métricas financieras",
            },
        ],
    },
    {
        "id": "control",
        "layout": "closing",
        "title": "Administración, control y escalabilidad",
        "subtitle": "NEXORA no solo ordena el trabajo técnico: también da gobierno sobre personas, reglas y decisiones.",
        "sections": [
            {
                "heading": "Control interno",
                "points": [
                    "Usuarios, roles y permisos granulares por acción y por pantalla.",
                    "Políticas de garantía administrables y excepciones por marca o modelo.",
                    "Historial de cambios y trazabilidad de eventos para auditoría operativa.",
                ],
            },
            {
                "heading": "Para quién sirve",
                "points": [
                    "Recepción: ordena ingreso, entrega y seguimiento.",
                    "Técnicos: concentran contexto, evidencia, test y resolución.",
                    "Jefatura: distribuye carga, aprueba, controla tiempos y analiza resultados.",
                    "Dirección: accede a indicadores, cartera y trazabilidad de punta a punta.",
                ],
            },
        ],
        "images": [
            {
                "key": "usuarios",
                "caption": "Usuarios y permisos: gobierno operativo por rol y por acción.",
            },
            {
                "key": "garantias",
                "caption": "Garantías: reglas administrables para sostener criterio técnico-comercial.",
            },
        ],
        "closing_title": "Cierre",
        "closing_blocks": [
            {
                "heading": "Despliegue",
                "text": "Puede implementarse sobre servidor interno y puestos operativos, con acceso controlado y trazabilidad centralizada.",
            },
            {
                "heading": "Adaptación",
                "text": "Puede rediseñarse para cada organización, incorporando funciones, pantallas, reportes y herramientas según su circuito de trabajo.",
            },
        ],
    },
]
