"""Qué presupuesto frena una orden de salida.

La regla vive acá, en un módulo sin dependencias, porque la tienen que decir igual
cuatro lugares distintos: la view que emite la orden (`RemitoSalidaPdfView`), la cola
"Listos para liberar" (`ingresos_views.AprobadosYReparadosView`), su contador del
sidebar (`colas_views`) y el front (`web/src/lib/release-flow.js`). Cuando la regla
estaba escrita suelta en cada uno, alcanzaba con que uno mirara distinto para que un
equipo apareciera listo para salir sin nada aprobado.

Regla, en una línea: **al equipo de un cliente, sin garantía, no se le entrega una
reparación que nadie presupuestó.**

- `presupuestado` o `emitido` (sin respuesta) frenan siempre: se estaría entregando -y
  facturando- un trabajo que el cliente todavía no aprobó.
- `pendiente` (ni siquiera se presupuestó) frena cuando la OS quedó `reparado` y el
  trabajo es cobrable. El caso que lo motivó (OS 29639, 09/09/2026) es justo ése: un
  aspirador de un cliente, sin garantía, reparado y liberado el mismo día, con el
  presupuesto todavía sin hacer.
- Un presupuesto anulado o desconocido tampoco habilita una reparación cobrable.
- Lo que no se le cobra a nadie -garantías, equipos nuestros, alquileres, demos- sale
  igual, sin presupuestar. Y para la reparación cobrable que igual se regala está el
  "No aplica" del presupuesto, que es una decisión explícita y queda registrada.
"""

from .mg_codes import MG_CODE_RE, sql_is_mg_code

# Estados en los que la orden de salida ya se emitió: volver a pedirla es
# reimprimir, no liberar, así que los controles previos a liberar no aplican.
ESTADOS_SALIDA_YA_EMITIDA = frozenset(
    {
        "liberado",
        "entregado",
        "alquilado",
        "baja",
        "vendido_pendiente_entrega",
        "vendido_entregado",
    }
)

# Estados de taller con trabajo cobrable o a presupuestar. Al equipo de un cliente,
# tanto una reparación como un control/revisión técnica sin defecto requieren tener
# el presupuesto resuelto (aprobado, rechazado o marcado explícitamente como 'no_aplica').
# Solo `no_se_repara` devuelve el equipo sin trabajo cobrable cuando está pendiente.
ESTADO_CON_TRABAJO_COBRABLE = "reparado"
ESTADOS_CON_TRABAJO_COBRABLE = frozenset({"reparado", "controlado_sin_defecto"})

# Motivos por los que el equipo no es del cliente (parque de alquiler, demos) o no hay
# reparación que cobrar. El enum convivió con y sin tilde -hay un comando de corrección
# de etiquetas-, así que se aceptan las dos escrituras en vez de apostar a cuál quedó.
MOTIVOS_SIN_PRESUPUESTO = frozenset(
    {
        "baja alquiler",
        "reparación alquiler",
        "reparacion alquiler",
        "devolución demo",
        "devolucion demo",
        "cotización de equipo",
        "cotizacion de equipo",
    }
)

# Un número interno así es un equipo nuestro. `MG_CODE_RE` es el criterio único de NEXORA
# (`mg_codes`): con el tope viejo de 4 dígitos, un "MG 001678" contaba como equipo de cliente.


def _texto(value) -> str:
    return str(value or "").strip().lower()


def _flag(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "sí", "si", "yes"}
    return bool(value)


def es_equipo_propio(row) -> bool:
    """¿El equipo es del parque de MG BIO?

    Un MG vendido dejó de ser nuestro (`mg_estado='inactivo_venta'`): a partir de ahí
    su reparación se le cobra al cliente que lo compró, como cualquier otra.
    """
    row = row or {}
    if _texto(row.get("mg_estado")) == "inactivo_venta":
        return False
    return bool(MG_CODE_RE.match(str(row.get("numero_interno") or "").strip()))


def ingreso_requiere_presupuesto(row) -> bool:
    """¿Hay que presupuestarle este trabajo a alguien antes de entregarlo?"""
    row = row or {}
    if _flag(row.get("garantia_fabrica")) or _flag(row.get("garantia")):
        return False
    if _flag(row.get("garantia_reparacion")):
        return False
    if _texto(row.get("motivo")) in MOTIVOS_SIN_PRESUPUESTO:
        return False
    if es_equipo_propio(row):
        return False
    return True


