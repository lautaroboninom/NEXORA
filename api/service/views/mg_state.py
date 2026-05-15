import re


MG_CODE_RE = re.compile(r"^MG\s*\d{1,4}$", re.IGNORECASE)


def is_mg_code(value: str) -> bool:
    return bool(MG_CODE_RE.match((value or "").strip()))


def normalize_mg(value: str):
    raw = (value or "").strip().upper()
    m = re.match(r"^(MG|NM|NV|CE)\s*(\d{1,4})$", raw, re.IGNORECASE)
    if not m:
        return None
    return f"{m.group(1).upper()} {m.group(2).zfill(4)}"


def is_mg_owner_device(numero_interno: str, numero_serie: str = "") -> bool:
    return is_mg_code(numero_interno) or is_mg_code(numero_serie)


def resolve_mg_flags(device: dict, mg_owner_id=None) -> dict:
    numero_interno = (device or {}).get("numero_interno") or ""
    numero_serie = (device or {}).get("numero_serie") or ""
    es_propietario_mg = is_mg_owner_device(numero_interno, numero_serie)

    explicit = str((device or {}).get("mg_estado") or "").strip().lower()
    if explicit not in ("activo", "inactivo_venta"):
        explicit = "activo"
        if (
            es_propietario_mg
            and not bool((device or {}).get("alquilado"))
            and (device or {}).get("customer_id")
        ):
            try:
                customer_id = int((device or {}).get("customer_id"))
                if mg_owner_id is None or customer_id != int(mg_owner_id):
                    explicit = "inactivo_venta"
            except Exception:
                pass

    mg_inactivo_venta = explicit == "inactivo_venta"
    return {
        "es_propietario_mg": bool(es_propietario_mg),
        "mg_estado": explicit,
        "mg_activo": not mg_inactivo_venta,
        "mg_inactivo_venta": mg_inactivo_venta,
        # Compatibilidad con front legacy.
        "vendido": mg_inactivo_venta,
    }
