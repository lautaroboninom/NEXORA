from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from service.models import User


class PendientesPorTecnicoAPITest(TestCase):
    @classmethod
    def setUpClass(cls):
        vendor = connection.vendor
        if vendor == "sqlite":
            auto_inc = "INTEGER PRIMARY KEY AUTOINCREMENT"
            bool_type = "INTEGER"
            datetime_type = "DATETIME"
        elif vendor == "postgresql":
            auto_inc = "BIGSERIAL PRIMARY KEY"
            bool_type = "BOOLEAN"
            datetime_type = "TIMESTAMPTZ"
        else:
            auto_inc = "INT AUTO_INCREMENT PRIMARY KEY"
            bool_type = "BOOLEAN"
            datetime_type = "DATETIME"

        engine_suffix = " ENGINE=InnoDB" if vendor == "mysql" else ""

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
                    marca_id INT,
                    nombre TEXT,
                    tipo_equipo TEXT,
                    variante TEXT
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS devices (
                    id {auto_inc},
                    customer_id INT,
                    marca_id INT,
                    model_id INT,
                    numero_serie TEXT,
                    numero_interno TEXT,
                    variante TEXT
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS locations (
                    id {auto_inc},
                    nombre TEXT
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS ingresos (
                    id {auto_inc},
                    device_id INT NOT NULL,
                    estado TEXT,
                    presupuesto_estado TEXT,
                    motivo TEXT,
                    equipo_variante TEXT,
                    fecha_ingreso {datetime_type} NULL,
                    ubicacion_id INT NULL,
                    asignado_a INT NULL
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS equipos_derivados (
                    id {auto_inc},
                    ingreso_id INT NOT NULL,
                    estado TEXT,
                    fecha_deriv {datetime_type} NULL
                ){engine_suffix}
                """
            )

            # Compatibilidad si otra suite dejó tablas mínimas ya creadas.
            for statement in (
                "ALTER TABLE models ADD COLUMN variante TEXT",
                "ALTER TABLE devices ADD COLUMN numero_interno TEXT",
                "ALTER TABLE devices ADD COLUMN variante TEXT",
                "ALTER TABLE ingresos ADD COLUMN presupuesto_estado TEXT",
                "ALTER TABLE ingresos ADD COLUMN motivo TEXT",
                "ALTER TABLE ingresos ADD COLUMN equipo_variante TEXT",
                f"ALTER TABLE ingresos ADD COLUMN fecha_ingreso {datetime_type} NULL",
                "ALTER TABLE ingresos ADD COLUMN ubicacion_id INT NULL",
                "ALTER TABLE ingresos ADD COLUMN asignado_a INT NULL",
            ):
                try:
                    cur.execute(statement)
                except Exception:
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
            cur.execute("DELETE FROM equipos_derivados")
            cur.execute("DELETE FROM ingresos")
            cur.execute("DELETE FROM devices")
            cur.execute("DELETE FROM models")
            cur.execute("DELETE FROM marcas")
            cur.execute("DELETE FROM customers")
            cur.execute("DELETE FROM locations")

        User.objects.filter(
            email__in=[
                "jefe-pendientes@example.com",
                "tecnico-pendientes@example.com",
            ]
        ).delete()

        cls.jefe_user = User.objects.create(
            nombre="Jefe Pendientes",
            email="jefe-pendientes@example.com",
            hash_pw="",
            rol="jefe",
            activo=True,
        )
        cls.tech_user = User.objects.create(
            nombre="Tecnico Pendientes",
            email="tecnico-pendientes@example.com",
            hash_pw="",
            rol="tecnico",
            activo=True,
        )

        with connection.cursor() as cur:
            cur.execute("INSERT INTO locations (nombre) VALUES (%s)", ["Taller"])
            cls.taller_id = cls._last_insert_id(cur)
            cur.execute("INSERT INTO locations (nombre) VALUES (%s)", ["Deposito"])
            deposito_id = cls._last_insert_id(cur)

            cur.execute("INSERT INTO customers (razon_social) VALUES (%s)", ["Clinica Demo"])
            customer_id = cls._last_insert_id(cur)

            cur.execute("INSERT INTO marcas (nombre) VALUES (%s)", ["ResMed"])
            marca_id = cls._last_insert_id(cur)

            cur.execute(
                "INSERT INTO models (marca_id, nombre, tipo_equipo, variante) VALUES (%s,%s,%s,%s)",
                [marca_id, "AirSense 10", "CPAP", ""],
            )
            model_id = cls._last_insert_id(cur)

            cur.execute(
                """
                INSERT INTO devices (customer_id, marca_id, model_id, numero_serie, numero_interno, variante)
                VALUES (%s,%s,%s,%s,%s,%s)
                """,
                [customer_id, marca_id, model_id, "SERIE-ASIGNADO", "MG 1001", ""],
            )
            device_assigned_id = cls._last_insert_id(cur)

            cur.execute(
                """
                INSERT INTO devices (customer_id, marca_id, model_id, numero_serie, numero_interno, variante)
                VALUES (%s,%s,%s,%s,%s,%s)
                """,
                [customer_id, marca_id, model_id, "SERIE-SIN-TECNICO", "MG 1002", ""],
            )
            device_unassigned_id = cls._last_insert_id(cur)

            cur.execute(
                """
                INSERT INTO devices (customer_id, marca_id, model_id, numero_serie, numero_interno, variante)
                VALUES (%s,%s,%s,%s,%s,%s)
                """,
                [customer_id, marca_id, model_id, "SERIE-DEPOSITO", "MG 1003", ""],
            )
            device_deposito_id = cls._last_insert_id(cur)

            cur.execute(
                """
                INSERT INTO ingresos (device_id, estado, presupuesto_estado, motivo, fecha_ingreso, ubicacion_id, asignado_a)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                [device_assigned_id, "diagnosticado", None, "reparacion", None, cls.taller_id, cls.tech_user.id],
            )
            cls.ingreso_assigned_id = cls._last_insert_id(cur)

            cur.execute(
                """
                INSERT INTO ingresos (device_id, estado, presupuesto_estado, motivo, fecha_ingreso, ubicacion_id, asignado_a)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                [device_unassigned_id, "diagnosticado", None, "reparacion", None, cls.taller_id, None],
            )
            cls.ingreso_unassigned_id = cls._last_insert_id(cur)

            cur.execute(
                """
                INSERT INTO ingresos (device_id, estado, presupuesto_estado, motivo, fecha_ingreso, ubicacion_id, asignado_a)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                [device_deposito_id, "diagnosticado", None, "reparacion", None, deposito_id, None],
            )
            cls.ingreso_deposito_id = cls._last_insert_id(cur)

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(user=self.jefe_user)

    def test_filtra_pendientes_sin_tecnico_asignado(self):
        resp = self.client.get("/api/ingresos/pendientes/?tecnico_id=sin_asignar")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual([row["id"] for row in resp.data], [self.ingreso_unassigned_id])

    def test_filtra_pendientes_por_tecnico_existente(self):
        resp = self.client.get(f"/api/ingresos/pendientes/?tecnico_id={self.tech_user.id}")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual([row["id"] for row in resp.data], [self.ingreso_assigned_id])