def presupuesto_bloquea_salida(
    estado: str | None,
    presupuesto_estado: str | None,
    *,
    requiere_presupuesto: bool = True,
) -> bool:
    """¿Este presupuesto frena la orden de salida?

    `requiere_presupuesto` sale de `ingreso_requiere_presupuesto()`: pasarlo en False
    es lo que deja salir a las garantías, los equipos propios y los alquileres, que se
    reparan sin presupuestar. Sobre una OS cuya orden ya se emitió el corte no aplica:
    ahí se está reimprimiendo.
    """
    estado_norm = _texto(estado)
    if estado_norm in ESTADOS_SALIDA_YA_EMITIDA:
        return False
    presupuesto = _texto(presupuesto_estado)
    if presupuesto in {"presupuestado", "emitido"}:
        return True
    if (
        presupuesto not in {"aprobado", "rechazado", "no_aplica"}
        and requiere_presupuesto
        and estado_norm in ESTADOS_CON_TRABAJO_COBRABLE
    ):
        return True
    return False


def motivo_del_bloqueo(presupuesto_estado: str | None) -> str:
    """El texto del 409, que es lo que el operador tiene que hacer para destrabarlo."""
    if _texto(presupuesto_estado) in {"presupuestado", "emitido"}:
        return (
            "El presupuesto está emitido y todavía sin aprobar. "
            "Aprobalo o rechazalo antes de imprimir la orden de salida."
        )
    return (
        "Esta orden no tiene presupuesto y el equipo es de un cliente. "
        "Emitilo y hacelo aprobar, o marcá el presupuesto como \"No aplica\" si el "
        "trabajo no se cobra, antes de imprimir la orden de salida."
    )


def _tiene_columna_mg_estado() -> bool:
    """`devices.mg_estado` puede faltar en bases viejas (ver `_has_mg_schema`)."""
    from django.db import connection

    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT 1
              FROM information_schema.columns
             WHERE table_schema = ANY(current_schemas(FALSE))
               AND table_name='devices'
               AND column_name='mg_estado'
             LIMIT 1
            """
        )
        return cur.fetchone() is not None


def sql_presupuesto_bloquea_salida(ingreso_alias: str = "t", device_alias: str = "d") -> str:
    """El mismo corte, en SQL, para el WHERE de las colas.

    Devuelve una expresión booleana: la cola de listos para liberar la usa negada. Pide
    el join con `devices` porque el número interno es lo que distingue un equipo nuestro
    de uno del cliente.
    """
    i = ingreso_alias
    d = device_alias
    motivos = ", ".join(f"'{m}'" for m in sorted(MOTIVOS_SIN_PRESUPUESTO))
    # El COALESCE no es cosmético: el equipo de un cliente no tiene número interno, y
    # `NULL ~* ...` daría NULL, con lo que el `NOT (...)` de afuera dejaría de frenar
    # justamente el caso que este corte existe para frenar.
    propio = sql_is_mg_code(f"{d}.numero_interno")
    if _tiene_columna_mg_estado():
        # El MG vendido volvió a ser del cliente: su reparación se cobra y se presupuesta.
        propio = f"({propio} AND LOWER(BTRIM(COALESCE({d}.mg_estado,'activo'))) <> 'inactivo_venta')"
    cobrables = ", ".join(f"'{e}'" for e in sorted(ESTADOS_CON_TRABAJO_COBRABLE))
    return f"""(
        LOWER(BTRIM(COALESCE({i}.estado::text,''))) NOT IN ({_sql_estados_ya_emitida()})
        AND (
          LOWER(BTRIM(COALESCE({i}.presupuesto_estado::text,''))) IN ('presupuestado', 'emitido')
          OR (
            LOWER(BTRIM(COALESCE({i}.presupuesto_estado::text,''))) NOT IN ('aprobado', 'rechazado', 'no_aplica')
            AND LOWER(BTRIM(COALESCE({i}.estado::text,''))) IN ({cobrables})
            AND NOT COALESCE({i}.garantia_fabrica, FALSE)
            AND NOT COALESCE({i}.garantia_reparacion, FALSE)
            AND LOWER(BTRIM(COALESCE({i}.motivo::text,''))) NOT IN ({motivos})
            AND NOT {propio}
          )
        )
    )"""


def _sql_estados_ya_emitida() -> str:
    return ", ".join(f"'{estado}'" for estado in sorted(ESTADOS_SALIDA_YA_EMITIDA))
