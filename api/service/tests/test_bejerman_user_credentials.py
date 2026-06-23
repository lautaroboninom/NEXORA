from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient
from unittest.mock import call, patch

from service.bejerman_sdk import BejermanSDKClient, BejermanSdkConfigError, BejermanSdkResponseError
from service.bejerman_user_credentials import (
    bejerman_workstation_for_role,
    decrypt_bejerman_password,
    encrypt_bejerman_password,
)
from service.models import User


class CapturingBejermanSDKClient(BejermanSDKClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_body = ""

    def _post(self, action, body):
        self.last_body = body
        return {"Resultado": "OK", "Token": "token-prueba"}


class BejermanUserCredentialsTests(SimpleTestCase):
    @override_settings(BEJERMAN_CREDENTIALS_SECRET="test-secret")
    def test_encrypts_and_decrypts_password(self):
        encrypted = encrypt_bejerman_password("clave-123")

        self.assertNotIn("clave-123", encrypted)
        self.assertEqual(decrypt_bejerman_password(encrypted), "clave-123")

    @override_settings(
        BEJERMAN_WSDL_URL="http://bejerman.test/EFlexSDK_Service.svc",
        BEJERMAN_COMPANY="SEP",
        BEJERMAN_WORKSTATION="STEC",
        BEJERMAN_BRANCH="",
        BEJERMAN_USER="SPST1",
        BEJERMAN_PASSWORD="no-usar",
    )
    def test_sdk_register_uses_actor_credentials_not_env_user(self):
        with patch("service.bejerman_sdk.get_user_bejerman_credentials", return_value=("OPER1", "clave-operador")), patch(
            "service.bejerman_sdk.resolve_user_bejerman_workstation", return_value="STEC"
        ):
            client = CapturingBejermanSDKClient(actor_user_id=12)
            client.register()

        self.assertIn("<xUsuario>OPER1</xUsuario>", client.last_body)
        self.assertIn("<xClave>clave-operador</xClave>", client.last_body)
        self.assertNotIn("SPST1", client.last_body)
        self.assertNotIn("no-usar", client.last_body)

    @override_settings(
        BEJERMAN_WSDL_URL="http://bejerman.test/EFlexSDK_Service.svc",
        BEJERMAN_COMPANY="SEP",
        BEJERMAN_WORKSTATION="STEC",
        BEJERMAN_BRANCH="",
        BEJERMAN_USER="SPST1",
        BEJERMAN_PASSWORD="no-usar",
    )
    def test_sdk_register_without_actor_does_not_fallback_to_env_user(self):
        client = CapturingBejermanSDKClient()

        with self.assertRaises(BejermanSdkConfigError):
            client.register()

        self.assertEqual(client.last_body, "")

    @override_settings(BEJERMAN_SERVICE_WORKSTATION="STEC", BEJERMAN_ADMIN_WORKSTATION="ADMV")
    def test_bejerman_workstation_is_resolved_by_role(self):
        self.assertEqual(bejerman_workstation_for_role("tecnico"), "STEC")
        self.assertEqual(bejerman_workstation_for_role("jefe"), "STEC")
        self.assertEqual(bejerman_workstation_for_role("admin"), "ADMV")
        self.assertEqual(bejerman_workstation_for_role("ventas"), "ADMV")
        self.assertEqual(bejerman_workstation_for_role("recepcion"), "ADMV")
        self.assertEqual(bejerman_workstation_for_role("cobranzas"), "ADMV")

    @override_settings(BEJERMAN_WORKSTATION="STEC", BEJERMAN_SERVICE_WORKSTATION="STEC", BEJERMAN_ADMIN_WORKSTATION="")
    def test_admin_workstation_falls_back_to_general_when_admin_not_configured(self):
        self.assertEqual(bejerman_workstation_for_role("admin"), "STEC")
        self.assertEqual(bejerman_workstation_for_role("ventas"), "STEC")
        self.assertEqual(bejerman_workstation_for_role("recepcion"), "STEC")

    @override_settings(
        BEJERMAN_WSDL_URL="http://bejerman.test/EFlexSDK_Service.svc",
        BEJERMAN_COMPANY="SEP",
        BEJERMAN_WORKSTATION="STEC",
        BEJERMAN_BRANCH="",
    )
    def test_sdk_register_uses_explicit_personal_workstation(self):
        client = CapturingBejermanSDKClient(
            bejerman_username="SPADM7",
            bejerman_password="clave-operador",
            bejerman_workstation="ADMV",
        )

        client.register()

        self.assertIn("<xPtoTrabajo>ADMV</xPtoTrabajo>", client.last_body)
        self.assertNotIn("<xPtoTrabajo>STEC</xPtoTrabajo>", client.last_body)


class BejermanUserCredentialsApiTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        vendor = connection.vendor
        auto_inc = "INTEGER PRIMARY KEY AUTOINCREMENT" if vendor == "sqlite" else "BIGSERIAL PRIMARY KEY"
        bool_type = "INTEGER" if vendor == "sqlite" else "BOOLEAN"
        bool_default = "1" if vendor == "sqlite" else "TRUE"
        timestamp_type = "DATETIME" if vendor == "sqlite" else "TIMESTAMPTZ"
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
                )
                """
            )
            columns = {column.name for column in connection.introspection.get_table_description(cur, "users")}
            if "bejerman_seller_code" not in columns:
                cur.execute("ALTER TABLE users ADD COLUMN bejerman_seller_code TEXT")
            if "bejerman_seller_code_confirmed_at" not in columns:
                cur.execute(f"ALTER TABLE users ADD COLUMN bejerman_seller_code_confirmed_at {timestamp_type} NULL")
            if vendor == "postgresql":
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_users_bejerman_seller_code_ci
                      ON users ((UPPER(TRIM(bejerman_seller_code))))
                      WHERE NULLIF(TRIM(bejerman_seller_code), '') IS NOT NULL
                    """
                )
        call_command("apply_user_permissions_schema", verbosity=0)
        call_command("apply_bejerman_user_credentials_schema", verbosity=0)

    def setUp(self):
        with connection.cursor() as cur:
            cur.execute("DELETE FROM user_bejerman_credentials")
        User.objects.all().delete()
        self.user = User.objects.create(
            nombre="Operador Bejerman",
            email="operador-bejerman@example.com",
            hash_pw="",
            rol="admin",
            activo=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @override_settings(BEJERMAN_CREDENTIALS_SECRET="api-test-secret")
    def test_session_flags_credentials_required_until_user_saves_valid_credentials(self):
        response = self.client.get("/api/auth/session/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["bejermanCredentialsRequired"])
        self.assertTrue(response.data["user"]["bejermanCredentials"]["required"])

    @override_settings(BEJERMAN_CREDENTIALS_SECRET="api-test-secret")
    def test_save_credentials_validates_and_persists_encrypted_password(self):
        with patch("service.views.auth_views.BejermanSDKClient") as sdk_cls:
            sdk_cls.return_value.register.return_value = {"Resultado": "OK", "Token": "token"}
            response = self.client.post(
                "/api/auth/bejerman-credentials/",
                {"username": "oper1", "password": "clave-personal"},
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["required"])
        self.assertTrue(response.data["valid"])
        self.assertEqual(
            sdk_cls.call_args_list,
            [
                call(
                    company_key="SEPID",
                    bejerman_username="OPER1",
                    bejerman_password="clave-personal",
                    bejerman_workstation="STEC",
                ),
                call(
                    company_key="MGBIO",
                    bejerman_username="OPER1",
                    bejerman_password="clave-personal",
                    bejerman_workstation="STEC",
                ),
            ],
        )

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT bejerman_username, encrypted_password, is_valid
                FROM user_bejerman_credentials
                WHERE user_id = %s
                """,
                [self.user.id],
            )
            username, encrypted_password, is_valid = cur.fetchone()

        self.assertEqual(username, "OPER1")
        self.assertTrue(is_valid)
        self.assertNotIn("clave-personal", encrypted_password)
        self.assertEqual(decrypt_bejerman_password(encrypted_password), "clave-personal")

        session = self.client.get("/api/auth/session/")
        self.assertFalse(session.data["bejermanCredentialsRequired"])

    @override_settings(BEJERMAN_CREDENTIALS_SECRET="api-test-secret")
    def test_save_credentials_rejects_user_valid_in_only_one_company(self):
        with patch("service.views.auth_views.BejermanSDKClient") as sdk_cls:
            sdk_cls.return_value.register.side_effect = [
                {"Resultado": "OK", "Token": "token"},
                BejermanSdkResponseError("El puesto de trabajo no está registrado en la empresa."),
            ]
            response = self.client.post(
                "/api/auth/bejerman-credentials/",
                {"username": "spadm7", "password": "clave-personal"},
                format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("MG BIO", response.data["detail"])
        self.assertIn("SEPID", response.data["detail"])
        self.assertEqual(
            sdk_cls.call_args_list,
            [
                call(
                    company_key="SEPID",
                    bejerman_username="SPADM7",
                    bejerman_password="clave-personal",
                    bejerman_workstation="STEC",
                ),
                call(
                    company_key="MGBIO",
                    bejerman_username="SPADM7",
                    bejerman_password="clave-personal",
                    bejerman_workstation="STEC",
                ),
            ],
        )

        with connection.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM user_bejerman_credentials WHERE user_id = %s",
                [self.user.id],
            )
            self.assertEqual(cur.fetchone()[0], 0)

    def test_save_credentials_rejects_invalid_sdk_login_without_persisting(self):
        with patch("service.views.auth_views.BejermanSDKClient") as sdk_cls:
            sdk_cls.return_value.register.side_effect = BejermanSdkResponseError("Credenciales inválidas")
            response = self.client.post(
                "/api/auth/bejerman-credentials/",
                {"username": "oper1", "password": "mal"},
                format="json",
            )

        self.assertEqual(response.status_code, 400)
        with connection.cursor() as cur:
            cur.execute("SELECT count(*) FROM user_bejerman_credentials WHERE user_id = %s", [self.user.id])
            self.assertEqual(cur.fetchone()[0], 0)

    def test_save_credentials_explains_disabled_bejerman_account(self):
        with patch("service.views.auth_views.BejermanSDKClient") as sdk_cls:
            sdk_cls.return_value.register.side_effect = BejermanSdkResponseError("CuentaDeshabilitada")
            response = self.client.post(
                "/api/auth/bejerman-credentials/",
                {"username": "stsp1", "password": "clave"},
                format="json",
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("cuenta está deshabilitada", response.data["detail"])
        self.assertIn("STSP1", response.data["detail"])
        self.assertNotIn("400 Bad Request", response.data["detail"])

    def test_session_seller_code_no_es_requerido_para_admin(self):
        response = self.client.get("/api/auth/session/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data["user"]["bejermanSellerCode"],
            {"code": "", "eligible": False, "confirmed": False, "required": False},
        )

    def _auth_as_ventas(self, *, email="ventas-bejerman@example.com"):
        user = User.objects.create(
            nombre="Ventas Bejerman",
            email=email,
            hash_pw="",
            rol="ventas",
            activo=True,
        )
        client = APIClient()
        client.force_authenticate(user=user)
        return user, client

    def test_session_seller_code_es_requerido_para_ventas_sin_confirmar(self):
        _user, client = self._auth_as_ventas()

        response = client.get("/api/auth/session/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data["user"]["bejermanSellerCode"],
            {"code": "", "eligible": True, "confirmed": False, "required": True},
        )

    def test_save_seller_code_normaliza_y_persiste(self):
        user, client = self._auth_as_ventas()

        response = client.post("/api/auth/bejerman-seller-code/", {"sellerCode": " eze "}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], "EZE")
        self.assertTrue(response.data["confirmed"])
        self.assertFalse(response.data["required"])
        with connection.cursor() as cur:
            cur.execute(
                "SELECT bejerman_seller_code, bejerman_seller_code_confirmed_at FROM users WHERE id = %s",
                [user.id],
            )
            code, confirmed_at = cur.fetchone()
        self.assertEqual(code, "EZE")
        self.assertIsNotNone(confirmed_at)

    def test_save_seller_code_permite_confirmar_sin_codigo(self):
        user, client = self._auth_as_ventas()

        response = client.post("/api/auth/bejerman-seller-code/", {"sellerCode": ""}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], "")
        self.assertTrue(response.data["confirmed"])
        self.assertFalse(response.data["required"])
        with connection.cursor() as cur:
            cur.execute("SELECT bejerman_seller_code, bejerman_seller_code_confirmed_at FROM users WHERE id = %s", [user.id])
            code, confirmed_at = cur.fetchone()
        self.assertIsNone(code)
        self.assertIsNotNone(confirmed_at)

    def test_save_seller_code_rechaza_duplicado_case_insensitive(self):
        User.objects.create(
            nombre="Ventas Existente",
            email="ventas-existente@example.com",
            hash_pw="",
            rol="ventas",
            activo=True,
            bejerman_seller_code="EZE",
        )
        _user, client = self._auth_as_ventas()

        response = client.post("/api/auth/bejerman-seller-code/", {"sellerCode": "eze"}, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("ya está asignado", response.data["detail"])

    def test_save_seller_code_rechaza_longitud_invalida(self):
        _user, client = self._auth_as_ventas()

        response = client.post("/api/auth/bejerman-seller-code/", {"sellerCode": "ABCDE"}, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("4 caracteres", response.data["detail"])

    def test_save_seller_code_adm_es_unico(self):
        first_user, first_client = self._auth_as_ventas(email="ventas-adm-1@example.com")
        second_user, second_client = self._auth_as_ventas(email="ventas-adm-2@example.com")

        first_response = first_client.post("/api/auth/bejerman-seller-code/", {"sellerCode": "adm"}, format="json")
        second_response = second_client.post("/api/auth/bejerman-seller-code/", {"sellerCode": "ADM"}, format="json")

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(first_response.data["code"], "ADM")
        self.assertEqual(second_response.status_code, 400)
        self.assertIn("ya está asignado", second_response.data["detail"])
        with connection.cursor() as cur:
            cur.execute("SELECT bejerman_seller_code FROM users WHERE id = %s", [first_user.id])
            self.assertEqual(cur.fetchone()[0], "ADM")
            cur.execute("SELECT bejerman_seller_code FROM users WHERE id = %s", [second_user.id])
            self.assertIsNone(cur.fetchone()[0])
