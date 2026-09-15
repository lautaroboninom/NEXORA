"""Diagnóstico de sólo lectura: diferencias de texto en la fuente actual de N/S.

Usa una sola conexión SSH para ambas empresas. No modifica datos ni reconstruye
filas eliminadas. La normalización local sólo busca candidatos; los grupos SQL
devueltos por el servidor determinan si la diferencia afecta la agrupación.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal
import json
import unicodedata

from samtronic_sql import Samtronic


FIELDS = {
    "cliente": "c.scvcli_Cod",
    "articulo": "d.sdvart_CodGen",
    "partida": "d.sdvstp_Partida",
    "descripcion": "d.sdv_Desc",
}
HEX_FIELDS = ",\n".join(
    f"CONVERT(varchar(max), CONVERT(varbinary(max), CONVERT(nvarchar(max), {column})), 2) AS {name}_hex"
    for name, column in FIELDS.items()
)
SQL = f"""
SELECT c.scv_ID, d.sdv_ID, CONVERT(char(10),c.scv_FEmision,23) AS fecha,
       d.sdv_CantUM1 AS cantidad,
       DENSE_RANK() OVER (ORDER BY c.scvcli_Cod,d.sdvart_CodGen,d.sdvstp_Partida) AS identidad_sql,
       DENSE_RANK() OVER (ORDER BY c.scvcli_Cod,d.sdvart_CodGen,d.sdvstp_Partida,d.sdv_Desc) AS grupo_sql,
       {HEX_FIELDS}
  FROM dbo.SegCabV c
  JOIN dbo.SegDetV d
    ON d.sdvemp_Codigo=c.scvemp_Codigo
   AND d.sdvsuc_Cod=c.scvsuc_Cod
   AND d.sdvscv_ID=c.scv_ID
 WHERE c.scvtop_Cod='ALQ' AND d.sdv_TipoIt='A'
 ORDER BY c.scv_ID,d.sdv_ID
"""


def normalized(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value).casefold()
    cleaned = "".join(
        char for char in decomposed
        if not unicodedata.combining(char) and unicodedata.category(char) != "Cf"
    )
    return " ".join(cleaned.split())


def show(value: object) -> None:
    print(json.dumps(value, ensure_ascii=True, default=str))


def inspect(rows: list[dict]) -> None:
    identities = defaultdict(list)
    normalized_identities = defaultdict(list)
    chars = Counter()
    for row in rows:
        for name in FIELDS:
            value = bytes.fromhex(row.pop(name + "_hex") or "").decode("utf-16-le")
            row[name] = value
            for char in set(value):
                if (char.isspace() and char != " ") or unicodedata.category(char).startswith("C"):
                    chars[(name, f"U+{ord(char):04X}")] += 1
            if "  " in value.strip():
                chars[(name, "doble_espacio_interno")] += 1
            if value != value.rstrip(" "):
                chars[(name, "espacio_final")] += 1
        row["cantidad"] = Decimal(row["cantidad"])
        identities[row["identidad_sql"]].append(row)
        key = tuple(normalized(row[name]) for name in ("cliente", "articulo", "partida"))
        if key[2]:
            normalized_identities[key].append(row)

    multiple_bytes = []
    multiple_sql = []
    conflicts = []
    for group in identities.values():
        if len({r["descripcion"] for r in group}) <= 1:
            continue
        multiple_bytes.append(group)
        sql_balances = defaultdict(Decimal)
        for row in group:
            sql_balances[row["grupo_sql"]] += row["cantidad"]
        if len(sql_balances) > 1:
            multiple_sql.append(group)
            if max(sql_balances.values()) > 0 and min(sql_balances.values()) < 0:
                conflicts.append(group)
    key_variants = [g for g in normalized_identities.values() if len({r["identidad_sql"] for r in g}) > 1]

    show({"filas": len(rows), "identidades": len(identities),
          "identidades_descripcion_distinta_bytes": len(multiple_bytes),
          "identidades_descripcion_distinta_sql": len(multiple_sql),
          "conflictos_saldos_por_descripcion": len(conflicts),
          "identidades_distintas_sql_iguales_normalizadas": len(key_variants),
          "caracteres_especiales_por_filas": [(*k, v) for k, v in sorted(chars.items())]})

    for label, groups in (("diferencias_que_sql_separa", multiple_sql),
                          ("ejemplos_diferencias_que_sql_iguala", [g for g in multiple_bytes if g not in multiple_sql][:5]),
                          ("claves_parecidas", key_variants)):
        for group in groups:
            variants = defaultdict(list)
            for row in group:
                key = tuple(row[f] for f in FIELDS) + (row["grupo_sql"],)
                variants[key].append(row)
            show({"categoria": label, "variantes": [
                {**dict(zip(FIELDS, key[:4])), "grupo_sql": key[4],
                 "saldo": sum((r["cantidad"] for r in members), Decimal()),
                 "lineas": [{k: r[k] for k in ("scv_ID", "sdv_ID", "fecha", "cantidad")} for r in members]}
                for key, members in variants.items()
            ]})
    witness = [r for r in rows if r["partida"].strip() == "MZJ5S121748"]
    if witness:
        show({"testigo_actual": witness})


def main() -> None:
    with Samtronic() as server:
        for database in ("SBDASEP", "SBDAMGBI"):
            show({"base": database})
            show({"reglas_comparacion": server.query("""
                SELECT c.name AS columna,c.collation_name
                  FROM sys.columns c
                 WHERE c.object_id=OBJECT_ID('dbo.SegDetV')
                   AND c.name IN ('sdv_Desc','sdvstp_Partida','sdvart_CodGen')
            """, database=database)})
            inspect(server.query(SQL, database=database))


if __name__ == "__main__":
    main()
