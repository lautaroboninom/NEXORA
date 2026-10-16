from io import StringIO

from django.core.management import call_command
from django.db import connection
from django.test import TestCase

from service.management.commands.load_technical_fixed_prices import FIXED_PRICES
from service.management.commands.load_technical_hourly_rates import adjusted_rates
from service.views.helpers import q, exec_void


SEED_NAME = 'technical_pricing_20261016_v1'


class TechnicalPricingSeedTests(TestCase):
    def setUp(self):
        self.assertEqual(connection.settings_dict['NAME'],'test_nexora_technical_dev')
        exec_void('DELETE FROM technical_data_seeds WHERE nombre=%s',[SEED_NAME])
        exec_void('DELETE FROM technical_labor_rates')
        for name,_ in FIXED_PRICES:
            exec_void('UPDATE technical_work_catalog SET precio_final=NULL WHERE LOWER(BTRIM(nombre))=LOWER(%s)',[name])

    def apply(self):
        call_command('apply_technical_interventions_schema',stdout=StringIO())

    def rates(self):
        return q('SELECT tipo_equipo,marca,primera_hora,hora_adicional,hora_desde_septima FROM technical_labor_rates ORDER BY tipo_equipo')

    def test_initial_seed_persists_confirmed_values_without_demo_data(self):
        counts=q('''SELECT (SELECT COUNT(*) FROM users) AS users,
            (SELECT COUNT(*) FROM ingresos) AS ingresos,
            (SELECT COUNT(*) FROM quotes) AS quotes''',one=True)
        parts=q('SELECT id,stock_on_hand FROM catalogo_repuestos ORDER BY id')
        documents=q("SELECT quote_id,encode(pdf,'hex') AS pdf FROM technical_quote_documents ORDER BY quote_id")
        for name,_ in FIXED_PRICES:
            exec_void("UPDATE technical_work_catalog SET nombre=nombre || ' (fixture anterior)' WHERE nombre=%s",[name])
        self.apply()
        expected=[]
        for name,first,following,seventh,aliases in adjusted_rates():
            expected.extend(dict(tipo_equipo=alias.lower(),marca='Inogen' if name=='Inogen' else None,
                primera_hora=first,hora_adicional=following,hora_desde_septima=seventh) for alias in aliases)
        self.assertCountEqual(self.rates(),expected)
        for name,amount in FIXED_PRICES:
            row=q('SELECT precio_final FROM technical_work_catalog WHERE nombre=%s',[name],one=True)
            self.assertEqual(str(row['precio_final']),amount)
        self.assertIsNotNone(q('SELECT applied_at FROM technical_data_seeds WHERE nombre=%s',[SEED_NAME],one=True))
        self.assertEqual(counts,q('''SELECT (SELECT COUNT(*) FROM users) AS users,
            (SELECT COUNT(*) FROM ingresos) AS ingresos,
            (SELECT COUNT(*) FROM quotes) AS quotes''',one=True))
        self.assertEqual(parts,q('SELECT id,stock_on_hand FROM catalogo_repuestos ORDER BY id'))
        self.assertEqual(documents,q("SELECT quote_id,encode(pdf,'hex') AS pdf FROM technical_quote_documents ORDER BY quote_id"))

    def test_first_seed_preserves_existing_prices_and_brand_configuration(self):
        exec_void("""INSERT INTO technical_labor_rates(tipo_equipo,marca,primera_hora,hora_adicional)
            VALUES ('cpap',NULL,999,444),('referencia-inogen-propia','inogen',888,333)""")
        name,_=FIXED_PRICES[0]
        exec_void('UPDATE technical_work_catalog SET precio_final=777,activo=FALSE WHERE nombre=%s',[name])
        exec_void('UPDATE technical_billing_config SET diagnostico_limpieza=12345 WHERE id=1')
        self.apply()
        row=q("SELECT primera_hora,hora_adicional,hora_desde_septima FROM technical_labor_rates WHERE tipo_equipo='cpap'",one=True)
        self.assertEqual(row,dict(primera_hora=999,hora_adicional=444,hora_desde_septima=None))
        self.assertIsNone(q("SELECT tipo_equipo FROM technical_labor_rates WHERE tipo_equipo='inogen'",one=True))
        self.assertEqual(q("SELECT primera_hora FROM technical_labor_rates WHERE marca='inogen'",one=True)['primera_hora'],888)
        self.assertEqual(q('SELECT precio_final,activo FROM technical_work_catalog WHERE nombre=%s',[name],one=True),dict(precio_final=777,activo=False))
        self.assertEqual(q('SELECT diagnostico_limpieza FROM technical_billing_config WHERE id=1',one=True)['diagnostico_limpieza'],12345)

    def test_repeated_preparation_preserves_edits_retired_prices_and_deleted_rates(self):
        self.apply()
        exec_void("UPDATE technical_labor_rates SET primera_hora=222,hora_desde_septima=NULL WHERE tipo_equipo='cpap'")
        exec_void("DELETE FROM technical_labor_rates WHERE tipo_equipo='aspirador'")
        exec_void('UPDATE technical_work_catalog SET precio_final=NULL,activo=FALSE WHERE nombre=%s',[FIXED_PRICES[0][0]])
        exec_void('UPDATE technical_work_catalog SET precio_final=123 WHERE nombre=%s',[FIXED_PRICES[1][0]])
        rates=self.rates()
        works=q('SELECT id,nombre,precio_final,activo FROM technical_work_catalog ORDER BY id')
        marker=q('SELECT applied_at FROM technical_data_seeds WHERE nombre=%s',[SEED_NAME],one=True)
        self.apply();self.apply()
        self.assertEqual(rates,self.rates())
        self.assertEqual(works,q('SELECT id,nombre,precio_final,activo FROM technical_work_catalog ORDER BY id'))
        self.assertEqual(marker,q('SELECT applied_at FROM technical_data_seeds WHERE nombre=%s',[SEED_NAME],one=True))
