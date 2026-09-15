"""Copia acotada de NS x cliente de SEP a SEPP; preview por defecto.

Sólo configura la vista en manager. No emite comprobantes ni altera stock.
El SQL valida la base de prueba, compara cada tabla antes del COMMIT y rechaza
colisiones. Una repetición verifica la copia existente sin volver a insertarla.
"""
import argparse
import json
from pathlib import Path

from samtronic_sql import Samtronic


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    path = Path(__file__).resolve().parents[1] / 'sql/ops/copy_ns_cliente_sep_sepp_20260908.sql'
    sql = path.read_text(encoding='utf-8')
    marker = 'DECLARE @Apply bit = 0;'
    if sql.count(marker) != 1:
        raise RuntimeError('No se reconoce la guarda de aplicación del SQL.')
    if args.apply:
        sql = sql.replace(marker, 'DECLARE @Apply bit = 1;')
    with Samtronic() as server:
        result = server.query(sql, database='manager', timeout=60)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
