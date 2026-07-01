from django.db import connection
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from service.auth import issue_token
from service.models import User


class IngresoSolicitudBajaRechazarAPITest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        vendor = connection.vendor
        if vendor == "sqlite":
            auto_inc = "INTEGER PRIMARY KEY AUTOINCREMENT"
            bool_type = "INTEGER"
            datetime_type = "DATETIME"
            engine_suffix = ""
        elif vendor == "postgresql":
            auto_inc = "BIGSERIAL PRIMARY KEY"
            bool_type = "BOOLEAN"
            datetime_type = "TIMESTAMPTZ"
            engine_suffix = ""
        else:
            auto_inc = "INT AUTO_INCREMENT PRIMARY KEY"
            bool_type = "BOOLEAN"
            datetime_type = "DATETIME"
            engine_suffix = " ENGINE=InnoDB"

        users_sql = f"""
            CREATE TABLE IF NOT EXISTS users (
                id {auto_inc},
                nombre TEXT,
                email VARCHAR(320) UNIQUE,
                hash_pw TEXT,
                rol TEXT,
                activo {bool_type} DEFAULT 1
            ){engine_suffix}
        """
        ingresos_sql = f"""
            CREATE TABLE IF NOT EXISTS ingresos (
                id {auto_inc},
                device_id INT NULL,
                estado TEXT,
                ubicacion_id INT NULL
            ){engine_suffix}
        """
        baja_req_sql = f"""
            CREATE TABLE IF NOT EXISTS ingreso_baja_requests (
                id {auto_inc},
                ingreso_id INT NOT NULL,
                usuario_id INT NOT NULL,
                motivo TEXT NOT NULL,
                created_at {datetime_type} DEFAULT CURRENT_TIMESTAMP,
                accepted_at {datetime_type} NULL,
                canceled_at {datetime_type} NULL
            ){engine_suffix}
        """
        customers_sql = f"""
            CREATE TABLE IF NOT EXISTS customers (
                id {auto_inc},
                cod_empresa TEXT,
                razon_social TEXT NOT NULL,
                telefono TEXT
            ){engine_suffix}
        """
        marcas_sql = f"""
            CREATE TABLE IF NOT EXISTS marcas (
                id {auto_inc},
                nombre TEXT NOT NULL
            ){engine_suffix}
        """
        models_sql = f"""
            CREATE TABLE IF NOT EXISTS models (
                id {auto_inc},
                marca_id INT NULL,
                nombre TEXT NOT NULL,
                tipo_equipo TEXT
            ){engine_suffix}
        """
        devices_sql = f"""
            CREATE TABLE IF NOT EXISTS devices (
                id {auto_inc},
                customer_id INT NOT NULL,
                marca_id INT NULL,
                model_id INT NULL,
                numero_serie TEXT,
                numero_interno TEXT,
                n_de_control TEXT,
                alquilado {bool_type} DEFAULT 0,
                alquiler_a TEXT,
                mg_estado TEXT DEFAULT 'activo'
            ){engine_suffix}
        """
        ingreso_events_sql = f"""
            CREATE TABLE IF NOT EXISTS ingreso_events (
                id {auto_inc},
                ticket_id INT NOT NULL,
                a_estado TEXT NOT NULL,
                comentario TEXT,
                ts {datetime_type} DEFAULT CURRENT_TIMESTAMP
            ){engine_suffix}
        """

        with connection.cursor() as cur:
            cur.execute(users_sql)
            cur.execute(customers_sql)
            cur.execute(marcas_sql)
            cur.execute(models_sql)
            cur.execute(devices_sql)
            cur.execute(ingresos_sql)
            cur.execute(baja_req_sql)
            cur.execute(ingreso_events_sql)
            if vendor == "postgresql":
                for statement in (
                    "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS device_id INT NULL",
                    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS numero_interno TEXT",
                    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS n_de_control TEXT",
                    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS alquilado BOOLEAN DEFAULT FALSE",
                    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS alquiler_a TEXT",
                    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS mg_estado TEXT DEFAULT 'activo'",
                ):
                    cur.execute(statement)
        if vendor == "postgresql":
            call_command("apply_bejerman_sync_schema", verbosity=0)

    @classmethod
    def _last_insert_id(cls, cur):
        if connection.vendor == "sqlite":
            cur.execute("SELECT last_insert_rowid()")
        elif connection.vendor == "postgresql":
            cur.execute("SELECT LASTVAL()")
        else:
            cur.execute("SELECT LAST_INSERT_ID()")
        return cur.fetchone()[0]

    @classmethod
    def setUpTestData(cls):
        with connection.cursor() as cur:
            if connection.vendor == "postgresql":
                cur.execute("DELETE FROM bejerman_sync_jobs")
            cur.execute("DELETE FROM ingreso_baja_requests")
            cur.execute("DELETE FROM ingreso_events")
            cur.execute("DELETE FROM ingresos")
            cur.execute("DELETE FROM devices")
            cur.execute("DELETE FROM models")
            cur.execute("DELETE FROM marcas")
            cur.execute("DELETE FROM customers")
        User.objects.all().delete()

        cls.jefe = User.objects.create(
            nombre="Jefe Baja",
            email="jefe-baja@example.com",
            hash_pw="",
            rol="jefe",
            activo=True,
        )
        cls.tecnico = User.objects.create(
            nombre="Tec Baja",
            email="tec-baja@example.com",
            hash_pw="",
            rol="tecnico",
            activo=True,
        )
        cls.jefe_token = issue_token(cls.jefe)
        cls.tecnico_token = issue_token(cls.tecnico)

        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO customers (cod_empresa, razon_social, telefono) VALUES (%s, %s, %s)",
                ["MGBIO", "MG BIO", ""],
            )
            customer_id = cls._last_insert_id(cur)
            cur.execute("INSERT INTO marcas (nombre) VALUES (%s)", ["ResMed"])
            marca_id = cls._last_insert_id(cur)
            cur.execute(
                "INSERT INTO models (marca_id, nombre, tipo_equipo) VALUES (%s, %s, %s)",
                [marca_id, "AirSense 10", "CPAP"],
            )
            model_id = cls._last_insert_id(cur)
            cur.execute(
                """
                INSERT INTO devices (
                    customer_id, marca_id, model_id, numero_serie, numero_interno,
                    n_de_control, alquilado, alquiler_a, mg_estado
                )
                VALUES (%s, %s, %s, %s, %s, %s, FALSE, NULL, 'activo')
                """,
                [customer_id, marca_id, model_id, "SN-BAJA-REQUEST", "MG 7799", "",],
            )
            device_id = cls._last_insert_id(cur)
            cur.execute(
                "INSERT INTO ingresos (device_id, estado) VALUES (%s, %s)",
                [device_id, "diagnosticado"],
            )
            cls.ingreso_id = cls._last_insert_id(cur)

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        with connection.cursor() as cur:
            if connection.vendor == "postgresql":
                cur.execute("DELETE FROM bejerman_sync_jobs")
            cur.execute("DELETE FROM ingreso_baja_requests")

    def _url(self, ingreso_id: int) -> str:
        return f"/api/ingresos/{ingreso_id}/solicitar-baja/rechazar/"

    def _url_baja(self, ingreso_id: int) -> str:
        return f"/api/ingresos/{ingreso_id}/baja/"

    def _insert_pending_request(self):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ingreso_baja_requests
                  (ingreso_id, usuario_id, motivo, created_at, accepted_at, canceled_at)
                VALUES (%s, %s, %s, CURRENT_TIMESTAMP, NULL, NULL)
                """,
                [self.ingreso_id, self.tecnico.id, "Motivo de prueba"],
            )

    def test_rechaza_solicitud_pendiente(self):
        self._insert_pending_request()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.jefe_token}")

        resp = self.client.post(self._url(self.ingreso_id), {}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data.get("ok"))
        self.assertTrue(resp.data.get("rejected"))

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT canceled_at, accepted_at
                  FROM ingreso_baja_requests
                 WHERE ingreso_id=%s
                 ORDER BY id DESC
                 LIMIT 1
                """,
                [self.ingreso_id],
            )
            row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertIsNotNone(row[0])
        self.assertIsNone(row[1])

    def test_rechazo_sin_pendiente_retorna_already_processed(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.jefe_token}")

        resp = self.client.post(self._url(self.ingreso_id), {}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data.get("ok"))
        self.assertTrue(resp.data.get("already_processed"))

    def test_rechazo_devuelve_404_si_ingreso_no_existe(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.jefe_token}")

        resp = self.client.post(self._url(999999), {}, format="json")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.data.get("detail"), "Ingreso no encontrado")

    def test_rechazo_sin_permiso_devuelve_403(self):
        self._insert_pending_request()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.tecnico_token}")

        resp = self.client.post(self._url(self.ingreso_id), {}, format="json")
        self.assertEqual(resp.status_code, 403)

    def test_dar_baja_acepta_solicitud_pendiente(self):
        self._insert_pending_request()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.jefe_token}")

        resp = self.client.post(self._url_baja(self.ingreso_id), {}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data.get("ok"))
        if connection.vendor == "postgresql":
            self.assertEqual(resp.data.get("bejerman_sync_job", {}).get("status"), "pending")
        else:
            self.assertIn("bejerman_sync_job", resp.data)

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT accepted_at, canceled_at
                  FROM ingreso_baja_requests
                 WHERE ingreso_id=%s
                 ORDER BY id DESC
                 LIMIT 1
                """,
                [self.ingreso_id],
            )
            row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertIsNotNone(row[0])
        self.assertIsNone(row[1])
