"""Permission catalog and role defaults for per-user overrides."""

from copy import deepcopy


PERMISSION_CATALOG = [
    {"code": "page.home_search", "label": "Ver inicio y buscadores", "type": "page", "group": "Páginas"},
    {"code": "page.general_cliente", "label": "Ver General por cliente", "type": "page", "group": "Páginas"},
    {"code": "page.ingresos_history", "label": "Ver historial de ingresos", "type": "page", "group": "Páginas"},
    {"code": "page.work_queues", "label": "Ver pendientes técnicos / pendientes de reparación / listos para liberar", "type": "page", "group": "Páginas"},
    {"code": "page.budget_queues", "label": "Ver pendientes de presupuesto/presupuestados", "type": "page", "group": "Páginas"},
    {"code": "page.logistics", "label": "Ver derivados, stock y depósitos", "type": "page", "group": "Páginas"},
    {"code": "page.liberados", "label": "Ver liberados", "type": "page", "group": "Páginas"},
    {"code": "page.service_sheet_principal", "label": "Ver pestaña Principal de hoja de servicio", "type": "page", "group": "Páginas"},
    {"code": "page.devices_preventivos", "label": "Ver equipos y preventivos", "type": "page", "group": "Páginas"},
    {"code": "page.new_ingreso", "label": "Ver pantalla de nuevo ingreso", "type": "page", "group": "Páginas"},
    {"code": "page.catalogs", "label": "Ver catálogos del sistema", "type": "page", "group": "Páginas"},
    {"code": "page.spare_parts", "label": "Ver repuestos", "type": "page", "group": "Páginas"},
    {"code": "page.metrics", "label": "Ver métricas", "type": "page", "group": "Páginas"},
    {"code": "page.warranty", "label": "Ver garantías", "type": "page", "group": "Páginas"},
    {"code": "page.users", "label": "Ver usuarios", "type": "page", "group": "Páginas"},
    {"code": "page.bejerman_sync", "label": "Ver sincronización Bejerman", "type": "page", "group": "Páginas"},
    {"code": "page.bejerman_purchase_entries", "label": "Ver ingresos de mercadería Bejerman", "type": "page", "group": "Páginas"},
    {"code": "page.work_dashboard", "label": "Ver tablero de trabajo y búsqueda global", "type": "page", "group": "Páginas"},
    {"code": "page.customer_catalog", "label": "Ver catálogo de clientes", "type": "page", "group": "Páginas"},
    {"code": "page.recepcion", "label": "Ver espacio de Recepción", "type": "page", "group": "NEXORA"},
    {"code": "page.delivery_orders", "label": "Ver órdenes de entrega", "type": "page", "group": "NEXORA"},
    {"code": "page.route_sheet", "label": "Ver Hoja de ruta", "type": "page", "group": "NEXORA"},
    {"code": "page.billing", "label": "Ver facturación y remitos pendientes", "type": "page", "group": "NEXORA"},
    {"code": "action.ingreso.create", "label": "Ingresar equipo (crear ingreso)", "type": "action", "group": "Ingresos"},
    {"code": "action.ingreso.edit_basics", "label": "Editar datos de ingreso", "type": "action", "group": "Ingresos"},
    {"code": "action.ingreso.edit_diagnosis", "label": "Editar diagnóstico/trabajos", "type": "action", "group": "Ingresos"},
    {"code": "action.ingreso.change_assignment", "label": "Cambiar asignación técnica", "type": "action", "group": "Ingresos"},
    {"code": "action.ingreso.edit_location", "label": "Editar ubicación", "type": "action", "group": "Ingresos"},
    {"code": "action.ingreso.edit_delivery", "label": "Editar datos de entrega", "type": "action", "group": "Ingresos"},
    {"code": "action.ingreso.repair_transitions", "label": "Cerrar reparación/cambiar estados", "type": "action", "group": "Ingresos"},
    # Permiso propio y acotado: recepción registra el cambio de equipo desde el mostrador
    # sin que se le abra toda la pestaña de diagnóstico.
    {"code": "action.ingreso.device_swap", "label": "Registrar cambio de equipo", "type": "action", "group": "Ingresos"},
    {"code": "action.ingreso.manage_derivations", "label": "Derivar/devolver derivaciones", "type": "action", "group": "Ingresos"},
    {"code": "action.ingreso.baja_alta", "label": "Dar baja/alta", "type": "action", "group": "Ingresos"},
    {"code": "action.ingreso.force_historical", "label": "Forzar correcciones históricas", "type": "action", "group": "Ingresos"},
    {"code": "action.ingreso.resolve_open_conflict", "label": "Resolver ingresos con orden anterior abierta", "type": "action", "group": "Ingresos"},
    {"code": "action.ingreso.print_exit_order", "label": "Imprimir orden de salida", "type": "action", "group": "Ingresos"},
    {"code": "action.ingreso.emit_ingress_order", "label": "Emitir RIS de ingreso", "type": "action", "group": "Ingresos"},
    {"code": "action.ingreso.fix_ris_preflight", "label": "Corregir validación previa del RIS", "type": "action", "group": "Ingresos"},
    {"code": "action.ingreso.print_barcode", "label": "Imprimir códigos de barras", "type": "action", "group": "Ingresos"},
    # Autorizar (o quitar) la reparación es decisión de jefatura: no alcanza con poder
    # mover estados de reparación, que tiene el técnico.
    {"code": "action.ingreso.authorize_repair", "label": "Autorizar / quitar reparación", "type": "action", "group": "Ingresos"},
    {"code": "action.ingreso.uncheck_rental", "label": "Destildar alquiler", "type": "action", "group": "Ingresos"},
    {"code": "action.presupuesto.manage", "label": "Presupuestar y administrar", "type": "action", "group": "Presupuestos"},
    {"code": "action.presupuesto.view_costs", "label": "Ver costos", "type": "action", "group": "Presupuestos"},
    {"code": "action.users.manage", "label": "Gestionar usuarios", "type": "action", "group": "Usuarios"},
    {"code": "action.users.manage_permissions", "label": "Editar permisos por usuario", "type": "action", "group": "Usuarios"},
    {"code": "action.catalogs.manage", "label": "Gestionar catalogos y garantias", "type": "action", "group": "Sistema"},
    {"code": "action.tests_protocol.manage", "label": "Gestionar protocolos de test", "type": "action", "group": "Sistema"},
    {"code": "action.spare_parts.manage", "label": "Gestionar repuestos y stock", "type": "action", "group": "Repuestos"},
    {"code": "action.spare_parts.manage_24h_permissions", "label": "Gestionar permisos 24h de repuestos", "type": "action", "group": "Repuestos"},
    {"code": "action.spare_parts.view_costs", "label": "Ver y editar costos de repuestos", "type": "action", "group": "Repuestos"},
    {"code": "action.devices_preventivos.manage", "label": "Gestionar edicion de equipos/preventivos", "type": "action", "group": "Equipos"},
    {"code": "action.devices_preventivos.create", "label": "Dar de alta equipos", "type": "action", "group": "Equipos"},
    # El alta de un equipo propio y la masiva mueven stock real (VAL→STL). Tiene permiso
    # propio para poder quitárselo a alguien sin sacarle el alta de equipos de clientes.
    {"code": "action.devices_preventivos.create_own", "label": "Alta de equipos propios y alta masiva (mueve stock a STL)", "type": "action", "group": "Equipos"},
    {"code": "action.metrics.configure", "label": "Configurar metricas", "type": "action", "group": "Sistema"},
    {"code": "action.work_dashboard.configure", "label": "Configurar objetivos y alertas del tablero", "type": "action", "group": "Sistema"},
    {"code": "action.bejerman_sync.manage", "label": "Gestionar sincronización Bejerman", "type": "action", "group": "Sistema"},
    {"code": "action.bejerman_sync.force_stock_entry", "label": "Forzar ingreso de stock en Bejerman", "type": "action", "group": "Sistema"},
    {"code": "action.bejerman_purchase_entries.manage", "label": "Gestionar ingresos de mercadería Bejerman", "type": "action", "group": "Sistema"},
    {"code": "action.bejerman_purchase_entries.emit", "label": "Registrar RT de compra en Bejerman", "type": "action", "group": "Sistema"},
    {"code": "action.delivery_order.create", "label": "Crear órdenes de entrega", "type": "action", "group": "Ordenes de entrega"},
    {"code": "action.delivery_order.prepare", "label": "Preparar órdenes de entrega", "type": "action", "group": "Ordenes de entrega"},
    {"code": "action.delivery_order.deliver", "label": "Entregar órdenes de entrega", "type": "action", "group": "Ordenes de entrega"},
    {"code": "action.delivery_order.invoice", "label": "Registrar facturas de órdenes de entrega", "type": "action", "group": "Cobranzas"},
    {"code": "action.delivery_order.cancel", "label": "Cancelar órdenes de entrega", "type": "action", "group": "Ordenes de entrega"},
    {"code": "action.delivery_order.update_remito_location", "label": "Actualizar ubicación de remitos", "type": "action", "group": "Cobranzas"},
    {"code": "action.delivery_order.generate_bejerman_remito", "label": "Generar remitos Bejerman", "type": "action", "group": "Ordenes de entrega"},
    {"code": "action.delivery_order.assign_articles", "label": "Asignar artículos y partidas", "type": "action", "group": "Ordenes de entrega"},
    {"code": "action.delivery_order.edit_discounts", "label": "Cargar descuentos en órdenes de entrega", "type": "action", "group": "Ordenes de entrega"},
    {"code": "action.delivery_order.sync_drive", "label": "Sincronizar órdenes de entrega con Google Drive", "type": "action", "group": "Ordenes de entrega"},
    {"code": "action.delivery_order.correct_commercial_terms", "label": "Corregir precio y plazo post-remito", "type": "action", "group": "Cobranzas"},
    {"code": "action.route_sheet.manage", "label": "Gestionar Hoja de ruta", "type": "action", "group": "Hoja de ruta"},
    {"code": "action.route_sheet.complete", "label": "Completar paradas de Hoja de ruta", "type": "action", "group": "Hoja de ruta"},
    {"code": "action.route_sheet.postpone", "label": "Posponer paradas de Hoja de ruta", "type": "action", "group": "Hoja de ruta"},
    {"code": "action.billing.view", "label": "Consultar facturación Bejerman", "type": "action", "group": "Cobranzas"},
    {"code": "action.billing.register_os_invoice", "label": "Registrar factura de OS", "type": "action", "group": "Cobranzas"},
]


