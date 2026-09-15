"""Qué es un número interno de MG BIO (MG, NM, NV, CE) y cómo se compara.

Es un criterio único para todo NEXORA. Hasta el 14/09/2026 había al menos seis variantes
sueltas: hasta 4 dígitos en la regla de liberación y en el front, hasta 6 en el sync de
Bejerman, un espacio obligatorio en `sync_alquileres`... Así, el mismo equipo contaba como
propio en una pantalla y como de cliente en otra. El caso real eran los equipos cargados con
ceros a la izquierda ("MG 001678", 13 en producción), que la variante de 4 dígitos no
reconocía.

El criterio: prefijo MG, NM, NV o CE sin distinguir mayúsculas, pegado al número o separado
por espacios, con o sin ceros a la izquierda y hasta 6 dígitos. "MG1678", "mg 1678",
"MG  1678" y "MG 001678" son el mismo equipo.

No depende de Django, para que lo puedan importar `release_gate`, las views, los comandos y el
sync de Bejerman. Su espejo en el front es `MG_CODE_RE` de `web/src/lib/ui-helpers.js`.
"""

import re

MG_PREFIXES = ("MG", "NM", "NV", "CE")

# Sirve tal cual para Python y para el `~*` de Postgres. El grupo 2 son los dígitos sin los
# ceros de adelante.
MG_CODE_PATTERN = r"^(MG|NM|NV|CE)\s*0*(\d{1,6})$"
MG_CODE_RE = re.compile(MG_CODE_PATTERN, re.IGNORECASE)

# Clave de comparación en Postgres: mayúsculas, un espacio y sin ceros adelante ("MG 1678").
# No es el texto que se guarda (`normalize_mg_code` rellena a 4: "MG 0012"), pero agrupa igual:
# dos valores dan la misma clave si y sólo si son el mismo número. Reemplaza a la vieja
# expresión con `LPAD('\2',4,'0')`, que rellenaba la cadena literal «\2» y no los dígitos.
MG_KEY_SQL = r"REGEXP_REPLACE(UPPER(BTRIM({col})), '^(MG|NM|NV|CE)\s*0*(\d{{1,6}})$', '\1 \2')"


def is_mg_code(value) -> bool:
    return bool(MG_CODE_RE.match(str(value or "").strip()))


def normalize_mg_code(value):
    """Cómo lo guarda y lo muestra NEXORA ("MG 0042"), o `None` si no es un código interno."""
    match = MG_CODE_RE.match(str(value or "").strip())
    if not match:
        return None
    return f"{match.group(1).upper()} {match.group(2).zfill(4)}"


def sql_is_mg_code(column: str) -> str:
    """El mismo criterio como condición de Postgres sobre una columna.

    El COALESCE no es cosmético: `NULL ~* ...` da NULL, y un `NOT (...)` de afuera dejaría de
    distinguir el equipo sin número interno.
    """
    return f"BTRIM(COALESCE({column},'')) ~* '{MG_CODE_PATTERN}'"


def sql_mg_key(column: str) -> str:
    """Clave de comparación de una columna o de un parámetro (`sql_mg_key("%s")`)."""
    return MG_KEY_SQL.format(col=column)
