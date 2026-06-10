"""Permission catalog and role defaults for per-user overrides."""

from copy import deepcopy


PERMISSION_CATALOG = [
    {"code": "page.home_search", "label": "Ver inicio y buscadores", "type": "page", "group": "Paginas"},
    {"code": "page.ingresos_history", "label": "Ver historico de ingresos", "type": "page", "group": "Paginas"},
    {"code": "page.work_queues", "label": "Ver pendientes tecnicos/aprobados/reparados", "type": "page", "group": "Paginas"},
    {"code": "page.budget_queues", "label": "Ver pendientes de presupuesto/presupuestados", "type": "page", "group": "Paginas"},
    {"code": "page.logistics", "label": "Ver derivados/listos/depositos/stock alquiler", "type": "page", "group": "Paginas"},
    {"code": "page.liberados", "label": "Ver liberados", "type": "page", "group": "Paginas"},
    {"code": "page.service_sheet_principal", "label": "Ver pestaña Principal de hoja de servicio", "type": "page", "group": "Paginas"},
    {"code": "page.devices_preventivos", "label": "Ver equipos y preventivos", "type": "page", "group": "Paginas"},
    {"code": "page.new_ingreso", "label": "Ver pantalla de nuevo ingreso", "type": "page", "group": "Paginas"},
    {"code": "page.catalogs", "label": "Ver catalogos del sistema", "type": "page", "group": "Paginas"},
    {"code": "page.spare_parts", "label": "Ver repuestos", "type": "page", "group": "Paginas"},
    {"code": "page.metrics", "label": "Ver metricas", "type": "page", "group": "Paginas"},
    {"code": "page.warranty", "label": "Ver garantias", "type": "page", "group": "Paginas"},
    {"code": "page.users", "label": "Ver usuarios", "type": "page", "group": "Paginas"},
    {"code": "page.bejerman_sync", "label": "Ver sincronización Bejerman", "type": "page", "group": "Paginas"},
    {"code": "action.ingreso.create", "label": "Ingresar equipo (crear ingreso)", "type": "action", "group": "Ingresos"},
    {"code": "action.ingreso.edit_basics", "label": "Editar datos de ingreso", "type": "action", "group": "Ingresos"},
    {"code": "action.ingreso.edit_diagnosis", "label": "Editar diagnostico/trabajos", "type": "action", "group": "Ingresos"},
    {"code": "action.ingreso.change_assignment", "label": "Cambiar asignacion tecnica", "type": "action", "group": "Ingresos"},
    {"code": "action.ingreso.edit_location", "label": "Editar ubicacion", "type": "action", "group": "Ingresos"},
    {"code": "action.ingreso.edit_delivery", "label": "Editar datos de entrega", "type": "action", "group": "Ingresos"},
    {"code": "action.ingreso.repair_transitions", "label": "Cerrar reparacion/cambiar estados", "type": "action", "group": "Ingresos"},
    {"code": "action.ingreso.manage_derivations", "label": "Derivar/devolver derivaciones", "type": "action", "group": "Ingresos"},
    {"code": "action.ingreso.baja_alta", "label": "Dar baja/alta", "type": "action", "group": "Ingresos"},
    {"code": "action.ingreso.force_historical", "label": "Forzar correcciones históricas", "type": "action", "group": "Ingresos"},
    {"code": "action.ingreso.print_exit_order", "label": "Imprimir orden de salida", "type": "action", "group": "Ingresos"},
    {"code": "action.ingreso.emit_ingress_order", "label": "Emitir RIS de ingreso", "type": "action", "group": "Ingresos"},
    {"code": "action.ingreso.print_barcode", "label": "Imprimir códigos de barras", "type": "action", "group": "Ingresos"},
    {"code": "action.presupuesto.manage", "label": "Presupuestar y administrar", "type": "action", "group": "Presupuestos"},
    {"code": "action.presupuesto.view_costs", "label": "Ver costos", "type": "action", "group": "Presupuestos"},
    {"code": "action.users.manage", "label": "Gestionar usuarios", "type": "action", "group": "Usuarios"},
    {"code": "action.users.manage_permissions", "label": "Editar permisos por usuario", "type": "action", "group": "Usuarios"},
    {"code": "action.catalogs.manage", "label": "Gestionar catalogos y garantias", "type": "action", "group": "Sistema"},
    {"code": "action.tests_protocol.manage", "label": "Gestionar protocolos de test", "type": "action", "group": "Sistema"},
    {"code": "action.spare_parts.manage", "label": "Gestionar repuestos y stock", "type": "action", "group": "Repuestos"},
    {"code": "action.spare_parts.manage_24h_permissions", "label": "Gestionar permisos 24h de repuestos", "type": "action", "group": "Repuestos"},
    {"code": "action.devices_preventivos.manage", "label": "Gestionar edicion de equipos/preventivos", "type": "action", "group": "Equipos"},
    {"code": "action.metrics.configure", "label": "Configurar metricas", "type": "action", "group": "Sistema"},
    {"code": "action.bejerman_sync.manage", "label": "Gestionar sincronización Bejerman", "type": "action", "group": "Sistema"},
]


PERMISSION_CODES = [item["code"] for item in PERMISSION_CATALOG]
PERMISSION_CODES_SET = set(PERMISSION_CODES)


def _empty_role_defaults():
    return {code: False for code in PERMISSION_CODES}


ROLE_DEFAULTS = {
    "tecnico": _empty_role_defaults(),
    "admin": _empty_role_defaults(),
    "jefe": {code: True for code in PERMISSION_CODES},
    "jefe_veedor": _empty_role_defaults(),
    "recepcion": _empty_role_defaults(),
}


def _grant(role, *codes):
    for code in codes:
        if code in ROLE_DEFAULTS[role]:
            ROLE_DEFAULTS[role][code] = True


# tecnico
_grant(
    "tecnico",
    "page.home_search",
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
    "page.ingresos_history",
    "page.logistics",
    "page.liberados",
    "page.service_sheet_principal",
    "page.devices_preventivos",
    "page.new_ingreso",
    "page.catalogs",
    "page.spare_parts",
    "page.warranty",
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
    "action.ingreso.print_exit_order",
    "action.ingreso.emit_ingress_order",
    "action.ingreso.print_barcode",
    "action.catalogs.manage",
    "action.tests_protocol.manage",
    "action.devices_preventivos.manage",
    "action.bejerman_sync.manage",
)

# jefe_veedor
_grant(
    "jefe_veedor",
    "page.home_search",
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
    "action.ingreso.print_exit_order",
    "action.ingreso.emit_ingress_order",
    "action.ingreso.print_barcode",
    "action.presupuesto.view_costs",
    "action.catalogs.manage",
    "action.spare_parts.manage",
    "action.spare_parts.manage_24h_permissions",
    "action.devices_preventivos.manage",
    "action.bejerman_sync.manage",
)

# recepcion
_grant(
    "recepcion",
    "page.liberados",
    "page.new_ingreso",
    "action.ingreso.create",
    "action.ingreso.emit_ingress_order",
    "action.ingreso.print_barcode",
)


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