PERMISSION_CODES = [item["code"] for item in PERMISSION_CATALOG]
PERMISSION_CODES_SET = set(PERMISSION_CODES)


def _empty_role_defaults():
    return {code: False for code in PERMISSION_CODES}


ROLE_DEFAULTS = {
    "tecnico": _empty_role_defaults(),
    "admin": _empty_role_defaults(),
    "supervisor": _empty_role_defaults(),
    "ventas": _empty_role_defaults(),
    "jefe": {code: True for code in PERMISSION_CODES},
    "jefe_veedor": _empty_role_defaults(),
    "jefe_suplente": _empty_role_defaults(),
    "recepcion": _empty_role_defaults(),
    "cobranzas": _empty_role_defaults(),
    "logistica": _empty_role_defaults(),
}


def _grant(role, *codes):
    for code in codes:
        if code in ROLE_DEFAULTS[role]:
            ROLE_DEFAULTS[role][code] = True


def _revoke(role, *codes):
    for code in codes:
        if code in ROLE_DEFAULTS[role]:
            ROLE_DEFAULTS[role][code] = False


# tecnico
_grant(
    "tecnico",
    "page.home_search",
    "page.general_cliente",
    "page.ingresos_history",
    "page.work_queues",
    "page.logistics",
    "page.liberados",
    "page.service_sheet_principal",
    "page.devices_preventivos",
    "page.spare_parts",
    "action.ingreso.edit_diagnosis",
    "action.ingreso.edit_location",
    "action.ingreso.repair_transitions",
    "action.ingreso.manage_derivations",
    "action.ingreso.print_barcode",
    "action.spare_parts.manage",
    "action.devices_preventivos.manage",
)

