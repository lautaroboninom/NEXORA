import io
import json
from unittest import skipUnless

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase


@skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL")
class RepairDeliveryOrderManualSplitsTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
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
                  razon_social TEXT NOT NULL DEFAULT ''
                )
                """
            )
            cur.execute("CREATE TABLE IF NOT EXISTS devices (id BIGSERIAL PRIMARY KEY)")
            cur.execute("CREATE TABLE IF NOT EXISTS ingresos (id BIGSERIAL PRIMARY KEY)")
        call_command("apply_delivery_orders_schema", verbosity=0)

    def setUp(self):
        with connection.cursor() as cur:
            cur.execute("DELETE FROM bejerman_integration_attempts")
            cur.execute("DELETE FROM delivery_order_remito_item_allocations")
            cur.execute("DELETE FROM delivery_order_remito_links")
            cur.execute("DELETE FROM delivery_order_events")
            cur.execute("DELETE FROM delivery_order_item_partidas")
            cur.execute("DELETE FROM delivery_order_items")
            cur.execute("DELETE FROM delivery_orders")
            cur.execute("DELETE FROM bejerman_remito_groups")
        self.source_id = "do-repair-source"
        self.derived_id = "do-repair-derived"
        self._create_split()

    def _create_split(self):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO delivery_orders (
                  id, order_number, customer_name, delivery_type, company_key,
                  status, source_system, source_external_id
                )
                VALUES
                  (%s, 'OE-REPAIR-001', 'Cliente Repair', 'sale', 'SEPID',
                   'pendiente_armado', 'nexora', 'repair-source'),
                  (%s, 'OE-REPAIR-001-P2-ABC123', 'Cliente Repair', 'sale', 'SEPID',
                   'pendiente_armado', 'nexora-rss-split', 'repair-derived')
                """,
                [self.source_id, self.derived_id],
            )
            cur.execute(
                """
                INSERT INTO delivery_order_items (
                  id, order_id, article_code, description, quantity, sort_order
                )
                VALUES
                  ('doi-repair-1', %s, 'ART-1', 'Artículo 1', 1, 0),
                  ('doi-repair-2', %s, 'ART-2', 'Artículo 2', 1, 1),
                  ('doi-repair-3', %s, 'ART-3', 'Artículo 3', 1, 0)
                """,
                [self.source_id, self.source_id, self.derived_id],
            )
            metadata = json.dumps(
                {
                    "sourceOrderId": self.source_id,
                    "splitOrderIds": [self.source_id, self.derived_id],
                    "chunkCount": 2,
                }
            )
            cur.execute(
                """
                INSERT INTO delivery_order_events (order_id, event_type, metadata)
                VALUES
                  (%s, 'delivery_order_manual_split_for_remito', %s::jsonb),
                  (%s, 'delivery_order_manual_split_for_remito', %s::jsonb)
                """,
                [self.source_id, metadata, self.derived_id, metadata],
            )

    def _order_item_counts(self):
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT order_id, COUNT(*)
                  FROM delivery_order_items
                 WHERE order_id = ANY(%s)
                 GROUP BY order_id
                 ORDER BY order_id
                """,
                [[self.source_id, self.derived_id]],
            )
            return dict(cur.fetchall())

    def test_dry_run_reports_safe_case_without_writing(self):
        output = io.StringIO()

        call_command("repair_delivery_order_manual_splits", "--dry-run", stdout=output)

        self.assertIn("SEGURO: OE-REPAIR-001", output.getvalue())
        self.assertEqual(self._order_item_counts(), {self.derived_id: 1, self.source_id: 2})

    def test_apply_merges_items_in_sequence_and_is_idempotent(self):
        call_command("repair_delivery_order_manual_splits", "--apply", stdout=io.StringIO())

        with connection.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM delivery_orders WHERE id = %s", [self.derived_id])
            self.assertEqual(cur.fetchone()[0], 0)
            cur.execute(
                "SELECT id, sort_order FROM delivery_order_items WHERE order_id = %s ORDER BY sort_order",
                [self.source_id],
            )
            self.assertEqual(cur.fetchall(), [("doi-repair-1", 0), ("doi-repair-2", 1), ("doi-repair-3", 2)])
            cur.execute(
                """
                SELECT metadata ->> 'itemCount'
                  FROM delivery_order_events
                 WHERE order_id = %s
                   AND event_type = 'delivery_order_manual_split_repaired'
                """,
                [self.source_id],
            )
            self.assertEqual(cur.fetchone()[0], "3")

        second_output = io.StringIO()
        call_command("repair_delivery_order_manual_splits", "--apply", stdout=second_output)
        self.assertIn("YA REPARADO", second_output.getvalue())

    def test_apply_blocks_any_fragment_with_remito(self):
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE delivery_orders SET remito_number = 'RT R 00004-00000001' WHERE id = %s",
                [self.derived_id],
            )

        with self.assertRaises(CommandError):
            call_command(
                "repair_delivery_order_manual_splits",
                "--apply",
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

        self.assertEqual(self._order_item_counts(), {self.derived_id: 1, self.source_id: 2})
