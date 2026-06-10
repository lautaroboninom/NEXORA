from unittest import skipUnless
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from service.auth import issue_token
from service.models import User
from service.views.mg_state import resolve_mg_flags


@skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL")
class RevisionTecnicaSchemaTest(TestCase):
    @classmethod
    def setUpClass(cls):
        with connection.cursor() as cur:
            cur.execute(
                """
                DO $$
                BEGIN
                  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'motivo_ingreso') THEN
                    CREATE TYPE motivo_ingreso AS ENUM (
                      'reparación',
                      'service preventivo',
                      'baja alquiler',
                      'reparación alquiler',
                      'urgente control',
                      'devolución demo',
                      'cotización de equipo',
                      'otros'
                    );
                  END IF;
                END
                $$;
                """
            )
        super().setUpClass()

    def test_apply_revision_tecnica_schema_es_idempotente(self):
        call_command("apply_revision_tecnica_schema", verbosity=0)
        call_command("apply_revision_tecnica_schema", verbosity=0)

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT e.enumlabel
                  FROM pg_type t
                  JOIN pg_enum e ON e.enumtypid = t.oid
                 WHERE t.typname = 'motivo_ingreso'
                 ORDER BY e.enumsortorder
                """
            )
            labels = [row[0] for row in cur.fetchall()]

        self.assertIn("Revisión Técnica", labels)


@skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL")
class IngresoConvertirPropioMgAPITest(TestCase):
    @classmethod
    def setUpClass(cls):
        with connection.cursor() as cur:
            cur.execute(
                """
                DO $$
                BEGIN
                  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'motivo_ingreso') THEN
                    CREATE TYPE motivo_ingreso AS ENUM (
                      'reparación',
                      'service preventivo',
                      'baja alquiler',
                      'reparación alquiler',
                      'urgente control',
                      'devolución demo',
                      'cotización de equipo',
                      'otros'
                    );
                  END IF;
                END
                $$;
                """
            )
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
                CREATE TABLE IF NOT EXISTS ingreso_events (
                    id BIGSERIAL PRIMARY KEY,
                    ticket_id INTEGER NULL,
                    ingreso_id INTEGER NULL,
                    a_estado TEXT,
                    comentario TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ingreso_baja_requests (
                    id BIGSERIAL PRIMARY KEY,
                    ingreso_id INTEGER NOT NULL,
                    usuario_id INTEGER NULL,
                    motivo TEXT,
                    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    accepted_at TIMESTAMPTZ NULL,
                    canceled_at TIMESTAMPTZ NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_permission_overrides (
                    user_id INTEGER NOT NULL,
                    permission_code TEXT NOT NULL,
                    effect TEXT NOT NULL,
                    PRIMARY KEY (user_id, permission_code)
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
                CREATE TABLE IF NOT EXISTS devices (
                    id BIGSERIAL PRIMARY KEY,
                    customer_id INTEGER NOT NULL REFERENCES customers(id),
                    marca_id INTEGER REFERENCES marcas(id),
                    model_id INTEGER REFERENCES models(id),
                    numero_serie TEXT,
                    numero_interno TEXT,
                    alquilado BOOLEAN NOT NULL DEFAULT FALSE,
                    alquiler_a TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS locations (
                    id BIGSERIAL PRIMARY KEY,
                    nombre TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ingresos (
                    id BIGSERIAL PRIMARY KEY,
                    device_id INTEGER NULL REFERENCES devices(id),
                    estado TEXT,
                    motivo TEXT,
                    ubicacion_id INTEGER NULL REFERENCES locations(id),
                    informe_preliminar TEXT,
                    descripcion_problema TEXT,
                    trabajos_realizados TEXT,
                    comentarios TEXT,
                    resolucion TEXT,
                    fecha_ingreso TIMESTAMPTZ NULL,
                    fecha_creacion TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        call_command("apply_revision_tecnica_schema", verbosity=0)
        super().setUpClass()

    @classmethod
    def _last_id(cls, cur):
        cur.execute("SELECT LASTVAL()")
        return int(cur.fetchone()[0])

    @classmethod
    def setUpTestData(cls):
        with connection.cursor() as cur:
            cur.execute("DELETE FROM user_permission_overrides")
            cur.execute("DELETE FROM ingresos")
            cur.execute("DELETE FROM devices")
            cur.execute("DELETE FROM models")
            cur.execute("DELETE FROM marcas")
            cur.execute("DELETE FROM customers")
        User.objects.all().delete()

        cls.user = User.objects.create(
            nombre="Admin MG",
            email="admin-revision-tecnica@example.com",
            hash_pw="",
            rol="admin",
            activo=True,
        )
        cls.token = issue_token(cls.user)
        cls.recepcion = User.objects.create(
            nombre="Recepcion MG",
            email="recepcion-revision-tecnica@example.com",
            hash_pw="",
            rol="recepcion",
            activo=True,
        )
        cls.recepcion_token = issue_token(cls.recepcion)
        cls.tecnico = User.objects.create(
            nombre="Tecnico MG",
            email="tecnico-revision-tecnica@example.com",
            hash_pw="",
            rol="tecnico",
            activo=True,
        )
        cls.tecnico_token = issue_token(cls.tecnico)

        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_permission_overrides (user_id, permission_code, effect)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id, permission_code) DO UPDATE
                SET effect = EXCLUDED.effect
                """,
                [cls.recepcion.id, "action.ingreso.baja_alta", "allow"],
            )
            cur.execute(
                "INSERT INTO customers(cod_empresa, razon_social, telefono) VALUES (%s,%s,%s)",
                ["CLI001", "Clinica Demo", "123"],
            )
            cls.customer_cli_id = cls._last_id(cur)
            cur.execute(
                "INSERT INTO customers(cod_empresa, razon_social, telefono) VALUES (%s,%s,%s)",
                ["MGBIO", "MG BIO", ""],
            )
            cls.customer_mg_id = cls._last_id(cur)
            cur.execute("INSERT INTO marcas(nombre) VALUES (%s)", ["ResMed"])
            cls.marca_id = cls._last_id(cur)
            cur.execute(
                "INSERT INTO models(marca_id, nombre, tipo_equipo) VALUES (%s,%s,%s)",
                [cls.marca_id, "AirSense 10", "CPAP"],
            )
            cls.model_id = cls._last_id(cur)

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self._auth(self.token)

    def _auth(self, token):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def _crear_equipo(self, *, numero_serie="NS-RT-100", numero_interno="", customer_id=None):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO devices(customer_id, marca_id, model_id, numero_serie, numero_interno, alquilado, alquiler_a)
                VALUES (%s,%s,%s,%s,NULLIF(%s,''),FALSE,NULL)
                """,
                [customer_id or self.customer_cli_id, self.marca_id, self.model_id, numero_serie, numero_interno],
            )
            return self._last_id(cur)

    def _crear_ingreso(self, *, device_id, motivo="Revisión Técnica"):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ingresos(device_id, estado, motivo, fecha_ingreso)
                VALUES (%s,'ingresado',%s,now())
                """,
                [device_id, motivo],
            )
            return self._last_id(cur)

    def _url(self, ingreso_id):
        return f"/api/ingresos/{ingreso_id}/convertir-propio-mg/"

    def _url_baja(self, ingreso_id):
        return f"/api/ingresos/{ingreso_id}/baja/"

    def _url_solicitar_baja(self, ingreso_id):
        return f"/api/ingresos/{ingreso_id}/solicitar-baja/"

    def test_convierte_equipo_a_propio_mg_en_mismo_device(self):
        device_id = self._crear_equipo()
        ingreso_id = self._crear_ingreso(device_id=device_id)

        resp = self.client.post(
            self._url(ingreso_id),
            {"numero_interno": "MG 0123"},
            format="json",
        )

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data.get("ok"))
        self.assertEqual(resp.data["device"]["id"], device_id)
        self.assertEqual(resp.data["device"]["customer_id"], self.customer_mg_id)
        self.assertEqual(resp.data["device"]["numero_interno"], "MG 0123")
        self.assertTrue(resp.data["device"]["es_propietario_mg"])

        with connection.cursor() as cur:
            cur.execute("SELECT customer_id, numero_interno, alquilado, alquiler_a FROM devices WHERE id=%s", [device_id])
            row = cur.fetchone()
        self.assertEqual(row[0], self.customer_mg_id)
        self.assertEqual(row[1], "MG 0123")
        self.assertFalse(row[2])
        self.assertIsNone(row[3])

    def test_convierte_equipo_con_mg_precargado_si_sigue_asociado_a_cliente(self):
        device_id = self._crear_equipo(numero_serie="NS-RT-100A", numero_interno="MG 0123")
        ingreso_id = self._crear_ingreso(device_id=device_id)

        resp = self.client.post(
            self._url(ingreso_id),
            {"numero_interno": "MG 0123"},
            format="json",
        )

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data.get("ok"))
        self.assertEqual(resp.data["device"]["customer_id"], self.customer_mg_id)
        self.assertTrue(resp.data["device"].get("es_cliente_mg_owner"))

    def test_codigo_mg_no_implica_propiedad_mg(self):
        flags = resolve_mg_flags(
            {
                "customer_id": self.customer_cli_id,
                "numero_interno": "MG 0123",
                "numero_serie": "NS-RT-100A1",
                "alquilado": False,
            },
            self.customer_mg_id,
        )

        self.assertTrue(flags.get("tiene_codigo_mg"))
        self.assertFalse(flags.get("es_propietario_mg"))

    @override_settings(BAJA_NOTIFY_RECIPIENTS=["patrimonio@example.com"])
    @patch("service.views.ingresos_views._send_mail_with_fallback", return_value=(True, {}))
    def test_alta_mg_envia_mail_patrimonial(self, mock_send):
        device_id = self._crear_equipo(numero_serie="NS-RT-100B")
        ingreso_id = self._crear_ingreso(device_id=device_id)

        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(
                self._url(ingreso_id),
                {"numero_interno": "MG 0555"},
                format="json",
            )

        self.assertEqual(resp.status_code, 200)
        mock_send.assert_called_once()
        subject, body, recipients = mock_send.call_args[0]
        self.assertIn("alta de MG", subject)
        self.assertIn("MG 0555", body)
        self.assertEqual(recipients, ["patrimonio@example.com"])

    def test_permite_alta_mg_con_permiso_baja_alta(self):
        self._auth(self.recepcion_token)
        device_id = self._crear_equipo(numero_serie="NS-RT-100C")
        ingreso_id = self._crear_ingreso(device_id=device_id)

        resp = self.client.post(
            self._url(ingreso_id),
            {"numero_interno": "MG 0777"},
            format="json",
        )

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data.get("ok"))

    def test_rechaza_baja_directa_si_equipo_no_es_propio_mg(self):
        device_id = self._crear_equipo(numero_serie="NS-RT-100D", customer_id=self.customer_cli_id)
        ingreso_id = self._crear_ingreso(device_id=device_id)

        resp = self.client.post(self._url_baja(ingreso_id), {}, format="json")

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("detail"), "Solo se puede dar de baja un equipo propio MG.")
        with connection.cursor() as cur:
            cur.execute("SELECT estado FROM ingresos WHERE id=%s", [ingreso_id])
            estado = cur.fetchone()[0]
        self.assertEqual(estado, "ingresado")

    def test_permite_baja_directa_si_equipo_es_propio_mg(self):
        device_id = self._crear_equipo(numero_serie="NS-RT-100E", numero_interno="MG 0888", customer_id=self.customer_mg_id)
        ingreso_id = self._crear_ingreso(device_id=device_id)

        resp = self.client.post(self._url_baja(ingreso_id), {}, format="json")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("estado"), "baja")
        with connection.cursor() as cur:
            cur.execute("SELECT estado FROM ingresos WHERE id=%s", [ingreso_id])
            estado = cur.fetchone()[0]
        self.assertEqual(estado, "baja")

    def test_rechaza_solicitud_baja_si_equipo_no_es_propio_mg(self):
        self._auth(self.tecnico_token)
        device_id = self._crear_equipo(numero_serie="NS-RT-100F", customer_id=self.customer_cli_id)
        ingreso_id = self._crear_ingreso(device_id=device_id)

        resp = self.client.post(
            self._url_solicitar_baja(ingreso_id),
            {"motivo": "Prueba"},
            format="json",
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("detail"), "Solo se puede solicitar BAJA para equipos propios MG.")

    def test_permite_alta_mg_con_cualquier_motivo(self):
        device_id = self._crear_equipo(numero_serie="NS-RT-101")
        ingreso_id = self._crear_ingreso(device_id=device_id, motivo="otros")

        resp = self.client.post(self._url(ingreso_id), {"numero_interno": "MG 0456"}, format="json")

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data.get("ok"))
        self.assertEqual(resp.data["device"]["customer_id"], self.customer_mg_id)

    def test_rechaza_mg_duplicado(self):
        self._crear_equipo(numero_serie="NS-RT-102", numero_interno="MG 0456", customer_id=self.customer_mg_id)
        device_id = self._crear_equipo(numero_serie="NS-RT-103")
        ingreso_id = self._crear_ingreso(device_id=device_id)

        resp = self.client.post(self._url(ingreso_id), {"numero_interno": "MG 0456"}, format="json")

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("conflict_type"), "MG_DUPLICATE")

    def test_rechaza_si_equipo_ya_es_mg(self):
        device_id = self._crear_equipo(numero_serie="NS-RT-104", numero_interno="MG 0789", customer_id=self.customer_mg_id)
        ingreso_id = self._crear_ingreso(device_id=device_id)

        resp = self.client.post(self._url(ingreso_id), {"numero_interno": "MG 0789"}, format="json")

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("conflict_type"), "EQUIPO_YA_PROPIO_MG")

    def test_rechaza_si_no_existe_cliente_mg_bio(self):
        device_id = self._crear_equipo(numero_serie="NS-RT-105")
        ingreso_id = self._crear_ingreso(device_id=device_id)

        with connection.cursor() as cur:
            cur.execute("DELETE FROM customers WHERE id=%s", [self.customer_mg_id])

        resp = self.client.post(self._url(ingreso_id), {"numero_interno": "MG 0999"}, format="json")

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.data.get("conflict_type"), "MG_OWNER_CUSTOMER_MISSING")
