from unittest import skipUnless
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from service.models import User
from service.views import ingresos_views, work_views


@skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL")
class GlobalSearchRemitosTests(TestCase):
    @classmethod
    def setUpClass(cls):
        with connection.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id BIGSERIAL PRIMARY KEY,
                    nombre TEXT,
                    email VARCHAR(320) UNIQUE,
                    hash_pw TEXT,
                    rol TEXT,
                    activo BOOLEAN DEFAULT TRUE
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS customers (
                    id BIGSERIAL PRIMARY KEY,
                    cod_empresa TEXT NULL,
                    razon_social TEXT NOT NULL,
                    cuit TEXT NULL,
                    telefono TEXT NULL,
                    email TEXT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS marcas (
                    id BIGSERIAL PRIMARY KEY,
                    nombre TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS models (
                    id BIGSERIAL PRIMARY KEY,
                    nombre TEXT NOT NULL,
                    tipo_equipo TEXT NULL,
                    variante TEXT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    id BIGSERIAL PRIMARY KEY,
                    customer_id INTEGER NOT NULL REFERENCES customers(id),
                    marca_id INTEGER NULL REFERENCES marcas(id),
                    model_id INTEGER NULL REFERENCES models(id),
                    numero_serie TEXT NULL,
                    numero_interno TEXT NULL,
                    variante TEXT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ingresos (
                    id BIGSERIAL PRIMARY KEY,
                    device_id INTEGER NOT NULL REFERENCES devices(id),
                    estado TEXT NOT NULL DEFAULT 'ingresado',
                    presupuesto_estado TEXT NULL,
                    motivo TEXT NOT NULL DEFAULT 'reparación',
                    fecha_ingreso TIMESTAMPTZ NULL,
                    fecha_creacion TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    equipo_variante TEXT NULL
                )
                """
            )
        call_command("apply_delivery_orders_schema", verbosity=0)
        super().setUpClass()
        call_command("apply_bejerman_ris_schema", verbosity=0)
        call_command("apply_user_permissions_schema", verbosity=0)

    @classmethod
    def setUpTestData(cls):
        cls.jefe = User.objects.create(
            nombre="Jefatura de prueba",
            email="jefatura@global-search-remitos.test",
            hash_pw="",
            rol="jefe",
            activo=True,
        )
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO customers(cod_empresa, razon_social) VALUES (%s, %s) RETURNING id",
                ["CLI-REM", "Cliente Remito Test"],
            )
            customer_id = int(cur.fetchone()[0])
            cur.execute("INSERT INTO devices(customer_id) VALUES (%s) RETURNING id", [customer_id])
            device_id = int(cur.fetchone()[0])

            cur.execute(
                "INSERT INTO ingresos(device_id, motivo) VALUES (%s, 'reparación') RETURNING id",
                [device_id],
            )
            cls.rda_ingreso_id = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO bejerman_ingreso_remitos(
                    ingreso_id, status, pdf_status, comprobante_tipo, comprobante_letra,
                    comprobante_pto_venta, comprobante_numero, remito_number
                ) VALUES (%s, 'generated', 'ready', 'RDA', 'R', '00004', '00004778', 'RDA R 00004-00004778')
                """,
                [cls.rda_ingreso_id],
            )

            cur.execute(
                "INSERT INTO ingresos(device_id, motivo) VALUES (%s, 'reparación') RETURNING id",
                [device_id],
            )
            cls.rss_ingreso_id = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO bejerman_remito_groups(
                    id, comprobante_tipo, comprobante_letra, comprobante_pto_venta, comprobante_numero,
                    remito_number, customer_code, customer_name, seller_code, payment_term_code,
                    operation_code, deposit_code, status
                ) VALUES (
                    'brg-test-remito-search', 'RSS', 'R', '00004', '00004500', 'RSS R 00004-00004500',
                    'CLI-REM', 'Cliente Remito Test', 'V1', 'CONTADO', 'OP1', 'DEP1', 'generated'
                )
                """
            )
            cur.execute(
                """
                INSERT INTO delivery_orders(id, order_number, customer_name, delivery_type, status, ingreso_id, bejerman_remito_group_id)
                VALUES ('do-test-remito-search', 'OE-TEST-1', 'Cliente Remito Test', 'service_release', 'facturado', %s, 'brg-test-remito-search')
                """,
                [cls.rss_ingreso_id],
            )
            cur.execute(
                """
                INSERT INTO bejerman_remito_groups(
                    id, comprobante_tipo, comprobante_letra, comprobante_pto_venta, comprobante_numero,
                    remito_number, customer_code, customer_name, seller_code, payment_term_code,
                    operation_code, deposit_code, status
                ) VALUES (
                    'brg-test-remito-search-2', 'RSS', 'R', '00004', '00004501', 'RSS R 00004-00004501',
                    'CLI-REM', 'Cliente Remito Test', 'V1', 'CONTADO', 'OP1', 'DEP1', 'reconciled'
                )
                """
            )
            cur.execute(
                """
                INSERT INTO delivery_order_remito_links(
                    id, order_id, remito_group_id, batch_id, sequence, expected_count, allocation_status
                ) VALUES
                    ('dorl-test-remito-search-1', 'do-test-remito-search', 'brg-test-remito-search',
                     'dorb-test-remito-search', 1, 2, 'external_unknown'),
                    ('dorl-test-remito-search-2', 'do-test-remito-search', 'brg-test-remito-search-2',
                     'dorb-test-remito-search', 2, 2, 'external_unknown')
                """
            )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.jefe)

    def test_busca_remito_individual_por_tipo_y_punto_de_venta_sin_ceros(self):
        resp = self.client.get("/api/busqueda/global/?q=RDA r 0004-")
        self.assertEqual(resp.status_code, 200)
        groups = {group["key"]: group["items"] for group in resp.data["groups"]}
        remito_ids = {item["id"] for item in groups["remitos"]}
        self.assertIn(self.rda_ingreso_id, remito_ids)

    def test_busca_remito_de_grupo_por_tipo_y_numero(self):
        resp = self.client.get("/api/busqueda/global/?q=RSS 4500")
        self.assertEqual(resp.status_code, 200)
        groups = {group["key"]: group["items"] for group in resp.data["groups"]}
        remito_ids = {item["id"] for item in groups["remitos"]}
        self.assertIn(self.rss_ingreso_id, remito_ids)

    def test_busca_segundo_remito_asociado_solo_por_vinculo_canonico(self):
        resp = self.client.get("/api/busqueda/global/?q=RSS 4501")

        self.assertEqual(resp.status_code, 200)
        groups = {group["key"]: group["items"] for group in resp.data["groups"]}
        remito_ids = {item["id"] for item in groups["remitos"]}
        self.assertIn(self.rss_ingreso_id, remito_ids)

    def test_busqueda_global_resuelve_ingreso_asociado_desde_articulo(self):
        with connection.cursor() as cur:
            cur.execute("UPDATE delivery_orders SET ingreso_id = NULL WHERE id = 'do-test-remito-search'")
            cur.execute(
                """
                INSERT INTO delivery_order_items(id, order_id, ingreso_id, description, quantity)
                VALUES ('doi-test-global-search', 'do-test-remito-search', %s, 'Equipo reparado', 1)
                """,
                [self.rss_ingreso_id],
            )

        resp = self.client.get("/api/busqueda/global/?q=RSS 4501")

        self.assertEqual(resp.status_code, 200)
        groups = {group["key"]: group["items"] for group in resp.data["groups"]}
        matches = [item for item in groups["remitos"] if item["id"] == self.rss_ingreso_id]
        self.assertEqual(len(matches), 1)

    def test_busqueda_global_resuelve_ingreso_asociado_desde_partida(self):
        with connection.cursor() as cur:
            cur.execute("UPDATE delivery_orders SET ingreso_id = NULL WHERE id = 'do-test-remito-search'")
            cur.execute(
                """
                INSERT INTO delivery_order_items(id, order_id, description, quantity)
                VALUES ('doi-test-global-search-partida', 'do-test-remito-search', 'Equipo reparado', 1)
                """
            )
            cur.execute(
                """
                INSERT INTO delivery_order_item_partidas(
                    id, order_item_id, ingreso_id, partida, assigned_quantity
                ) VALUES (
                    'doip-test-global-search', 'doi-test-global-search-partida', %s, 'NS-TEST', 1
                )
                """,
                [self.rss_ingreso_id],
            )

        resp = self.client.get("/api/busqueda/global/?q=RSS 4501")

        self.assertEqual(resp.status_code, 200)
        groups = {group["key"]: group["items"] for group in resp.data["groups"]}
        matches = [item for item in groups["remitos"] if item["id"] == self.rss_ingreso_id]
        self.assertEqual(len(matches), 1)

    def test_busqueda_global_no_duplica_ingreso_presente_en_orden_y_articulo(self):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO delivery_order_items(id, order_id, ingreso_id, description, quantity)
                VALUES ('doi-test-global-search-duplicate', 'do-test-remito-search', %s, 'Equipo reparado', 1)
                """,
                [self.rss_ingreso_id],
            )

        resp = self.client.get("/api/busqueda/global/?q=RSS 4501")

        self.assertEqual(resp.status_code, 200)
        groups = {group["key"]: group["items"] for group in resp.data["groups"]}
        matches = [item for item in groups["remitos"] if item["id"] == self.rss_ingreso_id]
        self.assertEqual(len(matches), 1)

    def test_service_sheet_expone_todos_los_rss_asociados(self):
        remitos = ingresos_views._service_release_remito_salida_for_ingreso(self.rss_ingreso_id)

        self.assertEqual(remitos, "RSS R 00004-00004500 / RSS R 00004-00004501")

    def test_service_sheet_resuelve_ingreso_asociado_desde_articulo(self):
        with connection.cursor() as cur:
            cur.execute("UPDATE delivery_orders SET ingreso_id = NULL WHERE id = 'do-test-remito-search'")
            cur.execute(
                """
                INSERT INTO delivery_order_items(id, order_id, ingreso_id, description, quantity)
                VALUES ('doi-test-remito-search', 'do-test-remito-search', %s, 'Equipo reparado', 1)
                """,
                [self.rss_ingreso_id],
            )

        remitos = ingresos_views._service_release_remito_salida_for_ingreso(self.rss_ingreso_id)

        self.assertEqual(remitos, "RSS R 00004-00004500 / RSS R 00004-00004501")

    def test_service_sheet_resuelve_ingreso_asociado_desde_partida(self):
        with connection.cursor() as cur:
            cur.execute("UPDATE delivery_orders SET ingreso_id = NULL WHERE id = 'do-test-remito-search'")
            cur.execute(
                """
                INSERT INTO delivery_order_items(id, order_id, description, quantity)
                VALUES ('doi-test-remito-search-partida', 'do-test-remito-search', 'Equipo reparado', 1)
                """
            )
            cur.execute(
                """
                INSERT INTO delivery_order_item_partidas(
                    id, order_item_id, ingreso_id, partida, assigned_quantity
                ) VALUES (
                    'doip-test-remito-search', 'doi-test-remito-search-partida', %s, 'NS-TEST', 1
                )
                """,
                [self.rss_ingreso_id],
            )

        remitos = ingresos_views._service_release_remito_salida_for_ingreso(self.rss_ingreso_id)

        self.assertEqual(remitos, "RSS R 00004-00004500 / RSS R 00004-00004501")

    def test_busqueda_global_conserva_fallback_escalar_sin_tabla_de_vinculos(self):
        table_exists = work_views._table_exists

        with patch.object(
            work_views,
            "_table_exists",
            side_effect=lambda table_name: False
            if table_name == "delivery_order_remito_links"
            else table_exists(table_name),
        ):
            resp = self.client.get("/api/busqueda/global/?q=RSS 4500")

        self.assertEqual(resp.status_code, 200)
        groups = {group["key"]: group["items"] for group in resp.data["groups"]}
        remito_ids = {item["id"] for item in groups["remitos"]}
        self.assertIn(self.rss_ingreso_id, remito_ids)

    def test_busqueda_global_fallback_escalar_resuelve_ingreso_desde_articulo(self):
        with connection.cursor() as cur:
            cur.execute("UPDATE delivery_orders SET ingreso_id = NULL WHERE id = 'do-test-remito-search'")
            cur.execute(
                """
                INSERT INTO delivery_order_items(id, order_id, ingreso_id, description, quantity)
                VALUES ('doi-test-global-search-legacy', 'do-test-remito-search', %s, 'Equipo reparado', 1)
                """,
                [self.rss_ingreso_id],
            )
        table_exists = work_views._table_exists

        with patch.object(
            work_views,
            "_table_exists",
            side_effect=lambda table_name: False
            if table_name == "delivery_order_remito_links"
            else table_exists(table_name),
        ):
            resp = self.client.get("/api/busqueda/global/?q=RSS 4500")

        self.assertEqual(resp.status_code, 200)
        groups = {group["key"]: group["items"] for group in resp.data["groups"]}
        matches = [item for item in groups["remitos"] if item["id"] == self.rss_ingreso_id]
        self.assertEqual(len(matches), 1)

    def test_service_sheet_conserva_fallback_escalar_sin_tabla_de_vinculos(self):
        table_exists = ingresos_views._table_exists

        with patch.object(
            ingresos_views,
            "_table_exists",
            side_effect=lambda table_name: False
            if table_name == "delivery_order_remito_links"
            else table_exists(table_name),
        ):
            remitos = ingresos_views._service_release_remito_salida_for_ingreso(self.rss_ingreso_id)

        self.assertEqual(remitos, "RSS R 00004-00004500")

    def test_cobranzas_no_considera_un_conjunto_de_remitos_parcial(self):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bejerman_remito_groups(
                    id, comprobante_tipo, comprobante_letra, comprobante_pto_venta, comprobante_numero,
                    remito_number, customer_code, customer_name, seller_code, payment_term_code,
                    operation_code, deposit_code, status
                ) VALUES
                    ('brg-test-billing-1', 'RT', 'R', '00004', '00004600', 'RT R 00004-00004600',
                     'CLI-REM', 'Cliente Remito Test', 'V1', 'CONTADO', 'OP1', 'DEP1', 'generated'),
                    ('brg-test-billing-2', 'RT', 'R', '00004', NULL, NULL,
                     'CLI-REM', 'Cliente Remito Test', 'V1', 'CONTADO', 'OP1', 'DEP1', 'pending')
                """
            )
            cur.execute(
                """
                INSERT INTO delivery_orders(
                    id, order_number, customer_name, delivery_type, status,
                    remito_number, bejerman_remito_group_id
                ) VALUES (
                    'do-test-remito-billing', 'OE-TEST-BILLING', 'Cliente Remito Test', 'sale',
                    'armado_pendiente_entrega', 'RT R 00004-00004600', 'brg-test-billing-1'
                )
                """
            )
            cur.execute(
                """
                INSERT INTO delivery_order_remito_links(
                    id, order_id, remito_group_id, batch_id, sequence, expected_count, allocation_status
                ) VALUES
                    ('dorl-test-billing-1', 'do-test-remito-billing', 'brg-test-billing-1',
                     'dorb-test-billing', 1, 2, 'known'),
                    ('dorl-test-billing-2', 'do-test-remito-billing', 'brg-test-billing-2',
                     'dorb-test-billing', 2, 2, 'known')
                """
            )

        pending_billing_sql = work_views._delivery_pending_billing_sql("o")
        with connection.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) FROM delivery_orders o WHERE o.id = %s AND {pending_billing_sql}",
                ["do-test-remito-billing"],
            )
            self.assertEqual(cur.fetchone()[0], 0)
            cur.execute(
                """
                UPDATE bejerman_remito_groups
                   SET status = 'generated',
                       remito_number = 'RT R 00004-00004601',
                       comprobante_numero = '00004601'
                 WHERE id = 'brg-test-billing-2'
                """
            )
            cur.execute(
                f"SELECT COUNT(*) FROM delivery_orders o WHERE o.id = %s AND {pending_billing_sql}",
                ["do-test-remito-billing"],
            )
            self.assertEqual(cur.fetchone()[0], 1)

    def test_cobranzas_conserva_orden_legacy_sin_vinculos(self):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO delivery_orders(
                    id, order_number, customer_name, delivery_type, status, remito_number
                ) VALUES (
                    'do-test-remito-billing-legacy', 'OE-TEST-BILLING-LEGACY',
                    'Cliente Remito Test', 'sale', 'armado_pendiente_entrega',
                    'RT R 00004-00004599'
                )
                """
            )

        pending_billing_sql = work_views._delivery_pending_billing_sql("o")
        with connection.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) FROM delivery_orders o WHERE o.id = %s AND {pending_billing_sql}",
                ["do-test-remito-billing-legacy"],
            )
            self.assertEqual(cur.fetchone()[0], 1)

    def test_termino_sin_relacion_no_devuelve_remitos(self):
        resp = self.client.get("/api/busqueda/global/?q=XYZQWERTY")
        self.assertEqual(resp.status_code, 200)
        groups = {group["key"]: group["items"] for group in resp.data["groups"]}
        self.assertEqual(groups["remitos"], [])
