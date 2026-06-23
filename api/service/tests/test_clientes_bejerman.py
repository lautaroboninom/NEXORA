from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from service.models import User


class _FakeBejermanClient:
    def __init__(self, records=None, *args, **kwargs):
        self.records = records

    def list_clientes(self):
        if self.records is not None:
            return {"DatosJSON": self.records}
        return {
            "DatosJSON": [
                {
                    "Cliente_Codigo": "SIM",
                    "Cliente_RazonSocial": "SIM ELECTRO MEDICINA",
                    "Cliente_NroDocumento": "30716787946",
                    "Cliente_SitIVA": "RI",
                    "Cliente_Provincia": "02",
                    "Cliente_Domicilio": "Av. Siempre Viva 123",
                    "Cliente_CondVenta": "30",
                },
                {
                    "Cliente_Codigo": "CMA",
                    "Cliente_RazonSocial": "CENTRO MEDICO AMENABAR SRL",
                    "Cliente_NroDocumento": "30707612971",
                    "Cliente_SitIVA": "EX",
                },
            ]
        }


class ClientesBejermanAPITest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        vendor = connection.vendor
        auto_inc = "INTEGER PRIMARY KEY AUTOINCREMENT" if vendor == "sqlite" else "BIGSERIAL PRIMARY KEY"
        bool_type = "INTEGER" if vendor == "sqlite" else "BOOLEAN"
        bool_default = "1" if vendor == "sqlite" else "TRUE"
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
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS user_permission_overrides (
                    id {auto_inc},
                    user_id INTEGER NOT NULL,
                    permission_code TEXT NOT NULL,
                    effect TEXT NOT NULL,
                    updated_by INTEGER NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS customers (
                    id {auto_inc},
                    cod_empresa TEXT,
                    razon_social TEXT NOT NULL,
                    cuit TEXT,
                    contacto TEXT,
                    telefono TEXT,
                    telefono_2 TEXT,
                    email TEXT
                )
                """
            )
            existing_customer_columns = {
                column.name for column in connection.introspection.get_table_description(cur, "customers")
            }
            for column in ("cod_empresa", "razon_social", "cuit", "contacto", "telefono", "telefono_2", "email"):
                if column not in existing_customer_columns:
                    cur.execute(f"ALTER TABLE customers ADD COLUMN {column} TEXT")
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS devices (
                    id {auto_inc},
                    customer_id INTEGER,
                    marca_id INTEGER,
                    model_id INTEGER,
                    numero_serie TEXT,
                    numero_interno TEXT,
                    tipo_equipo TEXT,
                    variante TEXT
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS marcas (
                    id {auto_inc},
                    nombre TEXT
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS models (
                    id {auto_inc},
                    marca_id INTEGER,
                    nombre TEXT,
                    tipo_equipo TEXT,
                    variante TEXT
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS ingresos (
                    id {auto_inc},
                    device_id INTEGER,
                    estado TEXT,
                    presupuesto_estado TEXT,
                    fecha_ingreso TIMESTAMPTZ,
                    fecha_creacion TIMESTAMPTZ,
                    equipo_variante TEXT
                )
                """
            )
            for statement in (
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS marca_id INTEGER",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS model_id INTEGER",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS numero_serie TEXT",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS numero_interno TEXT",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS tipo_equipo TEXT",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS variante TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS estado TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS presupuesto_estado TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS fecha_ingreso TIMESTAMPTZ",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS fecha_creacion TIMESTAMPTZ",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS equipo_variante TEXT",
            ):
                cur.execute(statement)
        call_command("apply_customer_bejerman_schema", verbosity=0)
        call_command("apply_customer_alias_schema", verbosity=0)

    def setUp(self):
        with connection.cursor() as cur:
            for table in ("ingresos", "devices", "customers", "user_permission_overrides"):
                cur.execute(f"DELETE FROM {table}")
        User.objects.all().delete()
        self.user = User.objects.create(
            nombre="Admin Clientes",
            email="admin-clientes@example.com",
            hash_pw="",
            rol="admin",
            activo=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _seed_customers(self):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO customers (cod_empresa, razon_social, cuit, telefono, email)
                VALUES (%s, %s, %s, %s, %s), (%s, %s, %s, %s, %s)
                """,
                [
                    "SIM",
                    "SIM ELECTRO MEDICINA",
                    "30716787946",
                    "1111",
                    "sim@example.com",
                    None,
                    "CENTRO MEDICO AMENABAR",
                    "30707612971",
                    "2222",
                    "cma@example.com",
                ],
            )
            cur.execute("SELECT id FROM customers WHERE cod_empresa=%s", ["SIM"])
            sim_id = cur.fetchone()[0]
            cur.execute("SELECT id FROM customers WHERE razon_social=%s", ["CENTRO MEDICO AMENABAR"])
            cma_id = cur.fetchone()[0]
            cur.execute("INSERT INTO devices (customer_id) VALUES (%s), (%s), (%s)", [sim_id, sim_id, cma_id])
            cur.execute("SELECT id FROM devices WHERE customer_id=%s ORDER BY id LIMIT 1", [sim_id])
            device_id = cur.fetchone()[0]
            cur.execute("INSERT INTO ingresos (device_id) VALUES (%s)", [device_id])
        return sim_id, cma_id

    @patch("service.views.clientes_views.BejermanSDKClient", return_value=_FakeBejermanClient())
    def test_customer_list_includes_bejerman_sync_and_activity_counts(self, _client_cls):
        sim_id, cma_id = self._seed_customers()

        response = self.client.get("/api/catalogos/clientes/?include_stats=1&include_bejerman=1")

        self.assertEqual(response.status_code, 200)
        by_id = {row["id"]: row for row in response.data}
        self.assertEqual(by_id[sim_id]["bejerman_sync"]["status"], "synced")
        self.assertEqual(by_id[sim_id]["bejerman_sync"]["candidate"]["details"]["condicionIva"], "RI")
        self.assertEqual(by_id[sim_id]["equipos_count"], 2)
        self.assertEqual(by_id[sim_id]["ingresos_count"], 1)
        self.assertEqual(by_id[cma_id]["bejerman_sync"]["status"], "missing_code")
        self.assertEqual(by_id[cma_id]["bejerman_sync"]["candidates"][0]["code"], "CMA")

    @patch("service.views.clientes_views.BejermanSDKClient", return_value=_FakeBejermanClient())
    def test_bejerman_candidate_search_matches_by_name(self, _client_cls):
        self._seed_customers()

        response = self.client.get("/api/catalogos/clientes/bejerman-candidatos/?q=amenabar")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["items"][0]["code"], "CMA")
        self.assertEqual(response.data["items"][0]["details"]["condicionIva"], "EX")

    @patch("service.views.clientes_views.BejermanSDKClient")
    def test_bejerman_sync_updates_existing_and_creates_missing_customer(self, client_cls):
        client_cls.return_value = _FakeBejermanClient(
            records=[
                {
                    "Cliente_Codigo": "SIM",
                    "Cliente_RazonSocial": "SIM ELECTRO MEDICINA SRL",
                    "Cliente_NroDocumento": "30716787946",
                    "Cliente_SitIVA": "RI",
                    "Cliente_Domicilio": "Av. Siempre Viva 123",
                    "Cliente_Localidad": "CABA",
                    "Cliente_CondVenta": "30",
                    "Cliente_Telefono": "5555",
                    "Cliente_Email": "bejerman-sim@example.com",
                },
                {
                    "Cliente_Codigo": "NUE",
                    "Cliente_RazonSocial": "CLIENTE NUEVO SA",
                    "Cliente_NroDocumento": "30799999999",
                    "Cliente_SitIVA": "MT",
                    "Cliente_Provincia": "01",
                },
            ]
        )
        sim_id, _ = self._seed_customers()
        with connection.cursor() as cur:
            cur.execute("UPDATE customers SET alias_interno=%s WHERE id=%s", ["SIMED", sim_id])

        response = self.client.post("/api/catalogos/clientes/sincronizar-bejerman/", {})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["updated"], 1)
        self.assertEqual(response.data["created"], 1)
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT razon_social, cuit, telefono, email, bejerman_condicion_iva, bejerman_domicilio, bejerman_raw, alias_interno
                  FROM customers
                 WHERE id=%s
                """,
                [sim_id],
            )
            sim = cur.fetchone()
            cur.execute("SELECT bejerman_condicion_iva FROM customers WHERE cod_empresa=%s", ["NUE"])
            created = cur.fetchone()

        self.assertEqual(sim[0], "SIM ELECTRO MEDICINA SRL")
        self.assertEqual(sim[1], "30716787946")
        self.assertEqual(sim[2], "1111")
        self.assertEqual(sim[3], "sim@example.com")
        self.assertEqual(sim[4], "RI")
        self.assertEqual(sim[5], "Av. Siempre Viva 123")
        self.assertIn("Cliente_SitIVA", str(sim[6]))
        self.assertEqual(sim[7], "SIMED")
        self.assertEqual(created[0], "MT")

    @patch("service.views.clientes_views.BejermanSDKClient")
    def test_bejerman_sync_does_not_rename_argas_to_argenlab_by_stale_code(self, client_cls):
        client_cls.return_value = _FakeBejermanClient(
            records=[
                {
                    "Cliente_Codigo": "ARG",
                    "Cliente_RazonSocial": "ARGENLAB S R L",
                    "Cliente_NroDocumento": "30645416933",
                },
                {
                    "Cliente_Codigo": "ARGAS",
                    "Cliente_RazonSocial": "ARGENTINA DE GASES S.A.",
                    "Cliente_NroDocumento": "30708604689",
                },
            ]
        )
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO customers (cod_empresa, razon_social, cuit) VALUES (%s, %s, %s)",
                ["ARG", "ARGENTINA DE GASES S.A.", ""],
            )
            cur.execute("SELECT id FROM customers WHERE cod_empresa=%s", ["ARG"])
            customer_id = cur.fetchone()[0]

        response = self.client.post("/api/catalogos/clientes/sincronizar-bejerman/", {})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["updated"], 1)
        self.assertEqual(response.data["created"], 0)
        self.assertEqual(response.data["identity_mismatch"], 1)
        with connection.cursor() as cur:
            cur.execute(
                "SELECT cod_empresa, razon_social, cuit FROM customers WHERE id=%s",
                [customer_id],
            )
            row = cur.fetchone()
            cur.execute("SELECT COUNT(*) FROM customers WHERE razon_social=%s", ["ARGENLAB S R L"])
            argenlab_count = cur.fetchone()[0]

        self.assertEqual(row[0], "ARGAS")
        self.assertEqual(row[1], "ARGENTINA DE GASES S.A.")
        self.assertEqual(row[2], "30708604689")
        self.assertEqual(argenlab_count, 0)

    def test_apply_customer_alias_schema_creates_column_index_and_seed_examples(self):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO customers (cod_empresa, razon_social)
                VALUES (%s, %s), (%s, %s)
                """,
                [
                    "TMD",
                    "TERAPIAS MEDICAS DOMICILIARIAS S.A.",
                    "ARG",
                    "ARGENTINA DE GASES S.A.",
                ],
            )

        out = StringIO()
        call_command("apply_customer_alias_schema", "--seed-examples", stdout=out)

        with connection.cursor() as cur:
            cur.execute("SELECT razon_social, alias_interno FROM customers ORDER BY razon_social")
            aliases = {row[0]: row[1] for row in cur.fetchall()}
            cur.execute(
                """
                SELECT 1
                  FROM pg_indexes
                 WHERE schemaname = ANY(current_schemas(false))
                   AND indexname = 'ux_customers_alias_interno_ci'
                 LIMIT 1
                """
            )
            index_exists = cur.fetchone()

        self.assertEqual(aliases["TERAPIAS MEDICAS DOMICILIARIAS S.A."], "TMD")
        self.assertEqual(aliases["ARGENTINA DE GASES S.A."], "ARGAS")
        self.assertIsNotNone(index_exists)
        self.assertIn("cargados=2", out.getvalue())

    def test_customer_alias_crud_and_basic_list(self):
        response = self.client.post(
            "/api/catalogos/clientes/",
            {
                "razon_social": "TERAPIAS MEDICAS DOMICILIARIAS S.A.",
                "cod_empresa": "TMD",
                "alias_interno": "TMD",
                "cuit": "30700000001",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        catalog_response = self.client.get("/api/catalogos/clientes/")
        basic_response = self.client.get("/api/clientes/")
        self.assertEqual(catalog_response.status_code, 200)
        self.assertEqual(basic_response.status_code, 200)
        self.assertEqual(catalog_response.data[0]["alias_interno"], "TMD")
        self.assertEqual(basic_response.data[0]["alias_interno"], "TMD")

    def test_customer_alias_duplicate_rejected_case_insensitive(self):
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO customers (cod_empresa, razon_social, alias_interno) VALUES (%s, %s, %s)",
                ["TMD", "TERAPIAS MEDICAS DOMICILIARIAS S.A.", "TMD"],
            )

        response = self.client.post(
            "/api/catalogos/clientes/",
            {"razon_social": "OTRO CLIENTE", "alias_interno": "tmd"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("alias_interno", response.data)

    def test_global_search_matches_customer_alias(self):
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO customers (cod_empresa, razon_social, alias_interno, cuit) VALUES (%s, %s, %s, %s)",
                ["TMD", "TERAPIAS MEDICAS DOMICILIARIAS S.A.", "TMD", "30700000001"],
            )

        response = self.client.get("/api/busqueda/global/?q=TMD")

        self.assertEqual(response.status_code, 200)
        groups = {group["key"]: group["items"] for group in response.data["groups"]}
        self.assertEqual(groups["clientes"][0]["razon_social"], "TERAPIAS MEDICAS DOMICILIARIAS S.A.")
        self.assertEqual(groups["clientes"][0]["alias_interno"], "TMD")

    @patch("service.management.commands.backfill_bejerman_customer_codes_by_cuit.BejermanSDKClient")
    def test_backfill_bejerman_customer_codes_by_unique_cuit(self, client_cls):
        client_cls.return_value = _FakeBejermanClient(
            records=[
                {
                    "Cliente_Codigo": "ADR",
                    "Cliente_RazonSocial": "ADR EQUIPOS MEDICOS",
                    "Cliente_NroDocumento": "30-71111111-1",
                },
                {
                    "Cliente_Codigo": "OXL",
                    "Cliente_RazonSocial": "OXIGENOTERAPIA LOMAS",
                    "Cliente_NroDocumento": "30-72222222-2",
                },
                {
                    "Cliente_Codigo": "OK",
                    "Cliente_RazonSocial": "CLIENTE SIN CAMBIOS",
                    "Cliente_NroDocumento": "30-73333333-3",
                },
                {
                    "Cliente_Codigo": "FIL",
                    "Cliente_RazonSocial": "CLIENTE PARA COMPLETAR CUIT",
                    "Cliente_NroDocumento": "30-76666666-6",
                },
                {
                    "Cliente_Codigo": "AMB1",
                    "Cliente_RazonSocial": "AMBIGUO UNO",
                    "Cliente_NroDocumento": "30-75555555-5",
                },
                {
                    "Cliente_Codigo": "AMB2",
                    "Cliente_RazonSocial": "AMBIGUO DOS",
                    "Cliente_NroDocumento": "30-75555555-5",
                },
            ]
        )
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO customers (cod_empresa, razon_social, cuit, telefono, email)
                VALUES
                  (%s, %s, %s, '', ''),
                  (%s, %s, %s, '', ''),
                  (%s, %s, %s, '', ''),
                  (%s, %s, %s, '', ''),
                  (%s, %s, %s, '', ''),
                  (%s, %s, %s, '', ''),
                  (%s, %s, %s, '', '')
                """,
                [
                    "OLD",
                    "ADR LOCAL",
                    "30711111111",
                    None,
                    "OXL LOCAL",
                    "30-72222222-2",
                    "OK",
                    "CLIENTE SIN CAMBIOS",
                    "30733333333",
                    "FIL",
                    "COMPLETAR CUIT",
                    "",
                    None,
                    "SIN CUIT",
                    "",
                    None,
                    "SIN BEJERMAN",
                    "30744444444",
                    None,
                    "AMBIGUO",
                    "30755555555",
                ],
            )

        out = StringIO()
        call_command("backfill_bejerman_customer_codes_by_cuit", "--apply", stdout=out)

        with connection.cursor() as cur:
            cur.execute("SELECT razon_social, COALESCE(cod_empresa, ''), COALESCE(cuit, '') FROM customers ORDER BY razon_social")
            by_name = {row[0]: {"code": row[1], "cuit": row[2]} for row in cur.fetchall()}

        self.assertEqual(by_name["ADR LOCAL"]["code"], "OLD")
        self.assertEqual(by_name["OXL LOCAL"]["code"], "OXL")
        self.assertEqual(by_name["CLIENTE SIN CAMBIOS"]["code"], "OK")
        self.assertEqual(by_name["COMPLETAR CUIT"]["code"], "FIL")
        self.assertEqual(by_name["COMPLETAR CUIT"]["cuit"], "30766666666")
        self.assertEqual(by_name["AMBIGUO"]["code"], "")
        self.assertEqual(by_name["SIN BEJERMAN"]["code"], "")
        self.assertIn("updated=1", out.getvalue())
        self.assertIn("cuit_filled=1", out.getvalue())
        self.assertIn("code_conflict_by_cuit=1", out.getvalue())
        self.assertIn("ambiguous_bejerman_cuit=1", out.getvalue())

    @patch("service.management.commands.backfill_bejerman_customer_codes_by_cuit.BejermanSDKClient")
    def test_backfill_cuit_by_code_skips_identity_mismatch(self, client_cls):
        client_cls.return_value = _FakeBejermanClient(
            records=[
                {
                    "Cliente_Codigo": "ARG",
                    "Cliente_RazonSocial": "ARGENLAB S R L",
                    "Cliente_NroDocumento": "30645416933",
                }
            ]
        )
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO customers (cod_empresa, razon_social, cuit) VALUES (%s, %s, %s)",
                ["ARG", "ARGENTINA DE GASES S.A.", ""],
            )

        out = StringIO()
        call_command("backfill_bejerman_customer_codes_by_cuit", "--apply", stdout=out)

        with connection.cursor() as cur:
            cur.execute("SELECT COALESCE(cuit, '') FROM customers WHERE cod_empresa=%s", ["ARG"])
            cuit = cur.fetchone()[0]

        self.assertEqual(cuit, "")
        self.assertIn("code_identity_mismatch=1", out.getvalue())
