"""La observación de la partida en Bejerman es el MG del equipo.

En SEPID ese campo (`StockPar.stp_Obs`, 20 caracteres) se usa para anotar qué MG corresponde a
cada N/S: "MG 7112". NEXORA lo llenaba con su propia referencia ("NEXORA-OS-29148",
"NEXORA 00029148", "RTA 00004-...", "STOCKFIX ...") y el 11/09/2026 había 614 partidas así en
SBDASEP, 153 de ellas con el MG anotado en la fila de otro depósito.

Bejerman escribe la observación cuando el movimiento crea la fila de la partida en ese depósito;
un movimiento sobre una fila que ya existe no la pisa (verificado en SBDASEP: los ENT de NEXORA
sobre filas con MG las dejaron intactas). Lo que se manda tiene que ser correcto desde el primer
movimiento, porque después no se corrige solo.

Sólo los equipos nuestros llevan el MG. Un equipo de cliente va sin observación, y eso incluye un MG
que se vendió: el MG queda como histórico en NEXORA para la trazabilidad, no en Bejerman (pedido del
usuario, 11/09/2026). Tampoco van referencias de NEXORA: esas viajan en `Comprobante_IdOrigen`.
"""
from __future__ import annotations

from typing import Any
import re

from django.db import connection

from .mg_codes import normalize_mg_code

PARTIDA_OBSERVACION_MAX = 20


def observacion_partida(*candidatos: Any, mg_estado: Any = "") -> str:
    """El primer candidato que es un código interno, como lo escribe NEXORA ("MG 0042").

    `mg_estado` es el de `devices`: con `inactivo_venta` el equipo ya es del cliente.
    """
    if str(mg_estado or "").strip().lower() == "inactivo_venta":
        return ""
    for candidato in candidatos:
        codigo = normalize_mg_code(candidato)
        if codigo:
            return codigo[:PARTIDA_OBSERVACION_MAX]
    return ""


def _rows(sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
    with connection.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def _observacion_de_fila(row: tuple[Any, ...]) -> str:
    numero_interno, n_de_control, mg_estado = row
    return observacion_partida(numero_interno, n_de_control, mg_estado=mg_estado)


def observacion_partida_de_equipo(device_id: Any) -> str:
    if not device_id:
        return ""
    rows = _rows("SELECT numero_interno, n_de_control, mg_estado FROM devices WHERE id = %s", [int(device_id)])
    return _observacion_de_fila(rows[0]) if rows else ""


def observacion_partida_de_ingreso(ingreso_id: Any) -> str:
    if not ingreso_id:
        return ""
    rows = _rows(
        """
        SELECT d.numero_interno, d.n_de_control, d.mg_estado
          FROM ingresos t
          JOIN devices d ON d.id = t.device_id
         WHERE t.id = %s
        """,
        [int(ingreso_id)],
    )
    return _observacion_de_fila(rows[0]) if rows else ""


def observacion_partida_de_serie(partida: Any) -> str:
    """El MG del equipo con esa serie, para los movimientos que sólo conocen la partida.

    La serie se compara como en `uq_devices_ns_tipo_norm` (sin espacios ni guiones). Puede haber
    más de un equipo con la misma serie (el humidificador se carga con la del CPAP): si todos los
    que son nuestros dicen el mismo MG, es ese; si dicen MG distintos, no se adivina.
    """
    clave = re.sub(r"[\s-]+", "", str(partida or "")).upper()
    if not clave:
        return ""
    rows = _rows(
        """
        SELECT numero_interno, n_de_control, mg_estado
          FROM devices
         WHERE UPPER(REPLACE(REPLACE(numero_serie, ' ', ''), '-', '')) = %s
        """,
        [clave],
    )
    codigos = {codigo for codigo in (_observacion_de_fila(row) for row in rows) if codigo}
    return codigos.pop() if len(codigos) == 1 else ""
