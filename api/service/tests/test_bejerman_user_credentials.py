from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient
from unittest.mock import call, patch

from service.bejerman_sdk import BejermanSDKClient, BejermanSdkConfigError, BejermanSdkResponseError
from service.bejerman_user_credentials import (
    BejermanUserCredentialsRequired,
    bejerman_workstation_for_role,
    decrypt_bejerman_password,
    encrypt_bejerman_password,
    get_user_bejerman_credentials,
    resolve_user_bejerman_workstation,
    save_user_bejerman_credentials,
    user_bejerman_credentials_status,
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

    @override_settings(
        BEJERMAN_WSDL_URL="http://bejerman.test/EFlexSDK_Service.svc",
        BEJERMAN_COMPANY="SEP",
        BEJERMAN_WORKSTATION="STEC",
        BEJERMAN_BRANCH="",
        BEJERMAN_USER="SPST1",
        BEJERMAN_PASSWORD="clave-sistema",
    )
    def test_sdk_register_can_fallback_to_system_credentials_when_allowed(self):
        with patch(
            "service.bejerman_sdk.get_user_bejerman_credentials",
            side_effect=BejermanUserCredentialsRequired("sin credenciales personales"),
        ), patch("service.bejerman_sdk.resolve_user_bejerman_workstation", return_value="STEC"):
            client = CapturingBejermanSDKClient(actor_user_id=12, allow_system_credentials=True)
            client.register()

        self.assertIn("<xUsuario>SPST1</xUsuario>", client.last_body)
        self.assertIn("<xClave>clave-sistema</xClave>", client.last_body)

    @override_settings(BEJERMAN_SERVICE_WORKSTATION="STEC", BEJERMAN_ADMIN_WORKSTATION="ADMV")
    def test_bejerman_workstation_is_resolved_by_role(self):
        self.assertEqual(bejerman_workstation_for_role("tecnico"), "STEC")
        self.assertEqual(bejerman_workstation_for_role("jefe"), "STEC")
        self.assertEqual(bejerman_workstation_for_role("admin"), "ADMV")
        self.assertEqual(bejerman_workstation_for_role("supervisor"), "ADMV")
        self.assertEqual(bejerman_workstation_for_role("ventas"), "ADMV")
        self.assertEqual(bejerman_workstation_for_role("recepcion"), "ADMV")
        self.assertEqual(bejerman_workstation_for_role("cobranzas"), "ADMV")

    @override_settings(BEJERMAN_WORKSTATION="STEC", BEJERMAN_SERVICE_WORKSTATION="STEC", BEJERMAN_ADMIN_WORKSTATION="")
    def test_admin_workstation_falls_back_to_general_when_admin_not_configured(self):
        self.assertEqual(bejerman_workstation_for_role("admin"), "STEC")
        self.assertEqual(bejerman_workstation_for_role("supervisor"), "STEC")
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
        self.assertFalse(response.data["user"]["bejermanCredentials"]["promptDismissed"])

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
    def test_requested_workstation_is_validated_and_persisted(self):
        with patch("service.views.auth_views.BejermanSDKClient") as sdk_cls:
            sdk_cls.return_value.register.return_value = {"Resultado": "OK", "Token": "token"}
            response = self.client.post(
                "/api/auth/bejerman-credentials/",
                {
                    "username": "oper1",
                    "password": "clave-personal",
                    "workstation": "vtas",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["workstation"], "VTAS")
        self.assertTrue(response.data["workstationConfigured"])
        with connection.cursor() as cur:
            cur.execute(
                "SELECT bejerman_workstation FROM users WHERE id = %s",
                [self.user.id],
            )
            self.assertEqual(cur.fetchone()[0], "VTAS")
        self.assertEqual(
            sdk_cls.call_args_list,
            [
                call(
                    company_key="SEPID",
                    bejerman_username="OPER1",
                    bejerman_password="clave-personal",
                    bejerman_workstation="VTAS",
                ),
                call(
                    company_key="MGBIO",
                    bejerman_username="OPER1",
                    bejerman_password="clave-personal",
                    bejerman_workstation="VTAS",
                ),
            ],
        )

    def test_workstation_lookup_returns_common_recommended_option(self):
        def remote_lookup(_sql, *, company_key=None, outcome=None, **_kwargs):
            outcome.update({"status": "success"})
            if company_key == "SEPID":
                return [
                    {"code": "DEPO", "label": "DEPÓSITO"},
                    {"code": "VTAS", "label": "VENTAS"},
                ]
            return [
                {"code": "GER", "label": "GERENCIA"},
                {"code": "VTAS", "label": "VENTAS"},
            ]

        with patch(
            "service.bejerman_delivery._remote_bejerman_sql_csv",
            side_effect=remote_lookup,
        ):
            response = self.client.get(
                "/api/auth/bejerman-workstations/?username=spvta"
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["automatic"])
        self.assertEqual(response.data["recommended"], "VTAS")
        self.assertEqual(
            response.data["options"],
            [{"code": "VTAS", "label": "VENTAS"}],
        )

    def test_user_can_persistently_dismiss_automatic_credentials_prompt(self):
        response = self.client.patch(
            "/api/auth/bejerman-credentials/",
            {"dismissPrompt": True},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["required"])
        self.assertTrue(response.data["promptDismissed"])

        session = self.client.get("/api/auth/session/")
        self.assertTrue(
            session.data["user"]["bejermanCredentials"]["promptDismissed"]
        )

    @override_settings(BEJERMAN_CREDENTIALS_SECRET="api-test-secret")
    def test_saving_credentials_reenables_future_automatic_prompt(self):
        self.client.patch(
            "/api/auth/bejerman-credentials/",
            {"dismissPrompt": True},
            format="json",
        )
        with patch("service.views.auth_views.BejermanSDKClient") as sdk_cls:
            sdk_cls.return_value.register.return_value = {"Resultado": "OK", "Token": "token"}
            response = self.client.post(
                "/api/auth/bejerman-credentials/",
                {
                    "username": "oper1",
                    "password": "clave-personal",
                    "workstation": "STEC",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["promptDismissed"])

    @override_settings(BEJERMAN_CREDENTIALS_SECRET="api-test-secret")
    def test_personal_workstation_overrides_role_during_validation(self):
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE users SET bejerman_workstation = %s WHERE id = %s",
                ["VTAS", self.user.id],
            )

        self.assertEqual(resolve_user_bejerman_workstation(self.user.id), "VTAS")

        with patch("service.views.auth_views.BejermanSDKClient") as sdk_cls:
            sdk_cls.return_value.register.return_value = {"Resultado": "OK", "Token": "token"}
            response = self.client.post(
                "/api/auth/bejerman-credentials/",
                {"username": "spvta", "password": "clave-personal"},
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["workstation"], "VTAS")
        self.assertEqual(
            sdk_cls.call_args_list,
            [
                call(
                    company_key="SEPID",
                    bejerman_username="SPVTA",
                    bejerman_password="clave-personal",
                    bejerman_workstation="VTAS",
                ),
                call(
                    company_key="MGBIO",
                    bejerman_username="SPVTA",
                    bejerman_password="clave-personal",
                    bejerman_workstation="VTAS",
                ),
            ],
        )

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


class BejermanTechnicalDelegateTests(TestCase):
    """Los técnicos no tienen usuario propio en Bejerman: operan con el delegado.

    Antes de esto, todo lo que disparaba un técnico se bloqueaba pidiéndole
    credenciales que por diseño nunca iba a tener, y el equipo quedaba parado.
    """

    DELEGATE_EMAIL = "delegado-bejerman@example.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command("apply_bejerman_user_credentials_schema", verbosity=0)

    def setUp(self):
        with connection.cursor() as cur:
            cur.execute("DELETE FROM user_bejerman_credentials")
        User.objects.all().delete()
        self.administrativo = User.objects.create(
            nombre="Administrativa",
            email="administrativa-bejerman@example.com",
            hash_pw="",
            rol="admin",
            activo=True,
        )

    def _crear_tecnico(self):
        return User.objects.create(
            nombre="Técnico sin credenciales",
            email="tecnico-sin-credenciales@example.com",
            hash_pw="",
            rol="tecnico",
            activo=True,
        )

    def _crear_delegado_con_credenciales(self):
        delegado = User.objects.create(
            nombre="Jefe con credenciales",
            email=self.DELEGATE_EMAIL,
            hash_pw="",
            rol="jefe",
            activo=True,
        )
        save_user_bejerman_credentials(delegado.id, "JEFE1", "clave-del-jefe")
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE user_bejerman_credentials SET is_valid = TRUE WHERE user_id = %s",
                [delegado.id],
            )
        return delegado

    @override_settings(BEJERMAN_CREDENTIALS_SECRET="delegate-secret")
    def test_tecnico_sin_credenciales_usa_las_del_delegado(self):
        tecnico = self._crear_tecnico()
        self._crear_delegado_con_credenciales()

        with override_settings(BEJERMAN_TECHNICAL_DELEGATE_EMAIL=self.DELEGATE_EMAIL):
            usuario, clave = get_user_bejerman_credentials(tecnico.id)

        self.assertEqual(usuario, "JEFE1")
        self.assertEqual(clave, "clave-del-jefe")

    @override_settings(BEJERMAN_CREDENTIALS_SECRET="delegate-secret")
    def test_a_un_tecnico_no_se_le_piden_credenciales_propias(self):
        tecnico = self._crear_tecnico()
        self._crear_delegado_con_credenciales()

        with override_settings(BEJERMAN_TECHNICAL_DELEGATE_EMAIL=self.DELEGATE_EMAIL):
            estado = user_bejerman_credentials_status(tecnico.id)

        self.assertTrue(estado["delegated"])
        self.assertFalse(estado["required"])

    @override_settings(BEJERMAN_CREDENTIALS_SECRET="delegate-secret")
    def test_sin_delegado_configurado_el_error_explica_por_que(self):
        tecnico = self._crear_tecnico()

        with override_settings(BEJERMAN_TECHNICAL_DELEGATE_EMAIL=""):
            with self.assertRaises(BejermanUserCredentialsRequired) as ctx:
                get_user_bejerman_credentials(tecnico.id)

        self.assertIn("credencial delegada", str(ctx.exception))

    @override_settings(BEJERMAN_CREDENTIALS_SECRET="delegate-secret")
    def test_delegado_sin_credenciales_dice_de_quien_son_las_que_faltan(self):
        tecnico = self._crear_tecnico()
        User.objects.create(
            nombre="Delegado sin credenciales",
            email=self.DELEGATE_EMAIL,
            hash_pw="",
            rol="jefe",
            activo=True,
        )

        with override_settings(BEJERMAN_TECHNICAL_DELEGATE_EMAIL=self.DELEGATE_EMAIL):
            with self.assertRaises(BejermanUserCredentialsRequired) as ctx:
                get_user_bejerman_credentials(tecnico.id)

        self.assertIn(self.DELEGATE_EMAIL, str(ctx.exception))

    @override_settings(BEJERMAN_CREDENTIALS_SECRET="delegate-secret")
    def test_un_administrativo_sin_credenciales_las_sigue_necesitando(self):
        """La delegación es sólo para técnicos: al resto hay que seguir pidiéndoselas."""
        self._crear_delegado_con_credenciales()

        with override_settings(BEJERMAN_TECHNICAL_DELEGATE_EMAIL=self.DELEGATE_EMAIL):
            with self.assertRaises(BejermanUserCredentialsRequired):
                get_user_bejerman_credentials(self.administrativo.id)
            self.assertTrue(user_bejerman_credentials_status(self.administrativo.id)["required"])
