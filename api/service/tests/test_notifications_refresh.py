from unittest import skipUnless
from unittest.mock import patch
from django.db import connection
from django.test import TestCase
from django.core.management import call_command

from service.notifications import emit_notification, user_ids_subscribed_to
from service.repair_ready_notifications import notify_repair_ready_for_remito

@skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL")
class NotificationsRefreshTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
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
            # Create users for testing
            cur.execute("INSERT INTO users (nombre, email, rol, activo) VALUES ('Admin', 'admin@test.com', 'admin', TRUE) RETURNING id")
            cls.admin_id = cur.fetchone()[0]
            
            cur.execute("INSERT INTO users (nombre, email, rol, activo) VALUES ('Jefe', 'jefe@test.com', 'jefe', TRUE) RETURNING id")
            cls.jefe_id = cur.fetchone()[0]
            
            cur.execute("INSERT INTO users (nombre, email, rol, activo) VALUES ('Tecnico', 'tec@test.com', 'tecnico', TRUE) RETURNING id")
            cls.tecnico_id = cur.fetchone()[0]
            
            cur.execute("INSERT INTO users (nombre, email, rol, activo) VALUES ('Recepcion', 'rec@test.com', 'recepcion', TRUE) RETURNING id")
            cls.recepcion_id = cur.fetchone()[0]

    def setUp(self):
        with connection.cursor() as cur:
            cur.execute("DELETE FROM notifications")
            cur.execute("DELETE FROM notification_user_preferences")
            cur.execute("DELETE FROM ingreso_events")
            cur.execute("DELETE FROM ingresos")
            cur.execute("DELETE FROM ingreso_presupuesto_alerts")

    def test_emit_notification_refresh_true(self):
        emit_notification(
            "presupuesto_pendiente",
            title="Primera",
            body="Cuerpo 1",
            user_ids=[self.admin_id],
            dedupe_key="test_refresh_1",
            refresh=True
        )
        with connection.cursor() as cur:
            cur.execute("UPDATE notifications SET read_at = CURRENT_TIMESTAMP WHERE dedupe_key = 'test_refresh_1'")
            
        emit_notification(
            "presupuesto_pendiente",
            title="Segunda",
            body="Cuerpo 2",
            user_ids=[self.admin_id],
            dedupe_key="test_refresh_1",
            refresh=True
        )
        
        with connection.cursor() as cur:
            cur.execute("SELECT title, body, read_at FROM notifications WHERE dedupe_key = 'test_refresh_1'")
            rows = cur.fetchall()
            
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "Segunda")
        self.assertEqual(rows[0][1], "Cuerpo 2")
        self.assertIsNone(rows[0][2])

    def test_emit_notification_refresh_false(self):
        emit_notification(
            "presupuesto_pendiente",
            title="Primera",
            body="Cuerpo 1",
            user_ids=[self.admin_id],
            dedupe_key="test_refresh_false_1",
            refresh=False
        )
        emit_notification(
            "presupuesto_pendiente",
            title="Segunda",
            body="Cuerpo 2",
            user_ids=[self.admin_id],
            dedupe_key="test_refresh_false_1",
            refresh=False
        )
        with connection.cursor() as cur:
            cur.execute("SELECT title, body FROM notifications WHERE dedupe_key = 'test_refresh_false_1'")
            rows = cur.fetchall()
            
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "Primera")

    def test_user_ids_subscribed_to(self):
        subs = user_ids_subscribed_to("reparacion_lista_remito", ["jefe"])
        self.assertIn(self.jefe_id, subs)
        
        with connection.cursor() as cur:
            cur.execute("""
                INSERT INTO notification_user_preferences (user_id, notification_key, enabled, bell_enabled, email_enabled, push_enabled)
                VALUES (%s, 'reparacion_lista_remito', FALSE, FALSE, FALSE, FALSE)
            """, [self.jefe_id])
            
        subs = user_ids_subscribed_to("reparacion_lista_remito", ["jefe"])
        self.assertNotIn(self.jefe_id, subs)

    def test_notify_repair_ready_for_remito_includes_jefe(self):
        with connection.cursor() as cur:
            cur.execute("""
                INSERT INTO ingresos (equipo_id, cliente_id, tecnico_id, estado, remito_entrada)
                VALUES (NULL, NULL, %s, 'reparado', 'RE-123')
                RETURNING id
            """, [self.tecnico_id])
            ingreso_id = cur.fetchone()[0]
            
        with connection.cursor() as cur:
            cur.execute("DELETE FROM notification_user_preferences WHERE user_id = %s", [self.jefe_id])
            
        notify_repair_ready_for_remito(ingreso_id)
        
        with connection.cursor() as cur:
            cur.execute("SELECT user_id FROM notifications WHERE notification_key = 'reparacion_lista_remito' AND entity_id = %s", [str(ingreso_id)])
            user_ids = {row[0] for row in cur.fetchall()}
            
        self.assertIn(self.jefe_id, user_ids)
        self.assertIn(self.recepcion_id, user_ids)
        self.assertIn(self.tecnico_id, user_ids)

    @patch("service.management.commands.send_presupuesto_alerts.queue_notification_email")
    def test_send_presupuesto_alerts_updates_last_sent_even_if_mail_fails(self, mock_queue_email):
        with connection.cursor() as cur:
            cur.execute("""
                INSERT INTO ingresos (equipo_id, cliente_id, tecnico_id, estado, presupuestado_en)
                VALUES (NULL, NULL, NULL, 'presupuesto_pendiente', CURRENT_TIMESTAMP - INTERVAL '5 days')
                RETURNING id
            """)
            ingreso_id = cur.fetchone()[0]
            
        mock_queue_email.side_effect = Exception("Fallo el mail")
        
        try:
            call_command("send_presupuesto_alerts")
        except Exception:
            pass
        
        with connection.cursor() as cur:
            cur.execute("SELECT last_sent_at FROM ingreso_presupuesto_alerts WHERE ingreso_id = %s", [ingreso_id])
            row = cur.fetchone()
            
        self.assertIsNotNone(row)
        self.assertIsNotNone(row[0])
