from unittest import skipUnless

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from service.auth import issue_token
from service.models import User
from service.notifications import (
    emit_notification,
    notify_estado_patrimonial,
    notify_ingreso_asignado,
    notify_ingreso_liberado,
)


@skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL")
class NotificationsAPITest(TestCase):
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
                CREATE TABLE IF NOT EXISTS customers (
                    id BIGSERIAL PRIMARY KEY,
                    cod_empresa TEXT NULL,
                    razon_social TEXT NOT NULL,
                    telefono TEXT NULL,
                    email TEXT NULL
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
                    marca_id INTEGER NULL REFERENCES marcas(id),
                    nombre TEXT NOT NULL,
                    tipo_equipo TEXT,
                    variante TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    id BIGSERIAL PRIMARY KEY,
                    customer_id INTEGER NOT NULL REFERENCES customers(id),
                    marca_id INTEGER NULL REFERENCES marcas(id),
                    model_id INTEGER NULL REFERENCES models(id),
                    numero_serie TEXT,
                    numero_interno TEXT,
                    variante TEXT
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
                    presupuesto_estado TEXT,
                    equipo_variante TEXT,
                    asignado_a INTEGER NULL REFERENCES users(id)
                )
                """
            )
            for statement in (
                "ALTER TABLE customers ADD COLUMN IF NOT EXISTS cod_empresa TEXT NULL",
                "ALTER TABLE customers ADD COLUMN IF NOT EXISTS telefono TEXT NULL",
                "ALTER TABLE customers ADD COLUMN IF NOT EXISTS email TEXT NULL",
                "ALTER TABLE models ADD COLUMN IF NOT EXISTS tipo_equipo TEXT",
                "ALTER TABLE models ADD COLUMN IF NOT EXISTS variante TEXT",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS numero_interno TEXT",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS variante TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS presupuesto_estado TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS equipo_variante TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS asignado_a INTEGER NULL",
            ):
                cur.execute(statement)

        call_command("apply_notifications_schema", verbosity=0)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        with connection.cursor() as cur:
            cur.execute("DELETE FROM notifications")
            cur.execute("DELETE FROM notification_user_preferences")
            cur.execute("DELETE FROM ingresos")
            cur.execute("DELETE FROM devices")
            cur.execute("DELETE FROM models")
            cur.execute("DELETE FROM marcas")
            cur.execute("DELETE FROM customers")
        User.objects.filter(email__endswith="@notif.test").delete()

        cls.jefe = User.objects.create(
            nombre="Jefe Notificaciones",
            email="jefe@notif.test",
            hash_pw="",
            rol="jefe",
            activo=True,
        )
        cls.tecnico = User.objects.create(
            nombre="Técnico Notificaciones",
            email="tecnico@notif.test",
            hash_pw="",
            rol="tecnico",
            activo=True,
        )
        cls.otro = User.objects.create(
            nombre="Técnico Sin Liberado",
            email="otro@notif.test",
            hash_pw="",
            rol="tecnico",
            activo=True,
        )
        cls.jefe_token = issue_token(cls.jefe)
        cls.tecnico_token = issue_token(cls.tecnico)
        cls.otro_token = issue_token(cls.otro)

        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO customers(cod_empresa, razon_social, telefono, email) VALUES (%s,%s,%s,%s) RETURNING id",
                ["CLI-NOTIF", "Clínica Notificaciones", "123", "ops@notif.test"],
            )
            customer_id = int(cur.fetchone()[0])
            cur.execute("INSERT INTO marcas(nombre) VALUES (%s) RETURNING id", ["ResMed"])
            marca_id = int(cur.fetchone()[0])
            cur.execute(
                "INSERT INTO models(marca_id, nombre, tipo_equipo, variante) VALUES (%s,%s,%s,%s) RETURNING id",
                [marca_id, "AirSense 10", "CPAP", ""],
            )
            model_id = int(cur.fetchone()[0])
            cur.execute(
                "INSERT INTO devices(customer_id, marca_id, model_id, numero_serie, numero_interno, variante) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                [customer_id, marca_id, model_id, "NS-NOTIF-001", "MG 9001", ""],
            )
            device_id = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO ingresos(
                    device_id, estado, motivo, fecha_ingreso, fecha_creacion,
                    presupuesto_estado, equipo_variante, asignado_a
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
                """,
                [
                    device_id,
                    "liberado",
                    "reparación",
                    timezone.now(),
                    timezone.now(),
                    "no_aplica",
                    "",
                    cls.tecnico.id,
                ],
            )
            cls.ingreso_id = int(cur.fetchone()[0])

    def setUp(self):
        self.client = APIClient()
        with connection.cursor() as cur:
            cur.execute("DELETE FROM notifications")
            cur.execute("DELETE FROM notification_user_preferences")

    def _auth_as_jefe(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.jefe_token}")

    def _auth_as_tecnico(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.tecnico_token}")

    def _enable_notification(self, user, notification_key, enabled=True):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO notification_user_preferences(user_id, notification_key, enabled)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id, notification_key) DO UPDATE SET enabled=EXCLUDED.enabled
                """,
                [user.id, notification_key, enabled],
            )

    def _enable_liberado_for_tecnico(self):
        self._enable_notification(self.tecnico, "ingreso_liberado", True)

    def test_ingreso_liberado_respeta_preferencia_y_no_duplica(self):
        self._enable_liberado_for_tecnico()

        inserted_1 = notify_ingreso_liberado(self.ingreso_id)
        inserted_2 = notify_ingreso_liberado(self.ingreso_id)

        self.assertEqual(inserted_1, 1)
        self.assertEqual(inserted_2, 0)
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT user_id, COUNT(*)
                  FROM notifications
                 WHERE notification_key = 'ingreso_liberado'
                 GROUP BY user_id
                 ORDER BY user_id
                """
            )
            rows = cur.fetchall()
        self.assertEqual(rows, [(self.tecnico.id, 1)])

    def test_override_activado_no_agrega_destinatarios_ajenos(self):
        self._enable_notification(self.otro, "ingreso_asignado", True)

        inserted = emit_notification(
            "ingreso_asignado",
            user_ids=[self.tecnico.id],
            title="Asignación directa",
            body="Solo el destinatario concreto debe recibirla.",
            href=f"/ingresos/{self.ingreso_id}?tab=principal",
            entity_type="ingreso",
            entity_id=self.ingreso_id,
            dedupe_key="test:asignacion:directa",
            payload={"ingreso_id": self.ingreso_id},
        )

        self.assertEqual(inserted, 1)
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT user_id
                  FROM notifications
                 WHERE notification_key = 'ingreso_asignado'
                   AND dedupe_key = 'test:asignacion:directa'
                 ORDER BY user_id
                """
            )
            rows = cur.fetchall()
        self.assertEqual(rows, [(self.tecnico.id,)])

    def test_ingreso_asignado_solo_notifica_al_tecnico_destinatario(self):
        self._enable_notification(self.tecnico, "ingreso_asignado", True)
        self._enable_notification(self.otro, "ingreso_asignado", True)

        inserted = notify_ingreso_asignado(self.ingreso_id, self.tecnico.id)

        self.assertEqual(inserted, 1)
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT user_id
                  FROM notifications
                 WHERE notification_key = 'ingreso_asignado'
                 ORDER BY user_id
                """
            )
            rows = cur.fetchall()
        self.assertEqual(rows, [(self.tecnico.id,)])

    def test_roles_no_crean_audiencia_interna(self):
        self._enable_notification(self.tecnico, "ingreso_asignado", True)

        inserted = emit_notification(
            "ingreso_asignado",
            roles=["tecnico"],
            title="Rol ignorado",
            body="El rol no debe crear audiencia interna.",
            href=f"/ingresos/{self.ingreso_id}?tab=principal",
            entity_type="ingreso",
            entity_id=self.ingreso_id,
            dedupe_key="test:rol:ignorado",
        )

        self.assertEqual(inserted, 0)

    def test_panel_no_marca_leida_y_click_oculta_notificacion(self):
        self._enable_liberado_for_tecnico()
        notify_ingreso_liberado(self.ingreso_id)
        self._auth_as_tecnico()

        resp = self.client.get("/api/notificaciones/?limit=20")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["unread_count"], 1)
        notification_id = resp.data["items"][0]["id"]

        with connection.cursor() as cur:
            cur.execute("SELECT read_at, clicked_at FROM notifications WHERE id=%s", [notification_id])
            self.assertEqual(cur.fetchone(), (None, None))

        click_resp = self.client.post(f"/api/notificaciones/{notification_id}/click/")
        self.assertEqual(click_resp.status_code, 200)
        self.assertTrue(click_resp.data["href"])

        resp_after = self.client.get("/api/notificaciones/?limit=20")
        self.assertEqual(resp_after.status_code, 200)
        self.assertEqual(resp_after.data["unread_count"], 0)
        self.assertEqual(resp_after.data["items"], [])
        with connection.cursor() as cur:
            cur.execute("SELECT read_at, clicked_at FROM notifications WHERE id=%s", [notification_id])
            read_at, clicked_at = cur.fetchone()
        self.assertIsNotNone(read_at)
        self.assertIsNotNone(clicked_at)

    def test_panel_filtra_por_usuario_y_no_permite_click_ajeno(self):
        emit_notification(
            "ingreso_asignado",
            user_ids=[self.tecnico.id],
            title="Notificación del técnico",
            body="Visible solo para el técnico.",
            href=f"/ingresos/{self.ingreso_id}?tab=principal",
            entity_type="ingreso",
            entity_id=self.ingreso_id,
            dedupe_key="test:panel:tecnico",
        )
        emit_notification(
            "solicitud_baja",
            user_ids=[self.jefe.id],
            title="Notificación del jefe",
            body="Visible solo para el jefe.",
            href=f"/ingresos/{self.ingreso_id}?tab=principal",
            entity_type="ingreso",
            entity_id=self.ingreso_id,
            dedupe_key="test:panel:jefe",
        )
        with connection.cursor() as cur:
            cur.execute("SELECT id FROM notifications WHERE dedupe_key = 'test:panel:tecnico'")
            tecnico_notification_id = cur.fetchone()[0]
            cur.execute("SELECT id FROM notifications WHERE dedupe_key = 'test:panel:jefe'")
            jefe_notification_id = cur.fetchone()[0]

        self._auth_as_tecnico()
        resp = self.client.get("/api/notificaciones/?limit=20")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["unread_count"], 1)
        self.assertEqual([item["id"] for item in resp.data["items"]], [tecnico_notification_id])

        click_resp = self.client.post(f"/api/notificaciones/{jefe_notification_id}/click/")
        self.assertEqual(click_resp.status_code, 404)

    def test_flujo_por_email_crea_notificacion_solo_para_email_resuelto(self):
        inserted = notify_estado_patrimonial(self.ingreso_id, "baja", emails=[self.jefe.email])

        self.assertEqual(inserted, 1)
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT user_id
                  FROM notifications
                 WHERE notification_key = 'baja_patrimonial'
                 ORDER BY user_id
                """
            )
            rows = cur.fetchall()
        self.assertEqual(rows, [(self.jefe.id,)])

    def test_preferencias_heredan_defaults_y_guardan_overrides(self):
        self._auth_as_jefe()

        resp = self.client.get(f"/api/usuarios/{self.tecnico.id}/notificaciones/")
        self.assertEqual(resp.status_code, 200)
        items = {item["key"]: item for item in resp.data["items"]}
        self.assertFalse(items["ingreso_liberado"]["default_enabled"])
        self.assertFalse(items["ingreso_liberado"]["effective_enabled"])
        self.assertTrue(items["ingreso_asignado"]["default_enabled"])
        self.assertTrue(items["ingreso_asignado"]["effective_enabled"])

        put_resp = self.client.put(
            f"/api/usuarios/{self.tecnico.id}/notificaciones/",
            {"preferences": {"ingreso_liberado": True, "ingreso_asignado": None}},
            format="json",
        )
        self.assertEqual(put_resp.status_code, 200)
        items_after = {item["key"]: item for item in put_resp.data["items"]}
        self.assertTrue(items_after["ingreso_liberado"]["override_enabled"])
        self.assertTrue(items_after["ingreso_liberado"]["effective_enabled"])
        self.assertIsNone(items_after["ingreso_asignado"]["override_enabled"])
        self.assertTrue(items_after["ingreso_asignado"]["effective_enabled"])

        reset_resp = self.client.put(
            f"/api/usuarios/{self.tecnico.id}/notificaciones/",
            {"preferences": {"ingreso_liberado": None}},
            format="json",
        )
        self.assertEqual(reset_resp.status_code, 200)
        reset_items = {item["key"]: item for item in reset_resp.data["items"]}
        self.assertIsNone(reset_items["ingreso_liberado"]["override_enabled"])
        self.assertFalse(reset_items["ingreso_liberado"]["effective_enabled"])
