"""Corrige la descripción histórica de la devolución EDU en SBDASEP.

Lectura por defecto; --apply actualiza sólo SegDetV.sdv_Desc del sdv_ID 111344.
No modifica artículo maestro, serie, cantidad, stock, cabecera ni movimiento.
"""
import argparse
import copy
import json
from pathlib import Path

from samtronic_sql import Samtronic

ROOT = Path(__file__).resolve().parents[1]
SDV_ID = 111344
OLD = 'Concentrador de oxígeno Longfian JAY-5AW   '
NEW = 'Concentrador de oxígeno Longfian JAY-5Q'


def rows(server):
    sql = """
            SELECT (SELECT c.scv_ID,c.scvemp_Codigo,c.scvsuc_Cod,c.scv_FEmision,c.scv_FecMod,
                   c.scvusu_Codigo,c.scvcli_Cod,c.scvtop_Cod,c.scv_ActStock,
                   d.sdv_ID,d.sdv_NReng,d.sdvemp_Codigo,d.sdvsuc_Cod,d.sdvart_CodGen,d.sdvstp_Partida,
                   d.sdv_CantUM1,d.sdv_Desc,d.sdvdep_Cod,d.sdvmst_ID
              FROM dbo.SegCabV c
              JOIN dbo.SegDetV d ON d.sdvemp_Codigo=c.scvemp_Codigo
               AND d.sdvsuc_Cod=c.scvsuc_Cod AND d.sdvscv_ID=c.scv_ID
             WHERE d.sdv_ID=111344
             FOR JSON PATH, INCLUDE_NULL_VALUES) AS datos
    """
    raw = server.query(sql, database='SBDASEP')[0]['datos']
    return json.loads(raw)


def report(server):
    sql = """
    SELECT c.scvcli_Cod AS SCVCLI_COD,c.scvcli_RazSoc AS SCVCLI_RAZSOC,
           d.sdvart_CodGen AS SDVART_CODGEN,d.sdv_Desc AS SDV_DESC,
           d.sdvstp_Partida AS SDVSTP_PARTIDA,SUM(d.sdv_CantUM1) AS SDV_CANTUM1
      FROM dbo.SegCabV c
      JOIN dbo.SegDetV d ON d.sdvemp_Codigo=c.scvemp_Codigo
       AND d.sdvsuc_Cod=c.scvsuc_Cod AND d.sdvscv_ID=c.scv_ID
      JOIN dbo.Articulos a ON a.art_CodGen=d.sdvart_CodGen
       AND a.art_CodEle1=d.sdvart_CodEle1 AND a.art_CodEle2=d.sdvart_CodEle2
       AND a.art_CodEle3=d.sdvart_CodEle3
      JOIN dbo.Defi1Art da ON da.da1_Cod=a.artda1_Cod
     WHERE c.scvcli_Cod LIKE '%EDU%'
       AND d.sdvart_CodGen LIKE '1115005%'
       AND d.sdvstp_Partida=N'MZJ5S79784'
       AND c.scvtop_Cod LIKE '%ALQ%'
       AND d.sdv_CantUM1<>0 AND c.scv_ActStock=1
       AND da.da1_Desc LIKE '%Equipa%'
     GROUP BY c.scvcli_Cod,c.scvcli_RazSoc,d.sdvart_CodGen,d.sdv_Desc,d.sdvstp_Partida
    """
    return server.query(sql, database='SBDASEP', timeout=90)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    with Samtronic() as server:
        before = rows(server)
        if len(before) != 1:
            raise RuntimeError(f'Guardia: se esperaba un solo renglón y llegaron {len(before)}')
        row = before[0]
        expected = {'sdv_ID': str(SDV_ID), 'sdvemp_Codigo': 'SEP', 'scvcli_Cod': '   EDU',
                    'sdvart_CodGen': '1115005 ', 'sdvstp_Partida': 'MZJ5S79784',
                    'sdv_CantUM1': '-1.0', 'sdv_Desc': OLD, 'scv_ActStock': 'True'}
        if row.get('sdv_Desc') == NEW:
            current_report = report(server)
            if len(current_report) != 1 or current_report[0].get('SDV_CANTUM1') != '0':
                raise RuntimeError('El renglón ya dice JAY-5Q pero la consulta no está en cero.')
            print(json.dumps({'base': 'SBDASEP', 'modo': 'lectura', 'ya_aplicado': True,
                              'despues': before, 'consulta_despues': current_report,
                              'cambio': {'sdv_ID': SDV_ID, 'campo': 'sdv_Desc', 'antes': OLD, 'despues': NEW},
                              'verificado': True}, ensure_ascii=False, indent=2))
            return
        for key, value in expected.items():
            actual = row.get(key)
            if str(actual) != value:
                raise RuntimeError(f'Guardia no coincide {key}: {row.get(key)!r} != {value!r}')
        before_report = report(server)
        if not any(r.get('SDV_DESC') == OLD and r.get('SDV_CANTUM1') == '-1' for r in before_report):
            raise RuntimeError('La consulta no muestra el desbalance esperado; no se escribe.')
        result = {'base': 'SBDASEP', 'modo': 'aplicación' if args.apply else 'lectura',
                  'antes': before, 'consulta_antes': before_report,
                  'cambio': {'sdv_ID': SDV_ID, 'campo': 'sdv_Desc', 'antes': OLD, 'despues': NEW}}
        if args.apply:
            sql = f"""
            SET NOCOUNT ON; SET XACT_ABORT ON; SET LOCK_TIMEOUT 10000;
            IF DB_NAME() <> 'SBDASEP' THROW 51000,'Base incorrecta.',1;
            BEGIN TRANSACTION;
            UPDATE dbo.SegDetV WITH (UPDLOCK,HOLDLOCK)
               SET sdv_Desc=N'{NEW}'
             WHERE sdvemp_Codigo='SEP' AND sdvsuc_Cod=' ' AND sdv_ID={SDV_ID}
               AND sdvart_CodGen='1115005 ' AND sdvstp_Partida=N'MZJ5S79784'
               AND sdv_CantUM1=-1 AND sdv_Desc=N'{OLD}';
            IF @@ROWCOUNT<>1 THROW 51001,'No se actualizó exactamente el renglón EDU.',1;
            COMMIT TRANSACTION;
            SELECT 'COMMIT' AS estado, SYSDATETIMEOFFSET() AS fecha_servidor;
            """
            result['transaccion'] = server.query(sql, database='SBDASEP', timeout=90)
            after = rows(server)
            after_report = report(server)
            if after[0].get('sdv_Desc') != NEW:
                raise RuntimeError('La descripción posterior no es JAY-5Q.')
            if len(after_report) != 1 or after_report[0].get('SDV_CANTUM1') != '0' or after_report[0].get('SDV_DESC') != NEW:
                raise RuntimeError('La consulta posterior no quedó en cero.')
            result.update({'despues': after, 'consulta_despues': after_report, 'verificado': True})
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
