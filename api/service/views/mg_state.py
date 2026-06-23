import re


MG_CODE_RE = re.compile(r"^(MG|NM|NV|CE)\s*\d{1,4}$", re.IGNORECASE)


def is_mg_code(value: str) -> bool:
    return bool(MG_CODE_RE.match((value or "").strip()))


def normalize_mg(value: str):
    raw = (value or "").strip().upper()
    m = re.match(r"^(MG|NM|NV|CE)\s*(\d{1,4})$", raw, re.IGNORECASE)
    if not m:
        return None
    return f"{m.group(1).upper()} {m.group(2).zfill(4)}"


def is_mg_owner_device(numero_interno: str, numero_serie: str = "", n_de_control: str = "") -> bool:
    return is_mg_code(numero_interno) or is_mg_code(numero_serie) or is_mg_code(n_de_control)


def resolve_mg_flags(device: dict, mg_owner_id=None) -> dict:
    numero_interno = (device or {}).get("numero_interno") or ""
    numero_serie = (device or {}).get("numero_serie") or ""
    n_de_control = (device or {}).get("n_de_control") or (device or {}).get("numero_control") or ""
    tiene_codigo_mg = is_mg_owner_device(numero_interno, numero_serie, n_de_control)
    customer_id = (device or {}).get("customer_id")

    explicit = str((device or {}).get("mg_estado") or "").strip().lower()
    if explicit not in ("activo", "inactivo_venta"):
        explicit = "activo"

    mg_inactivo_venta = explicit == "inactivo_venta"

    es_cliente_mg_owner = False
    try:
        if customer_id is not None and mg_owner_id is not None:
            es_cliente_mg_owner = int(customer_id) == int(mg_owner_id)
    except Exception:
        es_cliente_mg_owner = False

    # Un MG activo puede estar asignado operativamente a un cliente por alquiler
    # o servicio; eso no cambia que el patrimonio siga siendo MG BIO.
    es_propietario_mg = bool(es_cliente_mg_owner or (tiene_codigo_mg and not mg_inactivo_venta))
    return {
        "es_propietario_mg": bool(es_propietario_mg),
        "es_cliente_mg_owner": bool(es_propietario_mg),
        "tiene_codigo_mg": bool(tiene_codigo_mg),
        "mg_estado": explicit,
        "mg_activo": not mg_inactivo_venta,
        "mg_inactivo_venta": mg_inactivo_venta,
        # Compatibilidad con front legacy.
        "vendido": mg_inactivo_venta,
    }
