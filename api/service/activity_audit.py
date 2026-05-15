import re


READ_AUDIT_QUERY_EXCLUDE_KEYS = {"q", "search", "code", "ref"}
READ_AUDIT_EXCLUDED_PATHS = {
    "/api/busqueda/global/",
    "/api/scan/lookup/",
    "/api/accesorios/buscar/",
    "/api/catalogos/repuestos/",
}

READ_AUDIT_EXACT_RULES = [
    {
        "path": "/api/tecnico/mis-pendientes/",
        "activity_type": "apertura_cola",
        "title": "Abrió mis pendientes",
    },
    {
        "path": "/api/ingresos/pendientes/",
        "activity_type": "apertura_cola",
        "title": "Abrió pendientes generales",
    },
    {
        "path": "/api/presupuestos/pendientes/",
        "activity_type": "apertura_cola",
        "title": "Abrió pendientes de presupuesto",
    },
    {
        "path": "/api/ingresos/aprobados-para-reparar/",
        "activity_type": "apertura_cola",
        "title": "Abrió aprobados para reparar",
    },
    {
        "path": "/api/ingresos/aprobados-reparados/",
        "activity_type": "apertura_cola",
        "title": "Abrió reparados",
    },
    {
        "path": "/api/ingresos/liberados/",
        "activity_type": "apertura_cola",
        "title": "Abrió liberados",
    },
    {
        "path": "/api/ingresos/derivados/",
        "activity_type": "apertura_cola",
        "title": "Abrió derivados",
    },
    {
        "path": "/api/listos-para-retiro/",
        "activity_type": "apertura_cola",
        "title": "Abrió listos para retiro",
    },
    {
        "path": "/api/ingresos/",
        "activity_type": "apertura_historico",
        "title": "Abrió histórico de ingresos",
    },
    {
        "path": "/api/ingresos/historico/",
        "activity_type": "apertura_historico",
        "title": "Abrió histórico de ingresos",
    },
    {
        "path": "/api/repuestos/",
        "activity_type": "apertura_repuestos",
        "title": "Abrió listado de repuestos",
    },
    {
        "path": "/api/repuestos/movimientos/",
        "activity_type": "apertura_movimientos",
        "title": "Abrió movimientos de repuestos",
    },
    {
        "path": "/api/ingresos/presupuestados/",
        "activity_type": "apertura_cola",
        "title": "Abrió presupuestados",
    },
    {
        "path": "/api/equipos/",
        "activity_type": "apertura_equipos",
        "title": "Abrió listado de equipos",
    },
]

READ_AUDIT_REGEX_RULES = [
    {
        "pattern": re.compile(r"^/api/ingresos/(?P<ingreso_id>\d+)/$"),
        "activity_type": "apertura_hoja",
        "title": "Abrió hoja de servicio",
    },
    {
        "pattern": re.compile(r"^/api/quotes/(?P<ingreso_id>\d+)/$"),
        "activity_type": "apertura_presupuesto",
        "title": "Abrió presupuesto",
    },
]


def should_audit_read_request(path, query_params=None) -> bool:
    path_value = str(path or "").strip()
    if not path_value or path_value in READ_AUDIT_EXCLUDED_PATHS:
        return False

    keys = set()
    if query_params is not None:
        try:
            keys = {str(key or "").strip().lower() for key in query_params.keys()}
        except Exception:
            keys = set()
    if keys & READ_AUDIT_QUERY_EXCLUDE_KEYS:
        return False

    for rule in READ_AUDIT_EXACT_RULES:
        if path_value == rule["path"]:
            return True
    return any(rule["pattern"].match(path_value) for rule in READ_AUDIT_REGEX_RULES)


def classify_read_path(path):
    path_value = str(path or "").strip()
    if not path_value:
        return None

    for rule in READ_AUDIT_EXACT_RULES:
        if path_value == rule["path"]:
            return {
                "activity_type": rule["activity_type"],
                "title": rule["title"],
                "path": path_value,
            }

    for rule in READ_AUDIT_REGEX_RULES:
        match = rule["pattern"].match(path_value)
        if not match:
            continue
        data = {
            "activity_type": rule["activity_type"],
            "title": rule["title"],
            "path": path_value,
        }
        groups = match.groupdict() or {}
        if groups.get("ingreso_id"):
            try:
                data["ingreso_id"] = int(groups["ingreso_id"])
            except Exception:
                pass
        return data

    return None