# admin
_grant(
    "admin",
    "page.home_search",
    "page.general_cliente",
    "page.ingresos_history",
    "page.logistics",
    "page.liberados",
    "page.service_sheet_principal",
    "page.devices_preventivos",
    "page.new_ingreso",
    "page.catalogs",
    "page.spare_parts",
    "page.warranty",
    "page.recepcion",
    "page.route_sheet",
    "page.bejerman_purchase_entries",
    "action.ingreso.create",
    "action.ingreso.edit_basics",
    "action.ingreso.edit_diagnosis",
    "action.ingreso.change_assignment",
    "action.ingreso.edit_location",
    "action.ingreso.edit_delivery",
    "action.ingreso.repair_transitions",
    "action.ingreso.device_swap",
    "action.ingreso.manage_derivations",
    "action.ingreso.baja_alta",
    "action.ingreso.force_historical",
    "action.ingreso.resolve_open_conflict",
    "action.ingreso.print_exit_order",
    "action.ingreso.emit_ingress_order",
    "action.ingreso.fix_ris_preflight",
    "action.ingreso.print_barcode",
    "action.catalogs.manage",
    "action.tests_protocol.manage",
    "action.devices_preventivos.manage",
    "action.bejerman_sync.manage",
    "action.bejerman_purchase_entries.manage",
    "action.bejerman_purchase_entries.emit",
    "action.delivery_order.prepare",
    "action.delivery_order.deliver",
    "action.delivery_order.cancel",
    "action.delivery_order.update_remito_location",
    "action.route_sheet.manage",
    "action.route_sheet.complete",
)

