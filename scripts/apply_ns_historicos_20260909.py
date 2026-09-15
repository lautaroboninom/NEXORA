"""Conciliación histórica acotada en SBDASEP; lectura por defecto.

Sólo cambia sdvstp_Partida de tres devoluciones. No modifica el stock,
las cantidades, las descripciones, las cabeceras ni los comprobantes emitidos.
Exige el respaldo previo completo y valida la consulta real dentro de la
transacción. --rollback revierte exclusivamente estos tres campos.
"""
import argparse
import copy
import json
from decimal import Decimal
from pathlib import Path

from samtronic_sql import Samtronic


ROOT = Path(__file__).resolve().parents[1]
BACKUP = ROOT / 'docs/reportes/ns_historico_cinco_casos_antes_20260909.json'
CHANGES = {
    111088: ('N5143451', '5143451'),
    110836: ('MZJ2P00005', 'MZJP00005'),
    110843: ('MZJ2P00078', 'MZJP00078'),
}
ALIASES = (
    '5143451', 'N5143451', 'MZJP00005', 'MZJ2P00005',
    'MZJP00078', 'MZJ2P00078', 'MZJP00096', 'MZJ2P00096', 'MZJ5S117719',
)


def literal(value):
    return "N'" + value.replace("'", "''") + "'"


def unordered_equal(left, right):
    # Igualdad numérica de Python: el JSON de SQL puede devolver 0.0 y el
    # respaldo 0. No se compara la representación textual de esos números.
    remaining = list(right)
    for row in left:
        if row not in remaining:
            return False
        remaining.remove(row)
    return not remaining


def report_sql():
    sql = (ROOT / 'sql/ops/ns_testigo_MZJ5S121193_lectura_20260908.sql').read_text(encoding='utf-8')
    old = "SDVSTP_PARTIDA='MZJ5S121193'"
    if sql.count(old) != 1:
        raise RuntimeError('Cambió la consulta base: revisar antes de continuar.')
    return sql.replace(old, 'SDVSTP_PARTIDA IN (' + ','.join(map(literal, ALIASES)) + ')')


def snapshot(server):
    aliases = ','.join(map(literal, ALIASES))
    filters = {
        'SegDetV': f'sdvstp_Partida IN ({aliases})',
        'SegCabV': f'EXISTS (SELECT 1 FROM SegDetV d WHERE d.sdvemp_Codigo=scvemp_Codigo AND d.sdvsuc_Cod=scvsuc_Cod AND d.sdvscv_ID=scv_ID AND d.sdvstp_Partida IN ({aliases}))',
        'StockPar': f'stp_Partida IN ({aliases})',
        'MovStock': f'mststp_Partida IN ({aliases})',
    }
    result = {}
    for table, where in filters.items():
        rows = server.query(f'SELECT (SELECT * FROM dbo.{table} WHERE {where} FOR JSON PATH, INCLUDE_NULL_VALUES) AS datos', database='SBDASEP')
        result[table] = json.loads(rows[0]['datos'])
    return result


def assert_snapshot(actual, expected):
    for table in expected:
        if not unordered_equal(actual[table], expected[table]):
            raise RuntimeError(f'{table} difiere del estado esperado. No continuar automáticamente.')


