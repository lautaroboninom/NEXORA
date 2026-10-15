from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from service.auth import make_hash
from service.bejerman_user_credentials import (
    BejermanUserCredentialsInvalid,
    decrypt_bejerman_password,
)
from service.middleware import redact_sensitive_audit_data
from service.models import User


class UsuariosAdminApiTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if connection.vendor != "postgresql":
            raise RuntimeError("Estas pruebas enfocadas requieren PostgreSQL")
        with connection.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                  nombre TEXT NOT NULL,
                  email TEXT NOT NULL UNIQUE,
                  hash_pw TEXT NULL,
                  rol TEXT NOT NULL,
                  activo BOOLEAN NOT NULL DEFAULT TRUE,
                  creado_en TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  bejerman_seller_code TEXT NULL,
                  bejerman_seller_code_confirmed_at TIMESTAMPTZ NULL
                )
                """
            )
            cur.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS creado_en TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP"
            )
            cur.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS bejerman_seller_code TEXT NULL"
            )
            cur.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS bejerman_seller_code_confirmed_at TIMESTAMPTZ NULL"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS password_reset_tokens (
                  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                  token_hash TEXT NOT NULL,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  expires_at TIMESTAMPTZ NOT NULL,
                  used_at TIMESTAMPTZ NULL,
                  ip TEXT NULL,
                  user_agent TEXT NULL
                )
                """
            )
        call_command("apply_user_permissions_schema", verbosity=0)
        call_command("apply_bejerman_user_credentials_schema", verbosity=0)
        call_command("apply_notifications_schema", verbosity=0)
        call_command("apply_users_admin_schema", verbosity=0)

    def setUp(self):
        with connection.cursor() as cur:
            cur.execute("DELETE FROM notification_user_preferences")
            cur.execute("DELETE FROM user_permission_overrides")
            cur.execute("DELETE FROM user_bejerman_credentials")
            cur.execute("DELETE FROM password_reset_tokens")
        User.objects.all().delete()
        self.jefe = User.objects.create(
            nombre="Jefa de prueba",
            email="jefa-usuarios@example.com",
            hash_pw=make_hash("Clave-segura-123"),
            rol="jefe",
            activo=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.jefe)

    def _create_target(self, **overrides):
        values = {
            "nombre": "Técnico de prueba",
            "email": "tecnico-usuarios@example.com",
            "hash_pw": make_hash("Clave-tecnico-123"),
            "rol": "tecnico",
            "activo": True,
        }
        values.update(overrides)
        return User.objects.create(**values)

    def test_list_and_detail_return_operational_summary_without_secrets(self):
        target = self._create_target()
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                   SET last_login_at = CURRENT_TIMESTAMP,
                       last_login_ip = '10.20.30.40',
                       last_login_user_agent = 'Mozilla/5.0 Chrome/126.0 Windows NT 10.0'
                 WHERE id = %s
                """,
                [target.id],
            )
            cur.execute(
                """
                INSERT INTO user_permission_overrides(
                  user_id, permission_code, effect, updated_by
                )
                VALUES (%s, 'page.users', 'allow', %s)
                """,
                [target.id, self.jefe.id],
            )
            cur.execute(
                """
                INSERT INTO notification_user_preferences(
                  user_id, notification_key, enabled, updated_by
                )
                VALUES (%s, 'presupuesto_pendiente', TRUE, %s)
                """,
                [target.id, self.jefe.id],
            )
            cur.execute(
                """
                INSERT INTO user_bejerman_credentials(
                  user_id, bejerman_username, encrypted_password, is_valid,
                  validated_at
                )
                VALUES (%s, 'SPTEC1', 'cifrado-no-expuesto', TRUE, CURRENT_TIMESTAMP)
                """,
                [target.id],
            )

        response = self.client.get("/api/usuarios/")
        self.assertEqual(response.status_code, 200)
        row = next(item for item in response.data if item["id"] == target.id)
        self.assertTrue(row["password_configured"])
        self.assertEqual(row["last_login_ip"], "10.20.30.40")
        self.assertIn("Chrome", row["last_login_user_agent"])
        self.assertEqual(row["permisos_personalizados"], 1)
        self.assertEqual(row["notificaciones_personalizadas"], 1)
        self.assertEqual(row["bejerman_credentials_username"], "S****1")
        self.assertNotIn("encrypted_password", row)

        detail = self.client.get(f"/api/usuarios/{target.id}/")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data["id"], target.id)

    @patch("service.views.usuarios_views.send_mail", return_value=0)
    def test_create_is_explicit_and_duplicate_email_returns_conflict(self, _send_mail):
        created = self.client.post(
            "/api/usuarios/",
            {
                "nombre": "Nueva usuaria",
                "email": "nueva-usuarios@example.com",
                "rol": "tecnico",
            },
            format="json",
        )
        self.assertEqual(created.status_code, 201)
        self.assertTrue(created.data["created"])
        self.assertFalse(created.data["invitation"]["sent"])
        created_id = created.data["user"]["id"]

        duplicate = self.client.post(
            "/api/usuarios/",
            {
                "nombre": "Nombre que no debe reemplazar",
                "email": "nueva-usuarios@example.com",
                "rol": "admin",
            },
            format="json",
        )
        self.assertEqual(duplicate.status_code, 409)
        persisted = User.objects.get(id=created_id)
        self.assertEqual(persisted.nombre, "Nueva usuaria")
        self.assertEqual(persisted.rol, "tecnico")

    def test_last_active_jefe_cannot_be_deactivated_or_downgraded(self):
        deactivate = self.client.patch(
            f"/api/usuarios/{self.jefe.id}/activar/",
            {"activo": False},
            format="json",
        )
        self.assertEqual(deactivate.status_code, 400)
        self.assertIn("al menos un usuario Jefe", str(deactivate.data))

        downgrade = self.client.patch(
            f"/api/usuarios/{self.jefe.id}/roleperm/",
            {"rol": "admin"},
            format="json",
        )
        self.assertEqual(downgrade.status_code, 400)
        self.assertEqual(User.objects.get(id=self.jefe.id).rol, "jefe")

    def _admin_con_gestion_de_usuarios(self):
        admin = self._create_target(
            nombre="Admin con gestión de usuarios",
            email="admin-usuarios@example.com",
            rol="admin",
        )
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_permission_overrides(user_id, permission_code, effect, updated_by)
                VALUES (%s, 'action.users.manage', 'allow', %s),
                       (%s, 'page.users', 'allow', %s)
                """,
                [admin.id, self.jefe.id, admin.id, self.jefe.id],
            )
        client = APIClient()
        client.force_authenticate(user=admin)
        return admin, client

    def test_gestionar_usuarios_no_alcanza_para_asignar_jefatura(self):
        # `action.users.manage` se otorga por usuario: si con eso se pudiera cambiar a rol
        # Jefe, a quien se lo dieran le alcanzaba para quedarse con todo.
        admin, client = self._admin_con_gestion_de_usuarios()
        target = self._create_target()

        propio = client.patch(f"/api/usuarios/{admin.id}/", {"rol": "jefe"}, format="json")
        self.assertEqual(propio.status_code, 400)
        ajeno = client.patch(
            f"/api/usuarios/{target.id}/roleperm/",
            {"rol": "jefe_suplente"},
            format="json",
        )
        self.assertEqual(ajeno.status_code, 403)
        alta = client.post(
            "/api/usuarios/",
            {"nombre": "Jefe nuevo", "email": "jefe-nuevo@example.com", "rol": "jefe"},
            format="json",
        )
        self.assertEqual(alta.status_code, 403)
        desactivar_jefe = client.patch(
            f"/api/usuarios/{self.jefe.id}/activar/",
            {"activo": False},
            format="json",
        )
        self.assertEqual(desactivar_jefe.status_code, 403)

        self.assertEqual(User.objects.get(id=admin.id).rol, "admin")
        self.assertEqual(User.objects.get(id=target.id).rol, "tecnico")
        self.assertTrue(User.objects.get(id=self.jefe.id).activo)
        self.assertFalse(User.objects.filter(email="jefe-nuevo@example.com").exists())

        # Lo que sí puede: mover a alguien entre roles que no son de jefatura.
        comun = client.patch(
            f"/api/usuarios/{target.id}/roleperm/",
            {"rol": "recepcion"},
            format="json",
        )
        self.assertEqual(comun.status_code, 200)
        self.assertEqual(User.objects.get(id=target.id).rol, "recepcion")

    def test_nadie_cambia_su_propio_rol_aunque_haya_otro_jefe(self):
        self._create_target(nombre="Otro jefe", email="otro-jefe@example.com", rol="jefe")

        response = self.client.patch(f"/api/usuarios/{self.jefe.id}/", {"rol": "admin"}, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("propio rol", str(response.data))
        self.assertEqual(User.objects.get(id=self.jefe.id).rol, "jefe")

    def test_el_editor_devuelve_lo_que_hereda_del_rol(self):
        target = self._create_target(rol="recepcion")

        response = self.client.get(f"/api/usuarios/{target.id}/permisos/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["role_defaults"]["page.recepcion"])
        self.assertFalse(response.data["role_defaults"]["page.users"])
        self.assertEqual(response.data["unknown_overrides"], [])

    @override_settings(BEJERMAN_CREDENTIALS_SECRET="usuarios-admin-secret")
    def test_jefe_can_replace_and_clear_bejerman_credentials_without_exposing_password(self):
        target = self._create_target()
        with patch(
            "service.views.usuarios_views._validate_bejerman_credentials"
        ) as validate:
            saved = self.client.put(
                f"/api/usuarios/{target.id}/bejerman-credentials/",
                {
                    "username": "sptec1",
                    "password": "clave-bejerman",
                    "workstation": "stec",
                },
                format="json",
            )
        self.assertEqual(saved.status_code, 200)
        validate.assert_called_once_with(
            "SPTEC1",
            "clave-bejerman",
            target.id,
            workstation="STEC",
        )
        self.assertNotIn("clave-bejerman", str(saved.data))
        self.assertNotIn("encrypted_password", str(saved.data).lower())
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT encrypted_password
                  FROM user_bejerman_credentials
                 WHERE user_id = %s
                """,
                [target.id],
            )
            encrypted = cur.fetchone()[0]
        self.assertNotIn("clave-bejerman", encrypted)
        self.assertEqual(decrypt_bejerman_password(encrypted), "clave-bejerman")

        with patch(
            "service.views.usuarios_views._validate_bejerman_credentials",
            side_effect=BejermanUserCredentialsInvalid("fallo simulado"),
        ):
            rejected = self.client.put(
                f"/api/usuarios/{target.id}/bejerman-credentials/",
                {
                    "username": "otro",
                    "password": "otra-clave",
                    "workstation": "stec",
                },
                format="json",
            )
        self.assertEqual(rejected.status_code, 400)
        with connection.cursor() as cur:
            cur.execute(
                "SELECT bejerman_username FROM user_bejerman_credentials WHERE user_id = %s",
                [target.id],
            )
            self.assertEqual(cur.fetchone()[0], "SPTEC1")

        removed = self.client.delete(
            f"/api/usuarios/{target.id}/bejerman-credentials/"
        )
        self.assertEqual(removed.status_code, 200)
        self.assertFalse(removed.data["user"]["bejerman_credentials_configured"])
        self.assertFalse(
            removed.data["user"]["bejerman_credentials_prompt_dismissed"]
        )

    def test_workstation_discovery_uses_target_user_context(self):
        target = self._create_target()
        discovered_payload = {
            "username": "SPTEC1",
            "automatic": True,
            "recommended": "STEC",
            "options": [{"code": "STEC", "label": "Servicio técnico"}],
            "detail": "Puesto detectado.",
            "companies": [],
        }
        with patch(
            "service.views.usuarios_views._discover_bejerman_workstations",
            return_value=discovered_payload,
        ) as discover:
            response = self.client.get(
                f"/api/usuarios/{target.id}/bejerman-workstations/?username=sptec1"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["recommended"], "STEC")
        discover.assert_called_once_with("SPTEC1", target.id)

    def test_detail_patch_rejects_duplicate_email(self):
        target = self._create_target()
        other = self._create_target(
            nombre="Otro usuario",
            email="otro-usuarios@example.com",
        )
        response = self.client.patch(
            f"/api/usuarios/{target.id}/",
            {"email": other.email},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            User.objects.get(id=target.id).email,
            "tecnico-usuarios@example.com",
        )

    def test_successful_login_records_latest_access_and_failed_login_does_not_replace_it(self):
        login_client = APIClient()
        response = login_client.post(
            "/api/auth/login/",
            {
                "email": self.jefe.email,
                "password": "Clave-segura-123",
            },
            format="json",
            HTTP_X_FORWARDED_FOR="192.0.2.10, 10.0.0.1",
            HTTP_USER_AGENT="Mozilla/5.0 Chrome/126.0 Windows NT 10.0",
        )
        self.assertEqual(response.status_code, 200)
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT last_login_at, CAST(last_login_ip AS TEXT), last_login_user_agent
                  FROM users
                 WHERE id = %s
                """,
                [self.jefe.id],
            )
            recorded_at, recorded_ip, recorded_ua = cur.fetchone()
        self.assertIsNotNone(recorded_at)
        self.assertEqual(recorded_ip, "192.0.2.10/32")
        self.assertIn("Chrome", recorded_ua)

        failed = login_client.post(
            "/api/auth/login/",
            {"email": self.jefe.email, "password": "incorrecta"},
            format="json",
            HTTP_X_FORWARDED_FOR="192.0.2.99",
            HTTP_USER_AGENT="Otro navegador",
        )
        self.assertEqual(failed.status_code, 401)
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT last_login_at, CAST(last_login_ip AS TEXT), last_login_user_agent
                  FROM users
                 WHERE id = %s
                """,
                [self.jefe.id],
            )
            after_failed = cur.fetchone()
        self.assertEqual(after_failed, (recorded_at, recorded_ip, recorded_ua))


class AuditRedactionTests(SimpleTestCase):
    def test_redacts_nested_passwords_tokens_and_secrets(self):
        payload = {
            "username": "SPTEC1",
            "password": "clave-plana",
            "nested": {
                "token": "token-plano",
                "items": [{"clave": "otra-clave"}, {"safe": "visible"}],
            },
        }
        redacted = redact_sensitive_audit_data(payload)
        self.assertEqual(redacted["username"], "SPTEC1")
        self.assertEqual(redacted["password"], "[REDACTADO]")
        self.assertEqual(redacted["nested"]["token"], "[REDACTADO]")
        self.assertEqual(redacted["nested"]["items"][0]["clave"], "[REDACTADO]")
        self.assertEqual(redacted["nested"]["items"][1]["safe"], "visible")
