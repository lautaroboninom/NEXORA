# constants.py
# Constantes de dominio usadas por la app "service".

import unicodedata

# Nombres canónicos de ubicaciones por defecto
DEFAULT_LOCATION_NAMES = [
    "Taller",
    "Estantería de Alquiler",
    "Sarmiento",
    "Sepid",
    "-",
]

# Remapeos de alias de ubicaciones a forma canónica
LOCATION_NAME_REMAPS = {
    # Variantes comunes y errores tipográficos para "Estantería de Alquiler"
    "estanteria alquileres": "Estantería de Alquiler",
    "estanteria de alquiler": "Estantería de Alquiler",
    "estantería de aluiler": "Estantería de Alquiler",
    "estanteria de aluiler": "Estantería de Alquiler",
    # Variantes detectadas en BD: sin "de" y/o sin acentos
    "estantería alquiler": "Estantería de Alquiler",
    "estanteria alquiler": "Estantería de Alquiler",
    "estanteria de alquiler (stl)": "Estantería de Alquiler",
    "estanteria alquiler (stl)": "Estantería de Alquiler",
    "estanteria de alquiler stl": "Estantería de Alquiler",
    "estanteria alquiler stl": "Estantería de Alquiler",
    # Variantes históricas para "Sepid" (depósito VAL)
    "deposito sepid": "Sepid",
    "deposito sepid s.a.": "Sepid",
    "deposito sepid sa": "Sepid",
    "sepid s.a.": "Sepid",
    "sepid sa": "Sepid",
    "deposito sepid (val)": "Sepid",
    "deposito sepid val": "Sepid",
    "sepid (val)": "Sepid",
    "sepid val": "Sepid",
    "deposito central": "Sepid",
    "deposito central sepid": "Sepid",
    "valdenegro": "Sepid",
    "deposito valdenegro": "Sepid",
    "valdenegro 4578": "Sepid",
    # Variantes para "Sarmiento" (depósito SAR)
    "deposito sarmiento": "Sarmiento",
    "sarmiento (sar)": "Sarmiento",
    "sarmiento sar": "Sarmiento",
    "deposito sarmiento (sar)": "Sarmiento",
    "deposito sarmiento sar": "Sarmiento",
    # Variantes para "Taller" (depósito STR)
    "deposito taller": "Taller",
    "taller (str)": "Taller",
    "taller str": "Taller",
    "deposito taller (str)": "Taller",
    "deposito taller str": "Taller",
}

# Una ubicación operativa no es solamente un nombre dentro de NEXORA: cuando se la
# cambia desde la hoja de servicio, estas cuatro tienen su contraparte física en
# Bejerman. El guion es una ubicación virtual de los estados cerrados; no representa
# stock ni debe ofrecerse como destino manual.
LOCATION_BEJERMAN_DEPOSITS = {
    "Taller": "STR",
    "Sarmiento": "SAR",
    "Estantería de Alquiler": "STL",
    "Sepid": "VAL",
}

LOCATION_DISPLAY_NAMES = {
    "Sarmiento": "Sarmiento (SAR)",
    "Sepid": "Sepid (VAL)",
}


def location_name_key(value) -> str:
    """Clave comparable de una ubicación, sin depender de tildes o espacios."""
    text = str(value or "").strip()
    # Hubo una fila grabada como ``EstanterÃ­a`` (UTF-8 leído como latin-1). Al
    # compararla sin reparar, la tilde se convertía en una "a" y quedaba como un
    # falso nombre distinto, aunque para el operador se veía duplicada.
    if "Ã" in text or "Â" in text:
        try:
            text = text.encode("latin-1").decode("utf-8")
        except UnicodeError:
            pass
    text = text.casefold()
    text = "".join(
        char
        for char in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(char) and unicodedata.category(char) != "Cf"
    )
    return " ".join(text.split())


def canonical_location_name(value) -> str:
    """Devuelve el nombre canónico de una ubicación conocida.

    La tabla histórica admitió variantes sin tilde de la estantería. Centralizar la
    normalización evita que una variante vuelva a crear una opción visual duplicada.
    """
    key = location_name_key(value)
    canonical_by_key = {location_name_key(name): name for name in DEFAULT_LOCATION_NAMES}
    remaps_by_key = {location_name_key(alias): target for alias, target in LOCATION_NAME_REMAPS.items()}
    return remaps_by_key.get(key) or canonical_by_key.get(key) or str(value or "").strip()


def bejerman_deposit_for_location(value) -> str:
    """Código de depósito Bejerman asociado a la ubicación física, o vacío."""
    return LOCATION_BEJERMAN_DEPOSITS.get(canonical_location_name(value), "")


def location_display_name(value) -> str:
    """Nombre para selectores, con el código Bejerman donde evita ambigüedad."""
    canonical = canonical_location_name(value)
    return LOCATION_DISPLAY_NAMES.get(canonical, canonical)


def is_rental_shelf_location_name(value) -> bool:
    """¿Ese nombre de ubicación es la estantería de alquiler?

    Se compara por forma y no por id: los ids de `locations` son datos de instancia
    (en producción la ubicación '-' es la 10) y el nombre convivió escrito de varias
    maneras, con y sin acento, con y sin "de" -las mismas que corrige
    LOCATION_NAME_REMAPS-.
    """
    text = location_name_key(value)
    return "estanter" in text and "alquiler" in text


def is_sepid_location_name(value) -> bool:
    """¿Ese nombre de ubicación corresponde al depósito Sepid (VAL)?"""
    return canonical_location_name(value) == "Sepid"