# ventas
_grant(
    "ventas",
    "page.home_search",
    "page.general_cliente",
    "page.ingresos_history",
    "page.logistics",
    "page.liberados",
    "page.service_sheet_principal",
    "page.devices_preventivos",
    "page.new_ingreso",
    "page.catalogs",
    "page.spare_parts",
    "page.warranty",
    "page.recepcion",
    "page.delivery_orders",
    "page.route_sheet",
    "page.bejerman_purchase_entries",
    "action.ingreso.create",
    "action.ingreso.edit_basics",
    "action.ingreso.edit_diagnosis",
    "action.ingreso.change_assignment",
    "action.ingreso.edit_location",
    "action.ingreso.edit_delivery",
    "action.ingreso.repair_transitions",
    "action.ingreso.manage_derivations",
    "action.ingreso.baja_alta",
    "action.ingreso.force_historical",
    "action.ingreso.resolve_open_conflict",
    "action.ingreso.print_exit_order",
    "action.ingreso.emit_ingress_order",
    "action.ingreso.fix_ris_preflight",
    "action.ingreso.print_barcode",
    "action.catalogs.manage",
    "action.tests_protocol.manage",
    "action.devices_preventivos.manage",
    "action.bejerman_sync.manage",
    "action.bejerman_purchase_entries.manage",
    "action.bejerman_purchase_entries.emit",
    "action.delivery_order.create",
    "action.delivery_order.prepare",
    "action.delivery_order.deliver",
    "action.delivery_order.cancel",
    "action.delivery_order.update_remito_location",
    "action.delivery_order.generate_bejerman_remito",
    "action.delivery_order.assign_articles",
    "action.route_sheet.manage",
)

