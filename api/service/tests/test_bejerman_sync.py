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
    enqueue_stock_transfer_for_ingreso,
    process_bejerman_jobs,
)
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
        "BEJERMAN_NUMERA_FLEX": "S",
        "BEJERMAN_STOCK_TRANSFER_COMPROBANTE": "TRA",
        "BEJERMAN_STOCK_TRANSFER_TIPO_OPERACION": "",
        "BEJERMAN_MAX_ATTEMPTS": 8,
        "BEJERMAN_REQUEST_TIMEOUT": 5,
    }
    values.update(extra)
    return override_settings(**values)


class FakeBejermanClient:
    def __init__(self, *, source_records=None, target_records=None, stock_error=None, movement_error=None):
        self.source_records = source_records if source_records is not None else []
        self.target_records = target_records if target_records is not None else []
        self.stock_error = stock_error
        self.movement_error = movement_error
        self.stock_calls = []
        self.movements = []

    def stock_by_deposit_partida(self, deposit, serial):
        self.stock_calls.append((deposit, serial))
        if self.stock_error:
            raise self.stock_error
        records = self.target_records if deposit == "STL" else self.source_records
        return {"Resultado": "OK", "DatosJSON": json.dumps(records)}

    def ingresar_lista_comprobantes_json(self, comprobantes):
        self.movements.append(comprobantes)
        if self.movement_error:
            raise self.movement_error
        return {"Resultado": "OK", "DatosJSON": "{}"}


@skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL")
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
                    nombre TEXT NOT NULL
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
                    variante TEXT
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
                    numero_serie TEXT,
                    numero_interno TEXT,
                    alquilado BOOLEAN NOT NULL DEFAULT FALSE
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
                    asignado_a INTEGER NULL REFERENCES users(id)
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
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS numero_interno TEXT",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS alquilado BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS resolucion TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS presupuesto_estado TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS asignado_a INTEGER NULL",
            ):
                cur.execute(statement)

        call_command("apply_bejerman_sync_schema", verbosity=0)
        super().setUpClass()

    def setUp(self):
        super().setUp()
        with connection.cursor() as cur:
            cur.execute("DELETE FROM bejerman_sync_jobs")
            cur.execute("DELETE FROM ingreso_events")
            cur.execute("DELETE FROM ingresos")
            cur.execute("DELETE FROM devices")
            cur.execute("DELETE FROM models")
            cur.execute("DELETE FROM marcas")
            cur.execute("DELETE FROM customers")
        User.objects.filter(email__endswith="@bejerman.test").delete()
        self.admin = User.objects.create(
            nombre="Admin Bejerman",
            email="admin@bejerman.test",
            hash_pw="",
            rol="admin",
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
            cur.execute("INSERT INTO marcas(nombre) VALUES (%s) RETURNING id", ["ResMed"])
            self.marca_id = int(cur.fetchone()[0])
            cur.execute(
                "INSERT INTO models(marca_id, nombre, tipo_equipo, variante) VALUES (%s,%s,%s,%s) RETURNING id",
                [self.marca_id, "AirSense 10", "CPAP", ""],
            )
            self.model_id = int(cur.fetchone()[0])

    def _insert_ingreso(self, *, serial="SN-BEJ-001", estado="reparado", resolucion="reparado"):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO devices(customer_id, marca_id, model_id, numero_serie, numero_interno, alquilado)
                VALUES (%s,%s,%s,%s,%s,FALSE)
                RETURNING id
                """,
                [self.customer_id, self.marca_id, self.model_id, serial, "MG-BEJ-001"],
            )
            device_id = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO ingresos(
                    device_id, estado, motivo, fecha_ingreso, fecha_creacion,
                    resolucion, presupuesto_estado, asignado_a
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
                """,
                [
                    device_id,
                    estado,
                    "reparacion",
                    timezone.now(),
                    timezone.now(),
                    resolucion,
                    "no_aplica",
                    self.admin.id,
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

    def _job_row(self, ingreso_id):
        with connection.cursor() as cur:
            cur.execute("SELECT * FROM bejerman_sync_jobs WHERE ingreso_id=%s", [ingreso_id])
            cols = [col[0] for col in cur.description]
            row = cur.fetchone()
        return dict(zip(cols, row)) if row else None

    def _json_value(self, value):
        return json.loads(value) if isinstance(value, str) else value

    def _enqueue_job(self, *, serial="SN-BEJ-001"):
        ingreso_id = self._insert_ingreso(serial=serial)
        event_id = self._insert_liberado_event(ingreso_id)
        enqueue_stock_transfer_for_ingreso(ingreso_id, event_id)
        return ingreso_id

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

        self.assertIn("sync_type", columns)
        self.assertIn("ingreso_event_id", columns)
        self.assertIn("request_payload", columns)
        self.assertIn("uq_bejerman_sync_jobs_type_ingreso", indexes)
        self.assertIn("ix_bejerman_sync_jobs_due", indexes)
        self.assertIn("chk_bejerman_sync_jobs_status", constraints)

    def test_remito_liberado_encola_un_solo_job_si_se_reimprime(self):
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
        self.assertEqual(self._job_row(ingreso_id)["numero_serie"], "SN-REMITO-001")

    def test_payload_mueve_partida_desde_str_hacia_stl(self):
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
        self.assertEqual(len(fake.movements), 1)
        comprobantes = fake.movements[0]
        self.assertEqual(len(comprobantes), 2)
        source, target = comprobantes
        self.assertEqual(source["Comprobante_Tipo"], "TRA")
        self.assertEqual(target["Comprobante_Tipo"], "TRA")
        self.assertEqual(source["Comprobante_Numero"], target["Comprobante_Numero"])
        self.assertEqual(source["Comprobante_ArtDeposito"], "STR")
        self.assertEqual(source["Comprobante_CantidadUM1"], -1)
        self.assertEqual(source["Comprobante_CantidadUM2"], -1)
        self.assertEqual(target["Comprobante_ArtDeposito"], "STL")
        self.assertEqual(target["Comprobante_CantidadUM1"], 1)
        self.assertEqual(target["Comprobante_CantidadUM2"], 1)
        self.assertNotIn("Comprobante_DepositoDestino", source)
        self.assertNotIn("Comprobante_DepositoDestino", target)
        self.assertNotIn("Comprobante_TipoOperacion", source)
        self.assertEqual(source["Comprobante_ArtPartida"], "SN-MOVE-001")
        self.assertEqual(target["Comprobante_ArtPartida"], "SN-MOVE-001")
        self.assertEqual(source["Comprobante_Art_CodGen"], "ART-CPAP")
        self.assertEqual(target["Comprobante_Art_CodGen"], "ART-CPAP")
        self.assertEqual(source["Comprobante_IdOrigen"], f"NEXORA-OS-{ingreso_id}-STR")
        self.assertEqual(target["Comprobante_IdOrigen"], f"NEXORA-OS-{ingreso_id}-STL")
        self.assertEqual(self._job_row(ingreso_id)["status"], "succeeded")

    def test_cliente_sdk_envia_lista_stock_por_parametros_json(self):
        comprobantes = [{"Comprobante_Tipo": "TRA", "Comprobante_ArtPartida": "SN-MOVE-001"}]
        client = BejermanSDKClient()

        with _bejerman_settings(), patch.object(client, "execute", return_value={"Resultado": "OK"}) as execute:
            response = client.ingresar_lista_comprobantes_json(comprobantes)

        self.assertEqual(response["Resultado"], "OK")
        execute.assert_called_once_with(
            "STOCK",
            "IngresarListaComprobantesJSON",
            params_json=[json.dumps(comprobantes, ensure_ascii=False, default=str), "S"],
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

        with _bejerman_settings(BEJERMAN_STOCK_TRANSFER_COMPROBANTE=""):
            stats = process_bejerman_jobs(client=fake)

        self.assertEqual(stats["blocked"], 1)
        self.assertEqual(fake.stock_calls, [])
        job = self._job_row(ingreso_id)
        self.assertEqual(job["status"], "blocked")
        self.assertIn("BEJERMAN_STOCK_TRANSFER_COMPROBANTE", job["last_error"])

    def test_partida_no_encontrada_deja_job_blocked(self):
        ingreso_id = self._enqueue_job(serial="SN-MISSING-001")
        fake = FakeBejermanClient(source_records=[], target_records=[])

        with _bejerman_settings():
            stats = process_bejerman_jobs(client=fake)

        self.assertEqual(stats["blocked"], 1)
        job = self._job_row(ingreso_id)
        self.assertEqual(job["status"], "blocked")
        self.assertIn("Partida SN-MISSING-001 no encontrada", job["last_error"])