def report_matches(actual, expected):
    def normalize(rows):
        return [
            {k: Decimal(str(v)) if k == 'SDV_CANTUM1' else v.rstrip() for k, v in row.items()}
            for row in rows
        ]
    return unordered_equal(normalize(actual), normalize(expected))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--apply', action='store_true')
    action.add_argument('--rollback', action='store_true')
    args = parser.parse_args()
    backup = json.loads(BACKUP.read_text(encoding='utf-8'))
    before = backup['datos']
    corrected = copy.deepcopy(before)
    for row in corrected['SegDetV']:
        if row['sdv_ID'] in CHANGES:
            old, new = CHANGES[row['sdv_ID']]
            assert row['sdvemp_Codigo'] == 'SEP' and row['sdvsuc_Cod'] == ' '
            assert row['sdvstp_Partida'] == old and row['sdv_CantUM1'] == -1
            row['sdvstp_Partida'] = new

    projected = []
    old_aliases = {value[0] for value in CHANGES.values()}
    new_aliases = {value[1] for value in CHANGES.values()}
    for row in backup['consulta_antes']:
        if row['SDVSTP_PARTIDA'] in old_aliases:
            continue
        row = dict(row)
        if row['SDVSTP_PARTIDA'] in new_aliases:
            row['SDV_CANTUM1'] = '0'
        projected.append(row)

    source = corrected if args.rollback else before
    target = before if args.rollback else corrected
    expected_report = backup['consulta_antes'] if args.rollback else projected
    with Samtronic() as server:
        current = snapshot(server)
        is_target = all(unordered_equal(current[t], target[t]) for t in target)
        if not is_target:
            assert_snapshot(current, source)
        current_report = server.query(report_sql(), database='SBDASEP', timeout=90)
        required_before = expected_report if is_target else (projected if args.rollback else backup['consulta_antes'])
        if not report_matches(current_report, required_before):
            raise RuntimeError('La consulta actual difiere de la evidencia respaldada.')

        result = {'base': 'SBDASEP', 'modo': 'reversión' if args.rollback else 'aplicación' if args.apply else 'lectura', 'ya_en_destino': is_target, 'consulta_antes': current_report, 'consulta_esperada': expected_report}
        if (args.apply or args.rollback) and not is_target:
            # El JSON recién leído debe coincidir con el respaldo; se vuelve a
            # comparar bajo bloqueo dentro de la transacción antes del UPDATE.
            guards = []
            ids = ','.join(str(r['sdv_ID']) for r in source['SegDetV'])
            header_ids = ','.join(str(r['scv_ID']) for r in source['SegCabV'])
            for table, field, key_ids in [('SegDetV', 'sdv_ID', ids), ('SegCabV', 'scv_ID', header_ids)]:
                selection = f'SELECT * FROM dbo.{table} WHERE {field} IN ({key_ids}) ORDER BY {field} FOR JSON PATH, INCLUDE_NULL_VALUES'
                raw = server.query(f'SELECT ({selection}) AS datos', database='SBDASEP')[0]['datos']
                if not unordered_equal(json.loads(raw), source[table]):
                    raise RuntimeError(f'{table} cambió durante la lectura.')
                locked = selection.replace(f'dbo.{table} WHERE', f'dbo.{table} WITH (UPDLOCK,HOLDLOCK) WHERE')
                guards.append(f"IF CONVERT(varbinary(max),({locked})) <> CONVERT(varbinary(max),{literal(raw)}) THROW 51001, 'Cambió {table}: operación cancelada.', 1;")

            updates = []
            for row_id, (old, new) in CHANGES.items():
                if args.rollback:
                    old, new = new, old
                updates.append(f"UPDATE dbo.SegDetV SET sdvstp_Partida={literal(new)} WHERE sdvemp_Codigo='SEP' AND sdvsuc_Cod=' ' AND sdv_ID={row_id} AND CONVERT(varbinary(max),sdvstp_Partida)=CONVERT(varbinary(max),CONVERT(varchar(30),{literal(old)}));\nIF @@ROWCOUNT <> 1 THROW 51002, 'No se actualizó exactamente un renglón.', 1;")

            # Crear #Esperado sólo en tempdb. No se altera la estructura del ERP.
            values = []
            for r in expected_report:
                keys = ['SCVCLI_COD', 'SCVCLI_RAZSOC', 'SDVART_CODGEN', 'SDV_DESC', 'SDVSTP_PARTIDA']
                values.append('SELECT ' + ','.join(literal(r[k]) for k in keys) + ',' + str(Decimal(r['SDV_CANTUM1'])))
            base = (ROOT / 'sql/ops/corregir_ns_historicos_20260909.sql').read_text(encoding='utf-8')
            query = report_sql().strip()
            marker = ')) SELECT SCVCLI_COD,SCVCLI_RAZSOC,SDVART_CODGEN,SDV_DESC,SDVSTP_PARTIDA,SUM(SDV_CANTUM1) AS SDV_CANTUM1 FROM'
            if query.count(marker) != 1:
                raise RuntimeError('No se reconoce el SELECT final de la consulta.')
            query = query.replace(marker, marker.replace(' FROM', ' INTO #Actual FROM'))
            replacements = {'-- {{GUARDS}}': '\n'.join(guards), '-- {{UPDATES}}': '\n'.join(updates), '-- {{REPORT}}': query + ';', '-- {{EXPECTED}}': '\nUNION ALL\n'.join(values)}
            for key, value in replacements.items():
                if base.count(key) != 1:
                    raise RuntimeError('Plantilla SQL inválida.')
                base = base.replace(key, value)
            result['transaccion'] = server.query(base, database='SBDASEP', timeout=120)

        if args.apply or args.rollback:
            verified = snapshot(server)
            assert_snapshot(verified, target)
            final_report = server.query(report_sql(), database='SBDASEP', timeout=90)
            if not report_matches(final_report, expected_report):
                raise RuntimeError('La verificación posterior de la consulta no coincide. Revisar antes de nuevas acciones.')
            result['filas_completas_verificadas'] = {table: len(rows) for table, rows in verified.items()}
            result['unicos_campos_modificados'] = [
                {'sdv_ID': row_id, 'campo': 'sdvstp_Partida', 'antes': new if args.rollback else old, 'despues': old if args.rollback else new}
                for row_id, (old, new) in CHANGES.items()
            ]
            result['consulta_despues'] = final_report
            result['verificado'] = True
            result['tablas_sin_cambios'] = ['SegCabV', 'StockPar', 'MovStock']
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
