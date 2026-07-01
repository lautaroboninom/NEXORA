from django.test import SimpleTestCase

from service.bejerman_remito_currency_fix import (
    RemitoCurrencyTarget,
    build_bejerman_currency_fix_sql,
    default_test_database,
    target_from_row,
)


class BejermanRemitoCurrencyFixTests(SimpleTestCase):
    def test_target_from_row_parsea_remito_explicito(self):
        target = target_from_row(
            {
                "group_id": "brg-1",
                "company_key": "SEPID",
                "comprobante_tipo": "RT",
                "comprobante_letra": "R",
                "comprobante_pto_venta": "00004",
                "remito_number": "RT R 00004-00000059",
                "customer_code": "QUALI",
                "customer_name": "QUALIMED SA",
                "order_numbers": "OE-00059",
                "generated_at": "2026-06-30 09:41:00",
                "commercial_exchange_rate": "1495",
            }
        )

        self.assertEqual(target.tipo, "RT")
        self.assertEqual(target.letra, "R")
        self.assertEqual(target.punto, "00004")
        self.assertEqual(target.numero, "00000059")
        self.assertEqual(target.remito_number, "RT R 00004-00000059")
        self.assertEqual(target.customer_code, "QUALI")

    def test_default_database_es_prueba_por_empresa(self):
        self.assertEqual(default_test_database("SEPID"), "SBDPSEP")
        self.assertEqual(default_test_database("MGBIO"), "SBDPMGBI")

    def test_sql_es_dry_run_y_actualiza_solo_cabecera_moneda(self):
        sql = build_bejerman_currency_fix_sql(
            [
                RemitoCurrencyTarget(
                    company_key="SEPID",
                    group_id="brg-1",
                    remito_number="RT R 00004-00000059",
                    tipo="RT",
                    letra="R",
                    punto="00004",
                    numero="00000059",
                    customer_code="QUALI",
                    customer_name="QUALIMED SA",
                    order_numbers="OE-00059",
                    generated_at="2026-06-30",
                    commercial_exchange_rate="1495",
                )
            ],
            database="SBDPSEP",
            apply=False,
            backup_name="NEXORA_BKP_TEST",
        )

        self.assertIn("USE [SBDPSEP];", sql)
        self.assertIn("DECLARE @Apply bit = 0;", sql)
        self.assertIn("INTO dbo.NEXORA_BKP_TEST", sql)
        self.assertIn("SET scvmon_codigo = @LocalCurrencyCode", sql)
        self.assertIn("scvmtca_codigo = @LocalExchangeType", sql)
        self.assertIn("COALESCE(NULLIF(LTRIM(RTRIM(COALESCE(c.scvmtca_codigo, ''))), ''), '') <>", sql)
        self.assertIn("scvmcot_cotiza = 1", sql)
        self.assertIn("'RT'", sql)
        self.assertIn("'00000059'", sql)
        self.assertNotIn("DELETE", sql.upper())
