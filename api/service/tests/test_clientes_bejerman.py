import csv
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from service.models import User


class _FakeBejermanClient:
    def __init__(self, records=None, *args, **kwargs):
        self.records = records
        self.company_key = kwargs.get("company_key") or "SEPID"
        self.company = "SEP"

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
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS bejerman_seller_code TEXT")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS bejerman_seller_code_confirmed_at TIMESTAMPTZ NULL")
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
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS mg_venta_customer_id INTEGER",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS estado TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS presupuesto_estado TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS fecha_ingreso TIMESTAMPTZ",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS fecha_creacion TIMESTAMPTZ",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS equipo_variante TEXT",
            ):
                cur.execute(statement)
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS device_mg_events (
                    id {auto_inc},
                    device_id INTEGER NOT NULL,
                    accion TEXT NOT NULL DEFAULT 'venta',
                    venta_customer_id INTEGER NULL
                )
                """
            )
            cur.execute("ALTER TABLE device_mg_events ADD COLUMN IF NOT EXISTS venta_customer_id INTEGER")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS delivery_orders (
                    id TEXT PRIMARY KEY,
                    order_number TEXT NOT NULL UNIQUE,
                    customer_id INTEGER NULL,
                    bejerman_customer_code TEXT NULL,
                    customer_name TEXT NOT NULL DEFAULT ''
                )
                """
            )
            cur.execute("ALTER TABLE delivery_orders ADD COLUMN IF NOT EXISTS customer_id INTEGER")
            cur.execute("ALTER TABLE delivery_orders ADD COLUMN IF NOT EXISTS bejerman_customer_code TEXT")
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS preventivo_planes (
                    id {auto_inc},
                    scope_type TEXT NOT NULL DEFAULT 'customer',
                    device_id INTEGER NULL,
                    customer_id INTEGER NULL,
                    periodicidad_valor INTEGER NOT NULL DEFAULT 1,
                    periodicidad_unidad TEXT NOT NULL DEFAULT 'meses',
                    activa {bool_type} DEFAULT {bool_default}
                )
                """
            )
            cur.execute("ALTER TABLE preventivo_planes ADD COLUMN IF NOT EXISTS customer_id INTEGER")
        call_command("apply_customer_bejerman_schema", verbosity=0)
        call_command("apply_customer_alias_schema", verbosity=0)

    def setUp(self):
        with connection.cursor() as cur:
            for table in (
                "delivery_orders",
                "device_mg_events",
                "preventivo_planes",
                "ingresos",
                "devices",
                "customers",
                "user_permission_overrides",
            ):
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

    @patch("service.customer_bejerman_sync.BejermanSDKClient", return_value=_FakeBejermanClient())
    def test_customer_list_includes_bejerman_sync_and_activity_counts(self, _client_cls):
        sim_id, cma_id = self._seed_customers()

        response = self.client.get("/api/catalogos/clientes/?include_stats=1&include_bejerman=1")

        self.assertEqual(response.status_code, 200)
        by_id = {row["id"]: row for row in response.data}
        self.assertEqual(by_id[sim_id]["bejerman_sync"]["status"], "synced")
        self.assertEqual(by_id[sim_id]["bejerman_sync"]["candidate"]["details"]["condicionIva"], "RI")
        self.assertEqual(by_id[sim_id]["bejerman_sync"]["candidate"]["details"]["codigoEmpresa"], "SIM")
        self.assertEqual(by_id[sim_id]["bejerman_sync"]["candidate"]["details"]["companyKey"], "SEPID")
        self.assertEqual(by_id[sim_id]["equipos_count"], 2)
        self.assertEqual(by_id[sim_id]["ingresos_count"], 1)
        self.assertEqual(by_id[cma_id]["bejerman_sync"]["status"], "missing_code")
        self.assertEqual(by_id[cma_id]["bejerman_sync"]["candidates"][0]["code"], "CMA")

    @patch("service.customer_bejerman_sync.BejermanSDKClient", return_value=_FakeBejermanClient())
    def test_bejerman_candidate_search_matches_by_name(self, _client_cls):
        self._seed_customers()

        response = self.client.get("/api/catalogos/clientes/bejerman-candidatos/?q=amenabar")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["items"][0]["code"], "CMA")
        self.assertEqual(response.data["items"][0]["details"]["condicionIva"], "EX")

    @patch("service.customer_bejerman_sync.BejermanSDKClient")
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
                SELECT razon_social, cuit, telefono, email, bejerman_condicion_iva, bejerman_domicilio,
                       bejerman_raw, alias_interno, bejerman_cod_empresa, bejerman_company_key
                  FROM customers
                 WHERE id=%s
                """,
                [sim_id],
            )
            sim = cur.fetchone()
            cur.execute(
                "SELECT bejerman_condicion_iva, bejerman_cod_empresa, bejerman_company_key FROM customers WHERE cod_empresa=%s",
                ["NUE"],
            )
            created = cur.fetchone()

        self.assertEqual(sim[0], "SIM ELECTRO MEDICINA SRL")
        self.assertEqual(sim[1], "30716787946")
        self.assertEqual(sim[2], "1111")
        self.assertEqual(sim[3], "sim@example.com")
        self.assertEqual(sim[4], "RI")
        self.assertEqual(sim[5], "Av. Siempre Viva 123")
        self.assertIn("Cliente_SitIVA", str(sim[6]))
        self.assertEqual(sim[7], "SIMED")
        self.assertEqual(sim[8], "SIM")
        self.assertEqual(sim[9], "SEPID")
        self.assertEqual(created[0], "MT")
        self.assertEqual(created[1], "NUE")
        self.assertEqual(created[2], "SEPID")

    @patch("service.customer_bejerman_sync.BejermanSDKClient")
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

    def _insert_merge_customer(
        self,
        *,
        cod_empresa="",
        razon_social,
        cuit="",
        alias_interno="",
        telefono="",
        email="",
        synced=False,
        condicion_iva="",
        bejerman_email="",
        bejerman_cod_empresa="",
        bejerman_company_key="",
    ):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO customers (
                    cod_empresa, razon_social, cuit, alias_interno, telefono, email,
                    bejerman_synced_at, bejerman_condicion_iva, bejerman_email,
                    bejerman_cod_empresa, bejerman_company_key
                )
                VALUES (%s, %s, %s, %s, %s, %s,
                        CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE NULL END, %s, %s,
                        %s, %s)
                RETURNING id
                """,
                [
                    cod_empresa,
                    razon_social,
                    cuit,
                    alias_interno,
                    telefono,
                    email,
                    bool(synced),
                    condicion_iva,
                    bejerman_email,
                    bejerman_cod_empresa,
                    bejerman_company_key,
                ],
            )
            return int(cur.fetchone()[0])

    def _run_duplicate_report(self):
        tempdir = tempfile.TemporaryDirectory()
        report_path = Path(tempdir.name) / "clientes_duplicados.csv"
        out = StringIO()
        call_command("merge_duplicate_customers", "--report", str(report_path), stdout=out)
        with report_path.open("r", newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        return tempdir, report_path, rows, out.getvalue()

    def test_merge_duplicate_customers_report_classifies_safe_review_and_excludes_false_fuzzy(self):
        edu_1 = self._insert_merge_customer(
            cod_empresa="EDU",
            razon_social="ANDREOLI EDUARDO GABRIEL",
            cuit="20149334239",
        )
        edu_2 = self._insert_merge_customer(
            cod_empresa="EDU",
            razon_social="ANDREOLI EDUARDO GABRIEL",
            cuit="20149334239",
            synced=True,
        )
        gms_1 = self._insert_merge_customer(cod_empresa="GMS", razon_social="GRUPO MEDICO SAN FERNANDO")
        gms_2 = self._insert_merge_customer(
            cod_empresa="GMSF",
            razon_social="GRUPO MEDICO SAN FERNANDO S A",
            cuit="30677305157",
            synced=True,
        )
        nov = self._insert_merge_customer(cod_empresa="NOV", razon_social="NOVAMED S.A.", cuit="30710659768", synced=True)
        mgbalbin = self._insert_merge_customer(cod_empresa="NO", razon_social="MGBALBIN", cuit="30710659768")
        false_1 = self._insert_merge_customer(cod_empresa="JGE", razon_social="J G ELECTROMEDICINA")
        false_2 = self._insert_merge_customer(cod_empresa="RGE", razon_social="RG ELECTROMEDICINA")

        tempdir, _, rows, output = self._run_duplicate_report()
        self.addCleanup(tempdir.cleanup)

        by_pair = {(int(row["target_id"]), int(row["source_id"])): row for row in rows}
        self.assertEqual(by_pair[(edu_1, edu_2)]["confidence"], "safe")
        self.assertEqual(by_pair[(gms_1, gms_2)]["confidence"], "safe")
        self.assertEqual(by_pair[(nov, mgbalbin)]["confidence"], "review")
        self.assertNotIn((false_1, false_2), by_pair)
        self.assertIn("safe=2", output)
        self.assertIn("review=1", output)

    def test_merge_duplicate_customers_report_keeps_same_cuit_in_different_bejerman_companies_separate(self):
        self._insert_merge_customer(
            cod_empresa="SEP",
            razon_social="SEPID SA",
            cuit="30710069561",
            synced=True,
            bejerman_cod_empresa="SEP",
            bejerman_company_key="MGBIO",
        )
        self._insert_merge_customer(
            cod_empresa="BRITA",
            razon_social="HOSPITAL BRITANICO DE BUENOS AIRES ASOCI",
            cuit="30710069561",
            synced=True,
            bejerman_cod_empresa="BRITA",
            bejerman_company_key="SEPID",
        )

        tempdir, _, rows, output = self._run_duplicate_report()
        self.addCleanup(tempdir.cleanup)

        self.assertEqual(rows, [])
        self.assertIn("candidates=0", output)

    def test_merge_duplicate_customers_report_only_does_not_change_counts(self):
        target_id = self._insert_merge_customer(cod_empresa="AVIL", razon_social="AVIL SALUD SRL")
        source_id = self._insert_merge_customer(
            cod_empresa="AVI",
            razon_social="AVIL SALUD S.R.L",
            cuit="30715967681",
            synced=True,
        )
        with connection.cursor() as cur:
            cur.execute("INSERT INTO devices (customer_id) VALUES (%s), (%s)", [target_id, source_id])
            cur.execute("SELECT COUNT(*) FROM customers")
            customers_before = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM devices WHERE customer_id=%s", [source_id])
            source_devices_before = cur.fetchone()[0]

        tempdir, report_path, rows, _ = self._run_duplicate_report()
        self.addCleanup(tempdir.cleanup)

        with connection.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM customers")
            customers_after = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM devices WHERE customer_id=%s", [source_id])
            source_devices_after = cur.fetchone()[0]

        self.assertTrue(report_path.exists())
        self.assertEqual(customers_after, customers_before)
        self.assertEqual(source_devices_after, source_devices_before)
        self.assertEqual(rows[0]["confidence"], "safe")

    def test_merge_duplicate_customers_apply_moves_references_and_prefers_bejerman_fields(self):
        target_id = self._insert_merge_customer(
            cod_empresa="OLD",
            razon_social="CLIENTE LOCAL",
            cuit="",
            telefono="1111",
            email="local@example.com",
        )
        source_id = self._insert_merge_customer(
            cod_empresa="BEJ",
            razon_social="CLIENTE BEJERMAN S.A.",
            cuit="30700000001",
            telefono="2222",
            email="source@example.com",
            synced=True,
            condicion_iva="RI",
            bejerman_email="bejerman@example.com",
        )
        with connection.cursor() as cur:
            cur.execute("INSERT INTO devices (customer_id) VALUES (%s) RETURNING id", [source_id])
            source_device_id = cur.fetchone()[0]
            cur.execute("INSERT INTO devices (customer_id, mg_venta_customer_id) VALUES (%s, %s)", [target_id, source_id])
            cur.execute(
                "INSERT INTO device_mg_events (device_id, accion, venta_customer_id) VALUES (%s, %s, %s)",
                [source_device_id, "venta", source_id],
            )
            cur.execute(
                """
                INSERT INTO delivery_orders (id, order_number, customer_id, bejerman_customer_code, customer_name)
                VALUES (%s, %s, %s, %s, %s)
                """,
                ["do-1", "OE-1", source_id, "BEJ", "CLIENTE BEJERMAN S.A."],
            )
            cur.execute(
                """
                INSERT INTO preventivo_planes (scope_type, customer_id, periodicidad_valor, periodicidad_unidad)
                VALUES (%s, %s, %s, %s)
                """,
                ["customer", source_id, 1, "meses"],
            )

        with tempfile.TemporaryDirectory() as tempdir:
            input_path = Path(tempdir) / "aprobados.csv"
            with input_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["approved", "target_id", "source_id"])
                writer.writeheader()
                writer.writerow({"approved": "SI", "target_id": target_id, "source_id": source_id})

            out = StringIO()
            call_command(
                "merge_duplicate_customers",
                "--apply",
                "--input",
                str(input_path),
                "--actor-email",
                self.user.email,
                stdout=out,
            )

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT cod_empresa, razon_social, cuit, telefono, email, bejerman_condicion_iva, bejerman_email
                  FROM customers
                 WHERE id=%s
                """,
                [target_id],
            )
            target = cur.fetchone()
            cur.execute("SELECT COUNT(*) FROM customers WHERE id=%s", [source_id])
            source_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM devices WHERE customer_id=%s", [source_id])
            source_devices = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM devices WHERE customer_id=%s", [target_id])
            target_devices = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM devices WHERE mg_venta_customer_id=%s", [target_id])
            target_mg_sale = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM device_mg_events WHERE venta_customer_id=%s", [target_id])
            target_mg_events = cur.fetchone()[0]
            cur.execute("SELECT customer_id, bejerman_customer_code FROM delivery_orders WHERE id=%s", ["do-1"])
            delivery_order = cur.fetchone()
            cur.execute("SELECT COUNT(*) FROM preventivo_planes WHERE customer_id=%s", [target_id])
            target_plans = cur.fetchone()[0]

        self.assertEqual(target[0], "BEJ")
        self.assertEqual(target[1], "CLIENTE BEJERMAN S.A.")
        self.assertEqual(target[2], "30700000001")
        self.assertEqual(target[3], "2222")
        self.assertEqual(target[4], "source@example.com")
        self.assertEqual(target[5], "RI")
        self.assertEqual(target[6], "bejerman@example.com")
        self.assertEqual(source_count, 0)
        self.assertEqual(source_devices, 0)
        self.assertEqual(target_devices, 2)
        self.assertEqual(target_mg_sale, 1)
        self.assertEqual(target_mg_events, 1)
        self.assertEqual(delivery_order[0], target_id)
        self.assertEqual(delivery_order[1], "BEJ")
        self.assertEqual(target_plans, 1)
        self.assertIn("merged=1", out.getvalue())

    def test_merge_duplicate_customers_apply_blocks_alias_conflict(self):
        target_id = self._insert_merge_customer(
            cod_empresa="AL1",
            razon_social="ALIAS CLIENTE",
            alias_interno="UNO",
        )
        source_id = self._insert_merge_customer(
            cod_empresa="AL2",
            razon_social="ALIAS CLIENTE",
            alias_interno="DOS",
            synced=True,
        )
        with tempfile.TemporaryDirectory() as tempdir:
            input_path = Path(tempdir) / "aprobados.csv"
            with input_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["approved", "target_id", "source_id"])
                writer.writeheader()
                writer.writerow({"approved": "SI", "target_id": target_id, "source_id": source_id})

            with self.assertRaises(CommandError):
                call_command(
                    "merge_duplicate_customers",
                    "--apply",
                    "--input",
                    str(input_path),
                    "--actor-email",
                    self.user.email,
                    stdout=StringIO(),
                )

        with connection.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM customers WHERE id IN (%s, %s)", [target_id, source_id])
            remaining = cur.fetchone()[0]
        self.assertEqual(remaining, 2)
