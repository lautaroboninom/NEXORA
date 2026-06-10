from decimal import Decimal

from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from service.auth import issue_token
from service.models import User


class CerrarReparacionPermissionsAPITest(TestCase):
    @classmethod
    def setUpClass(cls):
        vendor = connection.vendor
        if vendor == "sqlite":
            auto_inc = "INTEGER PRIMARY KEY AUTOINCREMENT"
            bool_type = "INTEGER"
            engine_suffix = ""
        elif vendor == "postgresql":
            auto_inc = "BIGSERIAL PRIMARY KEY"
            bool_type = "BOOLEAN"
            engine_suffix = ""
        else:
            auto_inc = "INT AUTO_INCREMENT PRIMARY KEY"
            bool_type = "BOOLEAN"
            engine_suffix = " ENGINE=InnoDB"

        with connection.cursor() as cur:
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS users (
                    id {auto_inc},
                    nombre TEXT,
                    email VARCHAR(320) UNIQUE,
                    hash_pw TEXT,
                    rol TEXT,
                    activo {bool_type} DEFAULT 1
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS ingresos (
                    id {auto_inc},
                    motivo TEXT,
                    resolucion TEXT,
                    presupuesto_rechazado_cobro_neto NUMERIC(12,2) NULL,
                    presupuesto_rechazado_quote_id INT NULL,
                    permite_reparacion {bool_type} DEFAULT 1,
                    asignado_a INT NULL
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS quotes (
                    id {auto_inc},
                    ingreso_id INT NOT NULL,
                    version_num INT NOT NULL DEFAULT 1,
                    estado TEXT NOT NULL DEFAULT 'pendiente'
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS quote_items (
                    id {auto_inc},
                    quote_id INT NOT NULL,
                    tipo TEXT NOT NULL DEFAULT 'mano_obra',
                    descripcion TEXT NOT NULL DEFAULT '',
                    qty NUMERIC(12,2) NOT NULL DEFAULT 0,
                    precio_u NUMERIC(12,2) NOT NULL DEFAULT 0
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS user_permission_overrides (
                    id {auto_inc},
                    user_id INT NOT NULL,
                    permission_code TEXT NOT NULL,
                    effect TEXT NOT NULL,
                    updated_by INT NULL
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS repuestos_config (
                    id {auto_inc},
                    multiplicador_general NUMERIC(12,4) NULL
                ){engine_suffix}
                """
            )
            try:
                cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS motivo TEXT")
                cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS resolucion TEXT")
                cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS presupuesto_rechazado_cobro_neto NUMERIC(12,2) NULL")
                cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS presupuesto_rechazado_quote_id INT NULL")
                cur.execute(f"ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS permite_reparacion {bool_type} DEFAULT 1")
                cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS asignado_a INT NULL")
                cur.execute("ALTER TABLE quotes ADD COLUMN IF NOT EXISTS ingreso_id INT NOT NULL DEFAULT 0")
                cur.execute("ALTER TABLE quotes ADD COLUMN IF NOT EXISTS version_num INT NOT NULL DEFAULT 1")
                cur.execute("ALTER TABLE quotes ADD COLUMN IF NOT EXISTS estado TEXT NOT NULL DEFAULT 'pendiente'")
                cur.execute("ALTER TABLE quote_items ADD COLUMN IF NOT EXISTS quote_id INT NOT NULL DEFAULT 0")
                cur.execute("ALTER TABLE quote_items ADD COLUMN IF NOT EXISTS tipo TEXT NOT NULL DEFAULT 'mano_obra'")
                cur.execute("ALTER TABLE quote_items ADD COLUMN IF NOT EXISTS descripcion TEXT NOT NULL DEFAULT ''")
                cur.execute("ALTER TABLE quote_items ADD COLUMN IF NOT EXISTS qty NUMERIC(12,2) NOT NULL DEFAULT 0")
                cur.execute("ALTER TABLE quote_items ADD COLUMN IF NOT EXISTS precio_u NUMERIC(12,2) NOT NULL DEFAULT 0")
            except Exception:
                connection.rollback()
                pass
        super().setUpClass()

    @classmethod
    def _last_insert_id(cls, cur):
        if connection.vendor == "sqlite":
            cur.execute("SELECT last_insert_rowid()")
        elif connection.vendor == "postgresql":
            cur.execute("SELECT LASTVAL()")
        else:
            cur.execute("SELECT LAST_INSERT_ID()")
        return int(cur.fetchone()[0])

    @classmethod
    def setUpTestData(cls):
        with connection.cursor() as cur:
            cur.execute("DELETE FROM ingresos")
        User.objects.all().delete()

        cls.jefe = User.objects.create(
            nombre="Jefe Resolucion",
            email="jefe-resolucion@example.com",
            hash_pw="",
            rol="jefe",
            activo=True,
        )
        cls.tecnico = User.objects.create(
            nombre="Tecnico Resolucion",
            email="tecnico-resolucion@example.com",
            hash_pw="",
            rol="tecnico",
            activo=True,
        )
        cls.admin = User.objects.create(
            nombre="Admin Resolucion",
            email="admin-resolucion@example.com",
            hash_pw="",
            rol="admin",
            activo=True,
        )
        cls.jefe_veedor = User.objects.create(
            nombre="Jefe Veedor Resolucion",
            email="jefe-veedor-resolucion@example.com",
            hash_pw="",
            rol="jefe_veedor",
            activo=True,
        )

        cls.tokens = {
            "jefe": issue_token(cls.jefe),
            "tecnico": issue_token(cls.tecnico),
            "admin": issue_token(cls.admin),
            "jefe_veedor": issue_token(cls.jefe_veedor),
        }

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        with connection.cursor() as cur:
            cur.execute("DELETE FROM ingresos")
            cur.execute("DELETE FROM quote_items")
            cur.execute("DELETE FROM quotes")
            cur.execute(
                """
                INSERT INTO ingresos (
                    motivo,
                    resolucion,
                    presupuesto_rechazado_cobro_neto,
                    presupuesto_rechazado_quote_id,
                    permite_reparacion,
                    asignado_a
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                ["reparacion", None, None, None, True, self.tecnico.id],
            )
            self.ingreso_id = self._last_insert_id(cur)

    def _url(self):
        return f"/api/ingresos/{self.ingreso_id}/cerrar/"

    def _post_as(self, role, payload):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.tokens[role]}")
        return self.client.post(self._url(), payload, format="json")

    def _resolucion_actual(self):
        with connection.cursor() as cur:
            cur.execute("SELECT resolucion FROM ingresos WHERE id=%s", [self.ingreso_id])
            row = cur.fetchone()
        return row[0] if row else None

    def _estado_rechazado_actual(self):
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT resolucion, presupuesto_rechazado_cobro_neto, presupuesto_rechazado_quote_id
                FROM ingresos
                WHERE id=%s
                """,
                [self.ingreso_id],
            )
            row = cur.fetchone()
        return row

    def _crear_presupuesto_rechazado(self, subtotal="100.00"):
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO quotes (ingreso_id, version_num, estado) VALUES (%s, %s, %s)",
                [self.ingreso_id, 1, "rechazado"],
            )
            quote_id = self._last_insert_id(cur)
            cur.execute(
                """
                INSERT INTO quote_items (quote_id, tipo, descripcion, qty, precio_u)
                VALUES (%s, %s, %s, %s, %s)
                """,
                [quote_id, "mano_obra", "Diagnóstico y limpieza", Decimal("1"), Decimal(str(subtotal))],
            )
        return quote_id

    def test_jefe_puede_guardar_resolucion(self):
        resp = self._post_as("jefe", {"resolucion": "reparado"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._resolucion_actual(), "reparado")

    def test_tecnico_admin_y_jefe_veedor_pueden_guardar_resolucion_por_permiso_mapeado(self):
        for role in ("tecnico", "admin", "jefe_veedor"):
            with self.subTest(role=role):
                resp = self._post_as(role, {"resolucion": "reparado"})
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(self._resolucion_actual(), "reparado")
                with connection.cursor() as cur:
                    cur.execute("UPDATE ingresos SET resolucion=NULL WHERE id=%s", [self.ingreso_id])

    def test_jefe_reutiliza_cobro_guardado_en_presupuesto_rechazado(self):
        quote_id = self._crear_presupuesto_rechazado(subtotal="12800.00")
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE ingresos
                   SET presupuesto_rechazado_cobro_neto=%s,
                       presupuesto_rechazado_quote_id=%s
                 WHERE id=%s
                """,
                [Decimal("12800.00"), quote_id, self.ingreso_id],
            )

        resp = self._post_as("jefe", {"resolucion": "presupuesto_rechazado"})

        self.assertEqual(resp.status_code, 200, resp.data)
        resolucion, cobro_neto, linked_quote_id = self._estado_rechazado_actual()
        self.assertEqual(resolucion, "presupuesto_rechazado")
        self.assertEqual(Decimal(str(cobro_neto)), Decimal("12800.00"))
        self.assertEqual(int(linked_quote_id), quote_id)

    def test_jefe_puede_sobrescribir_cobro_de_presupuesto_rechazado(self):
        quote_id = self._crear_presupuesto_rechazado(subtotal="9800.00")
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE ingresos
                   SET presupuesto_rechazado_cobro_neto=%s,
                       presupuesto_rechazado_quote_id=%s
                 WHERE id=%s
                """,
                [Decimal("9800.00"), quote_id, self.ingreso_id],
            )

        resp = self._post_as(
            "jefe",
            {
                "resolucion": "presupuesto_rechazado",
                "presupuesto_rechazado_cobro_neto": "12800.00",
            },
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        resolucion, cobro_neto, linked_quote_id = self._estado_rechazado_actual()
        self.assertEqual(resolucion, "presupuesto_rechazado")
        self.assertEqual(Decimal(str(cobro_neto)), Decimal("12800.00"))
        self.assertEqual(int(linked_quote_id), quote_id)

    def test_jefe_recibe_400_si_falta_cobro_en_presupuesto_rechazado(self):
        self._crear_presupuesto_rechazado(subtotal="12800.00")

        resp = self._post_as("jefe", {"resolucion": "presupuesto_rechazado"})

        self.assertEqual(resp.status_code, 400)
        self.assertIn("cobro neto", str(resp.data.get("detail") or "").lower())
        resolucion, cobro_neto, linked_quote_id = self._estado_rechazado_actual()
        self.assertIsNone(resolucion)
        self.assertIsNone(cobro_neto)
        self.assertIsNone(linked_quote_id)
