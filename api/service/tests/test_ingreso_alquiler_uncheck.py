from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from service.auth import issue_token
from service.models import User


class IngresoAlquilerUncheckAPITest(TestCase):
    @classmethod
    def setUpClass(cls):
        cls._ensure_tables()
        super().setUpClass()

    @classmethod
    def _ensure_tables(cls):
        vendor = connection.vendor
        if vendor == "sqlite":
            auto_inc = "INTEGER PRIMARY KEY AUTOINCREMENT"
            bool_type = "INTEGER"
            bool_default = "1"
            datetime_type = "DATETIME"
        elif vendor == "postgresql":
            auto_inc = "BIGSERIAL PRIMARY KEY"
            bool_type = "BOOLEAN"
            bool_default = "TRUE"
            datetime_type = "TIMESTAMPTZ"
        else:
            auto_inc = "INT AUTO_INCREMENT PRIMARY KEY"
            bool_type = "BOOLEAN"
            bool_default = "1"
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
                    activo {bool_type} DEFAULT {bool_default}
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
                    updated_by INT NULL,
                    created_at {datetime_type} DEFAULT CURRENT_TIMESTAMP,
                    updated_at {datetime_type} DEFAULT CURRENT_TIMESTAMP
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS customers (
                    id {auto_inc},
                    razon_social TEXT,
                    cod_empresa TEXT,
                    telefono TEXT
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS marcas (
                    id {auto_inc},
                    nombre TEXT,
                    tecnico_id INT NULL
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS models (
                    id {auto_inc},
                    marca_id INT NULL,
                    nombre TEXT,
                    tipo_equipo TEXT,
                    tecnico_id INT NULL,
                    variante TEXT
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS devices (
                    id {auto_inc},
                    customer_id INT,
                    marca_id INT NULL,
                    model_id INT NULL,
                    numero_serie TEXT,
                    numero_interno TEXT,
                    propietario TEXT,
                    propietario_nombre TEXT,
                    propietario_contacto TEXT,
                    propietario_doc TEXT,
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
                    device_id INT,
                    motivo TEXT,
                    estado TEXT,
                    presupuesto_estado TEXT,
                    resolucion TEXT,
                    fecha_ingreso {datetime_type} DEFAULT CURRENT_TIMESTAMP,
                    fecha_servicio {datetime_type} NULL,
                    garantia_reparacion {bool_type} DEFAULT 0,
                    garantia_fabrica {bool_type} DEFAULT 0,
                    etiq_garantia_ok {bool_type} DEFAULT {bool_default},
                    faja_garantia TEXT,
                    remito_ingreso TEXT,
                    remito_salida TEXT,
                    factura_numero TEXT,
                    fecha_entrega {datetime_type} NULL,
                    alquilado {bool_type} DEFAULT 0,
                    alquiler_a TEXT,
                    alquiler_remito TEXT,
                    alquiler_fecha DATE NULL,
                    informe_preliminar TEXT,
                    descripcion_problema TEXT,
                    trabajos_realizados TEXT,
                    comentarios TEXT,
                    accesorios TEXT,
                    equipo_variante TEXT,
                    ubicacion_id INT,
                    asignado_a INT NULL,
                    recibido_por INT NULL
                ){engine_suffix}
                """
            )
            if vendor == "postgresql":
                for column_sql in (
                    "ADD COLUMN IF NOT EXISTS propietario TEXT",
                    "ADD COLUMN IF NOT EXISTS propietario_nombre TEXT",
                    "ADD COLUMN IF NOT EXISTS propietario_contacto TEXT",
                    "ADD COLUMN IF NOT EXISTS propietario_doc TEXT",
                ):
                    cur.execute(f"ALTER TABLE devices {column_sql}")
                for column_sql in (
                    "ADD COLUMN IF NOT EXISTS device_id INT",
                    "ADD COLUMN IF NOT EXISTS motivo TEXT",
                    "ADD COLUMN IF NOT EXISTS presupuesto_estado TEXT",
                    "ADD COLUMN IF NOT EXISTS resolucion TEXT",
                    "ADD COLUMN IF NOT EXISTS fecha_ingreso TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP",
                    "ADD COLUMN IF NOT EXISTS fecha_servicio TIMESTAMPTZ NULL",
                    "ADD COLUMN IF NOT EXISTS garantia_reparacion BOOLEAN DEFAULT FALSE",
                    "ADD COLUMN IF NOT EXISTS garantia_fabrica BOOLEAN DEFAULT FALSE",
                    "ADD COLUMN IF NOT EXISTS etiq_garantia_ok BOOLEAN DEFAULT TRUE",
                    "ADD COLUMN IF NOT EXISTS faja_garantia TEXT",
                    "ADD COLUMN IF NOT EXISTS remito_ingreso TEXT",
                    "ADD COLUMN IF NOT EXISTS remito_salida TEXT",
                    "ADD COLUMN IF NOT EXISTS factura_numero TEXT",
                    "ADD COLUMN IF NOT EXISTS fecha_entrega TIMESTAMPTZ NULL",
                    "ADD COLUMN IF NOT EXISTS alquilado BOOLEAN DEFAULT FALSE",
                    "ADD COLUMN IF NOT EXISTS alquiler_a TEXT",
                    "ADD COLUMN IF NOT EXISTS alquiler_remito TEXT",
                    "ADD COLUMN IF NOT EXISTS alquiler_fecha DATE NULL",
                    "ADD COLUMN IF NOT EXISTS informe_preliminar TEXT",
                    "ADD COLUMN IF NOT EXISTS descripcion_problema TEXT",
                    "ADD COLUMN IF NOT EXISTS trabajos_realizados TEXT",
                    "ADD COLUMN IF NOT EXISTS comentarios TEXT",
                    "ADD COLUMN IF NOT EXISTS accesorios TEXT",
                    "ADD COLUMN IF NOT EXISTS equipo_variante TEXT",
                    "ADD COLUMN IF NOT EXISTS ubicacion_id INT",
                    "ADD COLUMN IF NOT EXISTS asignado_a INT NULL",
                    "ADD COLUMN IF NOT EXISTS recibido_por INT NULL",
                ):
                    cur.execute(f"ALTER TABLE ingresos {column_sql}")

    @classmethod
    def setUpTestData(cls):
        with connection.cursor() as cur:
            for table in (
                "ingresos",
                "devices",
                "models",
                "marcas",
                "locations",
                "customers",
                "user_permission_overrides",
            ):
                cur.execute(f"DELETE FROM {table}")
        User.objects.all().delete()

        cls.jefe_user = User.objects.create(
            nombre="Jefe alquiler",
            email="jefe-alquiler@example.com",
            hash_pw="",
            rol="jefe",
            activo=True,
        )
        cls.jefe_token = issue_token(cls.jefe_user)

        with connection.cursor() as cur:
            cur.execute("INSERT INTO customers(id, razon_social) VALUES (1, 'Cliente Test')")
            cur.execute("INSERT INTO marcas(id, nombre) VALUES (1, 'Marca Test')")
            cur.execute(
                "INSERT INTO models(id, marca_id, nombre, tipo_equipo) VALUES (1, 1, 'Modelo Test', 'CPAP')"
            )
            cur.execute(
                "INSERT INTO devices(id, customer_id, marca_id, model_id, numero_serie, numero_interno) VALUES (1, 1, 1, 1, 'NS-ALQ-1', 'MG 0001')"
            )
            cur.execute("INSERT INTO locations(id, nombre) VALUES (1, 'taller')")
            cur.execute(
                """
                INSERT INTO ingresos(
                    id, device_id, motivo, estado, presupuesto_estado, resolucion, ubicacion_id,
                    asignado_a, recibido_por, alquilado, alquiler_a, alquiler_remito, fecha_entrega
                )
                VALUES (1, 1, 'reparacion', 'alquilado', 'no_aplica', 'reparado', 1, NULL, NULL, TRUE, 'Cliente Test', 'REM-ALQ-10', NULL)
                """
            )
            cur.execute(
                """
                INSERT INTO ingresos(
                    id, device_id, motivo, estado, presupuesto_estado, resolucion, ubicacion_id,
                    asignado_a, recibido_por, alquilado, alquiler_a, alquiler_remito, fecha_entrega
                )
                VALUES (2, 1, 'reparacion', 'entregado', 'no_aplica', 'reparado', 1, NULL, NULL, TRUE, 'Cliente Test', 'REM-ALQ-20', CURRENT_TIMESTAMP)
                """
            )

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.jefe_token}")

    def test_jefe_puede_destildar_alquiler_si_no_fue_entregado(self):
        resp = self.client.patch("/api/ingresos/1/", {"alquilado": False}, format="json")

        self.assertEqual(resp.status_code, 200)

        with connection.cursor() as cur:
            cur.execute("SELECT COALESCE(alquilado,false) FROM ingresos WHERE id=1")
            row = cur.fetchone()

        self.assertIsNotNone(row)
        self.assertFalse(bool(row[0]))

    def test_jefe_no_puede_destildar_alquiler_entregado_con_remito(self):
        resp = self.client.patch("/api/ingresos/2/", {"alquilado": False}, format="json")

        self.assertEqual(resp.status_code, 400)
        self.assertIn("remito de alquiler", str(resp.data.get("detail") or "").lower())

        with connection.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(alquilado,false), COALESCE(alquiler_remito,''), fecha_entrega FROM ingresos WHERE id=2"
            )
            row = cur.fetchone()

        self.assertIsNotNone(row)
        self.assertTrue(bool(row[0]))
        self.assertEqual(row[1], "REM-ALQ-20")
        self.assertIsNotNone(row[2])