# jefe_veedor
_grant(
    "jefe_veedor",
    "page.home_search",
    "page.general_cliente",
    "page.ingresos_history",
    "page.work_queues",
    "page.budget_queues",
    "page.logistics",
    "page.liberados",
    "page.service_sheet_principal",
    "page.devices_preventivos",
    "page.new_ingreso",
    "page.catalogs",
    "page.spare_parts",
    "page.metrics",
    "page.warranty",
    "page.users",
    "page.recepcion",
    "page.delivery_orders",
    "page.billing",
    "page.bejerman_purchase_entries",
    "action.ingreso.create",
    "action.ingreso.edit_basics",
    "action.ingreso.edit_diagnosis",
    "action.ingreso.change_assignment",
    "action.ingreso.edit_location",
    "action.ingreso.edit_delivery",
    "action.ingreso.repair_transitions",
    "action.ingreso.device_swap",
    "action.ingreso.manage_derivations",
    "action.ingreso.baja_alta",
    "action.ingreso.force_historical",
    "action.ingreso.resolve_open_conflict",
    "action.ingreso.print_exit_order",
    "action.ingreso.emit_ingress_order",
    "action.ingreso.fix_ris_preflight",
    "action.ingreso.print_barcode",
    "action.presupuesto.view_costs",
    "action.catalogs.manage",
    "action.spare_parts.manage",
    "action.spare_parts.manage_24h_permissions",
    "action.devices_preventivos.manage",
    "action.bejerman_sync.manage",
    "action.bejerman_purchase_entries.manage",
    "action.bejerman_purchase_entries.emit",
    "action.delivery_order.create",
    "action.delivery_order.prepare",
    "action.delivery_order.deliver",
    "action.delivery_order.invoice",
    "action.delivery_order.cancel",
    "action.delivery_order.update_remito_location",
    "action.delivery_order.generate_bejerman_remito",
    "action.delivery_order.assign_articles",
    "action.billing.view",
    "action.billing.register_os_invoice",
)

# recepcion
_grant(
    "recepcion",
    "page.home_search",
    "page.general_cliente",
    "page.logistics",
    "page.service_sheet_principal",
    "page.new_ingreso",
    "page.recepcion",
    "page.delivery_orders",
    "page.route_sheet",
    "page.bejerman_purchase_entries",
    # Recepción da de alta el equipo en el mostrador (muchas veces antes de que exista
    # una orden), así que ve la pestaña Equipos y puede el alta directa. NO se le da
    # `devices_preventivos.manage`: ese permiso es también el que habilita pasar un equipo
    # al patrimonio de SEPID desde el cambio de equipo, y esa decisión sigue en jefatura.
    "page.devices_preventivos",
    "action.devices_preventivos.create",
    "action.ingreso.create",
    # Recepción está en el mostrador cuando se define el cambio de equipo, así que
    # registra el cambio sin tener `repair_transitions` ni la pestaña de diagnóstico.
    "action.ingreso.device_swap",
    "action.ingreso.emit_ingress_order",
    "action.ingreso.fix_ris_preflight",
    # Recepción es quien está en el mostrador cuando aparece el conflicto. Puede cerrar la
    # orden anterior sólo cuando Bejerman muestra el RSS/RTA que prueba que el equipo salió;
    # sin esa prueba el cierre sigue pidiendo action.ingreso.force_historical.
    "action.ingreso.resolve_open_conflict",
    "action.ingreso.print_barcode",
    "action.bejerman_purchase_entries.manage",
    "action.bejerman_purchase_entries.emit",
    "action.delivery_order.create",
    "action.delivery_order.prepare",
    "action.delivery_order.deliver",
    "action.delivery_order.cancel",
    "action.delivery_order.update_remito_location",
    "action.delivery_order.generate_bejerman_remito",
    "action.delivery_order.assign_articles",
    "action.route_sheet.manage",
    "action.route_sheet.complete",
)

