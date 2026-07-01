from unittest import skipUnless

from django.db import connection
from django.test import SimpleTestCase, TestCase

from service.airsep_serial_fix import (
    airsep_fixed_serial,
    apply_local_corrections,
    build_bejerman_sql,
    load_airsep_candidates,
)


class AirSepSerialRuleTests(SimpleTestCase):
    def test_airsep_fixed_serial_rule(self):
        self.assertEqual(airsep_fixed_serial("5140930"), "N5140930")
        self.assertIsNone(airsep_fixed_serial("400132"))
        self.assertIsNone(airsep_fixed_serial("n5158095"))
        self.assertIsNone(airsep_fixed_serial("BUB0115121451"))


class AirSepBejermanSqlTests(SimpleTestCase):
    def test_bejerman_sql_skips_stockpar_target_conflicts_and_updates_history(self):
        sql = build_bejerman_sql(
            [{"old_partida": "5140930", "new_partida": "N5140930", "article_code": "1115007"}],
            include_history=True,
        )

        self.assertIn("dbo.StockPar", sql)
        self.assertIn("NOT EXISTS", sql)
        self.assertIn("#AirSepStockParConflicts", sql)
        self.assertIn("dbo.MovStock", sql)
        self.assertIn("mststp_Partida", sql)
        self.assertIn("dbo.SegDetV", sql)
        self.assertIn("sdvstp_Partida", sql)
        self.assertIn("dbo.SegDetC", sql)
        self.assertIn("sdcstp_Partida", sql)
        self.assertIn("#AirSepUnknownPartidaMatches", sql)


@skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL")
class AirSepLocalCorrectionTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with connection.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS customers (
                    id BIGSERIAL PRIMARY KEY,
                    razon_social TEXT
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
                    marca_id INTEGER,
                    nombre TEXT,
                    tipo_equipo TEXT,
                    variante TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    id BIGSERIAL PRIMARY KEY,
                    customer_id INTEGER,
                    marca_id INTEGER,
                    model_id INTEGER,
                    numero_serie TEXT,
                    numero_interno TEXT,
                    tipo_equipo TEXT,
                    variante TEXT,
                    garantia_vence DATE,
                    propietario TEXT,
                    n_de_control TEXT,
                    alquilado BOOLEAN DEFAULT FALSE,
                    alquiler_a TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ingresos (
                    id BIGSERIAL PRIMARY KEY,
                    device_id INTEGER,
                    fecha_ingreso TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    fecha_creacion TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS bejerman_sync_jobs (
                    id BIGSERIAL PRIMARY KEY,
                    sync_type TEXT,
                    ingreso_id INTEGER,
                    device_id INTEGER,
                    numero_serie TEXT NOT NULL DEFAULT '',
                    source_deposit TEXT DEFAULT 'STR',
                    target_deposit TEXT DEFAULT 'STL',
                    status TEXT DEFAULT 'pending'
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS delivery_orders (
                    id TEXT PRIMARY KEY,
                    order_number TEXT,
                    customer_name TEXT DEFAULT '',
                    delivery_type TEXT DEFAULT 'sale',
                    source_system TEXT DEFAULT 'nexora',
                    device_id INTEGER,
                    equipment_serial TEXT,
                    status TEXT DEFAULT 'pendiente_armado',
                    priority TEXT DEFAULT 'normal'
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS delivery_order_items (
                    id TEXT PRIMARY KEY,
                    order_id TEXT,
                    description TEXT DEFAULT '',
                    quantity NUMERIC DEFAULT 1,
                    device_id INTEGER,
                    partida TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS delivery_order_item_partidas (
                    id TEXT PRIMARY KEY,
                    order_item_id TEXT,
                    partida TEXT NOT NULL,
                    assigned_quantity NUMERIC DEFAULT 1
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS device_mg_events (
                    id BIGSERIAL PRIMARY KEY,
                    device_id INTEGER,
                    accion TEXT DEFAULT 'venta'
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS preventivo_planes (
                    id BIGSERIAL PRIMARY KEY,
                    device_id INTEGER,
                    activa BOOLEAN DEFAULT TRUE
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS preventivo_revision_items (
                    id BIGSERIAL PRIMARY KEY,
                    device_id INTEGER,
                    serie_snapshot TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS bejerman_article_mappings (
                    id BIGSERIAL PRIMARY KEY,
                    model_id INTEGER,
                    variante_norm TEXT DEFAULT '',
                    article_code TEXT
                )
                """
            )

    def setUp(self):
        super().setUp()
        with connection.cursor() as cur:
            for table in (
                "delivery_order_item_partidas",
                "delivery_order_items",
                "delivery_orders",
                "bejerman_sync_jobs",
                "preventivo_revision_items",
                "preventivo_planes",
                "device_mg_events",
                "ingresos",
                "devices",
                "bejerman_article_mappings",
                "models",
                "marcas",
                "customers",
            ):
                cur.execute(f"DELETE FROM {table}")

            cur.execute("INSERT INTO customers(razon_social) VALUES ('Cliente') RETURNING id")
            self.customer_id = int(cur.fetchone()[0])
            cur.execute("INSERT INTO marcas(nombre) VALUES ('AirSep') RETURNING id")
            self.brand_id = int(cur.fetchone()[0])
            cur.execute(
                "INSERT INTO models(marca_id, nombre, tipo_equipo) VALUES (%s, 'NewLife Elite', 'Concentrador') RETURNING id",
                [self.brand_id],
            )
            self.model_id = int(cur.fetchone()[0])
            cur.execute(
                "INSERT INTO bejerman_article_mappings(model_id, variante_norm, article_code) VALUES (%s, '', '1115007')",
                [self.model_id],
            )

    def _device(self, serial, internal=""):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO devices(customer_id, marca_id, model_id, numero_serie, numero_interno, tipo_equipo)
                VALUES (%s, %s, %s, %s, %s, 'Concentrador')
                RETURNING id
                """,
                [self.customer_id, self.brand_id, self.model_id, serial, internal],
            )
            return int(cur.fetchone()[0])

    def test_apply_local_updates_direct_candidates_and_merges_conflicts(self):
        direct_id = self._device("5140930", "MG 1719")
        source_id = self._device("5157952", "MG 2304")
        target_id = self._device("N5157952", "MG 9999")
        self._device("400132", "MG 0299")
        self._device("n5158095", "")
        self._device("BUB0115121451", "")

        with connection.cursor() as cur:
            cur.execute("INSERT INTO ingresos(device_id) VALUES (%s) RETURNING id", [source_id])
            ingreso_id = int(cur.fetchone()[0])
            cur.execute("INSERT INTO ingresos(device_id) VALUES (%s)", [target_id])
            cur.execute(
                """
                INSERT INTO bejerman_sync_jobs(sync_type, ingreso_id, device_id, numero_serie, source_deposit, target_deposit, status)
                VALUES ('stock_str_to_stl', %s, %s, '5157952', 'STR', 'STL', 'pending')
                """,
                [ingreso_id, source_id],
            )
            cur.execute(
                """
                INSERT INTO delivery_orders(id, order_number, customer_name, device_id, equipment_serial)
                VALUES ('ord-1', 'OE-1', 'Cliente', %s, '5157952')
                """,
                [source_id],
            )
            cur.execute(
                """
                INSERT INTO delivery_order_items(id, order_id, description, device_id, partida)
                VALUES ('item-1', 'ord-1', 'Equipo', %s, '5157952')
                """,
                [source_id],
            )
            cur.execute(
                """
                INSERT INTO delivery_order_item_partidas(id, order_item_id, partida)
                VALUES ('partida-1', 'item-1', '5157952')
                """
            )
            cur.execute("INSERT INTO device_mg_events(device_id) VALUES (%s)", [source_id])
            cur.execute("INSERT INTO preventivo_planes(device_id, activa) VALUES (%s, TRUE)", [source_id])
            cur.execute(
                "INSERT INTO preventivo_revision_items(device_id, serie_snapshot) VALUES (%s, '5157952')",
                [source_id],
            )

        candidates = load_airsep_candidates()
        self.assertEqual(sum(1 for item in candidates if item.action == "update"), 1)
        self.assertEqual(sum(1 for item in candidates if item.action == "merge"), 1)

        result = apply_local_corrections(candidates, apply=True)
        self.assertEqual(result["updated_devices"], 1)
        self.assertEqual(result["merged_devices"], 1)
        self.assertEqual(result["deleted_devices"], 1)

        with connection.cursor() as cur:
            cur.execute("SELECT numero_serie FROM devices WHERE id = %s", [direct_id])
            self.assertEqual(cur.fetchone()[0], "N5140930")

            cur.execute("SELECT COUNT(*) FROM devices WHERE id = %s", [source_id])
            self.assertEqual(cur.fetchone()[0], 0)

            cur.execute("SELECT numero_serie, numero_interno FROM devices WHERE id = %s", [target_id])
            self.assertEqual(cur.fetchone(), ("N5157952", "MG 9999"))

            for table in (
                "ingresos",
                "bejerman_sync_jobs",
                "delivery_orders",
                "delivery_order_items",
                "device_mg_events",
                "preventivo_planes",
                "preventivo_revision_items",
            ):
                cur.execute(f"SELECT COUNT(*) FROM {table} WHERE device_id = %s", [target_id])
                self.assertGreaterEqual(cur.fetchone()[0], 1, table)
                cur.execute(f"SELECT COUNT(*) FROM {table} WHERE device_id = %s", [source_id])
                self.assertEqual(cur.fetchone()[0], 0, table)

            cur.execute("SELECT numero_serie FROM bejerman_sync_jobs WHERE device_id = %s", [target_id])
            self.assertEqual(cur.fetchone()[0], "N5157952")
            cur.execute("SELECT equipment_serial FROM delivery_orders WHERE device_id = %s", [target_id])
            self.assertEqual(cur.fetchone()[0], "N5157952")
            cur.execute("SELECT partida FROM delivery_order_items WHERE device_id = %s", [target_id])
            self.assertEqual(cur.fetchone()[0], "N5157952")
            cur.execute("SELECT partida FROM delivery_order_item_partidas WHERE order_item_id = 'item-1'")
            self.assertEqual(cur.fetchone()[0], "N5157952")
            cur.execute("SELECT serie_snapshot FROM preventivo_revision_items WHERE device_id = %s", [target_id])
            self.assertEqual(cur.fetchone()[0], "N5157952")
