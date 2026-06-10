import datetime as dt
import re
from unittest import skipUnless

from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from service.activity_audit import classify_read_path, should_audit_read_request
from service.models import User


class ActivityAuditRulesTest(SimpleTestCase):
    def test_audita_apertura_de_hoja_y_excluye_busquedas(self):
        self.assertTrue(should_audit_read_request("/api/ingresos/123/"))
        self.assertFalse(should_audit_read_request("/api/busqueda/global/", {"q": "placa"}))

    def test_clasifica_movimientos_de_repuestos(self):
        data = classify_read_path("/api/repuestos/movimientos/")
        self.assertEqual(data["activity_type"], "apertura_movimientos")
        self.assertEqual(data["title"], "Abrió movimientos de repuestos")


@skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL")
class MetricasActividadTecnicosAPITest(TestCase):
    @classmethod
    def setUpClass(cls):
        with connection.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                  nombre TEXT,
                  email TEXT UNIQUE,
                  hash_pw TEXT,
                  rol TEXT,
                  activo BOOLEAN NOT NULL DEFAULT TRUE
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_permission_overrides (
                  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                  user_id INTEGER NOT NULL,
                  permission_code TEXT NOT NULL,
                  effect TEXT NOT NULL,
                  updated_by INTEGER NULL,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS customers (
                  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                  razon_social TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS marcas (
                  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                  nombre TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS models (
                  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                  marca_id INTEGER NULL,
                  nombre TEXT NOT NULL,
                  tipo_equipo TEXT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS devices (
                  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                  customer_id INTEGER NULL,
                  marca_id INTEGER NULL,
                  model_id INTEGER NULL,
                  numero_serie TEXT NULL,
                  numero_interno TEXT NULL,
                  variante TEXT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ingresos (
                  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                  device_id INTEGER NULL,
                  estado TEXT,
                  presupuesto_estado TEXT,
                  fecha_ingreso TIMESTAMPTZ NULL,
                  fecha_creacion TIMESTAMPTZ NULL DEFAULT CURRENT_TIMESTAMP,
                  asignado_a INTEGER NULL,
                  descripcion_problema TEXT NULL,
                  trabajos_realizados TEXT NULL,
                  comentarios TEXT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS quotes (
                  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                  ingreso_id INTEGER NOT NULL,
                  version_num INTEGER NOT NULL DEFAULT 1,
                  origen_quote_id INTEGER NULL,
                  estado TEXT NOT NULL DEFAULT 'pendiente',
                  fecha_emitido TIMESTAMPTZ NULL,
                  fecha_aprobado TIMESTAMPTZ NULL,
                  fecha_rechazado TIMESTAMPTZ NULL,
                  rechazo_comentario TEXT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS quote_items (
                  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                  quote_id INTEGER NOT NULL,
                  repuesto_id INTEGER NULL,
                  descripcion TEXT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS catalogo_repuestos (
                  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                  codigo TEXT NULL,
                  nombre TEXT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS repuestos_movimientos (
                  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                  repuesto_id INTEGER NOT NULL,
                  tipo TEXT NOT NULL,
                  qty NUMERIC(12,2) NOT NULL,
                  stock_prev NUMERIC(12,2) NULL,
                  stock_new NUMERIC(12,2) NULL,
                  ref_tipo TEXT NULL,
                  ref_id INTEGER NULL,
                  nota TEXT NULL,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  created_by INTEGER NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ingreso_events (
                  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                  ingreso_id INTEGER NOT NULL,
                  de_estado TEXT NULL,
                  a_estado TEXT NOT NULL,
                  usuario_id INTEGER NULL,
                  ts TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  comentario TEXT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_log (
                  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                  ts TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  user_id INTEGER NULL,
                  role TEXT NULL,
                  method TEXT NULL,
                  path TEXT NULL,
                  ip TEXT NULL,
                  user_agent TEXT NULL,
                  status_code INTEGER NULL,
                  body JSONB NULL
                )
                """
            )
            cur.execute("CREATE SCHEMA IF NOT EXISTS audit")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS audit.change_log (
                  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                  ts TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  user_id INTEGER NULL,
                  user_role TEXT NULL,
                  table_name TEXT NOT NULL,
                  record_id INTEGER NOT NULL,
                  column_name TEXT NOT NULL,
                  old_value TEXT NULL,
                  new_value TEXT NULL,
                  ingreso_id INTEGER NULL
                )
                """
            )
            for ddl in (
                "ALTER TABLE models ADD COLUMN IF NOT EXISTS tipo_equipo TEXT NULL",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS numero_interno TEXT NULL",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS variante TEXT NULL",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS presupuesto_estado TEXT",
                "ALTER TABLE quotes ADD COLUMN IF NOT EXISTS version_num INTEGER NOT NULL DEFAULT 1",
                "ALTER TABLE quotes ADD COLUMN IF NOT EXISTS origen_quote_id INTEGER NULL",
                "ALTER TABLE quotes ADD COLUMN IF NOT EXISTS fecha_rechazado TIMESTAMPTZ NULL",
                "ALTER TABLE quotes ADD COLUMN IF NOT EXISTS rechazo_comentario TEXT NULL",
            ):
                try:
                    cur.execute(ddl)
                except Exception:
                    connection.rollback()
                    pass

        call_command("apply_user_permissions_schema", verbosity=0)
        call_command("apply_metricas_actividad_schema", verbosity=0)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        User.objects.all().delete()
        cls.jefe = User.objects.create(
            nombre="Jefe Métricas",
            email="jefe-metricas@example.com",
            hash_pw="",
            rol="jefe",
            activo=True,
        )
        cls.jefe_veedor = User.objects.create(
            nombre="Jefe Veedor Métricas",
            email="jefe-veedor-metricas@example.com",
            hash_pw="",
            rol="jefe_veedor",
            activo=True,
        )
        cls.tecnico = User.objects.create(
            nombre="Técnico Métricas",
            email="tecnico-metricas@example.com",
            hash_pw="",
            rol="tecnico",
            activo=True,
        )
        cls.admin = User.objects.create(
            nombre="Admin Métricas",
            email="admin-metricas@example.com",
            hash_pw="",
            rol="admin",
            activo=True,
        )
        cls.recepcion = User.objects.create(
            nombre="Recepción Métricas",
            email="recepcion-metricas@example.com",
            hash_pw="",
            rol="recepcion",
            activo=True,
        )

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self._clear_seed_data()
        self._seed_activity_data()

    def _clear_seed_data(self):
        with connection.cursor() as cur:
            cur.execute("DELETE FROM audit.change_log")
            cur.execute("DELETE FROM audit_log")
            cur.execute("DELETE FROM ingreso_events")
            cur.execute("DELETE FROM repuestos_movimientos")
            cur.execute("DELETE FROM quote_items")
            cur.execute("DELETE FROM quotes")
            cur.execute("DELETE FROM ingresos")
            cur.execute("DELETE FROM devices")
            cur.execute("DELETE FROM models")
            cur.execute("DELETE FROM marcas")
            cur.execute("DELETE FROM customers")
            cur.execute("DELETE FROM catalogo_repuestos")

    def _insert_returning_id(self, sql, params):
        try:
            with connection.cursor() as cur:
                cur.execute(sql, params)
                return int(cur.fetchone()[0])
        except Exception as exc:
            if connection.vendor != "postgresql" or 'null value in column "id"' not in str(exc).lower():
                raise
            connection.rollback()
            sql_no_returning = re.sub(r"\s+RETURNING\s+id\s*$", "", sql.strip(), flags=re.IGNORECASE | re.DOTALL)
            table_match = re.search(r"INSERT\s+INTO\s+([a-zA-Z0-9_.]+)\s*\(", sql_no_returning, flags=re.IGNORECASE)
            if not table_match:
                raise
            table_name = table_match.group(1)
            with connection.cursor() as cur:
                cur.execute(f"SELECT COALESCE(MAX(id), 0) + 1 FROM {table_name}")
                next_id = int(cur.fetchone()[0])
            sql_with_id = re.sub(
                r"INSERT\s+INTO\s+([a-zA-Z0-9_.]+)\s*\(\s*",
                rf"INSERT INTO {table_name} (id, ",
                sql_no_returning,
                count=1,
                flags=re.IGNORECASE,
            )
            sql_with_id = re.sub(r"VALUES\s*\(\s*", "VALUES (%s, ", sql_with_id, count=1, flags=re.IGNORECASE)
            with connection.cursor() as cur:
                cur.execute(sql_with_id, [next_id, *params])
            return next_id

    def _seed_activity_data(self):
        now = timezone.now()
        today_base = now - dt.timedelta(minutes=10)
        yesterday_base = now - dt.timedelta(days=1, minutes=10)

        customer_id = self._insert_returning_id(
            "INSERT INTO customers (razon_social) VALUES (%s) RETURNING id",
            ["Cliente Actividad"],
        )
        marca_id = self._insert_returning_id(
            "INSERT INTO marcas (nombre) VALUES (%s) RETURNING id",
            ["Marca Actividad"],
        )
        model_id = self._insert_returning_id(
            "INSERT INTO models (marca_id, nombre, tipo_equipo) VALUES (%s, %s, %s) RETURNING id",
            [marca_id, "Modelo Actividad", "Analizador"],
        )
        device_id = self._insert_returning_id(
            """
            INSERT INTO devices (customer_id, marca_id, model_id, numero_serie, numero_interno, variante)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            [customer_id, marca_id, model_id, "NS-100", "NI-200", "V1"],
        )
        self.ingreso_id = self._insert_returning_id(
            """
            INSERT INTO ingresos (device_id, estado, presupuesto_estado, fecha_ingreso, fecha_creacion, asignado_a, descripcion_problema)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            [device_id, "ingresado", "pendiente", now, now, self.tecnico.id, "Sin diagnóstico"],
        )
        quote_id = self._insert_returning_id(
            "INSERT INTO quotes (ingreso_id, estado) VALUES (%s, %s) RETURNING id",
            [self.ingreso_id, "pendiente"],
        )
        self.repuesto_id = self._insert_returning_id(
            "INSERT INTO catalogo_repuestos (codigo, nombre) VALUES (%s, %s) RETURNING id",
            ["REP-001", "Placa lógica"],
        )
        self._insert_returning_id(
            "INSERT INTO quote_items (quote_id, repuesto_id, descripcion) VALUES (%s, %s, %s) RETURNING id",
            [quote_id, self.repuesto_id, "Placa lógica"],
        )

        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit.change_log
                  (ts, user_id, user_role, table_name, record_id, column_name, old_value, new_value, ingreso_id)
                VALUES
                  (%s, %s, %s, %s, %s, %s, %s, %s, %s),
                  (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    today_base,
                    self.tecnico.id,
                    "tecnico",
                    "ingresos",
                    self.ingreso_id,
                    "descripcion_problema",
                    "Sin diagnóstico",
                    "Falla intermitente",
                    self.ingreso_id,
                    today_base + dt.timedelta(minutes=1),
                    self.tecnico.id,
                    "tecnico",
                    "ingresos",
                    self.ingreso_id,
                    "descripcion_problema",
                    "Falla intermitente",
                    "Falla intermitente en placa",
                    self.ingreso_id,
                ],
            )
            cur.execute(
                """
                INSERT INTO ingreso_events
                  (ingreso_id, de_estado, a_estado, usuario_id, ts, comentario)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                [
                    self.ingreso_id,
                    "ingresado",
                    "diagnosticado",
                    self.tecnico.id,
                    today_base + dt.timedelta(minutes=2),
                    "Diagnóstico listo",
                ],
            )
            cur.execute(
                """
                INSERT INTO repuestos_movimientos
                  (repuesto_id, tipo, qty, stock_prev, stock_new, ref_tipo, ref_id, nota, created_at, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    self.repuesto_id,
                    "ajuste",
                    1,
                    3,
                    2,
                    "ingreso",
                    self.ingreso_id,
                    "Se reservó para prueba",
                    today_base + dt.timedelta(minutes=3),
                    self.tecnico.id,
                ],
            )
            cur.execute(
                """
                INSERT INTO audit_log
                  (ts, user_id, role, method, path, status_code, body)
                VALUES
                  (%s, %s, %s, %s, %s, %s, NULL),
                  (%s, %s, %s, %s, %s, %s, NULL),
                  (%s, %s, %s, %s, %s, %s, NULL)
                """,
                [
                    today_base + dt.timedelta(minutes=4),
                    self.tecnico.id,
                    "tecnico",
                    "GET",
                    f"/api/ingresos/{self.ingreso_id}/",
                    200,
                    yesterday_base,
                    self.tecnico.id,
                    "tecnico",
                    "GET",
                    "/api/repuestos/movimientos/",
                    200,
                    today_base + dt.timedelta(minutes=5),
                    self.tecnico.id,
                    "tecnico",
                    "GET",
                    "/api/busqueda/global/",
                    200,
                ],
            )

    def _get_as(self, user, params=None):
        self.client.force_authenticate(user=user)
        return self.client.get("/api/metricas/actividad-tecnicos/", params or {})

    def test_jefe_y_jefe_veedor_pueden_ver_la_actividad(self):
        resp_jefe = self._get_as(self.jefe, {"preset": "today"})
        resp_veedor = self._get_as(self.jefe_veedor, {"preset": "today"})

        self.assertEqual(resp_jefe.status_code, 200)
        self.assertEqual(resp_veedor.status_code, 200)
        self.assertEqual(resp_jefe.data["summary"]["total"], 4)

    def test_tecnico_admin_y_recepcion_no_pueden_ver_la_actividad(self):
        for user in (self.tecnico, self.admin, self.recepcion):
            with self.subTest(user=user.rol):
                resp = self._get_as(user, {"preset": "today"})
                self.assertEqual(resp.status_code, 403)

    def test_mezcla_fuentes_compacta_cambios_y_excluye_busquedas(self):
        resp = self._get_as(self.jefe, {"preset": "today"})

        self.assertEqual(resp.status_code, 200)
        timeline = resp.data["timeline"]
        self.assertEqual({row["source"] for row in timeline}, {"change_log", "ingreso_event", "repuestos_movimientos", "audit_log"})
        self.assertEqual(len(timeline), 4)

        change_row = next(row for row in timeline if row["source"] == "change_log")
        self.assertEqual(change_row["activity_type"], "edicion_diagnostico")
        self.assertGreaterEqual(change_row["meta"]["compact_count"], 2)

        audit_row = next(row for row in timeline if row["source"] == "audit_log")
        self.assertEqual(audit_row["activity_type"], "apertura_hoja")
        self.assertNotIn("/api/busqueda/global/", [row.get("path") for row in timeline])

    def test_tipo_movimiento_repuesto_filtra_correctamente(self):
        resp = self._get_as(self.jefe, {"preset": "today", "tipo": "movimiento_repuesto"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["summary"]["total"], 1)
        self.assertEqual(len(resp.data["timeline"]), 1)
        self.assertEqual(resp.data["timeline"][0]["source"], "repuestos_movimientos")
        self.assertEqual(resp.data["timeline"][0]["activity_type"], "movimiento_repuesto")

    def test_presets_today_yesterday_y_week(self):
        today_resp = self._get_as(self.jefe, {"preset": "today"})
        yesterday_resp = self._get_as(self.jefe, {"preset": "yesterday"})
        week_resp = self._get_as(self.jefe, {"preset": "week"})

        self.assertEqual(today_resp.status_code, 200)
        self.assertEqual(yesterday_resp.status_code, 200)
        self.assertEqual(week_resp.status_code, 200)

        self.assertEqual(len(today_resp.data["timeline"]), 4)
        self.assertEqual(len(yesterday_resp.data["timeline"]), 1)
        self.assertEqual(yesterday_resp.data["timeline"][0]["activity_type"], "apertura_movimientos")
        self.assertGreaterEqual(len(week_resp.data["timeline"]), 4)
        self.assertIn("apertura_hoja", {row["activity_type"] for row in week_resp.data["timeline"]})

    def test_apply_schema_crea_indices_requeridos(self):
        call_command("apply_metricas_actividad_schema", verbosity=0)

        expected = {
            "ix_audit_change_log_user_ts",
            "ix_audit_log_user_ts_method",
            "ix_ingreso_events_usuario_ts",
            "ix_repuestos_movimientos_created_by_ts",
        }
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT indexname
                  FROM pg_indexes
                 WHERE indexname = ANY(%s)
                """,
                [list(expected)],
            )
            found = {row[0] for row in cur.fetchall()}
        self.assertSetEqual(found, expected)