# cobranzas
_grant(
    "cobranzas",
    "page.delivery_orders",
    "page.billing",
    "action.delivery_order.invoice",
    "action.delivery_order.update_remito_location",
    "action.billing.view",
    "action.billing.register_os_invoice",
)

# logistica
_grant(
    "logistica",
    "page.route_sheet",
    "action.route_sheet.complete",
    "action.route_sheet.postpone",
    "action.delivery_order.deliver",
)


# Permisos que antes eran listas de roles escritas en las vistas. Se otorgan a los mismos
# roles que figuraban en esas listas: el deploy no cambia quién puede qué, sólo lo vuelve
# configurable por usuario. Supervisor y suplente los reciben por las uniones de abajo.
_grant("tecnico", "page.work_dashboard", "action.devices_preventivos.create_own")
_grant(
    "admin",
    "page.work_dashboard",
    "page.customer_catalog",
    "action.work_dashboard.configure",
    "action.spare_parts.view_costs",
    "action.delivery_order.edit_discounts",
    "action.delivery_order.sync_drive",
    "action.devices_preventivos.create_own",
)
_grant(
    "ventas",
    "page.work_dashboard",
    "page.customer_catalog",
    "action.delivery_order.edit_discounts",
    "action.delivery_order.sync_drive",
    "action.devices_preventivos.create_own",
)
_grant(
    "jefe_veedor",
    "page.work_dashboard",
    "page.customer_catalog",
    "action.work_dashboard.configure",
    "action.spare_parts.view_costs",
    "action.ingreso.authorize_repair",
    "action.bejerman_sync.force_stock_entry",
    "action.devices_preventivos.create_own",
)
_grant("recepcion", "page.work_dashboard", "action.devices_preventivos.create_own")
_grant("cobranzas", "page.work_dashboard")


def _set_union_role_defaults(target_role, *source_roles):
    target = ROLE_DEFAULTS[target_role]
    for code in PERMISSION_CODES:
        target[code] = any(ROLE_DEFAULTS[source].get(code, False) for source in source_roles)


_set_union_role_defaults("supervisor", "admin", "ventas", "recepcion")
_grant("supervisor", "action.route_sheet.postpone")
_grant("supervisor", "action.delivery_order.correct_commercial_terms")
# Forzar el ingreso de stock en Bejerman lo tenía supervisor sin que lo tuviera admin, así
# que no sale de la unión.
_grant("supervisor", "action.bejerman_sync.force_stock_entry")


# jefe_suplente — quien cubre a jefatura mientras el titular no está.
#
# Es la unión de técnico y jefe, y la unión importa: si el suplente es un técnico,
# tiene que seguir siendo asignable y seguir arreglando equipos. Reemplazarle el rol
# por uno de jefatura lo sacaba de las listas de técnicos y dejaba de poder recibir
# equipos asignados.
#
# Lo único que se le quita es la configuración de usuarios y permisos: el suplente
# cubre el trabajo, no la administración de quién puede hacer qué. Esa exclusión es
# el motivo por el que este rol existe en vez de simplemente usar `jefe`.
SUPLENTE_EXCLUSIONES = (
    "page.users",
    "action.users.manage",
    "action.users.manage_permissions",
)
_set_union_role_defaults("jefe_suplente", "tecnico", "jefe")
_revoke("jefe_suplente", *SUPLENTE_EXCLUSIONES)


def normalize_role(role):
    return (role or "").strip().lower()


def get_catalog():
    return deepcopy(PERMISSION_CATALOG)


def get_role_defaults(role):
    role_key = normalize_role(role)
    if role_key == "jefe":
        return {code: True for code in PERMISSION_CODES}
    base = ROLE_DEFAULTS.get(role_key)
    if base is None:
        return _empty_role_defaults()
    return deepcopy(base)
