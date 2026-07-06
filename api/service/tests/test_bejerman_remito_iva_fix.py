from django.test import SimpleTestCase

from service.bejerman_remito_iva_fix import (
    REPAIR_CORRECTION_MODE,
    SALE_CORRECTION_MODE,
    RemitoIvaTarget,
    build_bejerman_iva_fix_sql,
    correction_mode_from_delivery_types,
    default_production_database,
    default_test_database,
    target_from_row,
)


class BejermanRemitoIvaFixTests(SimpleTestCase):
    def test_target_from_row_detecta_venta_por_articulo(self):
        target = target_from_row(
            {
                "group_id": "brg-1",
                "company_key": "SEPID",
                "comprobante_tipo": "RT",
                "comprobante_letra": "R",
                "comprobante_pto_venta": "00004",
                "remito_number": "RT R 00004-00004706",
                "customer_code": "INSU",
                "customer_name": "INSUMED S.R.L.",
                "order_numbers": "OE-00035",
                "delivery_types": "sale",
                "generated_at": "2026-06-26 14:39:41",
            }
        )

        self.assertEqual(target.tipo, "RT")
        self.assertEqual(target.punto, "00004")
        self.assertEqual(target.numero, "00004706")
        self.assertEqual(target.correction_mode, SALE_CORRECTION_MODE)

    def test_service_release_es_reparacion_21(self):
        self.assertEqual(correction_mode_from_delivery_types("service_release"), REPAIR_CORRECTION_MODE)
        self.assertEqual(correction_mode_from_delivery_types("sale"), SALE_CORRECTION_MODE)

    def test_default_database_es_prueba_salvo_pedido_productivo(self):
        self.assertEqual(default_test_database("SEPID"), "SBDPSEP")
        self.assertEqual(default_test_database("MGBIO"), "SBDPMGBI")
        self.assertEqual(default_production_database("SEPID"), "SBDASEP")
        self.assertEqual(default_production_database("MGBIO"), "SBDAMGBI")

    def test_sql_es_dry_run_y_recalcula_detalle_y_totales(self):
        sql = build_bejerman_iva_fix_sql(
            [
                RemitoIvaTarget(
                    company_key="SEPID",
                    group_id="brg-1",
                    remito_number="RT R 00004-00004706",
                    tipo="RT",
                    letra="R",
                    punto="00004",
                    numero="00004706",
                    customer_code="INSU",
                    customer_name="INSUMED S.R.L.",
                    order_numbers="OE-00035",
                    delivery_types="sale",
                    correction_mode=SALE_CORRECTION_MODE,
                    generated_at="2026-06-26",
                )
            ],
            database="SBDPSEP",
            apply=False,
            backup_name="NEXORA_BKP_TEST_IVA",
        )

        self.assertIn("USE [SBDPSEP];", sql)
        self.assertIn("DECLARE @Apply bit = 0;", sql)
        self.assertIn("ARTICULO_SIN_IVA_VENTA", sql)
        self.assertIn("sdvtiv_Cod = lt.target_tiv_cod", sql)
        self.assertIn("DELETE tv", sql)
        self.assertIn("INSERT INTO SegTotV", sql)
        self.assertIn("'01', 21", sql)
        self.assertIn("'03', 10.5", sql)
        self.assertIn("'RT'", sql)
        self.assertIn("'00004706'", sql)
        self.assertIn("Dry-run: @Apply=0", sql)

    def test_sql_de_reparacion_fuerza_iva_21(self):
        sql = build_bejerman_iva_fix_sql(
            [
                RemitoIvaTarget(
                    company_key="SEPID",
                    group_id="brg-rss",
                    remito_number="RSS R 00004-00004733",
                    tipo="RSS",
                    letra="R",
                    punto="00004",
                    numero="00004733",
                    customer_code="CAT",
                    customer_name="MEDICINA CATAN S.A.",
                    order_numbers="OS-29450",
                    delivery_types="service_release",
                    correction_mode=REPAIR_CORRECTION_MODE,
                    generated_at="2026-07-01",
                )
            ],
            database="SBDPSEP",
            apply=True,
            backup_name="NEXORA_BKP_TEST_IVA",
        )

        self.assertIn("DECLARE @Apply bit = 1;", sql)
        self.assertIn(f"WHEN m.correction_mode = '{REPAIR_CORRECTION_MODE}' THEN '01'", sql)
        self.assertIn("'RSS'", sql)
        self.assertIn("'00004733'", sql)
