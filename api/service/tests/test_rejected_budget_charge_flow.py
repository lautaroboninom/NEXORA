from decimal import Decimal
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from service.models import User


class RejectedBudgetChargeFlowAPITest(TestCase):
    @classmethod
    def setUpClass(cls):
        vendor = connection.vendor
        if vendor == "sqlite":
            auto_inc = "INTEGER PRIMARY KEY AUTOINCREMENT"
            datetime_type = "DATETIME"
            bool_type = "INTEGER"
            engine_suffix = ""
        elif vendor == "postgresql":
            auto_inc = "BIGSERIAL PRIMARY KEY"
            datetime_type = "TIMESTAMPTZ"
            bool_type = "BOOLEAN"
            engine_suffix = ""
        else:
            auto_inc = "INT AUTO_INCREMENT PRIMARY KEY"
            datetime_type = "DATETIME"
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
                    activo {bool_type} DEFAULT TRUE
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS customers (
                    id {auto_inc},
                    razon_social TEXT
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS marcas (
                    id {auto_inc},
                    nombre TEXT
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS models (
                    id {auto_inc},
                    nombre TEXT,
                    tipo_equipo TEXT
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS devices (
                    id {auto_inc},
                    customer_id INT NULL,
                    marca_id INT NULL,
                    model_id INT NULL,
                    numero_serie TEXT NULL,
                    numero_interno TEXT NULL
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS ingresos (
                    id {auto_inc},
                    device_id INT NULL,
                    estado TEXT,
                    presupuesto_estado TEXT,
                    resolucion TEXT NULL,
                    fecha_ingreso {datetime_type} NULL,
                    asignado_a INT NULL,
                    presupuesto_rechazado_cobro_neto NUMERIC(12,2) NULL,
                    presupuesto_rechazado_quote_id INT NULL
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
                    precio_u NUMERIC(12,2) NOT NULL DEFAULT 0,
                    costo_u_neto NUMERIC(12,2) NULL
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS ingreso_events (
                    id {auto_inc},
                    ticket_id INT NULL,
                    ingreso_id INT NULL,
                    a_estado TEXT NOT NULL,
                    usuario_id INT NULL,
                    ts {datetime_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    comentario TEXT NULL
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
                cur.execute("ALTER TABLE customers ADD COLUMN IF NOT EXISTS razon_social TEXT")
                cur.execute("ALTER TABLE marcas ADD COLUMN IF NOT EXISTS nombre TEXT")
                cur.execute("ALTER TABLE models ADD COLUMN IF NOT EXISTS nombre TEXT")
                cur.execute("ALTER TABLE models ADD COLUMN IF NOT EXISTS tipo_equipo TEXT")
                cur.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS customer_id INT NULL")
                cur.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS marca_id INT NULL")
                cur.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS model_id INT NULL")
                cur.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS numero_serie TEXT NULL")
                cur.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS numero_interno TEXT NULL")
                cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS device_id INT NULL")
                cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS estado TEXT")
                cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS presupuesto_estado TEXT")
                cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS resolucion TEXT NULL")
                cur.execute(f"ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS fecha_ingreso {datetime_type} NULL")
                cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS asignado_a INT NULL")
                cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS presupuesto_rechazado_cobro_neto NUMERIC(12,2) NULL")
                cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS presupuesto_rechazado_quote_id INT NULL")
                cur.execute("ALTER TABLE quotes ADD COLUMN IF NOT EXISTS ingreso_id INT NOT NULL DEFAULT 0")
                cur.execute("ALTER TABLE quotes ADD COLUMN IF NOT EXISTS version_num INT NOT NULL DEFAULT 1")
                cur.execute("ALTER TABLE quotes ADD COLUMN IF NOT EXISTS estado TEXT NOT NULL DEFAULT 'pendiente'")
                cur.execute("ALTER TABLE quote_items ADD COLUMN IF NOT EXISTS tipo TEXT NOT NULL DEFAULT 'mano_obra'")
                cur.execute("ALTER TABLE quote_items ADD COLUMN IF NOT EXISTS descripcion TEXT NOT NULL DEFAULT ''")
                cur.execute("ALTER TABLE quote_items ADD COLUMN IF NOT EXISTS quote_id INT NOT NULL DEFAULT 0")
                cur.execute("ALTER TABLE quote_items ADD COLUMN IF NOT EXISTS qty NUMERIC(12,2) NOT NULL DEFAULT 0")
                cur.execute("ALTER TABLE quote_items ADD COLUMN IF NOT EXISTS precio_u NUMERIC(12,2) NOT NULL DEFAULT 0")
                cur.execute("ALTER TABLE quote_items ADD COLUMN IF NOT EXISTS costo_u_neto NUMERIC(12,2) NULL")
                cur.execute("ALTER TABLE ingreso_events ADD COLUMN IF NOT EXISTS ticket_id INT NULL")
                cur.execute("ALTER TABLE ingreso_events ADD COLUMN IF NOT EXISTS ingreso_id INT NULL")
                cur.execute("ALTER TABLE ingreso_events ADD COLUMN IF NOT EXISTS a_estado TEXT")
                cur.execute("ALTER TABLE ingreso_events ADD COLUMN IF NOT EXISTS usuario_id INT NULL")
                cur.execute(f"ALTER TABLE ingreso_events ADD COLUMN IF NOT EXISTS ts {datetime_type} NULL")
                cur.execute("ALTER TABLE ingreso_events ADD COLUMN IF NOT EXISTS comentario TEXT NULL")
                cur.execute("ALTER TABLE repuestos_config ADD COLUMN IF NOT EXISTS multiplicador_general NUMERIC(12,4) NULL")
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
            for table in ("ingreso_events", "quote_items", "quotes", "ingresos", "devices", "models", "marcas", "customers"):
                cur.execute(f"DELETE FROM {table}")
        User.objects.filter(email="jefe-rejected-budget@example.com").delete()
        cls.jefe = User.objects.create(
            nombre="Jefe Presupuesto Rechazado",
            email="jefe-rejected-budget@example.com",
            hash_pw="",
            rol="jefe",
            activo=True,
        )

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(user=self.jefe)
        with connection.cursor() as cur:
            for table in ("ingreso_events", "quote_items", "quotes", "ingresos", "devices", "models", "marcas", "customers"):
                cur.execute(f"DELETE FROM {table}")

    def _seed_ingreso_rechazado(self, *, charge_neto, linked_quote_version=1):
        now = timezone.now()
        with connection.cursor() as cur:
            cur.execute("INSERT INTO customers (razon_social) VALUES (%s)", ["Cliente Test"])
            customer_id = self._last_insert_id(cur)
            cur.execute("INSERT INTO marcas (nombre) VALUES (%s)", ["Marca Test"])
            marca_id = self._last_insert_id(cur)
            cur.execute("INSERT INTO models (nombre, tipo_equipo) VALUES (%s, %s)", ["Modelo Test", "Bomba"])
            model_id = self._last_insert_id(cur)
            cur.execute(
                """
                INSERT INTO devices (customer_id, marca_id, model_id, numero_serie, numero_interno)
                VALUES (%s, %s, %s, %s, %s)
                """,
                [customer_id, marca_id, model_id, "SN-REJECTED-001", "MG-001"],
            )
            device_id = self._last_insert_id(cur)
            cur.execute(
                """
                INSERT INTO ingresos (
                    device_id,
                    estado,
                    presupuesto_estado,
                    resolucion,
                    fecha_ingreso,
                    asignado_a,
                    presupuesto_rechazado_cobro_neto
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    device_id,
                    "reparado",
                    "rechazado",
                    "presupuesto_rechazado",
                    now,
                    self.jefe.id,
                    Decimal(str(charge_neto)) if charge_neto is not None else None,
                ],
            )
            ingreso_id = self._last_insert_id(cur)

            quote_ids = []
            for version_num, subtotal in ((1, Decimal("48000.00")), (2, Decimal("52000.00"))):
                cur.execute(
                    "INSERT INTO quotes (ingreso_id, version_num, estado) VALUES (%s, %s, %s)",
                    [ingreso_id, version_num, "rechazado"],
                )
                quote_id = self._last_insert_id(cur)
                quote_ids.append(quote_id)
                cur.execute(
                    """
                    INSERT INTO quote_items (quote_id, tipo, descripcion, qty, precio_u, costo_u_neto)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    [quote_id, "repuesto", f"Presupuesto rechazado V{version_num}", Decimal("1"), subtotal, Decimal("31000.00")],
                )

            linked_quote_id = quote_ids[linked_quote_version - 1]
            cur.execute(
                """
                UPDATE ingresos
                   SET presupuesto_rechazado_quote_id=%s
                 WHERE id=%s
                """,
                [linked_quote_id, ingreso_id],
            )
            cur.execute(
                """
                INSERT INTO ingreso_events (ticket_id, ingreso_id, a_estado, usuario_id, ts, comentario)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                [ingreso_id, ingreso_id, "liberado", self.jefe.id, now, "Liberado para métricas"],
            )

        return {
            "ingreso_id": ingreso_id,
            "quote_ids": quote_ids,
        }

    def test_metricas_finanzas_liberados_usa_cobro_efectivo_en_presupuesto_rechazado(self):
        seeded = self._seed_ingreso_rechazado(charge_neto="12800.00", linked_quote_version=2)
        ingreso_id = seeded["ingreso_id"]
        now = timezone.localtime(timezone.now())
        from_date = now.date().isoformat()
        to_date = now.date().isoformat()

        resp = self.client.get(
            f"/api/metricas/finanzas/liberados/?from={from_date}&to={to_date}"
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        row = next((item for item in resp.data if int(item["ingreso_id"]) == ingreso_id), None)
        self.assertIsNotNone(row)
        self.assertEqual(Decimal(str(row["ingresos_sin_iva"])), Decimal("12800.00"))
        self.assertEqual(Decimal(str(row["mano_obra"])), Decimal("12800.00"))
        self.assertEqual(Decimal(str(row["repuestos"])), Decimal("0"))
        self.assertEqual(Decimal(str(row["costo_repuestos"])), Decimal("0"))
        self.assertEqual(Decimal(str(row["margen"])), Decimal("12800.00"))

    def test_remito_rechazado_exige_cobro_neto_definido(self):
        seeded = self._seed_ingreso_rechazado(charge_neto=None, linked_quote_version=2)

        resp = self.client.get(f"/api/ingresos/{seeded['ingreso_id']}/remito/")

        self.assertEqual(resp.status_code, 409)
        self.assertIn("cobro neto", str(resp.data.get("detail") or "").lower())

    def test_remito_rechazado_actualiza_quote_vinculada_al_reimprimir(self):
        seeded = self._seed_ingreso_rechazado(charge_neto="12800.00", linked_quote_version=1)
        ingreso_id = seeded["ingreso_id"]
        latest_quote_id = seeded["quote_ids"][-1]

        with (
            patch("service.views.reportes_views.render_remito_salida_pdf", return_value=(b"%PDF-1.4", "remito.pdf")),
            patch("service.views.reportes_views.notify_ingreso_liberado", return_value=0),
            patch("service.views.reportes_views.ingreso_is_internal_equipment", return_value=False),
            patch("service.views.reportes_views.enqueue_client_ready_transfer_for_ingreso", return_value=None),
        ):
            resp = self.client.get(f"/api/ingresos/{ingreso_id}/remito/")

        self.assertEqual(resp.status_code, 200)
        with connection.cursor() as cur:
            cur.execute(
                "SELECT presupuesto_rechazado_quote_id FROM ingresos WHERE id=%s",
                [ingreso_id],
            )
            linked_quote_id = int(cur.fetchone()[0])
        self.assertEqual(linked_quote_id, latest_quote_id)
