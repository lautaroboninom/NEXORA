"""Restituye MZJ2P en seis renglones comerciales de NATIVA, sin tocar stock.

Lectura por defecto; --apply aplica y --rollback revierte esta segunda etapa.
La aclaración del usuario corrige el sentido de la conciliación inicial.
"""
import argparse
import copy
import json
from decimal import Decimal

from apply_ns_historicos_20260909 import (
    ROOT, assert_snapshot, literal, report_matches, report_sql, snapshot,
    unordered_equal,
)
from samtronic_sql import Samtronic


CHANGES = {
    46171: ('MZJP00005', 'MZJ2P00005', 1),
    110836: ('MZJP00005', 'MZJ2P00005', -1),
    85357: ('MZJP00078', 'MZJ2P00078', 1),
    110843: ('MZJP00078', 'MZJ2P00078', -1),
    47953: ('MZJP00096', 'MZJ2P00096', 1),
    95207: ('MZJP00096', 'MZJ2P00096', -1),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--apply', action='store_true')
    group.add_argument('--rollback', action='store_true')
    args = parser.parse_args()
    backup = json.loads((ROOT / 'docs/reportes/ns_historico_nativa_prefijo_antes_20260909.json').read_text(encoding='utf-8'))
    before = backup['datos']
    after = copy.deepcopy(before)
    found = set()
    for row in after['SegDetV']:
        if row['sdv_ID'] not in CHANGES:
            continue
        old, new, qty = CHANGES[row['sdv_ID']]
        assert row['sdvemp_Codigo'] == 'SEP' and row['sdvsuc_Cod'] == ' '
        assert row['sdvstp_Partida'] == old and row['sdv_CantUM1'] == qty
        assert row['sdvart_CodGen'].strip() == '1112004'
        header = next(h for h in after['SegCabV'] if h['scv_ID'] == row['sdvscv_ID'] and h['scvemp_Codigo'] == row['sdvemp_Codigo'] and h['scvsuc_Cod'] == row['sdvsuc_Cod'])
        assert header['scvcli_Cod'] == 'NATIVA' and header['scv_ActStock']
        row['sdvstp_Partida'] = new
        found.add(row['sdv_ID'])
    assert found == set(CHANGES)

    # Proyección exclusiva de estas tres series y cliente NATIVA.
    mapping = {old: new for old, new, _ in CHANGES.values()}
    projected = {}
    columns = ['SCVCLI_COD', 'SCVCLI_RAZSOC', 'SDVART_CODGEN', 'SDV_DESC', 'SDVSTP_PARTIDA']
    for original in backup['consulta_antes']:
        row = dict(original)
        if row['SCVCLI_COD'].strip() == 'NATIVA':
            row['SDVSTP_PARTIDA'] = mapping.get(row['SDVSTP_PARTIDA'], row['SDVSTP_PARTIDA'])
        key = tuple(row[c].rstrip() for c in columns)
        if key not in projected:
            projected[key] = {c: row[c] for c in columns}
            projected[key]['SDV_CANTUM1'] = Decimal(0)
        projected[key]['SDV_CANTUM1'] += Decimal(original['SDV_CANTUM1'])
    projected = [{**r, 'SDV_CANTUM1': str(r['SDV_CANTUM1'])} for r in projected.values()]
    source, target = (after, before) if args.rollback else (before, after)
    old_report, expected_report = (projected, backup['consulta_antes']) if args.rollback else (backup['consulta_antes'], projected)

    with Samtronic() as server:
        current = snapshot(server)
        done = all(unordered_equal(current[t], target[t]) for t in target)
        assert_snapshot(current, target if done else source)
        current_report = server.query(report_sql(), database='SBDASEP', timeout=90)
        if not report_matches(current_report, expected_report if done else old_report):
            raise RuntimeError('Cambió el resultado de la consulta: revisar antes de aplicar.')
        result = {'base': 'SBDASEP', 'modo': 'reversión' if args.rollback else 'aplicación' if args.apply else 'lectura', 'ya_en_destino': done, 'consulta_antes': current_report, 'consulta_esperada': expected_report}
        if (args.apply or args.rollback) and not done:
            guards = []
            for table, field in [('SegDetV', 'sdv_ID'), ('SegCabV', 'scv_ID')]:
                ids = ','.join(str(r[field]) for r in source[table])
                selection = f'SELECT * FROM dbo.{table} WHERE {field} IN ({ids}) ORDER BY {field} FOR JSON PATH, INCLUDE_NULL_VALUES'
                raw = server.query(f'SELECT ({selection}) AS datos', database='SBDASEP')[0]['datos']
                if not unordered_equal(json.loads(raw), source[table]):
                    raise RuntimeError(f'{table} cambió durante la lectura.')
                locked = selection.replace(f'dbo.{table} WHERE', f'dbo.{table} WITH (UPDLOCK,HOLDLOCK) WHERE')
                guards.append(f"IF CONVERT(varbinary(max),({locked})) <> CONVERT(varbinary(max),{literal(raw)}) THROW 51001, 'Cambió {table}: operación cancelada.', 1;")
            updates = []
            for row_id, (old, new, _) in CHANGES.items():
                if args.rollback:
                    old, new = new, old
                updates.append(f"UPDATE dbo.SegDetV SET sdvstp_Partida={literal(new)} WHERE sdvemp_Codigo='SEP' AND sdvsuc_Cod=' ' AND sdv_ID={row_id} AND CONVERT(varbinary(max),sdvstp_Partida)=CONVERT(varbinary(max),CONVERT(varchar(30),{literal(old)}));\nIF @@ROWCOUNT <> 1 THROW 51002, 'No se actualizó exactamente un renglón.', 1;")
            values = ['SELECT ' + ','.join(literal(r[c]) for c in columns) + ',' + str(Decimal(r['SDV_CANTUM1'])) for r in expected_report]
            base = (ROOT / 'sql/ops/corregir_ns_historicos_20260909.sql').read_text(encoding='utf-8')
            query = report_sql().strip()
            marker = ')) SELECT SCVCLI_COD,SCVCLI_RAZSOC,SDVART_CODGEN,SDV_DESC,SDVSTP_PARTIDA,SUM(SDV_CANTUM1) AS SDV_CANTUM1 FROM'
            assert query.count(marker) == 1
            query = query.replace(marker, marker.replace(' FROM', ' INTO #Actual FROM'))
            replacements = {'-- {{GUARDS}}': '\n'.join(guards), '-- {{UPDATES}}': '\n'.join(updates), '-- {{REPORT}}': query + ';', '-- {{EXPECTED}}': '\nUNION ALL\n'.join(values), '3 AS renglones_corregidos': '6 AS renglones_corregidos'}
            for key, value in replacements.items():
                assert base.count(key) == 1
                base = base.replace(key, value)
            result['transaccion'] = server.query(base, database='SBDASEP', timeout=120)
        if args.apply or args.rollback:
            verified = snapshot(server)
            assert_snapshot(verified, target)
            final_report = server.query(report_sql(), database='SBDASEP', timeout=90)
            if not report_matches(final_report, expected_report):
                raise RuntimeError('La verificación posterior no coincide: revisar antes de otra acción.')
            result['consulta_despues'] = final_report
            result['filas_completas_verificadas'] = {table: len(rows) for table, rows in verified.items()}
            result['unicos_campos_modificados'] = [{'sdv_ID': rid, 'campo': 'sdvstp_Partida', 'antes': new if args.rollback else old, 'despues': old if args.rollback else new} for rid, (old, new, _) in CHANGES.items()]
            result['tablas_sin_cambios'] = ['SegCabV', 'StockPar', 'MovStock']
            result['verificado'] = True
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
