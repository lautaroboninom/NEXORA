import json
from unittest import skipUnless
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from service.auth import issue_token
from service.bejerman_sync import (
    BejermanSDKClient,
    BejermanTransientError,
    SYNC_TYPE_STOCK_ENTRY_STR,
    SYNC_TYPE_STOCK_EXIT_RTS,
    SYNC_TYPE_STOCK_STR_TO_STC,
    SYNC_TYPE_STOCK_STR_TO_STL,
    enqueue_stock_exit_for_ingreso,
    enqueue_stock_entry_for_ingreso,
    enqueue_stock_transfer_for_ingreso,
    normalize_article_variant,
    process_bejerman_jobs,
    restore_target_stock_from_jobs,
)
from service.bejerman_sdk import BejermanPdfPendingError, BejermanSdkResponseError
from service.models import User


def _bejerman_settings(**extra):
    values = {
        "BEJERMAN_SYNC_ENABLED": True,
        "BEJERMAN_WSDL_URL": "http://bejerman.test/EFlexSDK_Service.svc",
        "BEJERMAN_USER": "sdk-user",
        "BEJERMAN_PASSWORD": "sdk-password",
        "BEJERMAN_COMPANY": "EMP",
        "BEJERMAN_WORKSTATION": "WS",
        "BEJERMAN_BRANCH": "",
        "BEJERMAN_SOURCE_DEPOSIT": "STR",
        "BEJERMAN_TARGET_DEPOSIT": "STL",
        "BEJERMAN_CLIENT_TARGET_DEPOSIT": "STC",
        "BEJERMAN_NUMERA_FLEX": "S",
        "BEJERMAN_STOCK_NUMERA_FLEX": "N",
        "BEJERMAN_STOCK_ENTRY_COMPROBANTE": "ENT",
        "BEJERMAN_STOCK_EXIT_COMPROBANTE": "SAL",
        "BEJERMAN_STOCK_TRANSFER_COMPROBANTE": "TRA",
        "BEJERMAN_STOCK_TRANSFER_OUT_COMPROBANTE": "SAL",
        "BEJERMAN_STOCK_TRANSFER_IN_COMPROBANTE": "ENT",
        "BEJERMAN_STOCK_TRANSFER_TIPO_OPERACION": "",
        "BEJERMAN_ARTICLE_AUTO_MATCH": True,
        "BEJERMAN_MAX_ATTEMPTS": 8,
        "BEJERMAN_REQUEST_TIMEOUT": 5,
    }
    values.update(extra)
    return override_settings(**values)


class FakeBejermanClient:
    def __init__(
        self,
        *,
        source_records=None,
        target_records=None,
        deposit_records=None,
        articles=None,
        stock_error=None,
        movement_error=None,
    ):
        self.source_records = source_records if source_records is not None else []
        self.target_records = target_records if target_records is not None else []
        self.deposit_records = deposit_records
        self.articles = articles if articles is not None else []
        self.stock_error = stock_error
        self.movement_error = movement_error
        self.stock_calls = []
        self.article_calls = []
        self.movements = []

    def stock_by_deposit_partida(self, deposit, serial):
        self.stock_calls.append((deposit, serial))
        if self.stock_error:
            raise self.stock_error
        if self.deposit_records is not None:
            records = self.deposit_records.get(deposit, [])
        else:
            records = self.target_records if deposit in ("STL", "STC", "STCL") else self.source_records
        return {"Resultado": "OK", "DatosJSON": json.dumps(records)}

    def obtener_articulos(self, article_code=""):
        self.article_calls.append(article_code)
        return {"Resultado": "OK", "DatosJSON": json.dumps(self.articles)}

    def ingresar_lista_comprobantes_json(self, comprobantes):
        self.movements.append(comprobantes)
        if self.movement_error:
            raise self.movement_error
        return {"Resultado": "OK", "DatosJSON": "{}"}


class FakeRisClient:
    def __init__(self, number="00001234"):
        self.number = number
        self.ingresar_calls = 0

    def ingresar_comprobante_ventas_json(self, comprobante, **kwargs):
        self.ingresar_calls += 1
        return {
            "Resultado": "OK",
            "DatosJSON": json.dumps(
                {
                    "Comprobante_Tipo": "RIS",
                    "Comprobante_Letra": "R",
                    "Comprobante_PtoVenta": "00004",
                    "Comprobante_Numero": self.number,
                }
            ),
        }


def _ris_customer_fields():
    return {
        "Cliente_RazonSocial": "Cliente Bejerman",
        "Cliente_NroDocumento": "30700000000",
        "Cliente_Provincia": "02",
        "Cliente_SitIVA": "RI",
    }


@skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL")
@override_settings(SECURE_SSL_REDIRECT=False)
class BejermanSyncTest(TestCase):
    @classmethod
    def setUpClass(cls):
        with connection.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
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
                    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
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
                    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    cod_empresa TEXT,
                    razon_social TEXT NOT NULL,
                    telefono TEXT,
                    email TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS marcas (
                    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    nombre TEXT NOT NULL,
                    tecnico_id INTEGER NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS models (
                    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    marca_id INTEGER REFERENCES marcas(id),
                    nombre TEXT NOT NULL,
                    tipo_equipo TEXT,
                    variante TEXT,
                    tecnico_id INTEGER NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS locations (
                    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    nombre TEXT NOT NULL UNIQUE
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    customer_id INTEGER NOT NULL REFERENCES customers(id),
                    marca_id INTEGER REFERENCES marcas(id),
                    model_id INTEGER REFERENCES models(id),
                    tipo_equipo TEXT,
                    numero_serie TEXT,
                    numero_interno TEXT,
                    variante TEXT,
                    ubicacion_id INTEGER NULL REFERENCES locations(id),
                    garantia_vence DATE NULL,
                    alquilado BOOLEAN NOT NULL DEFAULT FALSE,
                    alquiler_a TEXT,
                    mg_estado TEXT DEFAULT 'activo'
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ingresos (
                    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    device_id INTEGER NOT NULL REFERENCES devices(id),
                    estado TEXT,
                    motivo TEXT,
                    fecha_ingreso TIMESTAMPTZ NULL,
                    fecha_creacion TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    resolucion TEXT,
                    ubicacion_id INTEGER NULL,
                    presupuesto_estado TEXT,
                    asignado_a INTEGER NULL REFERENCES users(id),
                    recibido_por INTEGER NULL REFERENCES users(id),
                    informe_preliminar TEXT,
                    accesorios TEXT,
                    comentarios TEXT,
                    equipo_variante TEXT,
                    remito_salida TEXT,
                    factura_numero TEXT,
                    fecha_entrega TIMESTAMPTZ NULL,
                    alquilado BOOLEAN NOT NULL DEFAULT FALSE,
                    propietario_nombre TEXT,
                    propietario_contacto TEXT,
                    propietario_doc TEXT,
                    garantia_reparacion BOOLEAN DEFAULT FALSE,
                    garantia_fabrica BOOLEAN DEFAULT FALSE,
                    etiq_garantia_ok BOOLEAN DEFAULT TRUE
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ingreso_events (
                    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    ticket_id INTEGER NOT NULL REFERENCES ingresos(id) ON DELETE CASCADE,
                    ingreso_id INTEGER GENERATED ALWAYS AS (ticket_id) STORED,
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
                DO $$ BEGIN
                  IF NOT EXISTS (
                    SELECT 1
                      FROM information_schema.columns
                     WHERE table_name = 'ingreso_events'
                       AND column_name = 'ingreso_id'
                  ) THEN
                    ALTER TABLE ingreso_events
                    ADD COLUMN ingreso_id INTEGER GENERATED ALWAYS AS (ticket_id) STORED;
                  END IF;
                END $$;
                """
            )
            for statement in (
                "ALTER TABLE customers ADD COLUMN IF NOT EXISTS cod_empresa TEXT",
                "ALTER TABLE customers ADD COLUMN IF NOT EXISTS telefono TEXT",
                "ALTER TABLE customers ADD COLUMN IF NOT EXISTS email TEXT",
                "ALTER TABLE models ADD COLUMN IF NOT EXISTS tipo_equipo TEXT",
                "ALTER TABLE models ADD COLUMN IF NOT EXISTS variante TEXT",
                "ALTER TABLE models ADD COLUMN IF NOT EXISTS tecnico_id INTEGER NULL",
                "ALTER TABLE marcas ADD COLUMN IF NOT EXISTS tecnico_id INTEGER NULL",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS numero_interno TEXT",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS tipo_equipo TEXT",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS variante TEXT",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS ubicacion_id INTEGER NULL REFERENCES locations(id)",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS garantia_vence DATE NULL",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS alquilado BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS alquiler_a TEXT",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS mg_estado TEXT DEFAULT 'activo'",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS resolucion TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS motivo TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS fecha_ingreso TIMESTAMPTZ NULL",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS fecha_creacion TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS ubicacion_id INTEGER NULL",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS presupuesto_estado TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS asignado_a INTEGER NULL",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS recibido_por INTEGER NULL",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS informe_preliminar TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS accesorios TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS comentarios TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS equipo_variante TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS remito_ingreso TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS remito_salida TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS factura_numero TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS fecha_entrega TIMESTAMPTZ NULL",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS alquilado BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS propietario_nombre TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS propietario_contacto TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS propietario_doc TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS garantia_reparacion BOOLEAN DEFAULT FALSE",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS garantia_fabrica BOOLEAN DEFAULT FALSE",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS etiq_garantia_ok BOOLEAN DEFAULT TRUE",
            ):
                cur.execute(statement)

        call_command("apply_bejerman_sync_schema", verbosity=0)
        call_command("apply_bejerman_ris_schema", verbosity=0)
        call_command("apply_user_permissions_schema", verbosity=0)
        super().setUpClass()

    def setUp(self):
        super().setUp()
        with connection.cursor() as cur:
            cur.execute("DELETE FROM bejerman_ingreso_remitos")
            cur.execute("DELETE FROM bejerman_article_mappings")
            cur.execute("DELETE FROM bejerman_sync_jobs")
            cur.execute("DELETE FROM ingreso_events")
            cur.execute("DELETE FROM ingresos")
            cur.execute("DELETE FROM devices")
            cur.execute("DELETE FROM models")
            cur.execute("DELETE FROM marcas")
            cur.execute("DELETE FROM customers")
            cur.execute("DELETE FROM locations")
        User.objects.filter(email__endswith="@bejerman.test").delete()
        self.admin = User.objects.create(
            nombre="Admin Bejerman",
            email="admin@bejerman.test",
            hash_pw="",
            rol="admin",
            activo=True,
        )
        self.jefe = User.objects.create(
            nombre="Jefe Bejerman",
            email="jefe@bejerman.test",
            hash_pw="",
            rol="jefe",
            activo=True,
        )
        self.token = issue_token(self.admin)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.token}")

        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO customers(cod_empresa, razon_social, telefono, email) VALUES (%s,%s,%s,%s) RETURNING id",
                ["CLI-BEJ", "Cliente Bejerman", "123", "ops@bejerman.test"],
            )
            self.customer_id = int(cur.fetchone()[0])
            cur.execute(
                "INSERT INTO customers(cod_empresa, razon_social, telefono, email) VALUES (%s,%s,%s,%s) RETURNING id",
                ["MGBIO", "MG BIO", "", "mgbio@bejerman.test"],
            )
            self.mg_customer_id = int(cur.fetchone()[0])
            cur.execute("INSERT INTO locations(nombre) VALUES (%s) RETURNING id", ["Taller"])
            self.taller_id = int(cur.fetchone()[0])
            cur.execute("INSERT INTO locations(nombre) VALUES (%s) RETURNING id", ["Estantería de Alquiler"])
            self.estanteria_id = int(cur.fetchone()[0])
            cur.execute("INSERT INTO marcas(nombre) VALUES (%s) RETURNING id", ["ResMed"])
            self.marca_id = int(cur.fetchone()[0])
            cur.execute(
                "INSERT INTO models(marca_id, nombre, tipo_equipo, variante) VALUES (%s,%s,%s,%s) RETURNING id",
                [self.marca_id, "AirSense 10", "CPAP", ""],
            )
            self.model_id = int(cur.fetchone()[0])

    def _insert_ingreso(
        self,
        *,
        serial="SN-BEJ-001",
        numero_interno="",
        estado="reparado",
        resolucion="reparado",
        ubicacion_id=None,
        equipo_variante="",
        customer_id=None,
        device_id=None,
        mg_estado="activo",
    ):
        with connection.cursor() as cur:
            if device_id is None:
                cur.execute(
                    """
                    INSERT INTO devices(
                        customer_id, marca_id, model_id, numero_serie, numero_interno, variante, alquilado, mg_estado
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,FALSE,%s)
                    RETURNING id
                    """,
                    [
                        customer_id or self.customer_id,
                        self.marca_id,
                        self.model_id,
                        serial,
                        numero_interno,
                        equipo_variante,
                        mg_estado,
                    ],
                )
                device_id = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO ingresos(
                    device_id, estado, motivo, fecha_ingreso, fecha_creacion,
                    resolucion, ubicacion_id, presupuesto_estado, asignado_a, equipo_variante
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
                """,
                [
                    device_id,
                    estado,
                    "reparacion",
                    timezone.now(),
                    timezone.now(),
                    resolucion,
                    ubicacion_id or self.taller_id,
                    "no_aplica",
                    self.admin.id,
                    equipo_variante,
                ],
            )
            return int(cur.fetchone()[0])

    def _insert_liberado_event(self, ingreso_id):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ingreso_events(ticket_id, a_estado, usuario_id, comentario)
                VALUES (%s,'liberado',%s,'test')
                RETURNING id
                """,
                [ingreso_id, self.admin.id],
            )
            return int(cur.fetchone()[0])

    def _job_row(self, ingreso_id, sync_type=None):
        with connection.cursor() as cur:
            if sync_type:
                cur.execute(
                    "SELECT * FROM bejerman_sync_jobs WHERE ingreso_id=%s AND sync_type=%s",
                    [ingreso_id, sync_type],
                )
            else:
                cur.execute("SELECT * FROM bejerman_sync_jobs WHERE ingreso_id=%s ORDER BY id", [ingreso_id])
            cols = [col[0] for col in cur.description]
            row = cur.fetchone()
        return dict(zip(cols, row)) if row else None

    def _json_value(self, value):
        return json.loads(value) if isinstance(value, str) else value

    def _enqueue_job(
        self,
        *,
        serial="SN-BEJ-001",
        numero_interno="MG 9999",
        customer_id=None,
        equipo_variante="",
    ):
        ingreso_id = self._insert_ingreso(
            serial=serial,
            numero_interno=numero_interno,
            customer_id=customer_id or self.mg_customer_id,
            equipo_variante=equipo_variante,
        )
        event_id = self._insert_liberado_event(ingreso_id)
        enqueue_stock_transfer_for_ingreso(ingreso_id, event_id)
        return ingreso_id

    def _insert_mapping(self, *, article_code="ART-CPAP", variante=""):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bejerman_article_mappings(
                    model_id, variante, variante_norm, article_code, article_description, match_source
                )
                VALUES (%s,%s,%s,%s,%s,'manual')
                RETURNING id
                """,
                [
                    self.model_id,
                    variante,
                    normalize_article_variant(variante),
                    article_code,
                    "Artículo CPAP",
                ],
            )
            return int(cur.fetchone()[0])

    def test_ris_emitir_devuelve_json_con_remito_sin_bloquear_por_pdf(self):
        ingreso_id = self._insert_ingreso(serial="SN-RIS-JSON-001")
        fake = FakeRisClient(number="00004561")

        with (
            _bejerman_settings(),
            patch("service.bejerman_ris.BejermanSDKClient", return_value=fake),
            patch("service.bejerman_ris.resolve_customer_document_fields", return_value=_ris_customer_fields()),
        ):
            response = self.client.post(f"/api/ingresos/{ingreso_id}/ris/emitir/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["remito_number"], "RIS R 00004-00004561")
        self.assertEqual(response.json()["pdf_status"], "pending")
        self.assertEqual(response.json()["print_url"], f"/api/ingresos/{ingreso_id}/ris/print/")
        self.assertEqual(fake.ingresar_calls, 1)

        status_response = self.client.get(f"/api/ingresos/{ingreso_id}/ris/")
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json()["remito_number"], "RIS R 00004-00004561")
        self.assertEqual(status_response.json()["pdf_url"], f"/api/ingresos/{ingreso_id}/ris/pdf/")

    def test_ris_emitir_generado_no_vuelve_a_emitir_en_bejerman(self):
        ingreso_id = self._insert_ingreso(serial="SN-RIS-IDEMP-001")
        fake = FakeRisClient(number="00004562")

        with (
            _bejerman_settings(),
            patch("service.bejerman_ris.BejermanSDKClient", return_value=fake),
            patch("service.bejerman_ris.resolve_customer_document_fields", return_value=_ris_customer_fields()),
        ):
            first = self.client.post(f"/api/ingresos/{ingreso_id}/ris/emitir/")
            second = self.client.post(f"/api/ingresos/{ingreso_id}/ris/emitir/")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["remito_number"], "RIS R 00004-00004562")
        self.assertEqual(fake.ingresar_calls, 1)

    def test_ris_pdf_pendiente_devuelve_202_sin_marcar_emision_fallida(self):
        ingreso_id = self._insert_ingreso(serial="SN-RIS-PDF-PENDING")
        fake = FakeRisClient(number="00004563")

        with (
            _bejerman_settings(),
            patch("service.bejerman_ris.BejermanSDKClient", return_value=fake),
            patch("service.bejerman_ris.resolve_customer_document_fields", return_value=_ris_customer_fields()),
        ):
            self.client.post(f"/api/ingresos/{ingreso_id}/ris/emitir/")

        with patch(
            "service.bejerman_ris.fetch_comprobante_pdf",
            side_effect=BejermanPdfPendingError(retry_after_ms=1800),
        ):
            response = self.client.get(f"/api/ingresos/{ingreso_id}/ris/pdf/")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["pdf_status"], "pending")
        self.assertEqual(response["Retry-After"], "2")
        with connection.cursor() as cur:
            cur.execute("SELECT status, pdf_status, last_error FROM bejerman_ingreso_remitos WHERE ingreso_id=%s", [ingreso_id])
            status, pdf_status, last_error = cur.fetchone()
        self.assertEqual(status, "generated")
        self.assertEqual(pdf_status, "pending")
        self.assertFalse(last_error)

    def test_ris_pdf_listo_devuelve_pdf_y_marca_ready(self):
        ingreso_id = self._insert_ingreso(serial="SN-RIS-PDF-READY")
        fake = FakeRisClient(number="00004564")

        with (
            _bejerman_settings(),
            patch("service.bejerman_ris.BejermanSDKClient", return_value=fake),
            patch("service.bejerman_ris.resolve_customer_document_fields", return_value=_ris_customer_fields()),
        ):
            self.client.post(f"/api/ingresos/{ingreso_id}/ris/emitir/")

        with patch("service.bejerman_ris.fetch_comprobante_pdf", return_value=(b"%PDF-1.4", "application/pdf")):
            response = self.client.get(f"/api/ingresos/{ingreso_id}/ris/pdf/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        with connection.cursor() as cur:
            cur.execute("SELECT pdf_status FROM bejerman_ingreso_remitos WHERE ingreso_id=%s", [ingreso_id])
            pdf_status = cur.fetchone()[0]
        self.assertEqual(pdf_status, "ready")

    def test_ris_validacion_bejerman_queda_como_error_visible(self):
        ingreso_id = self._insert_ingreso(serial="SN-RIS-IVA-MISSING")

        with (
            _bejerman_settings(),
            patch("service.bejerman_ris.BejermanSDKClient", return_value=FakeRisClient()),
            patch(
                "service.bejerman_ris.resolve_customer_document_fields",
                side_effect=BejermanSdkResponseError("El comprobante no se importó. Debe indicar la Situación de IVA del cliente."),
            ),
        ):
            response = self.client.post(f"/api/ingresos/{ingreso_id}/ris/emitir/")

        self.assertEqual(response.status_code, 502)
        self.assertIn("Situación de IVA", response.json()["detail"])
        self.assertEqual(response.json()["ris"]["status"], "failed")

    def test_apply_schema_crea_tabla_indices_y_constraints(self):
        call_command("apply_bejerman_sync_schema", verbosity=0)
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                  FROM information_schema.columns
                 WHERE table_name = 'bejerman_sync_jobs'
                """
            )
            columns = {row[0] for row in cur.fetchall()}
            cur.execute(
                """
                SELECT indexname
                  FROM pg_indexes
                 WHERE tablename = 'bejerman_sync_jobs'
                """
            )
            indexes = {row[0] for row in cur.fetchall()}
            cur.execute(
                """
                SELECT conname
                  FROM pg_constraint
                 WHERE conrelid = 'bejerman_sync_jobs'::regclass
                """
            )
            constraints = {row[0] for row in cur.fetchall()}
            cur.execute(
                """
                SELECT column_name
                  FROM information_schema.columns
                 WHERE table_name = 'bejerman_article_mappings'
                """
            )
            mapping_columns = {row[0] for row in cur.fetchall()}
            cur.execute(
                """
                SELECT indexname
                  FROM pg_indexes
                 WHERE tablename IN ('bejerman_sync_jobs','bejerman_article_mappings')
                """
            )
            all_indexes = {row[0] for row in cur.fetchall()}

        self.assertIn("sync_type", columns)
        self.assertIn("ingreso_event_id", columns)
        self.assertIn("article_code", columns)
        self.assertIn("request_payload", columns)
        self.assertIn("uq_bejerman_sync_jobs_type_ingreso", indexes)
        self.assertIn("ix_bejerman_sync_jobs_due", indexes)
        self.assertIn("chk_bejerman_sync_jobs_status", constraints)
        self.assertIn("model_id", mapping_columns)
        self.assertIn("variante_norm", mapping_columns)
        self.assertIn("article_code", mapping_columns)
        self.assertIn("uq_bejerman_article_mappings_model_variant", all_indexes)

    def test_remito_liberado_encola_transferencia_sin_rss_y_sin_duplicar_si_se_reimprime(self):
        ingreso_id = self._insert_ingreso(serial="SN-REMITO-001")
        url = f"/api/ingresos/{ingreso_id}/remito/"

        with (
            override_settings(SECURE_SSL_REDIRECT=False),
            patch("service.views.reportes_views.render_remito_salida_pdf", return_value=(b"%PDF-1.4", "remito.pdf")),
            patch("service.views.reportes_views.notify_ingreso_liberado", return_value=0),
        ):
            first = self.client.get(url)
            second = self.client.get(url)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        with connection.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM bejerman_sync_jobs WHERE ingreso_id=%s", [ingreso_id])
            jobs = int(cur.fetchone()[0])
            cur.execute("SELECT COUNT(*) FROM ingreso_events WHERE ingreso_id=%s AND a_estado='liberado'", [ingreso_id])
            events = int(cur.fetchone()[0])
        self.assertEqual(jobs, 1)
        self.assertEqual(events, 1)
        transfer = self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STC)
        self.assertEqual(transfer["numero_serie"], "SN-REMITO-001")
        self.assertEqual(transfer["target_deposit"], "STC")
        self.assertIsNone(self._job_row(ingreso_id, SYNC_TYPE_STOCK_EXIT_RTS))

    def test_remito_liberado_equipo_propio_encola_stl_sin_rss(self):
        ingreso_id = self._insert_ingreso(
            serial="SN-REMITO-MG-001",
            numero_interno="MG 0005",
            customer_id=self.mg_customer_id,
        )
        url = f"/api/ingresos/{ingreso_id}/remito/"

        with (
            override_settings(SECURE_SSL_REDIRECT=False),
            patch("service.views.reportes_views.render_remito_salida_pdf", return_value=(b"%PDF-1.4", "remito.pdf")),
            patch("service.views.reportes_views.notify_ingreso_liberado", return_value=0),
        ):
            response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        transfer = self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL)
        self.assertIsNotNone(transfer)
        self.assertEqual(transfer["source_deposit"], "STR")
        self.assertEqual(transfer["target_deposit"], "STL")
        self.assertIsNone(self._job_row(ingreso_id, SYNC_TYPE_STOCK_EXIT_RTS))

    def test_remito_liberado_codigo_mg_de_cliente_encola_stc_sin_rss(self):
        ingreso_id = self._insert_ingreso(serial="SN-REMITO-MG-CLIENTE", numero_interno="MG 0005")
        url = f"/api/ingresos/{ingreso_id}/remito/"

        with (
            override_settings(SECURE_SSL_REDIRECT=False),
            patch("service.views.reportes_views.render_remito_salida_pdf", return_value=(b"%PDF-1.4", "remito.pdf")),
            patch("service.views.reportes_views.notify_ingreso_liberado", return_value=0),
        ):
            response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL))
        transfer = self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STC)
        self.assertIsNotNone(transfer)
        self.assertEqual(transfer["source_deposit"], "STR")
        self.assertEqual(transfer["target_deposit"], "STC")
        self.assertIsNone(self._job_row(ingreso_id, SYNC_TYPE_STOCK_EXIT_RTS))

    def test_remito_liberado_mg_inactivo_por_venta_encola_stc_sin_stl(self):
        ingreso_id = self._insert_ingreso(
            serial="SN-REMITO-MG-VENDIDO",
            numero_interno="MG 0006",
            customer_id=self.mg_customer_id,
            mg_estado="inactivo_venta",
        )
        url = f"/api/ingresos/{ingreso_id}/remito/"

        with (
            override_settings(SECURE_SSL_REDIRECT=False),
            patch("service.views.reportes_views.render_remito_salida_pdf", return_value=(b"%PDF-1.4", "remito.pdf")),
            patch("service.views.reportes_views.notify_ingreso_liberado", return_value=0),
        ):
            response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL))
        transfer = self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STC)
        self.assertIsNotNone(transfer)
        self.assertEqual(transfer["source_deposit"], "STR")
        self.assertEqual(transfer["target_deposit"], "STC")
        self.assertIsNone(self._job_row(ingreso_id, SYNC_TYPE_STOCK_EXIT_RTS))

    def test_entrega_cliente_encola_solo_transferencia_a_stc(self):
        ingreso_id = self._insert_ingreso(serial="SN-ENTREGA-001")
        url = f"/api/ingresos/{ingreso_id}/entregar/"

        response = self.client.post(url, {"remito_salida": "RSS-0001"}, format="json")
        duplicate = self.client.post(url, {"remito_salida": "RSS-0001"}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(duplicate.status_code, 200, duplicate.data)
        with connection.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM bejerman_sync_jobs WHERE ingreso_id=%s", [ingreso_id])
            jobs = int(cur.fetchone()[0])
        self.assertEqual(jobs, 1)
        transfer = self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STC)
        self.assertEqual(transfer["source_deposit"], "STR")
        self.assertEqual(transfer["target_deposit"], "STC")
        self.assertIsNone(self._job_row(ingreso_id, SYNC_TYPE_STOCK_EXIT_RTS))

    def test_entrega_equipo_propio_encola_solo_transferencia_a_stl(self):
        ingreso_id = self._insert_ingreso(
            serial="SN-ENTREGA-MG-001",
            numero_interno="MG 0003",
            customer_id=self.mg_customer_id,
        )
        url = f"/api/ingresos/{ingreso_id}/entregar/"

        response = self.client.post(url, {"remito_salida": "RSS-0002"}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        transfer = self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL)
        self.assertEqual(transfer["source_deposit"], "STR")
        self.assertEqual(transfer["target_deposit"], "STL")
        self.assertIsNone(self._job_row(ingreso_id, SYNC_TYPE_STOCK_EXIT_RTS))

    def test_entrega_nm_nv_y_ce_se_tratan_como_equipos_de_cliente(self):
        for prefix in ("NM", "NV", "CE"):
            with self.subTest(prefix=prefix):
                ingreso_id = self._insert_ingreso(
                    serial=f"SN-ENTREGA-{prefix}-001",
                    numero_interno=f"{prefix} 0004",
                )
                url = f"/api/ingresos/{ingreso_id}/entregar/"

                response = self.client.post(url, {"remito_salida": f"RSS-{prefix}"}, format="json")

                self.assertEqual(response.status_code, 200, response.data)
                self.assertIsNone(self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL))
                transfer = self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STC)
                self.assertEqual(transfer["target_deposit"], "STC")
                self.assertIsNone(self._job_row(ingreso_id, SYNC_TYPE_STOCK_EXIT_RTS))

    def test_nuevo_ingreso_no_encola_entrada_str(self):
        payload = {
            "cliente": {"id": self.customer_id},
            "equipo": {
                "marca_id": self.marca_id,
                "modelo_id": self.model_id,
                "numero_serie": "SN-ENTRY-QUEUE",
                "numero_interno": "",
            },
            "motivo": "otros",
            "ubicacion_id": self.taller_id,
        }

        with (
            patch("service.views.ingresos_views._map_motivo_to_db_label", return_value="otros"),
            patch(
                "service.views.ingresos_views.compute_warranty",
                return_value={"garantia": False, "vence_el": None, "fecha_venta": None},
            ),
        ):
            response = self.client.post("/api/ingresos/nuevo/", payload, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        ingreso_id = response.data["ingreso_id"]
        self.assertIsNone(self._job_row(ingreso_id, SYNC_TYPE_STOCK_ENTRY_STR))

    def test_nuevo_ingreso_guarda_empresa_bejerman(self):
        payload = {
            "cliente": {"id": self.customer_id},
            "equipo": {
                "marca_id": self.marca_id,
                "modelo_id": self.model_id,
                "numero_serie": "SN-COMPANY-MGBIO",
                "numero_interno": "",
            },
            "motivo": "otros",
            "ubicacion_id": self.taller_id,
            "empresa_bejerman": "MGBIO",
        }

        with (
            patch("service.views.ingresos_views._map_motivo_to_db_label", return_value="otros"),
            patch(
                "service.views.ingresos_views.compute_warranty",
                return_value={"garantia": False, "vence_el": None, "fecha_venta": None},
            ),
        ):
            response = self.client.post("/api/ingresos/nuevo/", payload, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        ingreso_id = response.data["ingreso_id"]
        with connection.cursor() as cur:
            cur.execute(
                "SELECT empresa_bejerman, empresa_facturar FROM ingresos WHERE id=%s",
                [ingreso_id],
            )
            row = cur.fetchone()
        self.assertEqual(row, ("MGBIO", "MGBIO"))

    def test_entrada_str_legacy_se_bloquea_sin_emitir_movimientos(self):
        ingreso_id = self._insert_ingreso(serial="SN-ENTRY-BLOCKED")
        queued = enqueue_stock_entry_for_ingreso(ingreso_id)
        job = self._job_row(ingreso_id, SYNC_TYPE_STOCK_ENTRY_STR)
        self.assertEqual(queued["status"], "blocked")
        self.assertEqual(job["status"], "blocked")
        self.assertIn("Bejerman antes de NEXORA", job["last_error"])

        with connection.cursor() as cur:
            cur.execute(
                "UPDATE bejerman_sync_jobs SET status='pending', last_error=NULL WHERE id=%s",
                [job["id"]],
            )
        fake = FakeBejermanClient(deposit_records={"STR": []})

        with _bejerman_settings():
            stats = process_bejerman_jobs(client=fake)

        self.assertEqual(stats["blocked"], 1)
        self.assertEqual(fake.stock_calls, [])
        self.assertEqual(fake.movements, [])
        job = self._job_row(ingreso_id, SYNC_TYPE_STOCK_ENTRY_STR)
        self.assertEqual(job["status"], "blocked")
        self.assertIn("NEXORA no emite ENT/RIS de ingreso", job["last_error"])

    def test_salida_fisica_se_bloquea_por_gestion_portal(self):
        ingreso_id = self._insert_ingreso(serial="SN-RSS-OK")
        queued = enqueue_stock_exit_for_ingreso(ingreso_id, source_deposit="STC")
        fake = FakeBejermanClient(
            deposit_records={
                "STC": [
                    {
                        "Comprobante_ArtPartida": "SN-RSS-OK",
                        "Comprobante_ArtDeposito": "STC",
                        "Comprobante_Art_CodGen": "ART-RSS",
                        "Stock": 1,
                    }
                ]
            }
        )

        with _bejerman_settings():
            stats = process_bejerman_jobs(client=fake)

        self.assertEqual(queued["status"], "blocked")
        self.assertEqual(stats["processed"], 0)
        self.assertEqual(fake.stock_calls, [])
        self.assertEqual(fake.movements, [])
        job = self._job_row(ingreso_id, SYNC_TYPE_STOCK_EXIT_RTS)
        self.assertEqual(job["status"], "blocked")
        self.assertIn("Portal", job["last_error"])

    def test_job_stl_legacy_de_cliente_se_cierra_sin_emitir_movimientos(self):
        ingreso_id = self._insert_ingreso(serial="", numero_interno="MG 7358")
        with connection.cursor() as cur:
            cur.execute("SELECT device_id FROM ingresos WHERE id=%s", [ingreso_id])
            device_id = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO bejerman_sync_jobs(
                    sync_type, ingreso_id, device_id, numero_serie, source_deposit,
                    target_deposit, status, request_payload
                )
                VALUES (%s,%s,%s,'','STR','STL','pending',%s::jsonb)
                """,
                [
                    SYNC_TYPE_STOCK_STR_TO_STL,
                    ingreso_id,
                    device_id,
                    json.dumps({"source": "test", "trigger": "equipo_propio_listo_alquiler"}),
                ],
            )
        fake = FakeBejermanClient()

        with _bejerman_settings():
            stats = process_bejerman_jobs(client=fake)

        self.assertEqual(stats["succeeded"], 1)
        self.assertEqual(fake.stock_calls, [])
        self.assertEqual(fake.movements, [])
        job = self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL)
        self.assertEqual(job["status"], "succeeded")
        self.assertTrue(self._json_value(job["request_payload"])["skipped_not_applicable"])
        self.assertIn("no corresponde transferir a STL", self._json_value(job["request_payload"])["reason"])

    def test_articulo_faltante_o_ambiguo_bloquea_con_candidatos(self):
        ingreso_id = self._enqueue_job(serial="SN-ARTICLE-MISSING")
        fake = FakeBejermanClient(deposit_records={"STR": []}, articles=[])

        with _bejerman_settings():
            stats = process_bejerman_jobs(client=fake)

        self.assertEqual(stats["blocked"], 1)
        missing_job = self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL)
        self.assertIn("artículo Bejerman", missing_job["last_error"])
        self.assertEqual(self._json_value(missing_job["response_payload"])["candidates"], [])

        ingreso_id = self._enqueue_job(serial="SN-ARTICLE-AMB")
        fake = FakeBejermanClient(
            deposit_records={"STR": []},
            articles=[
                {"Art_CodGenerico": "ART-1", "Art_DescripcionGeneral": "ResMed AirSense 10"},
                {"Art_CodGenerico": "ART-2", "Art_DescripcionGeneral": "ResMed AirSense 10 CPAP"},
            ],
        )

        with _bejerman_settings():
            stats = process_bejerman_jobs(client=fake)

        self.assertEqual(stats["blocked"], 1)
        ambiguous_job = self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL)
        self.assertIn("más de un artículo", ambiguous_job["last_error"])
        self.assertEqual(len(self._json_value(ambiguous_job["response_payload"])["candidates"]), 2)

    def test_equipo_propio_encola_str_a_stl_al_quedar_listo(self):
        ingreso_id = self._insert_ingreso(
            serial="SN-OWN-READY",
            numero_interno="MG 0001",
            estado="en_reparacion",
            ubicacion_id=self.taller_id,
            customer_id=self.mg_customer_id,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_token(self.jefe)}")

        response = self.client.post(f"/api/ingresos/{ingreso_id}/reparado/", {}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        job = self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL)
        self.assertIsNotNone(job)
        self.assertEqual(job["source_deposit"], "STR")
        self.assertEqual(job["target_deposit"], "STL")

    def test_controlado_sin_defecto_propio_encola_str_a_stl(self):
        ingreso_id = self._insert_ingreso(
            serial="SN-OWN-CONTROL",
            numero_interno="MG 0002",
            estado="en_reparacion",
            ubicacion_id=self.taller_id,
            customer_id=self.mg_customer_id,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_token(self.jefe)}")

        response = self.client.post(f"/api/ingresos/{ingreso_id}/controlado-sin-defecto/", {}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        job = self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL)
        self.assertIsNotNone(job)
        self.assertEqual(job["target_deposit"], "STL")

    def test_codigo_mg_de_cliente_no_encola_str_a_stl_al_quedar_listo(self):
        ingreso_id = self._insert_ingreso(
            serial="SN-CLIENT-MG-CODE",
            numero_interno="MG 0007",
            estado="en_reparacion",
            ubicacion_id=self.taller_id,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_token(self.jefe)}")

        response = self.client.post(f"/api/ingresos/{ingreso_id}/reparado/", {}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL))

    def test_ce_reparado_no_encola_str_a_stl(self):
        ingreso_id = self._insert_ingreso(
            serial="SN-CE-READY",
            numero_interno="CE 0002",
            estado="en_reparacion",
            ubicacion_id=self.taller_id,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_token(self.jefe)}")

        response = self.client.post(f"/api/ingresos/{ingreso_id}/reparado/", {}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL))

    def test_payload_mueve_partida_desde_str_hacia_stl_con_sal_y_ent(self):
        ingreso_id = self._enqueue_job(serial="SN-MOVE-001")
        fake = FakeBejermanClient(
            source_records=[
                {
                    "Comprobante_ArtPartida": "SN-MOVE-001",
                    "Comprobante_ArtDeposito": "STR",
                    "Comprobante_Art_CodGen": "ART-CPAP",
                    "Stock": 1,
                }
            ],
            target_records=[],
        )

        with _bejerman_settings():
            stats = process_bejerman_jobs(client=fake)

        self.assertEqual(stats["succeeded"], 1)
        self.assertEqual(len(fake.movements), 2)
        sal = fake.movements[0][0]
        ent = fake.movements[1][0]
        self.assertEqual(sal["Comprobante_Tipo"], "SAL")
        self.assertEqual(ent["Comprobante_Tipo"], "ENT")
        self.assertEqual(sal["Comprobante_Numero"], ent["Comprobante_Numero"])
        self.assertRegex(sal["Comprobante_FechaEmision"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(sal["Comprobante_FechaEmision"], ent["Comprobante_FechaEmision"])
        self.assertEqual(sal["Comprobante_ArtDeposito"], "STR")
        self.assertEqual(sal["Comprobante_CantidadUM1"], -1)
        self.assertEqual(sal["Comprobante_CantidadUM2"], -1)
        self.assertEqual(sal["Comprobante_PrecioTotalMonLocal"], 0)
        self.assertEqual(ent["Comprobante_ArtDeposito"], "STL")
        self.assertEqual(ent["Comprobante_CantidadUM1"], 1)
        self.assertEqual(ent["Comprobante_CantidadUM2"], 1)
        self.assertEqual(ent["Comprobante_PrecioTotalMonLocal"], 0)
        self.assertNotIn("Comprobante_DepositoDestino", sal)
        self.assertNotIn("Comprobante_DepositoDestino", ent)
        self.assertNotIn("Comprobante_TipoOperacion", sal)
        self.assertNotIn("Comprobante_TipoOperacion", ent)
        self.assertEqual(sal["Comprobante_ArtPartida"], "SN-MOVE-001")
        self.assertEqual(ent["Comprobante_ArtPartida"], "SN-MOVE-001")
        self.assertEqual(sal["Comprobante_Art_CodGen"], "ART-CPAP")
        self.assertEqual(ent["Comprobante_Art_CodGen"], "ART-CPAP")
        self.assertEqual(sal["Comprobante_IdOrigen"], f"NEXORA-OS-{ingreso_id}-SAL-STR")
        self.assertEqual(ent["Comprobante_IdOrigen"], f"NEXORA-OS-{ingreso_id}-ENT-STL")
        job = self._job_row(ingreso_id)
        self.assertEqual(job["status"], "succeeded")
        request_payload = self._json_value(job["request_payload"])
        self.assertTrue(request_payload["sal_done"])
        self.assertTrue(request_payload["target_stock_entry_done"])
        self.assertEqual(request_payload["transfer_phase"], "target_entry_done")
        response_payload = self._json_value(job["response_payload"])
        self.assertIn("sal", response_payload)
        self.assertIn("target_stock_entry", response_payload)

    def test_cliente_sdk_envia_lista_stock_por_parametros_json(self):
        comprobantes = [{"Comprobante_Tipo": "TRA", "Comprobante_ArtPartida": "SN-MOVE-001"}]
        client = BejermanSDKClient()

        with _bejerman_settings(), patch.object(client, "execute", return_value={"Resultado": "OK"}) as execute:
            response = client.ingresar_lista_comprobantes_json(comprobantes)

        self.assertEqual(response["Resultado"], "OK")
        execute.assert_called_once_with(
            "STOCK",
            "IngresarListaComprobantesJSON",
            params_json=[json.dumps(comprobantes, ensure_ascii=False, default=str), "N"],
        )

    def test_idempotencia_remota_si_ya_esta_en_stl(self):
        ingreso_id = self._enqueue_job(serial="SN-STL-001")
        fake = FakeBejermanClient(
            source_records=[],
            target_records=[
                {
                    "Comprobante_ArtPartida": "SN-STL-001",
                    "Comprobante_ArtDeposito": "STL",
                    "Comprobante_Art_CodGen": "ART-CPAP",
                    "Stock": 1,
                }
            ],
        )

        with _bejerman_settings():
            stats = process_bejerman_jobs(client=fake)

        self.assertEqual(stats["succeeded"], 1)
        self.assertEqual(fake.movements, [])
        job = self._job_row(ingreso_id)
        self.assertEqual(job["status"], "succeeded")
        self.assertTrue(self._json_value(job["request_payload"])["idempotent"])

    def test_restauracion_stock_destino_historico_emite_ent_por_serie(self):
        ingreso_id = self._enqueue_job(serial="SN-RESTORE-001")
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE bejerman_sync_jobs
                   SET status='succeeded',
                       article_code='ART-RESTORE'
                 WHERE ingreso_id=%s
                """,
                [ingreso_id],
            )
        fake = FakeBejermanClient(source_records=[], target_records=[])

        with _bejerman_settings():
            stats = restore_target_stock_from_jobs(client=fake)

        self.assertEqual(stats["checked"], 1)
        self.assertEqual(stats["restored"], 1)
        self.assertEqual(len(fake.movements), 1)
        ent = fake.movements[0][0]
        self.assertEqual(ent["Comprobante_Tipo"], "ENT")
        self.assertEqual(ent["Comprobante_ArtDeposito"], "STL")
        self.assertEqual(ent["Comprobante_ArtPartida"], "SN-RESTORE-001")
        self.assertEqual(ent["Comprobante_Art_CodGen"], "ART-RESTORE")
        job = self._job_row(ingreso_id)
        self.assertEqual(job["status"], "succeeded")
        request_payload = self._json_value(job["request_payload"])
        self.assertTrue(request_payload["restore_target_stock"])
        self.assertTrue(request_payload["target_stock_entry_done"])

    def test_restauracion_stock_destino_sin_articulo_deja_diagnostico(self):
        ingreso_id = self._enqueue_job(serial="SN-RESTORE-NO-ARTICLE")
        fake = FakeBejermanClient(source_records=[], target_records=[], articles=[])

        with _bejerman_settings():
            stats = restore_target_stock_from_jobs(client=fake)

        self.assertEqual(stats["checked"], 1)
        self.assertEqual(stats["not_found"], 1)
        job = self._job_row(ingreso_id)
        self.assertEqual(job["status"], "blocked")
        self.assertIn("No se pudo restaurar stock en destino", job["last_error"])
        self.assertIn("partida SN-RESTORE-NO-ARTICLE", job["last_error"])
        response_payload = self._json_value(job["response_payload"])
        self.assertEqual(response_payload["candidates"], [])
        diagnostic = response_payload["stock_restore_diagnostic"]
        self.assertEqual(diagnostic["serial"], "SN-RESTORE-NO-ARTICLE")
        self.assertEqual(diagnostic["source_deposit"], "STR")
        self.assertEqual(diagnostic["target_deposit"], "STL")
        self.assertEqual(diagnostic["source_record_count"], 0)
        self.assertEqual(diagnostic["target_record_count"], 0)

    def test_reintento_con_sal_done_emite_solo_ent_destino(self):
        ingreso_id = self._enqueue_job(serial="SN-SAL-DONE")
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE bejerman_sync_jobs
                   SET request_payload=%s::jsonb,
                       response_payload=%s::jsonb,
                       article_code=%s
                 WHERE ingreso_id=%s
                """,
                [
                    json.dumps(
                        {
                            "sal_done": True,
                            "transfer_phase": "sal_done",
                            "article_code": "ART-CPAP",
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps({"sal": {"Resultado": "OK"}}, ensure_ascii=False),
                    "ART-CPAP",
                    ingreso_id,
                ],
            )
        fake = FakeBejermanClient(deposit_records={"STL": []})

        with _bejerman_settings():
            stats = process_bejerman_jobs(client=fake)

        self.assertEqual(stats["succeeded"], 1)
        self.assertEqual(fake.stock_calls, [("STL", "SN-SAL-DONE"), ("STR", "SN-SAL-DONE")])
        self.assertEqual(len(fake.movements), 1)
        ent = fake.movements[0][0]
        self.assertEqual(ent["Comprobante_Tipo"], "ENT")
        self.assertEqual(ent["Comprobante_ArtDeposito"], "STL")
        self.assertEqual(ent["Comprobante_ArtPartida"], "SN-SAL-DONE")
        self.assertEqual(ent["Comprobante_CantidadUM1"], 1)
        self.assertEqual(ent["Comprobante_CantidadUM2"], 1)
        job = self._job_row(ingreso_id)
        self.assertEqual(job["status"], "succeeded")
        request_payload = self._json_value(job["request_payload"])
        self.assertTrue(request_payload["sal_done"])
        self.assertTrue(request_payload["target_stock_entry_done"])
        self.assertEqual(request_payload["transfer_phase"], "target_entry_done")
        response_payload = self._json_value(job["response_payload"])
        self.assertIn("sal", response_payload)
        self.assertIn("target_stock_entry", response_payload)

    def test_error_transitorio_reintenta_sin_duplicar_jobs(self):
        ingreso_id = self._enqueue_job(serial="SN-RETRY-001")
        fake = FakeBejermanClient(stock_error=BejermanTransientError("HTTP timeout"))
        before = timezone.now()

        with _bejerman_settings():
            stats = process_bejerman_jobs(client=fake)

        self.assertEqual(stats["failed"], 1)
        with connection.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM bejerman_sync_jobs WHERE ingreso_id=%s", [ingreso_id])
            self.assertEqual(int(cur.fetchone()[0]), 1)
        job = self._job_row(ingreso_id)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["attempts"], 1)
        self.assertGreater(job["next_attempt_at"], before)
        self.assertIn("HTTP timeout", job["last_error"])

    def test_configuracion_incompleta_deja_job_blocked(self):
        ingreso_id = self._enqueue_job(serial="SN-CONFIG-001")
        fake = FakeBejermanClient()

        with _bejerman_settings(BEJERMAN_WSDL_URL=""):
            stats = process_bejerman_jobs(client=fake)

        self.assertEqual(stats["blocked"], 1)
        self.assertEqual(fake.stock_calls, [])
        job = self._job_row(ingreso_id)
        self.assertEqual(job["status"], "blocked")
        self.assertIn("BEJERMAN_WSDL_URL", job["last_error"])

    def test_partida_no_encontrada_ingresa_destino_con_articulo_mapeado(self):
        ingreso_id = self._enqueue_job(serial="SN-MISSING-001")
        self._insert_mapping(article_code="ART-MAPPED")
        fake = FakeBejermanClient(source_records=[], target_records=[])

        with _bejerman_settings():
            stats = process_bejerman_jobs(client=fake)

        self.assertEqual(stats["succeeded"], 1)
        self.assertEqual(len(fake.movements), 1)
        ent = fake.movements[0][0]
        self.assertEqual(ent["Comprobante_Tipo"], "ENT")
        self.assertEqual(ent["Comprobante_ArtDeposito"], "STL")
        self.assertEqual(ent["Comprobante_ArtPartida"], "SN-MISSING-001")
        self.assertEqual(ent["Comprobante_Art_CodGen"], "ART-MAPPED")
        job = self._job_row(ingreso_id)
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["article_code"], "ART-MAPPED")

    def test_partida_sin_stock_positivo_asienta_articulo_por_serie(self):
        ingreso_id = self._enqueue_job(serial="SN-ZERO-STOCK")
        fake = FakeBejermanClient(
            source_records=[
                {
                    "Comprobante_ArtPartida": "SN-ZERO-STOCK",
                    "Comprobante_ArtDeposito": "STR",
                    "Comprobante_Art_CodGen": "ART-ZERO",
                    "Stock": 0,
                }
            ],
            target_records=[],
        )

        with _bejerman_settings():
            stats = process_bejerman_jobs(client=fake)

        self.assertEqual(stats["succeeded"], 1)
        self.assertEqual(len(fake.movements), 1)
        ent = fake.movements[0][0]
        self.assertEqual(ent["Comprobante_Tipo"], "ENT")
        self.assertEqual(ent["Comprobante_ArtDeposito"], "STL")
        self.assertEqual(ent["Comprobante_ArtPartida"], "SN-ZERO-STOCK")
        self.assertEqual(ent["Comprobante_Art_CodGen"], "ART-ZERO")
        job = self._job_row(ingreso_id)
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["article_code"], "ART-ZERO")
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT article_code, match_source, source_payload
                  FROM bejerman_article_mappings
                 WHERE model_id=%s AND variante_norm=%s
                """,
                [self.model_id, normalize_article_variant("")],
            )
            row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "ART-ZERO")
        self.assertEqual(row[1], "auto")
        self.assertEqual(self._json_value(row[2])["source"], "stock_transfer_partida")

    def test_partida_en_destino_positivo_cierra_idempotente_sin_emitir_comprobantes(self):
        ingreso_id = self._enqueue_job(serial="SN-DUP-DEP")
        fake = FakeBejermanClient(
            source_records=[
                {
                    "Comprobante_ArtPartida": "SN-DUP-DEP",
                    "Comprobante_ArtDeposito": "STR",
                    "Comprobante_Art_CodGen": "ART-CPAP",
                    "Stock": 1,
                }
            ],
            target_records=[
                {
                    "Comprobante_ArtPartida": "SN-DUP-DEP",
                    "Comprobante_ArtDeposito": "STL",
                    "Comprobante_Art_CodGen": "ART-CPAP",
                    "Stock": 1,
                }
            ],
        )

        with _bejerman_settings():
            stats = process_bejerman_jobs(client=fake)

        self.assertEqual(stats["succeeded"], 1)
        self.assertEqual(fake.movements, [])
        job = self._job_row(ingreso_id)
        self.assertEqual(job["status"], "succeeded")
        self.assertTrue(self._json_value(job["request_payload"])["idempotent"])

    def test_endpoints_listado_retry_y_mapeo(self):
        ingreso_id = self._enqueue_job(serial="SN-ENDPOINTS")
        job = self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL)
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE bejerman_sync_jobs SET status='blocked', last_error='Artículo pendiente' WHERE id=%s",
                [job["id"]],
            )

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_token(self.jefe)}")
        response = self.client.get("/api/bejerman/jobs/?status=blocked&q=SN-ENDPOINTS")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["counters"]["blocked"], 1)
        self.assertEqual(response.data["items"][0]["id"], job["id"])

        response = self.client.post(f"/api/bejerman/jobs/{job['id']}/retry/", {}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "pending")

        with connection.cursor() as cur:
            cur.execute(
                "UPDATE bejerman_sync_jobs SET status='blocked', last_error='Artículo pendiente' WHERE id=%s",
                [job["id"]],
            )
        related_ingreso_id = self._enqueue_job(serial="SN-ENDPOINTS-REL")
        related_job = self._job_row(related_ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL)
        other_variant_ingreso_id = self._enqueue_job(serial="SN-ENDPOINTS-VARIANT", equipo_variante="BiPAP")
        other_variant_job = self._job_row(other_variant_ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL)
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE bejerman_sync_jobs SET status='blocked', last_error='Artículo pendiente' WHERE id = ANY(%s)",
                [[related_job["id"], other_variant_job["id"]]],
            )

        fake = FakeBejermanClient(
            articles=[
                {
                    "Art_CodGenerico": "ART-ENDPOINT",
                    "Art_DescripcionGeneral": "ResMed AirSense 10 CPAP",
                    "Art_StockPorPartida": "S",
                    "Art_ParticipaCircuitoStock": "S",
                    "Art_Tipo": "1",
                }
            ]
        )

        with _bejerman_settings(), patch("service.views.bejerman_views.BejermanSDKClient", return_value=fake):
            response = self.client.post(
                "/api/bejerman/article-mappings/",
                {
                    "job_id": job["id"],
                    "article_code": "ART-ENDPOINT",
                    "article_description": "Artículo confirmado",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["mapping"]["article_code"], "ART-ENDPOINT")
        self.assertEqual(response.data["reopened_jobs"], 2)
        updated = self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL)
        self.assertEqual(updated["status"], "pending")
        self.assertEqual(updated["article_code"], "ART-ENDPOINT")
        related_updated = self._job_row(related_ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL)
        self.assertEqual(related_updated["status"], "pending")
        self.assertEqual(related_updated["article_code"], "ART-ENDPOINT")
        other_variant_updated = self._job_row(other_variant_ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL)
        self.assertEqual(other_variant_updated["status"], "blocked")
        self.assertFalse(other_variant_updated["article_code"])

    def test_mapeo_edita_articulo_de_job_fallido_con_codigo_existente(self):
        ingreso_id = self._enqueue_job(serial="SN-ARTICLE-EDIT")
        job = self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL)
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE bejerman_sync_jobs
                   SET status='failed',
                       attempts=3,
                       article_code='ART-VIEJO',
                       last_error='Artículo incorrecto'
                 WHERE id=%s
                """,
                [job["id"]],
            )

        fake = FakeBejermanClient(
            articles=[
                {
                    "Art_CodGenerico": "ART-NUEVO",
                    "Art_DescripcionGeneral": "ResMed AirSense 10 CPAP",
                    "Art_StockPorPartida": "S",
                    "Art_ParticipaCircuitoStock": "S",
                    "Art_Tipo": "1",
                }
            ]
        )

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_token(self.jefe)}")
        with _bejerman_settings(), patch("service.views.bejerman_views.BejermanSDKClient", return_value=fake):
            response = self.client.post(
                "/api/bejerman/article-mappings/",
                {
                    "job_id": job["id"],
                    "article_code": "ART-NUEVO",
                    "article_description": "Artículo corregido",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["updated_job"])
        self.assertEqual(response.data["reopened_jobs"], 0)
        updated = self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL)
        self.assertEqual(updated["status"], "pending")
        self.assertEqual(updated["attempts"], 0)
        self.assertEqual(updated["article_code"], "ART-NUEVO")
        self.assertIsNone(updated["last_error"])

    def test_listado_bejerman_incluye_resolucion_articulo(self):
        ingreso_id = self._enqueue_job(serial="SN-RESOLUTION")
        job = self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL)
        self._insert_mapping(article_code="ART-MAP")
        payload = {
            "candidates": [
                {
                    "Art_CodGenerico": "ART-RESMED",
                    "Art_DescripcionGeneral": "ResMed AirSense 10 CPAP",
                    "Art_StockPorPartida": "S",
                    "Art_ParticipaCircuitoStock": "S",
                    "Art_CodDeposito": "STR",
                }
            ]
        }
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE bejerman_sync_jobs
                   SET status='blocked',
                       last_error='Artículo pendiente',
                       response_payload=%s::jsonb
                 WHERE id=%s
                """,
                [json.dumps(payload, ensure_ascii=False), job["id"]],
            )

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_token(self.jefe)}")
        response = self.client.get("/api/bejerman/jobs/?status=blocked&q=SN-RESOLUTION")

        self.assertEqual(response.status_code, 200, response.data)
        resolution = response.data["items"][0]["article_resolution"]
        self.assertEqual(resolution["scope"], "modelo_variante")
        self.assertEqual(resolution["related_blocked_jobs"], 1)
        self.assertEqual(resolution["candidates"][0]["article_code"], "ART-RESMED")
        self.assertIn("Coincide con el modelo", resolution["candidates"][0]["reasons"])
        self.assertTrue(resolution["candidates"][0]["flags"]["stock_by_partida"])
        self.assertEqual(response.data["items"][0]["article_mapping"]["article_code"], "ART-MAP")
        self.assertEqual(response.data["items"][0]["article_resolution"]["mapping"]["article_code"], "ART-MAP")

    def test_editar_identificador_actualiza_snapshot_bejerman_operativo(self):
        ingreso_id = self._enqueue_job(serial="SN-OLD-SNAPSHOT")
        job = self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL)

        response = self.client.patch(
            f"/api/devices/{job['device_id']}/identificadores/",
            {"numero_serie": "SN-NEW-SNAPSHOT"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        updated = self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL)
        self.assertEqual(updated["numero_serie"], "SN-NEW-SNAPSHOT")

    def test_busqueda_articulos_bejerman_rankea_y_exige_permiso(self):
        ingreso_id = self._enqueue_job(serial="SN-ARTICLE-SEARCH")
        job = self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL)
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE bejerman_sync_jobs SET status='blocked', last_error='Artículo pendiente' WHERE id=%s",
                [job["id"]],
            )

        tecnico = User.objects.create(
            nombre="Técnico Bejerman",
            email="tecnico@bejerman.test",
            hash_pw="",
            rol="tecnico",
            activo=True,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_token(tecnico)}")
        response = self.client.get(f"/api/bejerman/articles/?job_id={job['id']}&q=AirSense")
        self.assertEqual(response.status_code, 403)

        fake = FakeBejermanClient(
            articles=[
                {
                    "Art_CodGenerico": "REP-FILTRO",
                    "Art_DescripcionGeneral": "Filtro ResMed AirSense 10",
                    "Art_StockPorPartida": "N",
                    "Art_ParticipaCircuitoStock": "S",
                },
                {
                    "Art_CodGenerico": "ART-CPAP",
                    "Art_DescripcionGeneral": "ResMed AirSense 10 CPAP",
                    "Art_StockPorPartida": "S",
                    "Art_ParticipaCircuitoStock": "S",
                    "Art_Tipo": "1",
                    "Art_CodDeposito": "STR",
                },
                {
                    "Art_CodGenerico": "ART-LUMIS",
                    "Art_DescripcionGeneral": "ResMed Lumis 150",
                    "Art_StockPorPartida": "S",
                    "Art_ParticipaCircuitoStock": "S",
                },
            ]
        )

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_token(self.jefe)}")
        with _bejerman_settings(), patch("service.views.bejerman_views.BejermanSDKClient", return_value=fake):
            response = self.client.get(f"/api/bejerman/articles/?job_id={job['id']}&q=AirSense")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["related_blocked_jobs"], 1)
        self.assertEqual(response.data["items"][0]["article_code"], "ART-CPAP")
        self.assertIn("Coincide con la búsqueda", response.data["items"][0]["reasons"])
        self.assertIn("Parece repuesto o accesorio", response.data["items"][1]["warnings"])

    def test_mapeo_rechaza_codigo_bejerman_inexistente(self):
        ingreso_id = self._enqueue_job(serial="SN-ARTICLE-BAD")
        job = self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL)
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE bejerman_sync_jobs SET status='blocked', last_error='Artículo pendiente' WHERE id=%s",
                [job["id"]],
            )

        fake = FakeBejermanClient(articles=[])
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_token(self.jefe)}")
        with _bejerman_settings(), patch("service.views.bejerman_views.BejermanSDKClient", return_value=fake):
            response = self.client.post(
                "/api/bejerman/article-mappings/",
                {"job_id": job["id"], "article_code": "ART-INEXISTENTE"},
                format="json",
            )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("No se encontró el artículo Bejerman ART-INEXISTENTE", response.data["detail"])

    def test_listado_bejerman_oculta_legacy_y_retry_lo_rechaza(self):
        ingreso_id = self._insert_ingreso(serial="SN-RSS-HIDE")
        queued = enqueue_stock_exit_for_ingreso(ingreso_id, source_deposit="STC")
        entry_ingreso_id = self._insert_ingreso(serial="SN-ENTRY-HIDE")
        entry_queued = enqueue_stock_entry_for_ingreso(entry_ingreso_id)

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_token(self.jefe)}")
        response = self.client.get("/api/bejerman/jobs/?q=SN-RSS-HIDE")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["items"], [])
        self.assertEqual(response.data["counters"], {})

        response = self.client.get("/api/bejerman/jobs/?q=SN-ENTRY-HIDE")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["items"], [])
        self.assertEqual(response.data["counters"], {})

        response = self.client.get("/api/bejerman/jobs/?q=SN-RSS-HIDE&include_legacy=1")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["items"][0]["id"], queued["id"])

        response = self.client.get("/api/bejerman/jobs/?q=SN-ENTRY-HIDE&include_legacy=1")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["items"][0]["id"], entry_queued["id"])

        response = self.client.post(f"/api/bejerman/jobs/{queued['id']}/retry/", {}, format="json")

        self.assertEqual(response.status_code, 409, response.data)
        self.assertIn("Portal", response.data["detail"])

        response = self.client.post(f"/api/bejerman/jobs/{entry_queued['id']}/retry/", {}, format="json")

        self.assertEqual(response.status_code, 409, response.data)
        self.assertIn("NEXORA no emite ENT/RIS de ingreso", response.data["detail"])

    def test_listado_bejerman_exige_permiso_de_pagina_especifico(self):
        response = self.client.get("/api/bejerman/jobs/")
        self.assertEqual(response.status_code, 403)

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_token(self.jefe)}")
        response = self.client.get("/api/bejerman/jobs/")
        self.assertEqual(response.status_code, 200, response.data)
