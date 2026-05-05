from datetime import timedelta
from unittest import skipUnless
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from service.auth import issue_token
from service.models import User


@skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL")
class IngresoHistoricalCorrectionsAPITest(TestCase):
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
                CREATE TABLE IF NOT EXISTS audit_log (
                    id BIGSERIAL PRIMARY KEY,
                    ts TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    user_id INTEGER NULL,
                    role TEXT,
                    method TEXT NOT NULL,
                    path TEXT NOT NULL,
                    ip TEXT,
                    user_agent TEXT,
                    status_code INTEGER NOT NULL,
                    body JSONB
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS customers (
                    id BIGSERIAL PRIMARY KEY,
                    cod_empresa TEXT,
                    razon_social TEXT NOT NULL,
                    telefono TEXT
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
                    marca_id INTEGER REFERENCES marcas(id),
                    nombre TEXT NOT NULL,
                    tipo_equipo TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS locations (
                    id BIGSERIAL PRIMARY KEY,
                    nombre TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    id BIGSERIAL PRIMARY KEY,
                    customer_id INTEGER NOT NULL REFERENCES customers(id),
                    marca_id INTEGER REFERENCES marcas(id),
                    model_id INTEGER REFERENCES models(id),
                    numero_serie TEXT,
                    numero_interno TEXT,
                    alquilado BOOLEAN NOT NULL DEFAULT FALSE
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ingresos (
                    id BIGSERIAL PRIMARY KEY,
                    device_id INTEGER NOT NULL REFERENCES devices(id),
                    estado TEXT,
                    motivo TEXT,
                    fecha_ingreso TIMESTAMPTZ NULL,
                    fecha_creacion TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    fecha_entrega TIMESTAMPTZ NULL,
                    ubicacion_id INTEGER NULL REFERENCES locations(id),
                    informe_preliminar TEXT,
                    descripcion_problema TEXT,
                    trabajos_realizados TEXT,
                    comentarios TEXT,
                    resolucion TEXT,
                    remito_salida TEXT,
                    factura_numero TEXT,
                    alquilado BOOLEAN NOT NULL DEFAULT FALSE,
                    alquiler_a TEXT,
                    alquiler_remito TEXT,
                    alquiler_fecha DATE
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ingreso_events (
                    id BIGSERIAL PRIMARY KEY,
                    ticket_id INTEGER NOT NULL REFERENCES ingresos(id) ON DELETE CASCADE,
                    de_estado TEXT NULL,
                    a_estado TEXT NOT NULL,
                    usuario_id INTEGER NULL REFERENCES users(id),
                    ts TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    comentario TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS device_mg_events (
                    id BIGSERIAL PRIMARY KEY,
                    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                    accion TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'equipos'
                )
                """
            )

        call_command("apply_user_permissions_schema")
        call_command("apply_historical_corrections_schema")
        super().setUpClass()

    @classmethod
    def _last_id(cls, cur):
        cur.execute("SELECT LASTVAL()")
        return int(cur.fetchone()[0])

    @classmethod
    def setUpTestData(cls):
        with connection.cursor() as cur:
            cur.execute("DELETE FROM ingreso_historical_corrections")
            cur.execute("DELETE FROM ingreso_events")
            cur.execute("DELETE FROM ingresos")
            cur.execute("DELETE FROM device_mg_events")
            cur.execute("DELETE FROM devices")
            cur.execute("DELETE FROM models")
            cur.execute("DELETE FROM marcas")
            cur.execute("DELETE FROM locations")
            cur.execute("DELETE FROM customers")
            cur.execute("DELETE FROM user_permission_overrides")
        User.objects.all().delete()

        cls.admin = User.objects.create(
            nombre="Admin Hist",
            email="admin-hist@example.com",
            hash_pw="",
            rol="admin",
            activo=True,
        )
        cls.tecnico = User.objects.create(
            nombre="Tec Hist",
            email="tec-hist@example.com",
            hash_pw="",
            rol="tecnico",
            activo=True,
        )
        cls.admin_token = issue_token(cls.admin)
        cls.tecnico_token = issue_token(cls.tecnico)

        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO customers(cod_empresa, razon_social, telefono) VALUES (%s,%s,%s)",
                ["CLI-HIST", "Clinica Historica", "123"],
            )
            cls.customer_id = cls._last_id(cur)
            cur.execute("INSERT INTO marcas(nombre) VALUES (%s)", ["ResMed"])
            cls.marca_id = cls._last_id(cur)
            cur.execute(
                "INSERT INTO models(marca_id, nombre, tipo_equipo) VALUES (%s,%s,%s)",
                [cls.marca_id, "AirSense 10", "CPAP"],
            )
            cls.model_id = cls._last_id(cur)
            cur.execute("INSERT INTO locations(nombre) VALUES (%s)", ["-"])
            cls.loc_dash_id = cls._last_id(cur)
            cur.execute("INSERT INTO locations(nombre) VALUES (%s)", ["Taller"])
            cls.loc_taller_id = cls._last_id(cur)

            cur.execute(
                """
                INSERT INTO devices(customer_id, marca_id, model_id, numero_serie, numero_interno, alquilado)
                VALUES (%s,%s,%s,%s,%s,FALSE)
                """,
                [cls.customer_id, cls.marca_id, cls.model_id, "NS-HIST-001", "MG 1001"],
            )
            cls.device_id = cls._last_id(cur)

            cur.execute(
                """
                INSERT INTO ingresos(device_id, estado, motivo, fecha_ingreso, ubicacion_id, alquilado)
                VALUES (%s,%s,%s,%s,%s,FALSE)
                """,
                [cls.device_id, "diagnosticado", "reparacion", timezone.now(), cls.loc_taller_id],
            )
            cls.ingreso_id = cls._last_id(cur)

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.admin_token}")
        with connection.cursor() as cur:
            cur.execute("DELETE FROM ingreso_historical_corrections WHERE ingreso_id=%s", [self.ingreso_id])
            cur.execute("DELETE FROM ingreso_events WHERE ticket_id=%s", [self.ingreso_id])
            cur.execute(
                """
                UPDATE ingresos
                   SET estado='diagnosticado',
                       fecha_entrega=NULL,
                       remito_salida=NULL,
                       factura_numero=NULL,
                       alquilado=FALSE,
                       alquiler_a=NULL,
                       alquiler_remito=NULL,
                       alquiler_fecha=NULL,
                       ubicacion_id=%s
                 WHERE id=%s
                """,
                [self.loc_taller_id, self.ingreso_id],
            )

    def _url(self, ingreso_id=None):
        iid = ingreso_id or self.ingreso_id
        return f"/api/ingresos/{iid}/correcciones-historicas/"

    def test_rechaza_sin_permiso(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.tecnico_token}")
        payload = {
            "accion": "entrega",
            "fecha_efectiva": timezone.now().isoformat(),
            "motivo": "Ajuste histórico",
            "factura_numero": "FAC-H-1",
        }
        resp = self.client.post(self._url(), payload, format="json")
        self.assertEqual(resp.status_code, 403)

    def test_rechaza_fecha_futura_y_motivo_faltante(self):
        futura = timezone.now() + timedelta(days=1)
        resp_future = self.client.post(
            self._url(),
            {
                "accion": "entrega",
                "fecha_efectiva": futura.isoformat(),
                "motivo": "Carga fuera de termino",
            },
            format="json",
        )
        self.assertEqual(resp_future.status_code, 400)
        self.assertEqual(resp_future.data.get("detail"), "fecha_efectiva no puede ser futura")

        resp_no_reason = self.client.post(
            self._url(),
            {
                "accion": "entrega",
                "fecha_efectiva": timezone.now().isoformat(),
            },
            format="json",
        )
        self.assertEqual(resp_no_reason.status_code, 400)
        self.assertEqual(resp_no_reason.data.get("detail"), "motivo requerido")

    def test_rechaza_accion_invalida(self):
        resp = self.client.post(
            self._url(),
            {
                "accion": "invalid_action",
                "fecha_efectiva": timezone.now().isoformat(),
                "motivo": "Prueba",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("detail"), "accion inválida")

    def test_entrega_forzada_actualiza_ingreso_evento_y_auditoria(self):
        fecha_efectiva = timezone.localtime(timezone.now() - timedelta(days=4)).replace(second=0, microsecond=0)
        payload = {
            "accion": "entrega",
            "fecha_efectiva": fecha_efectiva.isoformat(),
            "motivo": "Regularización de migración",
            "factura_numero": "FAC-H-100",
            "remito_salida": "REM-H-100",
        }
        resp = self.client.post(self._url(), payload, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data.get("ok"))
        self.assertEqual(resp.data.get("estado"), "entregado")

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT estado, factura_numero, remito_salida, fecha_entrega
                  FROM ingresos
                 WHERE id=%s
                """,
                [self.ingreso_id],
            )
            ingreso_row = cur.fetchone()
            cur.execute(
                """
                SELECT a_estado, ts
                  FROM ingreso_events
                 WHERE ticket_id=%s
                 ORDER BY id DESC
                 LIMIT 1
                """,
                [self.ingreso_id],
            )
            event_row = cur.fetchone()
            cur.execute(
                """
                SELECT accion, fecha_efectiva, motivo, notificar, created_at
                  FROM ingreso_historical_corrections
                 WHERE ingreso_id=%s
                 ORDER BY id DESC
                 LIMIT 1
                """,
                [self.ingreso_id],
            )
            audit_row = cur.fetchone()

        self.assertIsNotNone(ingreso_row)
        self.assertEqual(ingreso_row[0], "entregado")
        self.assertEqual(ingreso_row[1], "FAC-H-100")
        self.assertEqual(ingreso_row[2], "REM-H-100")
        self.assertIsNotNone(ingreso_row[3])

        self.assertIsNotNone(event_row)
        self.assertEqual(event_row[0], "entregado")
        self.assertEqual(int(event_row[1].timestamp()), int(fecha_efectiva.timestamp()))

        self.assertIsNotNone(audit_row)
        self.assertEqual(audit_row[0], "entrega")
        self.assertEqual(int(audit_row[1].timestamp()), int(fecha_efectiva.timestamp()))
        self.assertEqual(audit_row[2], "Regularización de migración")
        self.assertTrue(bool(audit_row[3]))
        self.assertIsNotNone(audit_row[4])

    def test_alta_y_baja_alquiler_forzadas(self):
        fecha_alta = timezone.localtime(timezone.now() - timedelta(days=6)).replace(second=0, microsecond=0)
        resp_alta = self.client.post(
            self._url(),
            {
                "accion": "alta_alquiler",
                "fecha_efectiva": fecha_alta.isoformat(),
                "motivo": "Alta histórica no cargada",
                "alquiler_a": "Clinica Centro",
                "alquiler_remito": "REM-ALQ-44",
                "alquiler_fecha": "2026-03-12",
            },
            format="json",
        )
        self.assertEqual(resp_alta.status_code, 200)
        self.assertEqual(resp_alta.data.get("estado"), "alquilado")

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT estado, alquilado, alquiler_a, alquiler_remito, alquiler_fecha
                  FROM ingresos
                 WHERE id=%s
                """,
                [self.ingreso_id],
            )
            alta_row = cur.fetchone()
        self.assertEqual(alta_row[0], "alquilado")
        self.assertTrue(bool(alta_row[1]))
        self.assertEqual(alta_row[2], "Clinica Centro")
        self.assertEqual(alta_row[3], "REM-ALQ-44")
        self.assertIsNotNone(alta_row[4])

        fecha_baja = timezone.localtime(timezone.now() - timedelta(days=3)).replace(second=0, microsecond=0)
        resp_baja = self.client.post(
            self._url(),
            {
                "accion": "baja_alquiler",
                "fecha_efectiva": fecha_baja.isoformat(),
                "motivo": "Baja histórica no cargada",
            },
            format="json",
        )
        self.assertEqual(resp_baja.status_code, 200)
        self.assertEqual(resp_baja.data.get("estado"), "ingresado")

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT estado, alquilado, alquiler_a, alquiler_remito, alquiler_fecha
                  FROM ingresos
                 WHERE id=%s
                """,
                [self.ingreso_id],
            )
            baja_row = cur.fetchone()
            cur.execute(
                """
                SELECT accion
                  FROM ingreso_historical_corrections
                 WHERE ingreso_id=%s
                 ORDER BY id ASC
                """,
                [self.ingreso_id],
            )
            acciones = [r[0] for r in cur.fetchall()]

        self.assertEqual(baja_row[0], "ingresado")
        self.assertFalse(bool(baja_row[1]))
        self.assertIsNone(baja_row[2])
        self.assertIsNone(baja_row[3])
        self.assertIsNone(baja_row[4])
        self.assertEqual(acciones, ["alta_alquiler", "baja_alquiler"])

    def test_notificar_en_baja_alta_respeta_flag(self):
        fecha_baja = timezone.localtime(timezone.now() - timedelta(days=2)).replace(second=0, microsecond=0)
        fecha_alta = timezone.localtime(timezone.now() - timedelta(days=1)).replace(second=0, microsecond=0)

        with patch("service.views.ingresos_views._notify_estado_patrimonial") as notify_mock:
            resp_baja = self.client.post(
                self._url(),
                {
                    "accion": "baja_ingreso",
                    "fecha_efectiva": fecha_baja.isoformat(),
                    "motivo": "Baja histórica administrativa",
                    "notificar": False,
                },
                format="json",
            )
            self.assertEqual(resp_baja.status_code, 200)
            self.assertEqual(resp_baja.data.get("estado"), "baja")
            notify_mock.assert_not_called()

            resp_alta = self.client.post(
                self._url(),
                {
                    "accion": "alta_ingreso",
                    "fecha_efectiva": fecha_alta.isoformat(),
                    "motivo": "Alta histórica administrativa",
                    "notificar": True,
                },
                format="json",
            )
            self.assertEqual(resp_alta.status_code, 200)
            self.assertEqual(resp_alta.data.get("estado"), "ingresado")
            self.assertEqual(notify_mock.call_count, 1)

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT accion, notificar
                  FROM ingreso_historical_corrections
                 WHERE ingreso_id=%s
                 ORDER BY id ASC
                """,
                [self.ingreso_id],
            )
            rows = cur.fetchall()
        self.assertEqual(rows[0][0], "baja_ingreso")
        self.assertFalse(bool(rows[0][1]))
        self.assertEqual(rows[1][0], "alta_ingreso")
        self.assertTrue(bool(rows[1][1]))
