from django.db import connection
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

        with connection.cursor() as cur:
            cur.execute(users_sql)
            cur.execute(ingresos_sql)
            cur.execute(baja_req_sql)

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
            cur.execute("DELETE FROM ingreso_baja_requests")
            cur.execute("DELETE FROM ingresos")
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
            cur.execute("INSERT INTO ingresos (estado) VALUES (%s)", ["diagnosticado"])
            cls.ingreso_id = cls._last_insert_id(cur)

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        with connection.cursor() as cur:
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
