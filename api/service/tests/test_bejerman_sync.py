import json
from contextlib import ExitStack, contextmanager
from unittest import skipUnless
from unittest.mock import patch

from django.core import mail
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from service.auth import issue_token
from service.bejerman_sync import (
    BejermanBlockedError,
    BejermanSDKClient,
    BejermanTransientError,
    SYNC_TYPE_STOCK_ENTRY_STR,
    SYNC_TYPE_STOCK_EXIT_RTS,
    SYNC_TYPE_STOCK_STR_TO_STC,
    SYNC_TYPE_STOCK_STR_TO_STL,
    SYNC_TYPE_STOCK_STR_TO_VAL,
    SYNC_TYPE_STOCK_FROM_DESGUACE,
    SYNC_TYPE_STOCK_TO_DESGUACE,
    enqueue_stock_alta_for_ingreso,
    enqueue_stock_baja_for_ingreso,
    enqueue_stock_exit_for_ingreso,
    enqueue_stock_entry_for_ingreso,
    enqueue_stock_transfer_for_ingreso,
    normalize_article_variant,
    process_bejerman_jobs,
    restore_target_stock_from_jobs,
)
from service.bejerman_sdk import BejermanPdfPendingError, BejermanSdkResponseError, BejermanSdkUnavailable, build_document_id
from service.bejerman_stock_corrections import (
    ApprovedStockCorrection,
    apply_approved_stock_corrections,
    apply_duplicate_stock_corrections,
    audit_duplicate_stock,
)
from service.bejerman_ris import BejermanRisError, register_ris_batch
from service.models import User
from service.views.delivery_orders_views import CobranzasRemitoPdfView


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
        "BEJERMAN_DEMO_TARGET_DEPOSIT": "VAL",
        "BEJERMAN_NUMERA_FLEX": "S",
        "BEJERMAN_STOCK_NUMERA_FLEX": "N",
        "BEJERMAN_STOCK_ENTRY_COMPROBANTE": "ENT",
        "BEJERMAN_STOCK_EXIT_COMPROBANTE": "SAL",
        "BEJERMAN_STOCK_TRANSFER_COMPROBANTE": "TRA",
        "BEJERMAN_STOCK_TRANSFER_OUT_COMPROBANTE": "SAL",
        "BEJERMAN_STOCK_TRANSFER_IN_COMPROBANTE": "ENT",
        "BEJERMAN_STOCK_TRANSFER_TIPO_OPERACION": "",
        "BEJERMAN_STOCK_BAJA_TARGET_DEPOSIT": "DES",
        "BEJERMAN_STOCK_BAJA_CAUSA_EMISION": "DES",
        "BEJERMAN_STOCK_BAJA_OUT_COMPROBANTE": "SAL",
        "BEJERMAN_STOCK_BAJA_IN_COMPROBANTE": "ENT",
        "BEJERMAN_STOCK_ALTA_SOURCE_DEPOSIT": "DES",
        "BEJERMAN_STOCK_ALTA_TARGET_DEPOSIT": "STR",
        "BEJERMAN_STOCK_ALTA_CAUSA_EMISION": "ALQ",
        "BEJERMAN_STOCK_ALTA_OUT_COMPROBANTE": "SAL",
        "BEJERMAN_STOCK_ALTA_IN_COMPROBANTE": "ENT",
        "BEJERMAN_STOCK_CORRECTION_COMPROBANTE": "SAL",
        "BEJERMAN_STOCK_CORRECTION_CAUSA_EMISION": "ERR",
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
    def __init__(
        self,
        number="00001234",
        clients=None,
        sales_records=None,
        sales_details=None,
        sales_error=None,
        ingresar_side_effects=None,
        stock_records=None,
    ):
        self.number = number
        self.clients = clients
        self.sales_records = sales_records or []
        self.sales_details = sales_details or {}
        self.sales_error = sales_error
        self.ingresar_side_effects = list(ingresar_side_effects or [])
        self.stock_records = {key: list(value) for key, value in (stock_records or {}).items()}
        self.stock_comprobantes = []
        self.ingresar_calls = 0
        self.comprobantes = []
        self.ingresar_kwargs = []
        self.sales_list_calls = 0
        self.sales_filters = []

    def ingresar_comprobante_ventas_json(self, comprobante, **kwargs):
        self.ingresar_calls += 1
        self.comprobantes.append(comprobante)
        self.ingresar_kwargs.append(kwargs)
        if self.ingresar_side_effects:
            effect = self.ingresar_side_effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
            if isinstance(effect, dict):
                return effect
        registered = kwargs.get("emite_reg") == "R"
        point = comprobante.get("Comprobante_PtoVenta") if registered else comprobante.get("Comprobante_PtoVenta", "00004")
        number = comprobante.get("Comprobante_Numero") if registered else self.number
        return {
            "Resultado": "OK",
            "DatosJSON": json.dumps(
                {
                    "Comprobante_Tipo": comprobante.get("Comprobante_Tipo", "RIS"),
                    "Comprobante_Letra": comprobante.get("Comprobante_Letra", "R"),
                    "Comprobante_PtoVenta": point,
                    "Comprobante_Numero": number,
                }
            ),
        }

    def list_clientes(self):
        return {
            "Resultado": "OK",
            "DatosJSON": json.dumps(
                self.clients
                if self.clients is not None
                else [
                    {
                        "Cliente_Codigo": "CLI-BEJ",
                        "Cliente_RazonSocial": "Cliente Bejerman",
                        "Cliente_NroDocumento": "30700000000",
                        "Cliente_Provincia": "02",
                        "Cliente_SitIVA": "RI",
                    }
                ]
            ),
        }

    def list_comprobantes_ventas(self, filters=None):
        self.sales_list_calls += 1
        self.sales_filters.append(filters)
        if self.sales_error:
            raise self.sales_error
        return {
            "Resultado": "OK",
            "DatosJSON": json.dumps(self.sales_records),
        }

    def detalle_comprobante_ventas(self, comprobante_id):
        detail = self.sales_details.get(str(comprobante_id), {})
        return {
            "Resultado": "OK",
            "DatosJSON": json.dumps(detail),
        }

    def obtener_stock_partida(self, partida):
        return {
            "Resultado": "OK",
            "DatosJSON": json.dumps(self.stock_records.get(str(partida or "").strip(), [])),
        }

    def ingresar_lista_comprobantes_stock_json(self, comprobantes, *, numera_flex="N"):
        self.stock_comprobantes.append({"comprobantes": comprobantes, "numeraFlex": numera_flex})
        for item in comprobantes or []:
            partida = str(item.get("Comprobante_ArtPartida") or "").strip()
            deposit = str(item.get("Comprobante_ArtDeposito") or "").strip()
            article = str(item.get("Comprobante_Art_CodGen") or "").strip()
            if not partida or not deposit or not article:
                continue
            rows = self.stock_records.setdefault(partida, [])
            if not any(
                str(row.get("Art_CodDeposito") or "").strip() == deposit
                and str(row.get("Art_CodGen") or "").strip() == article
                and str(row.get("Art_Partida") or "").strip() == partida
                for row in rows
            ):
                rows.append(
                    {
                        "Art_CodDeposito": deposit,
                        "Art_Partida": partida,
                        "Art_CodGen": article,
                        "Art_RealUM1": 0,
                        "Art_CompUM1": 0,
                        "Art_DispUM1": 0,
                        "Art_RealUM2": 0,
                        "Art_CompUM2": 0,
                        "Art_DispUM2": 0,
                    }
                )
        return {"Resultado": "OK", "DatosJSON": json.dumps(comprobantes or [])}


def _ris_customer_fields():
    return {
        "Cliente_RazonSocial": "Cliente Bejerman",
        "Cliente_NroDocumento": "30700000000",
        "Cliente_Provincia": "02",
        "Cliente_SitIVA": "RI",
    }


def _ris_article_choice(article_code, **kwargs):
    return {
        "article_code": article_code,
        "article_description": "Artículo CPAP",
        "raw": {},
    }


@contextmanager
def _valid_ris_preflight(fake=None):
    fake = fake or FakeRisClient()
    with ExitStack() as stack:
        stack.enter_context(override_settings(BEJERMAN_RIS_ALLOW_GENERIC_ARTICLE=True))
        stack.enter_context(patch("service.bejerman_ris.BejermanSDKClient", return_value=fake))
        stack.enter_context(patch("service.bejerman_ris.validate_bejerman_article_choice", side_effect=_ris_article_choice))
        yield fake


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
                    activo BOOLEAN DEFAULT TRUE,
                    bejerman_seller_code TEXT,
                    bejerman_seller_code_confirmed_at TIMESTAMPTZ NULL
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
                CREATE TABLE IF NOT EXISTS catalogo_accesorios (
                    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    nombre TEXT NOT NULL UNIQUE
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ingreso_accesorios (
                    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    ingreso_id INTEGER NOT NULL REFERENCES ingresos(id) ON DELETE CASCADE,
                    accesorio_id INTEGER NULL REFERENCES catalogo_accesorios(id),
                    referencia TEXT,
                    descripcion TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ingreso_alquiler_accesorios (
                    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    ingreso_id INTEGER NOT NULL REFERENCES ingresos(id) ON DELETE CASCADE,
                    accesorio_id INTEGER NULL REFERENCES catalogo_accesorios(id),
                    referencia TEXT,
                    descripcion TEXT
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
                CREATE TABLE IF NOT EXISTS ingreso_baja_requests (
                    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    ingreso_id INTEGER NOT NULL REFERENCES ingresos(id) ON DELETE CASCADE,
                    usuario_id INTEGER NULL REFERENCES users(id),
                    motivo TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    accepted_at TIMESTAMPTZ NULL,
                    canceled_at TIMESTAMPTZ NULL
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
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS bejerman_seller_code TEXT",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS bejerman_seller_code_confirmed_at TIMESTAMPTZ NULL",
                "ALTER TABLE models ADD COLUMN IF NOT EXISTS tipo_equipo TEXT",
                "ALTER TABLE models ADD COLUMN IF NOT EXISTS variante TEXT",
                "ALTER TABLE models ADD COLUMN IF NOT EXISTS tecnico_id INTEGER NULL",
                "ALTER TABLE marcas ADD COLUMN IF NOT EXISTS tecnico_id INTEGER NULL",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS numero_interno TEXT",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS n_de_control TEXT",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS tipo_equipo TEXT",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS variante TEXT",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS ubicacion_id INTEGER NULL REFERENCES locations(id)",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS garantia_vence DATE NULL",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS alquilado BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS alquiler_a TEXT",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS mg_estado TEXT DEFAULT 'activo'",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS propietario TEXT",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS propietario_nombre TEXT",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS propietario_contacto TEXT",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS propietario_doc TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS resolucion TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS motivo TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS fecha_ingreso TIMESTAMPTZ NULL",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS fecha_creacion TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS fecha_servicio TIMESTAMPTZ NULL",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS ubicacion_id INTEGER NULL",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS presupuesto_estado TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS asignado_a INTEGER NULL",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS recibido_por INTEGER NULL",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS informe_preliminar TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS descripcion_problema TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS trabajos_realizados TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS accesorios TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS comentarios TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS equipo_variante TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS remito_ingreso TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS remito_salida TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS factura_numero TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS fecha_entrega TIMESTAMPTZ NULL",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS alquilado BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS alquiler_a TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS alquiler_remito TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS alquiler_fecha DATE",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS faja_garantia TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS propietario_nombre TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS propietario_contacto TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS propietario_doc TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS garantia_reparacion BOOLEAN DEFAULT FALSE",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS garantia_fabrica BOOLEAN DEFAULT FALSE",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS etiq_garantia_ok BOOLEAN DEFAULT TRUE",
                "ALTER TABLE catalogo_accesorios ADD COLUMN IF NOT EXISTS activo BOOLEAN NOT NULL DEFAULT TRUE",
            ):
                cur.execute(statement)

        call_command("apply_bejerman_sync_schema", verbosity=0)
        call_command("apply_bejerman_ris_schema", verbosity=0)
        call_command("apply_bejerman_user_credentials_schema", verbosity=0)
        call_command("apply_user_permissions_schema", verbosity=0)
        super().setUpClass()

    def setUp(self):
        super().setUp()
        with connection.cursor() as cur:
            cur.execute("DELETE FROM bejerman_ingreso_remitos")
            cur.execute("DELETE FROM bejerman_article_mappings")
            cur.execute("DELETE FROM bejerman_sync_jobs")
            cur.execute("DELETE FROM ingreso_accesorios")
            cur.execute("DELETE FROM ingreso_events")
            cur.execute("DELETE FROM ingresos")
            cur.execute("DELETE FROM devices")
            cur.execute("DELETE FROM models")
            cur.execute("DELETE FROM marcas")
            cur.execute("DELETE FROM catalogo_accesorios")
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
        self.recepcion = User.objects.create(
            nombre="Recepción Bejerman",
            email="recepcion@bejerman.test",
            hash_pw="",
            rol="recepcion",
            activo=True,
        )
        self.cobranzas = User.objects.create(
            nombre="Cobranzas Bejerman",
            email="cobranzas@bejerman.test",
            hash_pw="",
            rol="cobranzas",
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
        n_de_control="",
        estado="reparado",
        resolucion="reparado",
        ubicacion_id=None,
        equipo_variante="",
        customer_id=None,
        device_id=None,
        mg_estado="activo",
        motivo="reparacion",
        empresa_bejerman="SEPID",
    ):
        with connection.cursor() as cur:
            if device_id is None:
                cur.execute(
                    """
                    INSERT INTO devices(
                        customer_id, marca_id, model_id, numero_serie, numero_interno, n_de_control, variante, alquilado, mg_estado
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,%s,FALSE,%s)
                    RETURNING id
                    """,
                    [
                        customer_id or self.customer_id,
                        self.marca_id,
                        self.model_id,
                        serial,
                        numero_interno,
                        n_de_control,
                        equipo_variante,
                        mg_estado,
                    ],
                )
                device_id = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO ingresos(
                    device_id, estado, motivo, fecha_ingreso, fecha_creacion,
                    resolucion, ubicacion_id, presupuesto_estado, asignado_a, equipo_variante,
                    empresa_bejerman, empresa_facturar
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
                """,
                [
                    device_id,
                    estado,
                    motivo,
                    timezone.now(),
                    timezone.now(),
                    resolucion,
                    ubicacion_id or self.taller_id,
                    "no_aplica",
                    self.admin.id,
                    equipo_variante,
                    empresa_bejerman,
                    empresa_bejerman,
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

    def test_ingreso_detalle_separa_remito_ingreso_y_salida_rss(self):
        ingreso_id = self._insert_ingreso(serial="SN-RSS-SALIDA", estado="liberado", resolucion="reparado")

        with connection.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS bejerman_remito_groups (
                  id                         TEXT PRIMARY KEY,
                  company_key                TEXT NULL,
                  comprobante_tipo           TEXT NOT NULL,
                  comprobante_letra          TEXT NOT NULL DEFAULT 'R',
                  comprobante_pto_venta      TEXT NULL,
                  comprobante_numero         TEXT NULL,
                  remito_number              TEXT NULL,
                  customer_code              TEXT NOT NULL,
                  customer_name              TEXT NOT NULL,
                  seller_code                TEXT NOT NULL,
                  payment_term_code          TEXT NOT NULL,
                  operation_code             TEXT NOT NULL,
                  deposit_code               TEXT NOT NULL,
                  status                     TEXT NOT NULL DEFAULT 'pending',
                  order_ids                  JSONB NOT NULL DEFAULT '[]'::jsonb,
                  response_summary           JSONB NOT NULL DEFAULT '{}'::jsonb,
                  created_at                 TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  generated_at               TIMESTAMPTZ NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS delivery_orders (
                  id                         TEXT PRIMARY KEY,
                  order_number               TEXT NOT NULL UNIQUE,
                  ingreso_id                 INTEGER NULL,
                  delivery_type              TEXT NOT NULL DEFAULT 'sale',
                  customer_name              TEXT NOT NULL DEFAULT '',
                  bejerman_remito_group_id   TEXT NULL,
                  created_at                 TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cur.execute("DELETE FROM delivery_orders")
            cur.execute("DELETE FROM bejerman_remito_groups")
            cur.execute(
                "UPDATE ingresos SET remito_ingreso = %s, remito_salida = NULL WHERE id = %s",
                ["RIS R 00004-00004667", ingreso_id],
            )
            cur.execute(
                """
                INSERT INTO bejerman_remito_groups(
                  id, company_key, comprobante_tipo, comprobante_letra, comprobante_pto_venta,
                  comprobante_numero, remito_number, customer_code, customer_name, seller_code,
                  payment_term_code, operation_code, deposit_code, status, order_ids,
                  response_summary, generated_at
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,CURRENT_TIMESTAMP)
                """,
                [
                    "grp-rss-salida",
                    "SEPID",
                    "RSS",
                    "R",
                    "00004",
                    "00004715",
                    "RSS R 00004-00004715",
                    "RUSSO",
                    "RUSSO PABLO MATIAS",
                    "1",
                    "1",
                    "REP",
                    "STC",
                    "generated",
                    '["OE-TEST"]',
                    '{"profile":{"type":"RSS"}}',
                ],
            )
            cur.execute(
                """
                INSERT INTO delivery_orders(
                  id, order_number, ingreso_id, delivery_type, customer_name, bejerman_remito_group_id
                )
                VALUES (%s,%s,%s,%s,%s,%s)
                """,
                ["order-rss-salida", "OS-29444", ingreso_id, "service_release", "RUSSO PABLO MATIAS", "grp-rss-salida"],
            )

        response = self.client.get(f"/api/ingresos/{ingreso_id}/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["remito_ingreso"], "RIS R 00004-00004667")
        self.assertEqual(response.data["remito_salida"], "RSS R 00004-00004715")
        self.assertEqual(response.data["remito_salida_bejerman"], "RSS R 00004-00004715")
        with connection.cursor() as cur:
            cur.execute("SELECT remito_salida FROM ingresos WHERE id = %s", [ingreso_id])
            self.assertEqual(cur.fetchone()[0], "RSS R 00004-00004715")

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
        empresa_bejerman="SEPID",
    ):
        ingreso_id = self._insert_ingreso(
            serial=serial,
            numero_interno=numero_interno,
            customer_id=customer_id or self.mg_customer_id,
            equipo_variante=equipo_variante,
            empresa_bejerman=empresa_bejerman,
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

    def test_worker_blocks_job_without_actor_user_session(self):
        ingreso_id = self._enqueue_job(serial="SN-SIN-ACTOR")

        with _bejerman_settings():
            stats = process_bejerman_jobs()

        self.assertEqual(stats["blocked"], 1)
        job = self._job_row(ingreso_id)
        self.assertEqual(job["status"], "blocked")
        self.assertIn("sesión de usuario", job["last_error"])

    def test_ris_emitir_devuelve_json_con_remito_sin_bloquear_por_pdf(self):
        ingreso_id = self._insert_ingreso(serial="SN-RIS-JSON-001")
        fake = FakeRisClient(number="00004561")

        with (
            _bejerman_settings(),
            _valid_ris_preflight(fake),
            patch("service.bejerman_ris.resolve_customer_document_fields", return_value=_ris_customer_fields()),
        ):
            response = self.client.post(f"/api/ingresos/{ingreso_id}/ris/emitir/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["remito_number"], "RIS R 00004-00004561")
        self.assertEqual(response.json()["pdf_status"], "pending")
        self.assertEqual(response.json()["print_url"], f"/api/ingresos/{ingreso_id}/ris/print/")
        self.assertEqual(fake.ingresar_calls, 1)
        article_lines = [item for item in fake.comprobantes[0]["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual([item["Item_CantidadUM1"] for item in article_lines], [-1])

        status_response = self.client.get(f"/api/ingresos/{ingreso_id}/ris/")
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json()["remito_number"], "RIS R 00004-00004561")
        self.assertEqual(status_response.json()["pdf_url"], f"/api/ingresos/{ingreso_id}/ris/pdf/")

    def test_ris_emitir_generado_no_vuelve_a_emitir_en_bejerman(self):
        ingreso_id = self._insert_ingreso(serial="SN-RIS-IDEMP-001")
        fake = FakeRisClient(number="00004562")

        with (
            _bejerman_settings(),
            _valid_ris_preflight(fake),
            patch("service.bejerman_ris.resolve_customer_document_fields", return_value=_ris_customer_fields()),
        ):
            first = self.client.post(f"/api/ingresos/{ingreso_id}/ris/emitir/")
            second = self.client.post(f"/api/ingresos/{ingreso_id}/ris/emitir/")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["remito_number"], "RIS R 00004-00004562")
        self.assertEqual(fake.ingresar_calls, 1)

    def test_ris_emitir_reintenta_registro_manual_pv1_fallido_sin_consulta_previa(self):
        ingreso_id = self._insert_ingreso(serial="SN-RIS-REG-FAILED-PV1")
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bejerman_ingreso_remitos (
                    ingreso_id,
                    status,
                    pdf_status,
                    document_mode,
                    manual_remito_number,
                    comprobante_tipo,
                    comprobante_letra,
                    comprobante_pto_venta,
                    comprobante_numero,
                    remito_number,
                    company_key,
                    last_error
                )
                VALUES (%s, 'failed', 'not_applicable', 'register', %s, 'RIS', 'R', '00001', '00026285',
                        %s, 'SEPID', %s)
                """,
                [
                    ingreso_id,
                    "RIS R 00001-00026285",
                    "RIS R 00001-00026285",
                    "Invalid object name 'expCuotasV'.",
                ],
            )

        fake = FakeRisClient(
            sales_error=BejermanSdkResponseError("Invalid object name 'expCuotasV'."),
        )

        with (
            _bejerman_settings(),
            _valid_ris_preflight(fake),
            patch("service.bejerman_ris.resolve_customer_document_fields", return_value=_ris_customer_fields()),
        ):
            status_before = self.client.get(f"/api/ingresos/{ingreso_id}/ris/")
            response = self.client.post(f"/api/ingresos/{ingreso_id}/ris/emitir/")

        self.assertEqual(status_before.status_code, 200)
        self.assertFalse(status_before.json()["ris"]["is_registered"])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.json()["ris"]["status"], "generated")
        self.assertEqual(response.json()["ris"]["document_mode"], "register")
        self.assertEqual(response.json()["remito_number"], "RIS R 00001-00026285")
        self.assertEqual(fake.sales_list_calls, 0)
        self.assertEqual(fake.ingresar_calls, 1)
        self.assertEqual(fake.ingresar_kwargs[0]["emite_reg"], "R")
        self.assertEqual(fake.ingresar_kwargs[0]["numera_flex"], "N")

    def test_ris_emitir_bloquea_si_bejerman_ya_tiene_remito_para_equipo(self):
        ingreso_id = self._insert_ingreso(serial="SN-RIS-DUP-EMIT")
        today = timezone.localdate().isoformat()
        fake = FakeRisClient(
            sales_records=[
                {
                    "Comprobante_ID": "dup-emit-1",
                    "Comprobante_Tipo": "RIS",
                    "Comprobante_Letra": "R",
                    "Comprobante_PtoVenta": "00004",
                    "Comprobante_Numero": "00004583",
                    "Comprobante_FechaEmision": today,
                }
            ],
            sales_details={
                "dup-emit-1": {
                    "Comprobante_Tipo": "RIS",
                    "Comprobante_Letra": "R",
                    "Comprobante_PtoVenta": "00004",
                    "Comprobante_Numero": "00004583",
                    "Comprobante_FechaEmision": today,
                    "Comprobante_Items": [
                        {"Item_Tipo": "L", "Item_DescripArticulo": "Ingreso a servicio tecnico - Serie SN-RIS-DUP-EMIT"},
                    ],
                }
            },
        )

        with (
            _bejerman_settings(),
            _valid_ris_preflight(fake),
            patch("service.bejerman_ris.resolve_customer_document_fields", return_value=_ris_customer_fields()),
        ):
            response = self.client.post(f"/api/ingresos/{ingreso_id}/ris/emitir/")

        self.assertEqual(response.status_code, 409, response.data)
        self.assertTrue(any(issue["code"] == "INGRESO_REMITO_EQUIPMENT_DUPLICATE_REMOTE" for issue in response.data["issues"]))
        self.assertEqual(fake.ingresar_calls, 0)
        self.assertIsNone(self.client.get(f"/api/ingresos/{ingreso_id}/ris/").json().get("remito_number") or None)

    def test_ris_pdf_pendiente_devuelve_202_sin_marcar_emision_fallida(self):
        ingreso_id = self._insert_ingreso(serial="SN-RIS-PDF-PENDING")
        fake = FakeRisClient(number="00004563")

        with (
            _bejerman_settings(),
            _valid_ris_preflight(fake),
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
            _valid_ris_preflight(fake),
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

    def test_ris_status_no_adopta_remito_ingreso_corto_sin_punto_venta(self):
        ingreso_id = self._insert_ingreso(serial="SN-RIS-LEGACY-VIEW")
        fake = FakeRisClient(number="00004565")
        with connection.cursor() as cur:
            cur.execute("UPDATE ingresos SET remito_ingreso = '4439' WHERE id = %s", [ingreso_id])

        with (
            _bejerman_settings(),
            patch("service.bejerman_ris.BejermanSDKClient", return_value=fake),
        ):
            response = self.client.get(f"/api/ingresos/{ingreso_id}/ris/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["remito_number"], "")
        self.assertEqual(response.data["ris"]["status"], "pending")
        self.assertEqual(fake.ingresar_calls, 0)
        with connection.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM bejerman_ingreso_remitos WHERE ingreso_id = %s", [ingreso_id])
            self.assertEqual(cur.fetchone()[0], 0)
            cur.execute("SELECT remito_ingreso FROM ingresos WHERE id = %s", [ingreso_id])
            self.assertEqual(cur.fetchone()[0], "4439")

    def test_ris_status_adopta_remito_ingreso_completo_sin_emitir(self):
        ingreso_id = self._insert_ingreso(serial="SN-RIS-LEGACY-FULL")
        fake = FakeRisClient(number="00004565")
        with connection.cursor() as cur:
            cur.execute("UPDATE ingresos SET remito_ingreso = 'RDN R 00001-00025310' WHERE id = %s", [ingreso_id])

        with (
            _bejerman_settings(),
            patch("service.bejerman_ris.BejermanSDKClient", return_value=fake),
        ):
            response = self.client.get(f"/api/ingresos/{ingreso_id}/ris/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["remito_number"], "RDN R 00001-00025310")
        self.assertEqual(response.data["ris"]["status"], "generated")
        self.assertEqual(response.data["ris"]["document_mode"], "register")
        self.assertEqual(fake.ingresar_calls, 0)
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT status, document_mode, manual_remito_number, pdf_status,
                       comprobante_tipo, comprobante_pto_venta, comprobante_numero, remito_number
                  FROM bejerman_ingreso_remitos
                 WHERE ingreso_id = %s
                """,
                [ingreso_id],
            )
            self.assertEqual(
                cur.fetchone(),
                (
                    "generated",
                    "register",
                    "RDN R 00001-00025310",
                    "not_applicable",
                    "RDN",
                    "00001",
                    "00025310",
                    "RDN R 00001-00025310",
                ),
            )
            cur.execute("SELECT remito_ingreso FROM ingresos WHERE id = %s", [ingreso_id])
            self.assertEqual(cur.fetchone()[0], "RDN R 00001-00025310")

    def test_ris_pdf_usa_remito_ingreso_existente_sin_emitir(self):
        ingreso_id = self._insert_ingreso(serial="SN-RIS-LEGACY-PDF")
        fake = FakeRisClient(number="00004565")
        references = []
        with connection.cursor() as cur:
            cur.execute("UPDATE ingresos SET remito_ingreso = 'RIS R 00004-00004439' WHERE id = %s", [ingreso_id])

        def capture_pdf(_client, reference, *, interactive=True):
            references.append(reference)
            return b"%PDF-1.4\nlegacy", "application/pdf"

        with (
            _bejerman_settings(),
            patch("service.bejerman_ris.BejermanSDKClient", return_value=fake),
            patch("service.bejerman_ris.fetch_comprobante_pdf", side_effect=capture_pdf),
        ):
            response = self.client.get(f"/api/ingresos/{ingreso_id}/ris/pdf/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(fake.ingresar_calls, 0)
        self.assertEqual(len(references), 1)
        self.assertEqual(references[0].type, "RIS")
        self.assertEqual(references[0].letter, "R")
        self.assertEqual(references[0].point_of_sale, "00004")
        self.assertEqual(references[0].number, "00004439")

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="noreply@nexora.test",
    )
    def test_ris_emitido_envia_pdf_por_mail_a_roles_obligatorios(self):
        mail.outbox = []
        ingreso_id = self._insert_ingreso(serial="SN-RIS-MAIL-001")
        fake = FakeRisClient(number="00004566")

        with (
            _bejerman_settings(),
            _valid_ris_preflight(fake),
            patch("service.bejerman_ris.resolve_customer_document_fields", return_value=_ris_customer_fields()),
            patch("service.bejerman_ris._fetch_pdf", return_value=(b"%PDF-1.4\nRIS mail", "application/pdf")) as pdf_mock,
        ):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(f"/api/ingresos/{ingreso_id}/ris/emitir/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.json()["remito_number"], "RIS R 00004-00004566")
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(set(message.to), {"admin@bejerman.test", "cobranzas@bejerman.test", "recepcion@bejerman.test"})
        self.assertIn("RIS R 00004-00004566", message.subject)
        self.assertIn("Se emitió un remito en Bejerman.", message.body)
        self.assertEqual(len(message.attachments), 1)
        self.assertTrue(message.attachments[0][1].startswith(b"%PDF-"))
        pdf_mock.assert_called_once()

    def test_ris_print_page_incluye_polling_resiliente_y_textos_utf8(self):
        ingreso_id = self._insert_ingreso(serial="SN-RIS-PRINT-PAGE")

        response = self.client.get(f"/api/ingresos/{ingreso_id}/ris/print/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        html = response.content.decode("utf-8")
        self.assertIn("Retry-After", html)
        self.assertIn("retry_after_ms", html)
        self.assertIn("NEXORA volverá a buscar el remito automáticamente", html)
        self.assertIn("no se encontro el comprobante asociado", html)
        self.assertIn("Cerrar pestaña", html)
        self.assertIn("Esta pestaña se reemplazará por el PDF", html)
        self.assertNotIn("maxAttempts", html)
        self.assertNotIn("maxWaitMs", html)
        self.assertNotIn("shouldStopWaiting", html)
        self.assertNotIn("Reintente en unos segundos", html)
        self.assertNotIn("no es necesario volver a emitir el remito", html)
        self.assertNotIn("Intento ", html)
        self.assertNotIn("Esperando PDF (", html)
        self.assertNotIn(chr(0x00C3), html)

    def test_ris_pdf_sin_emision_devuelve_409_accionable(self):
        ingreso_id = self._insert_ingreso(serial="SN-RIS-PDF-NOT-EMITTED")

        response = self.client.get(f"/api/ingresos/{ingreso_id}/ris/pdf/")

        self.assertEqual(response.status_code, 409)
        self.assertIn("RIS", response.json()["detail"])

    def test_ris_pdf_error_no_marca_emision_como_fallida(self):
        ingreso_id = self._insert_ingreso(serial="SN-RIS-PDF-ERROR")
        fake = FakeRisClient(number="00004565")

        with (
            _bejerman_settings(),
            _valid_ris_preflight(fake),
            patch("service.bejerman_ris.resolve_customer_document_fields", return_value=_ris_customer_fields()),
        ):
            self.client.post(f"/api/ingresos/{ingreso_id}/ris/emitir/")

        with patch(
            "service.bejerman_ris.fetch_comprobante_pdf",
            side_effect=BejermanSdkResponseError("Servicio PDF no disponible"),
        ):
            response = self.client.get(f"/api/ingresos/{ingreso_id}/ris/pdf/")

        self.assertEqual(response.status_code, 502)
        with connection.cursor() as cur:
            cur.execute("SELECT status, pdf_status, last_error FROM bejerman_ingreso_remitos WHERE ingreso_id=%s", [ingreso_id])
            status, pdf_status, last_error = cur.fetchone()
        self.assertEqual(status, "generated")
        self.assertEqual(pdf_status, "failed")
        self.assertIn("Servicio PDF no disponible", last_error)

    def test_ris_pdf_timeout_queda_pendiente_sin_marcar_fallido(self):
        ingreso_id = self._insert_ingreso(serial="SN-RIS-PDF-TIMEOUT")
        fake = FakeRisClient(number="00004566")

        with (
            _bejerman_settings(),
            _valid_ris_preflight(fake),
            patch("service.bejerman_ris.resolve_customer_document_fields", return_value=_ris_customer_fields()),
        ):
            self.client.post(f"/api/ingresos/{ingreso_id}/ris/emitir/")

        with patch(
            "service.bejerman_ris.fetch_comprobante_pdf",
            side_effect=BejermanSdkUnavailable("Error HTTP Bejerman: Read timed out"),
        ):
            response = self.client.get(f"/api/ingresos/{ingreso_id}/ris/pdf/")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["pdf_status"], "pending")
        self.assertEqual(response["Retry-After"], "5")
        with connection.cursor() as cur:
            cur.execute("SELECT status, pdf_status, last_error FROM bejerman_ingreso_remitos WHERE ingreso_id=%s", [ingreso_id])
            status, pdf_status, last_error = cur.fetchone()
        self.assertEqual(status, "generated")
        self.assertEqual(pdf_status, "pending")
        self.assertFalse(last_error)

    def test_ris_validacion_bejerman_queda_como_error_visible(self):
        ingreso_id = self._insert_ingreso(serial="SN-RIS-IVA-MISSING")

        with (
            _bejerman_settings(),
            _valid_ris_preflight(FakeRisClient()),
            patch(
                "service.bejerman_ris.resolve_customer_document_fields",
                side_effect=BejermanSdkResponseError("El comprobante no se importó. Debe indicar la Situación de IVA del cliente."),
            ),
        ):
            response = self.client.post(f"/api/ingresos/{ingreso_id}/ris/emitir/")

        self.assertEqual(response.status_code, 502)
        self.assertIn("Situación de IVA", response.json()["detail"])
        self.assertEqual(response.json()["ris"]["status"], "failed")

    def test_ris_preflight_valido_no_emite_en_bejerman(self):
        ingreso_id = self._insert_ingreso(serial="SN-RIS-PREFLIGHT-OK")
        fake = FakeRisClient(number="00009999")

        with (
            _bejerman_settings(),
            _valid_ris_preflight(fake),
        ):
            response = self.client.post(f"/api/ingresos/{ingreso_id}/ris/preflight/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["can_emit"])
        self.assertEqual(response.data["preview"]["customerCode"], "CLI-BEJ")
        self.assertEqual(len(response.data["preview"]["items"]), 1)
        self.assertEqual(fake.ingresar_calls, 0)

    def test_ris_preflight_sin_mapeo_no_valida_articulo_generico(self):
        ingreso_id = self._insert_ingreso(serial="SN-RIS-NO-MAPPING")
        fake = FakeRisClient()
        stock_client = FakeBejermanClient(deposit_records={"": []})

        with (
            _bejerman_settings(),
            patch("service.bejerman_ris.BejermanSDKClient", return_value=fake),
            patch("service.bejerman_ris.SyncBejermanSDKClient", return_value=stock_client),
            patch("service.bejerman_ris.validate_bejerman_article_choice") as validate_article,
        ):
            response = self.client.post(f"/api/ingresos/{ingreso_id}/ris/preflight/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["can_emit"])
        validate_article.assert_not_called()
        issue = next(item for item in response.data["issues"] if item["code"] == "BEJERMAN_ARTICLE_MAPPING_REQUIRED")
        self.assertNotIn("SERVICIO", issue["message"])
        self.assertEqual(issue["field"], "equipo.modelo_id")
        self.assertEqual(issue["fix"]["type"], "article_mapping")
        self.assertEqual(issue["fix"]["model_id"], self.model_id)
        self.assertEqual(issue["fix"]["variante"], "")
        self.assertEqual(response.data["preview"]["items"][0]["articleCode"], "")

    def test_stock_ingress_preflight_no_busca_partida_con_stock_positivo(self):
        cases = [
            ("SN-RDA-NO-STOCK", "baja alquiler", {}, "RDA"),
            ("SN-RDN-NO-STOCK", "devolución demo", {}, "RDN"),
            ("SN-RIS-NO-STOCK", "reparación", {"BEJERMAN_RIS_UPDATE_STOCK": True}, "RIS"),
        ]
        for serial, motivo, settings_extra, document_type in cases:
            with self.subTest(document_type=document_type):
                ingreso_id = self._insert_ingreso(serial=serial, motivo=motivo)
                fake = FakeRisClient()

                with (
                    _bejerman_settings(**settings_extra),
                    patch("service.bejerman_ris.BejermanSDKClient", return_value=fake),
                    patch("service.bejerman_ris._stock_article_candidates") as stock_candidates,
                    patch("service.bejerman_ris.validate_bejerman_article_choice") as validate_article,
                ):
                    response = self.client.post(f"/api/ingresos/{ingreso_id}/ris/preflight/")

                self.assertEqual(response.status_code, 200, response.data)
                self.assertFalse(response.data["can_emit"])
                stock_candidates.assert_not_called()
                validate_article.assert_not_called()
                issue = next(item for item in response.data["issues"] if item["code"] == "BEJERMAN_ARTICLE_MAPPING_REQUIRED")
                self.assertEqual(issue["candidates"], [])
                self.assertIn("stock positivo previo", issue["message"])
                self.assertEqual(response.data["preview"]["documentProfile"]["type"], document_type)
                self.assertEqual(response.data["preview"]["items"][0]["partida"], serial)
                self.assertEqual(response.data["preview"]["items"][0]["deposit"], "STR")

    def test_rda_preflight_usa_numero_interno_como_partida_si_falta_serie(self):
        ingreso_id = self._insert_ingreso(serial="", numero_interno="MG 6499", motivo="baja alquiler")
        fake = FakeRisClient()

        with (
            _bejerman_settings(),
            _valid_ris_preflight(fake),
        ):
            response = self.client.post(f"/api/ingresos/{ingreso_id}/ris/preflight/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["can_emit"], response.data)
        self.assertFalse(any(item["code"] == "RDA_PARTIDA_REQUIRED" for item in response.data["issues"]))
        self.assertEqual(response.data["preview"]["documentProfile"]["type"], "RDA")
        self.assertEqual(response.data["preview"]["items"][0]["serial"], "MG 6499")
        self.assertEqual(response.data["preview"]["items"][0]["internalNumber"], "MG 6499")
        self.assertEqual(response.data["preview"]["items"][0]["partida"], "MG 6499")
        self.assertEqual(response.data["preview"]["items"][0]["partidaPolicy"], "identifier")

    def test_ris_preflight_cliente_no_encontrado_informa_candidato(self):
        ingreso_id = self._insert_ingreso(serial="SN-RIS-CUSTOMER-CANDIDATE")
        fake = FakeRisClient(
            clients=[
                {
                    "Cliente_Codigo": "CLI-FIX",
                    "Cliente_RazonSocial": "Cliente Bejerman",
                    "Cliente_NroDocumento": "30700000000",
                    "Cliente_Provincia": "02",
                    "Cliente_SitIVA": "RI",
                }
            ]
        )

        with (
            _bejerman_settings(),
            patch("service.bejerman_ris.BejermanSDKClient", return_value=fake),
            patch("service.bejerman_ris.validate_bejerman_article_choice", side_effect=_ris_article_choice),
        ):
            response = self.client.post(f"/api/ingresos/{ingreso_id}/ris/preflight/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["can_emit"])
        issue = next(item for item in response.data["issues"] if item["code"] == "BEJERMAN_CUSTOMER_NOT_FOUND")
        self.assertEqual(issue["candidates"][0]["code"], "CLI-FIX")
        self.assertEqual(issue["fix"]["customer_id"], self.customer_id)

    def test_ris_emitir_preflight_invalido_no_llama_bejerman_ni_marca_failed(self):
        ingreso_id = self._insert_ingreso(serial="SN-RIS-PREFLIGHT-BLOCK")
        fake = FakeRisClient(clients=[])

        with (
            _bejerman_settings(),
            patch("service.bejerman_ris.BejermanSDKClient", return_value=fake),
            patch("service.bejerman_ris.validate_bejerman_article_choice", side_effect=_ris_article_choice),
        ):
            response = self.client.post(f"/api/ingresos/{ingreso_id}/ris/emitir/")

        self.assertEqual(response.status_code, 409, response.data)
        self.assertFalse(response.data["can_emit"])
        self.assertEqual(fake.ingresar_calls, 0)
        with connection.cursor() as cur:
            cur.execute("SELECT status, last_error FROM bejerman_ingreso_remitos WHERE ingreso_id=%s", [ingreso_id])
            status, last_error = cur.fetchone()
        self.assertNotEqual(status, "failed")
        self.assertFalse(last_error)

    def test_ris_preflight_articulo_invalido_sugiere_fix_unico_por_partida(self):
        ingreso_id = self._insert_ingreso(serial="SN-RIS-ARTICLE-FIX")
        fake = FakeRisClient()
        stock_client = FakeBejermanClient(
            deposit_records={
                "": [
                    {
                        "Articulo_Codigo": "ART-CPAP",
                        "Art_DescripcionGeneral": "Equipo CPAP AirSense 10",
                        "Art_CodDeposito": "STR",
                        "Partida": "SN-RIS-ARTICLE-FIX",
                    }
                ]
            }
        )

        with (
            _bejerman_settings(BEJERMAN_RIS_ALLOW_GENERIC_ARTICLE=True, BEJERMAN_RIS_UPDATE_STOCK=False),
            patch("service.bejerman_ris.BejermanSDKClient", return_value=fake),
            patch("service.bejerman_ris.SyncBejermanSDKClient", return_value=stock_client),
            patch(
                "service.bejerman_ris.validate_bejerman_article_choice",
                side_effect=BejermanBlockedError("No se encontró el artículo Bejerman SERVICIO"),
            ),
        ):
            response = self.client.post(f"/api/ingresos/{ingreso_id}/ris/preflight/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["can_emit"])
        issue = next(item for item in response.data["issues"] if item["code"] == "BEJERMAN_ARTICLE_INVALID")
        self.assertEqual(issue["fix"]["type"], "article_mapping")
        self.assertEqual(issue["fix"]["article_code"], "ART-CPAP")
        self.assertEqual(issue["fix"]["model_id"], self.model_id)

    def test_nuevo_ingreso_lote_preflight_falla_no_crea_ingresos(self):
        payload = self._lote_payload("SN-LOTE-PREFLIGHT-BLOCK-1", "SN-LOTE-PREFLIGHT-BLOCK-2")
        preflight = {
            "can_emit": False,
            "issues": [
                {
                    "code": "BEJERMAN_CUSTOMER_NOT_FOUND",
                    "severity": "error",
                    "scope": "cliente",
                    "field": "cliente.cod_empresa",
                    "item_index": 0,
                    "message": "Cliente no encontrado en Bejerman.",
                    "candidates": [],
                    "fix": {},
                }
            ],
            "preview": {"items": [], "lineCount": 0},
            "detail": "La validación previa del RIS encontró problemas.",
        }

        with patch("service.views.ingresos_views.preflight_ris_for_request_payload", return_value=preflight):
            response = self.client.post("/api/ingresos/nuevo/lote/", payload, format="json")

        self.assertEqual(response.status_code, 409, response.data)
        self.assertFalse(response.data["can_emit"])
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                  FROM ingresos t
                  JOIN devices d ON d.id = t.device_id
                 WHERE d.numero_serie IN ('SN-LOTE-PREFLIGHT-BLOCK-1', 'SN-LOTE-PREFLIGHT-BLOCK-2')
                """
            )
            self.assertEqual(cur.fetchone()[0], 0)

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
        self.assertIn("company_key", columns)
        self.assertIn("request_payload", columns)
        self.assertIn("uq_bejerman_sync_jobs_type_ingreso", indexes)
        self.assertIn("ix_bejerman_sync_jobs_due", indexes)
        self.assertIn("ix_bejerman_sync_jobs_company_key", indexes)
        self.assertIn("chk_bejerman_sync_jobs_status", constraints)
        self.assertIn("model_id", mapping_columns)
        self.assertIn("variante_norm", mapping_columns)
        self.assertIn("article_code", mapping_columns)
        self.assertIn("uq_bejerman_article_mappings_model_variant", all_indexes)

    def test_sync_sdk_registra_empresa_mgbio(self):
        client = BejermanSDKClient(
            company_key="MGBIO",
            bejerman_username="sdk-user",
            bejerman_password="sdk-password",
            bejerman_workstation="WS",
        )

        with patch.object(client, "_post", return_value={"Token": "TOKEN"}) as post:
            client.register()

        body = post.call_args.args[1]
        self.assertIn("<xCodEmpresa>MGBI</xCodEmpresa>", body)

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

    def test_remitos_salida_bulk_devuelve_un_pdf_y_libera_todos_los_ingresos(self):
        first_id = self._insert_ingreso(serial="SN-REMITO-BULK-001")
        second_id = self._insert_ingreso(serial="SN-REMITO-BULK-002")
        url = f"/api/ingresos/remitos-salida/?ids={first_id},{second_id}"

        with (
            override_settings(SECURE_SSL_REDIRECT=False),
            patch("service.views.reportes_views.render_remito_salida_pdf", side_effect=[
                (b"%PDF-1.4 first", "remito-1.pdf"),
                (b"%PDF-1.4 second", "remito-2.pdf"),
            ]),
            patch("service.views.reportes_views._merge_pdf_documents", return_value=b"%PDF-1.4 merged") as merge_pdfs,
            patch("service.views.reportes_views.notify_ingreso_liberado", return_value=0),
        ):
            response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(response.content, b"%PDF-1.4 merged")
        merge_pdfs.assert_called_once_with([b"%PDF-1.4 first", b"%PDF-1.4 second"])
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                  FROM ingreso_events
                 WHERE ingreso_id IN (%s,%s)
                   AND a_estado='liberado'
                """,
                [first_id, second_id],
            )
            events = int(cur.fetchone()[0])
        self.assertEqual(events, 2)

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

    def test_remito_liberado_codigo_mg_activo_de_cliente_encola_stl_sin_rss(self):
        ingreso_id = self._insert_ingreso(serial="SN-REMITO-MG-CLIENTE", numero_interno="MG 0005")
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
        self.assertIsNone(self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STC))
        self.assertIsNone(self._job_row(ingreso_id, SYNC_TYPE_STOCK_EXIT_RTS))

    def test_remito_liberado_devolucion_demo_encola_val_sin_stc(self):
        ingreso_id = self._insert_ingreso(serial="SN-REMITO-DEMO", motivo="devolución demo")
        url = f"/api/ingresos/{ingreso_id}/remito/"

        with (
            override_settings(SECURE_SSL_REDIRECT=False),
            patch("service.views.reportes_views.render_remito_salida_pdf", return_value=(b"%PDF-1.4", "remito.pdf")),
            patch("service.views.reportes_views.notify_ingreso_liberado", return_value=0),
        ):
            response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL))
        self.assertIsNone(self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STC))
        transfer = self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_VAL)
        self.assertIsNotNone(transfer)
        self.assertEqual(transfer["source_deposit"], "STR")
        self.assertEqual(transfer["target_deposit"], "VAL")

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

    def test_entrega_devolucion_demo_encola_solo_transferencia_a_val(self):
        ingreso_id = self._insert_ingreso(serial="SN-ENTREGA-DEMO", motivo="devolución demo")
        url = f"/api/ingresos/{ingreso_id}/entregar/"

        response = self.client.post(url, {"remito_salida": "RTN-0001"}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STC))
        transfer = self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_VAL)
        self.assertIsNotNone(transfer)
        self.assertEqual(transfer["source_deposit"], "STR")
        self.assertEqual(transfer["target_deposit"], "VAL")

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

    def test_entrega_nm_nv_y_ce_activos_encolan_stl(self):
        for prefix in ("NM", "NV", "CE"):
            with self.subTest(prefix=prefix):
                ingreso_id = self._insert_ingreso(
                    serial=f"SN-ENTREGA-{prefix}-001",
                    numero_interno=f"{prefix} 0004",
                )
                url = f"/api/ingresos/{ingreso_id}/entregar/"

                response = self.client.post(url, {"remito_salida": f"RSS-{prefix}"}, format="json")

                self.assertEqual(response.status_code, 200, response.data)
                transfer = self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL)
                self.assertIsNotNone(transfer)
                self.assertEqual(transfer["target_deposit"], "STL")
                self.assertIsNone(self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STC))
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
            _bejerman_settings(),
            _valid_ris_preflight(),
            patch("service.views.ingresos_views._map_motivo_to_db_label", return_value="otros"),
            patch(
                "service.views.ingresos_views._compute_factory_warranty_from_bejerman_sale",
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
            _bejerman_settings(),
            _valid_ris_preflight(),
            patch("service.views.ingresos_views._map_motivo_to_db_label", return_value="otros"),
            patch(
                "service.views.ingresos_views._compute_factory_warranty_from_bejerman_sale",
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

    def _lote_payload(self, *serials, accesorios_items=None):
        return {
            "cliente": {"id": self.customer_id},
            "empresa_bejerman": "SEPID",
            "items": [
                {
                    "equipo": {
                        "marca_id": self.marca_id,
                        "modelo_id": self.model_id,
                        "numero_serie": serial,
                        "numero_interno": "",
                    },
                    "motivo": "otros",
                    "ubicacion_id": self.taller_id,
                    "accesorios_items": accesorios_items if index == 0 and accesorios_items else [],
                }
                for index, serial in enumerate(serials)
            ],
        }

    def test_nuevo_ingreso_lote_crea_ingresos_y_un_solo_ris(self):
        with connection.cursor() as cur:
            cur.execute("INSERT INTO catalogo_accesorios(nombre) VALUES (%s) RETURNING id", ["Bolso"])
            accesorio_id = int(cur.fetchone()[0])
        fake = FakeRisClient(number="00004570")
        payload = self._lote_payload(
            "SN-LOTE-001",
            "SN-LOTE-002",
            accesorios_items=[{"accesorio_id": accesorio_id, "referencia": "B-1"}],
        )

        with (
            _bejerman_settings(),
            patch("service.views.ingresos_views._map_motivo_to_db_label", return_value="otros"),
            patch(
                "service.views.ingresos_views._compute_factory_warranty_from_bejerman_sale",
                return_value={"garantia": False, "vence_el": None, "fecha_venta": None},
            ),
            _valid_ris_preflight(fake),
            patch("service.bejerman_ris.resolve_customer_document_fields", return_value=_ris_customer_fields()),
        ):
            response = self.client.post("/api/ingresos/nuevo/lote/", payload, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        ingreso_ids = response.data["ingreso_ids"]
        self.assertEqual(len(ingreso_ids), 2)
        self.assertEqual(response.data["remito_number"], "RIS R 00004-00004570")
        self.assertEqual(fake.ingresar_calls, 1)
        legend_lines = [
            item["Item_DescripArticulo"]
            for item in fake.comprobantes[0]["Comprobante_Items"]
            if item["Item_Tipo"] == "L"
        ]
        self.assertEqual(legend_lines[0], "Se recibe para servicio técnico:")
        self.assertEqual(legend_lines.count("------------------------------"), 2)
        self.assertTrue(any("SN-LOTE-001" in line for line in legend_lines))
        self.assertTrue(any("Accesorios: Bolso (ref: B-1)" in line for line in legend_lines))
        with connection.cursor() as cur:
            cur.execute("SELECT remito_ingreso FROM ingresos WHERE id = ANY(%s) ORDER BY id", [ingreso_ids])
            self.assertEqual([row[0] for row in cur.fetchall()], ["RIS R 00004-00004570", "RIS R 00004-00004570"])
            cur.execute("SELECT COUNT(*) FROM ingreso_accesorios WHERE ingreso_id = %s", [ingreso_ids[0]])
            self.assertEqual(cur.fetchone()[0], 1)
        for ingreso_id in ingreso_ids:
            self.assertIsNone(self._job_row(ingreso_id, SYNC_TYPE_STOCK_ENTRY_STR))

    def test_nuevo_ingreso_lote_mg_baja_alquiler_emite_rda_con_stock_str(self):
        fake = FakeRisClient(number="00004571")
        payload = self._lote_payload("SN-RDA-001")
        payload["items"][0]["equipo"]["numero_interno"] = "MG 1234"
        payload["items"][0]["motivo"] = "baja alquiler"

        with (
            _bejerman_settings(BEJERMAN_RIS_RENTAL_RETURN_POINT_OF_SALE="00001"),
            patch("service.views.ingresos_views._map_motivo_to_db_label", return_value="baja alquiler"),
            patch(
                "service.views.ingresos_views._compute_factory_warranty_from_bejerman_sale",
                return_value={"garantia": False, "vence_el": None, "fecha_venta": None},
            ),
            _valid_ris_preflight(fake),
            patch("service.bejerman_ris.resolve_customer_document_fields", return_value=_ris_customer_fields()),
        ):
            response = self.client.post("/api/ingresos/nuevo/lote/", payload, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["remito_number"], "RDA R 00004-00004571")
        comprobante = fake.comprobantes[0]
        self.assertEqual(comprobante["Comprobante_Tipo"], "RDA")
        self.assertEqual(comprobante["Comprobante_TipoOperacion"], "ALQ")
        self.assertEqual(comprobante["Comprobante_ActualizaStock"], "S")
        article_lines = [item for item in comprobante["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        legend_lines = [
            item["Item_DescripArticulo"]
            for item in comprobante["Comprobante_Items"]
            if item["Item_Tipo"] == "L"
        ]
        self.assertEqual([item["Item_CantidadUM1"] for item in article_lines], [-1])
        self.assertEqual([item["Item_CantidadUM2"] for item in article_lines], [-1])
        self.assertEqual({item["Item_Deposito"] for item in article_lines}, {"STR"})
        self.assertEqual([item["Item_Partida"] for item in article_lines], ["SN-RDA-001"])
        self.assertIn("Motivo de ingreso: baja alquiler", legend_lines)
        self.assertEqual(fake.stock_comprobantes, [])

    def test_nuevo_ingreso_lote_rda_usa_serie_sin_reintento_de_partida(self):
        fake = FakeRisClient(number="00004577")
        payload = self._lote_payload("G3GB4000030")
        payload["items"][0]["equipo"]["numero_interno"] = "MG 2686"
        payload["items"][0]["motivo"] = "baja alquiler"

        with (
            _bejerman_settings(),
            patch("service.views.ingresos_views._map_motivo_to_db_label", return_value="baja alquiler"),
            patch(
                "service.views.ingresos_views._compute_factory_warranty_from_bejerman_sale",
                return_value={"garantia": False, "vence_el": None, "fecha_venta": None},
            ),
            _valid_ris_preflight(fake),
            patch("service.bejerman_ris.resolve_customer_document_fields", return_value=_ris_customer_fields()),
        ):
            response = self.client.post("/api/ingresos/nuevo/lote/", payload, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["remito_number"], "RDA R 00004-00004577")
        self.assertEqual(fake.ingresar_calls, 1)
        article = [item for item in fake.comprobantes[0]["Comprobante_Items"] if item["Item_Tipo"] == "A"][0]
        self.assertEqual(article["Item_Partida"], "G3GB4000030")
        self.assertEqual(article["Item_Deposito"], "STR")
        self.assertEqual(article["Item_CantidadUM1"], -1)
        self.assertEqual(article["Item_CantidadUM2"], -1)
        self.assertEqual(fake.stock_comprobantes, [])

    def test_nuevo_ingreso_lote_baja_alquiler_sin_serie_usa_numero_interno_como_partida(self):
        fake = FakeRisClient(number="00004576")
        payload = self._lote_payload("")
        payload["items"][0]["equipo"]["numero_interno"] = "MG 6810"
        payload["items"][0]["motivo"] = "baja alquiler"

        with (
            _bejerman_settings(),
            patch("service.views.ingresos_views._map_motivo_to_db_label", return_value="baja alquiler"),
            patch(
                "service.views.ingresos_views._compute_factory_warranty_from_bejerman_sale",
                return_value={"garantia": False, "vence_el": None, "fecha_venta": None},
            ),
            _valid_ris_preflight(fake),
            patch("service.bejerman_ris.resolve_customer_document_fields", return_value=_ris_customer_fields()),
        ):
            response = self.client.post("/api/ingresos/nuevo/lote/", payload, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["remito_number"], "RDA R 00004-00004576")
        self.assertEqual(fake.ingresar_calls, 1)
        comprobante = fake.comprobantes[0]
        article_lines = [item for item in comprobante["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual([item["Item_CantidadUM1"] for item in article_lines], [-1])
        self.assertEqual([item["Item_Deposito"] for item in article_lines], ["STR"])
        self.assertEqual([item["Item_Partida"] for item in article_lines], ["MG 6810"])

    def test_nuevo_ingreso_lote_baja_alquiler_sin_identificador_bloquea_partida(self):
        fake = FakeRisClient(number="00004576")
        payload = self._lote_payload("")
        payload["items"][0]["equipo"]["numero_interno"] = ""
        payload["items"][0]["motivo"] = "baja alquiler"

        with (
            _bejerman_settings(),
            patch("service.views.ingresos_views._map_motivo_to_db_label", return_value="baja alquiler"),
            patch(
                "service.views.ingresos_views._compute_factory_warranty_from_bejerman_sale",
                return_value={"garantia": False, "vence_el": None, "fecha_venta": None},
            ),
            _valid_ris_preflight(fake),
            patch("service.bejerman_ris.resolve_customer_document_fields", return_value=_ris_customer_fields()),
        ):
            response = self.client.post("/api/ingresos/nuevo/lote/", payload, format="json")

        self.assertEqual(response.status_code, 409, response.data)
        issue = next(item for item in response.data["issues"] if item["code"] == "RDA_PARTIDA_REQUIRED")
        self.assertIn("número interno", issue["message"])
        self.assertEqual(fake.ingresar_calls, 0)

    def test_nuevo_ingreso_lote_baja_alquiler_sin_mg_emite_rda(self):
        fake = FakeRisClient(number="00004572")
        payload = self._lote_payload("SN-RIS-ALQ-001")
        payload["items"][0]["motivo"] = "baja alquiler"

        with (
            _bejerman_settings(),
            patch("service.views.ingresos_views._map_motivo_to_db_label", return_value="baja alquiler"),
            patch(
                "service.views.ingresos_views._compute_factory_warranty_from_bejerman_sale",
                return_value={"garantia": False, "vence_el": None, "fecha_venta": None},
            ),
            _valid_ris_preflight(fake),
            patch("service.bejerman_ris.resolve_customer_document_fields", return_value=_ris_customer_fields()),
        ):
            response = self.client.post("/api/ingresos/nuevo/lote/", payload, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["remito_number"], "RDA R 00004-00004572")
        comprobante = fake.comprobantes[0]
        self.assertEqual(comprobante["Comprobante_Tipo"], "RDA")
        self.assertEqual(comprobante["Comprobante_TipoOperacion"], "ALQ")
        self.assertEqual(comprobante["Comprobante_ActualizaStock"], "S")
        article_lines = [item for item in comprobante["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual([item["Item_CantidadUM1"] for item in article_lines], [-1])
        self.assertEqual([item["Item_CantidadUM2"] for item in article_lines], [-1])
        self.assertEqual({item["Item_Deposito"] for item in article_lines}, {"STR"})
        self.assertEqual([item["Item_Partida"] for item in article_lines], ["SN-RIS-ALQ-001"])

    def test_nuevo_ingreso_lote_baja_alquiler_existente_alquilado_sin_mg_emite_rda(self):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO devices(
                    customer_id, marca_id, model_id, numero_serie, numero_interno, alquilado, alquiler_a
                )
                VALUES (%s,%s,%s,%s,%s,TRUE,%s)
                """,
                [self.customer_id, self.marca_id, self.model_id, "SN-RDA-ALQ-EXIST", "", "ANDREOLI"],
            )
        fake = FakeRisClient(number="00004572")
        payload = self._lote_payload("SN-RDA-ALQ-EXIST")
        payload["items"][0]["motivo"] = "baja alquiler"

        with (
            _bejerman_settings(),
            patch("service.views.ingresos_views._map_motivo_to_db_label", return_value="baja alquiler"),
            patch(
                "service.views.ingresos_views._compute_factory_warranty_from_bejerman_sale",
                return_value={"garantia": False, "vence_el": None, "fecha_venta": None},
            ),
            _valid_ris_preflight(fake),
            patch("service.bejerman_ris.resolve_customer_document_fields", return_value=_ris_customer_fields()),
        ):
            response = self.client.post("/api/ingresos/nuevo/lote/", payload, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["remito_number"], "RDA R 00004-00004572")
        self.assertEqual(fake.comprobantes[0]["Comprobante_Tipo"], "RDA")

    def test_nuevo_ingreso_lote_mgbio_baja_alquiler_emite_rda_punto_00007(self):
        fake = FakeRisClient(number="00004575")
        payload = self._lote_payload("SN-RDA-MGBIO-001")
        payload["empresa_bejerman"] = "MGBIO"
        payload["items"][0]["motivo"] = "baja alquiler"

        with (
            _bejerman_settings(BEJERMAN_RIS_RENTAL_RETURN_POINT_OF_SALE="00004"),
            patch("service.views.ingresos_views._map_motivo_to_db_label", return_value="baja alquiler"),
            patch(
                "service.views.ingresos_views._compute_factory_warranty_from_bejerman_sale",
                return_value={"garantia": False, "vence_el": None, "fecha_venta": None},
            ),
            _valid_ris_preflight(fake),
            patch("service.bejerman_ris.resolve_customer_document_fields", return_value=_ris_customer_fields()),
        ):
            response = self.client.post("/api/ingresos/nuevo/lote/", payload, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["remito_number"], "RDA R 00007-00004575")
        comprobante = fake.comprobantes[0]
        self.assertEqual(comprobante["Comprobante_Tipo"], "RDA")
        self.assertEqual(comprobante["Comprobante_PtoVenta"], "00007")

    def test_nuevo_ingreso_lote_devolucion_demo_emite_rdn_con_stock_str(self):
        fake = FakeRisClient(number="00004573")
        payload = self._lote_payload("SN-RDN-001")
        payload["items"][0]["motivo"] = "devolución demo"

        with (
            _bejerman_settings(),
            patch("service.views.ingresos_views._map_motivo_to_db_label", return_value="devolución demo"),
            patch(
                "service.views.ingresos_views._compute_factory_warranty_from_bejerman_sale",
                return_value={"garantia": False, "vence_el": None, "fecha_venta": None},
            ),
            _valid_ris_preflight(fake),
            patch("service.bejerman_ris.resolve_customer_document_fields", return_value=_ris_customer_fields()),
        ):
            response = self.client.post("/api/ingresos/nuevo/lote/", payload, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["remito_number"], "RDN R 00004-00004573")
        comprobante = fake.comprobantes[0]
        self.assertEqual(comprobante["Comprobante_Tipo"], "RDN")
        self.assertEqual(comprobante["Comprobante_TipoOperacion"], "DEMO")
        self.assertEqual(comprobante["Comprobante_ActualizaStock"], "S")
        article_lines = [item for item in comprobante["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual([item["Item_CantidadUM1"] for item in article_lines], [-1])
        self.assertEqual([item["Item_CantidadUM2"] for item in article_lines], [-1])
        self.assertEqual({item["Item_Deposito"] for item in article_lines}, {"STR"})
        self.assertEqual([item["Item_Partida"] for item in article_lines], ["SN-RDN-001"])
        self.assertEqual(fake.stock_comprobantes, [])

    def test_nuevo_ingreso_lote_ris_con_stock_str_firma_cantidad_sin_stock_previo(self):
        fake = FakeRisClient(number="00004578")
        payload = self._lote_payload("SN-RIS-STOCK-001")
        payload["items"][0]["motivo"] = "reparación"

        with (
            _bejerman_settings(BEJERMAN_RIS_UPDATE_STOCK=True),
            patch("service.views.ingresos_views._map_motivo_to_db_label", return_value="reparación"),
            patch(
                "service.views.ingresos_views._compute_factory_warranty_from_bejerman_sale",
                return_value={"garantia": False, "vence_el": None, "fecha_venta": None},
            ),
            _valid_ris_preflight(fake),
            patch("service.bejerman_ris.resolve_customer_document_fields", return_value=_ris_customer_fields()),
        ):
            response = self.client.post("/api/ingresos/nuevo/lote/", payload, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["remito_number"], "RIS R 00004-00004578")
        comprobante = fake.comprobantes[0]
        self.assertEqual(comprobante["Comprobante_Tipo"], "RIS")
        self.assertEqual(comprobante["Comprobante_TipoOperacion"], "REP")
        self.assertEqual(comprobante["Comprobante_ActualizaStock"], "S")
        article_lines = [item for item in comprobante["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual([item["Item_CantidadUM1"] for item in article_lines], [-1])
        self.assertEqual([item["Item_CantidadUM2"] for item in article_lines], [-1])
        self.assertEqual([item["Item_Deposito"] for item in article_lines], ["STR"])
        self.assertEqual([item["Item_Partida"] for item in article_lines], ["SN-RIS-STOCK-001"])
        self.assertEqual(fake.stock_comprobantes, [])

    def test_nuevo_ingreso_lote_mixto_bloquea_sin_crear_ingresos(self):
        fake = FakeRisClient(number="00004574")
        payload = self._lote_payload("SN-MIX-RDA", "SN-MIX-RIS")
        payload["items"][0]["equipo"]["numero_interno"] = "MG 1235"
        payload["items"][0]["motivo"] = "baja alquiler"
        payload["items"][1]["motivo"] = "otros"

        with (
            _bejerman_settings(),
            patch("service.views.ingresos_views._map_motivo_to_db_label", return_value="otros"),
            patch(
                "service.views.ingresos_views._compute_factory_warranty_from_bejerman_sale",
                return_value={"garantia": False, "vence_el": None, "fecha_venta": None},
            ),
            _valid_ris_preflight(fake),
        ):
            response = self.client.post("/api/ingresos/nuevo/lote/", payload, format="json")

        self.assertEqual(response.status_code, 409, response.data)
        self.assertTrue(any(issue["code"] == "INGRESO_DOCUMENT_PROFILE_MISMATCH" for issue in response.data["issues"]))
        self.assertEqual(fake.ingresar_calls, 0)
        with connection.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM devices WHERE numero_serie IN (%s, %s)", ["SN-MIX-RDA", "SN-MIX-RIS"])
            self.assertEqual(cur.fetchone()[0], 0)

    def test_nuevo_ingreso_lote_bloquea_si_bejerman_ya_tiene_remito_para_equipo(self):
        today = timezone.localdate().isoformat()
        fake = FakeRisClient(
            sales_records=[
                {
                    "Comprobante_ID": "dup-remote-1",
                    "Comprobante_Tipo": "RIS",
                    "Comprobante_Letra": "R",
                    "Comprobante_PtoVenta": "00004",
                    "Comprobante_Numero": "00004580",
                    "Comprobante_FechaEmision": today,
                }
            ],
            sales_details={
                "dup-remote-1": {
                    "Comprobante_Tipo": "RIS",
                    "Comprobante_Letra": "R",
                    "Comprobante_PtoVenta": "00004",
                    "Comprobante_Numero": "00004580",
                    "Comprobante_FechaEmision": today,
                    "Comprobante_Items": [
                        {"Item_Tipo": "L", "Item_DescripArticulo": "OS 123 - Serie SN-DUP-REMOTE"},
                    ],
                }
            },
        )
        payload = self._lote_payload("SN-DUP-REMOTE")

        with (
            _bejerman_settings(),
            patch("service.views.ingresos_views._map_motivo_to_db_label", return_value="otros"),
            patch(
                "service.views.ingresos_views._compute_factory_warranty_from_bejerman_sale",
                return_value={"garantia": False, "vence_el": None, "fecha_venta": None},
            ),
            _valid_ris_preflight(fake),
        ):
            response = self.client.post("/api/ingresos/nuevo/lote/", payload, format="json")

        self.assertEqual(response.status_code, 409, response.data)
        self.assertTrue(any(issue["code"] == "INGRESO_REMITO_EQUIPMENT_DUPLICATE_REMOTE" for issue in response.data["issues"]))
        self.assertIn("RIS R 00004-00004580", response.data["issues"][-1]["message"])
        self.assertEqual(fake.ingresar_calls, 0)
        with connection.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM devices WHERE numero_serie = 'SN-DUP-REMOTE'")
            self.assertEqual(cur.fetchone()[0], 0)

    def test_nuevo_ingreso_lote_bloquea_si_nexora_ya_tiene_remito_para_equipo(self):
        existing_id = self._insert_ingreso(serial="SN-DUP-LOCAL", motivo="otros")
        today = timezone.localdate().isoformat()
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bejerman_ingreso_remitos (
                    ingreso_id,
                    status,
                    pdf_status,
                    document_mode,
                    comprobante_tipo,
                    comprobante_letra,
                    comprobante_pto_venta,
                    comprobante_numero,
                    remito_number,
                    company_key,
                    issue_date
                )
                VALUES (%s, 'generated', 'pending', 'emit', 'RIS', 'R', '00004', '00004581', 'RIS R 00004-00004581', 'SEPID', %s)
                """,
                [existing_id, today],
            )
        fake = FakeRisClient(number="00004582")
        payload = self._lote_payload("SN-DUP-LOCAL")

        with (
            _bejerman_settings(),
            patch("service.views.ingresos_views._map_motivo_to_db_label", return_value="otros"),
            patch(
                "service.views.ingresos_views._compute_factory_warranty_from_bejerman_sale",
                return_value={"garantia": False, "vence_el": None, "fecha_venta": None},
            ),
            _valid_ris_preflight(fake),
        ):
            response = self.client.post("/api/ingresos/nuevo/lote/", payload, format="json")

        self.assertEqual(response.status_code, 409, response.data)
        self.assertTrue(any(issue["code"] == "INGRESO_REMITO_EQUIPMENT_DUPLICATE_LOCAL" for issue in response.data["issues"]))
        self.assertIn("RIS R 00004-00004581", response.data["issues"][-1]["message"])
        self.assertEqual(fake.ingresar_calls, 0)
        with connection.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM devices WHERE numero_serie = 'SN-DUP-LOCAL'")
            self.assertEqual(cur.fetchone()[0], 1)

    def test_nuevo_ingreso_lote_registra_remito_manual_sin_pdf(self):
        fake = FakeRisClient()
        payload = self._lote_payload("SN-LOTE-REG-001", "SN-LOTE-REG-002")
        payload.update({"ris_mode": "register", "manual_remito_number": "00001-00026249"})

        with (
            _bejerman_settings(),
            patch("service.views.ingresos_views._map_motivo_to_db_label", return_value="otros"),
            patch(
                "service.views.ingresos_views._compute_factory_warranty_from_bejerman_sale",
                return_value={"garantia": False, "vence_el": None, "fecha_venta": None},
            ),
            _valid_ris_preflight(fake),
            patch("service.bejerman_ris.resolve_customer_document_fields", return_value=_ris_customer_fields()),
        ):
            response = self.client.post("/api/ingresos/nuevo/lote/", payload, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        ingreso_ids = response.data["ingreso_ids"]
        self.assertEqual(response.data["document_mode"], "register")
        self.assertEqual(response.data["pdf_status"], "not_applicable")
        self.assertEqual(response.data["remito_number"], "RIS R 00001-00026249")
        self.assertNotIn("pdf_url", response.data)
        self.assertNotIn("print_url", response.data)
        self.assertEqual(fake.ingresar_calls, 1)
        self.assertEqual(fake.ingresar_kwargs[0]["emite_reg"], "R")
        self.assertEqual(fake.ingresar_kwargs[0]["numera_flex"], "N")
        comprobante = fake.comprobantes[0]
        self.assertEqual(comprobante["Comprobante_Tipo"], "RIS")
        self.assertEqual(comprobante["Comprobante_Letra"], "R")
        self.assertEqual(comprobante["Comprobante_PtoVenta"], "00001")
        self.assertEqual(comprobante["Comprobante_Numero"], "00026249")
        self.assertEqual(comprobante["Comprobante_ActualizaStock"], "S")
        article_lines = [item for item in comprobante["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual(len(article_lines), 2)
        self.assertEqual([item["Item_CantidadUM1"] for item in article_lines], [-1, -1])
        self.assertEqual([item["Item_CantidadUM2"] for item in article_lines], [-1, -1])
        self.assertEqual({item["Item_Deposito"] for item in article_lines}, {"STR"})
        self.assertEqual([item["Item_Partida"] for item in article_lines], ["SN-LOTE-REG-001", "SN-LOTE-REG-002"])
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT r.document_mode, r.manual_remito_number, r.pdf_status, t.remito_ingreso
                  FROM bejerman_ingreso_remitos r
                  JOIN ingresos t ON t.id = r.ingreso_id
                 WHERE r.ingreso_id = ANY(%s)
                 ORDER BY r.ingreso_id
                """,
                [ingreso_ids],
            )
            rows = cur.fetchall()
        self.assertEqual(
            rows,
            [
                ("register", "RIS R 00001-00026249", "not_applicable", "RIS R 00001-00026249"),
                ("register", "RIS R 00001-00026249", "not_applicable", "RIS R 00001-00026249"),
            ],
        )

        pdf_response = self.client.get(f"/api/ingresos/{ingreso_ids[0]}/ris/pdf/")
        self.assertEqual(pdf_response.status_code, 409, pdf_response.data)
        self.assertIn("registrado", pdf_response.data["detail"])
        self.assertEqual(pdf_response.data["code"], "RIS_REGISTERED_NO_PDF")

    def test_cobranzas_pdf_remito_registrado_no_intenta_sdk_y_explica_causa(self):
        ingreso_id = self._insert_ingreso(serial="SN-REG-COD-PDF")
        issue_date = timezone.localdate().isoformat()
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bejerman_ingreso_remitos (
                    ingreso_id,
                    status,
                    pdf_status,
                    document_mode,
                    manual_remito_number,
                    comprobante_tipo,
                    comprobante_letra,
                    comprobante_pto_venta,
                    comprobante_numero,
                    remito_number,
                    company_key,
                    customer_code,
                    customer_name,
                    issue_date
                )
                VALUES (%s, 'generated', 'not_applicable', 'register', %s, 'RIS', 'R', '00001', '00026250',
                        %s, 'SEPID', 'CLI-BEJ', 'Cliente Bejerman', %s)
                """,
                [ingreso_id, "RIS R 00001-00026250", "RIS R 00001-00026250", issue_date],
            )
        document_id = build_document_id(
            {"t": "RIS", "l": "R", "p": "00001", "n": "00026250", "f": issue_date, "c": "CLI-BEJ"}
        )
        request = type(
            "Request",
            (),
            {
                "query_params": {"customerCode": "CLI-BEJ", "companyKey": "SEPID"},
                "user": self.admin,
            },
        )()

        with (
            patch("service.views.delivery_orders_views.resolve_remito_group_for_document_id", return_value=None),
            patch("service.views.delivery_orders_views.get_facturacion_pdf") as pdf_mock,
        ):
            response = CobranzasRemitoPdfView().get(request, document_id)

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["code"], "BEJERMAN_REMITO_REGISTERED_NO_PDF")
        self.assertIn("registrado manualmente", response.data["detail"])
        pdf_mock.assert_not_called()

    def test_nuevo_ingreso_lote_registrar_exige_numero_manual(self):
        fake = FakeRisClient()
        payload = self._lote_payload("SN-LOTE-REG-REQ")
        payload["ris_mode"] = "register"

        with (
            _bejerman_settings(),
            _valid_ris_preflight(fake),
            patch("service.views.ingresos_views._map_motivo_to_db_label", return_value="otros"),
            patch(
                "service.views.ingresos_views._compute_factory_warranty_from_bejerman_sale",
                return_value={"garantia": False, "vence_el": None, "fecha_venta": None},
            ),
        ):
            response = self.client.post("/api/ingresos/nuevo/lote/", payload, format="json")

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["document_mode"], "register")
        self.assertTrue(any(issue["code"] == "MANUAL_REMITO_REQUIRED" for issue in response.data["issues"]))
        self.assertEqual(fake.ingresar_calls, 0)
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                  FROM ingresos t
                  JOIN devices d ON d.id = t.device_id
                 WHERE d.numero_serie = 'SN-LOTE-REG-REQ'
                """
            )
            self.assertEqual(cur.fetchone()[0], 0)

    def test_nuevo_ingreso_lote_registrar_rechaza_numero_manual_sin_punto(self):
        fake = FakeRisClient()
        payload = self._lote_payload("SN-LOTE-REG-PARTIAL")
        payload.update({"ris_mode": "register", "manual_remito_number": "26249"})

        with (
            _bejerman_settings(),
            _valid_ris_preflight(fake),
            patch("service.views.ingresos_views._map_motivo_to_db_label", return_value="otros"),
            patch(
                "service.views.ingresos_views._compute_factory_warranty_from_bejerman_sale",
                return_value={"garantia": False, "vence_el": None, "fecha_venta": None},
            ),
        ):
            response = self.client.post("/api/ingresos/nuevo/lote/", payload, format="json")

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["document_mode"], "register")
        self.assertTrue(any(issue["code"] == "MANUAL_REMITO_INVALID" for issue in response.data["issues"]))
        self.assertIn("punto de venta", response.data["issues"][-1]["message"])
        self.assertEqual(fake.ingresar_calls, 0)
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                  FROM ingresos t
                  JOIN devices d ON d.id = t.device_id
                 WHERE d.numero_serie = 'SN-LOTE-REG-PARTIAL'
                """
            )
            self.assertEqual(cur.fetchone()[0], 0)

    def test_nuevo_ingreso_lote_registrar_acepta_punto_venta_manual_uno(self):
        fake = FakeRisClient()
        payload = self._lote_payload("SN-LOTE-REG-PV1")
        payload.update({"ris_mode": "register", "manual_remito_number": "RIS R 00001-00026249"})

        with (
            _bejerman_settings(),
            _valid_ris_preflight(fake),
            patch("service.views.ingresos_views._map_motivo_to_db_label", return_value="otros"),
            patch(
                "service.views.ingresos_views._compute_factory_warranty_from_bejerman_sale",
                return_value={"garantia": False, "vence_el": None, "fecha_venta": None},
            ),
            patch("service.bejerman_ris.resolve_customer_document_fields", return_value=_ris_customer_fields()),
        ):
            response = self.client.post("/api/ingresos/nuevo/lote/", payload, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["document_mode"], "register")
        self.assertEqual(response.data["remito_number"], "RIS R 00001-00026249")
        self.assertEqual(fake.ingresar_calls, 1)
        self.assertEqual(fake.comprobantes[0]["Comprobante_PtoVenta"], "00001")

    def test_nuevo_ingreso_lote_registrar_manual_pv1_no_consulta_duplicado_remoto(self):
        fake = FakeRisClient(
            sales_records=[
                {
                    "Comprobante_Tipo": "RIS",
                    "Comprobante_Letra": "R",
                    "Comprobante_PtoVenta": "00001",
                    "Comprobante_Numero": "00026249",
                }
            ],
            sales_error=BejermanSdkResponseError("Invalid object name 'expCuotasV'."),
        )
        payload = self._lote_payload("SN-LOTE-REG-MANUAL-DUP")
        payload.update({"ris_mode": "register", "manual_remito_number": "RIS R 00001-00026249"})

        with (
            _bejerman_settings(),
            _valid_ris_preflight(fake),
            patch("service.views.ingresos_views._map_motivo_to_db_label", return_value="otros"),
            patch(
                "service.views.ingresos_views._compute_factory_warranty_from_bejerman_sale",
                return_value={"garantia": False, "vence_el": None, "fecha_venta": None},
            ),
        ):
            response = self.client.post("/api/ingresos/nuevo/lote/", payload, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["document_mode"], "register")
        self.assertEqual(response.data["remito_number"], "RIS R 00001-00026249")
        self.assertEqual(fake.sales_list_calls, 0)
        self.assertEqual(fake.ingresar_calls, 1)

    def test_nuevo_ingreso_lote_registrar_rechaza_punto_venta_invalido(self):
        fake = FakeRisClient()
        payload = self._lote_payload("SN-LOTE-REG-PV2")
        payload.update({"ris_mode": "register", "manual_remito_number": "RIS R 00002-00026249"})

        with (
            _bejerman_settings(),
            _valid_ris_preflight(fake),
            patch("service.views.ingresos_views._map_motivo_to_db_label", return_value="otros"),
            patch(
                "service.views.ingresos_views._compute_factory_warranty_from_bejerman_sale",
                return_value={"garantia": False, "vence_el": None, "fecha_venta": None},
            ),
        ):
            response = self.client.post("/api/ingresos/nuevo/lote/", payload, format="json")

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["document_mode"], "register")
        self.assertTrue(any(issue["code"] == "MANUAL_REMITO_INVALID" for issue in response.data["issues"]))
        self.assertIn("00001 o 00004", response.data["issues"][-1]["message"])
        self.assertEqual(fake.ingresar_calls, 0)

    def test_nuevo_ingreso_lote_registrar_baja_alquiler_rechaza_ris_manual(self):
        fake = FakeRisClient()
        payload = self._lote_payload("SN-LOTE-REG-RDA")
        payload["items"][0]["motivo"] = "baja alquiler"
        payload.update({"ris_mode": "register", "manual_remito_number": "RIS R 00004-00026249"})

        with (
            _bejerman_settings(),
            _valid_ris_preflight(fake),
            patch("service.views.ingresos_views._map_motivo_to_db_label", return_value="baja alquiler"),
            patch(
                "service.views.ingresos_views._compute_factory_warranty_from_bejerman_sale",
                return_value={"garantia": False, "vence_el": None, "fecha_venta": None},
            ),
        ):
            response = self.client.post("/api/ingresos/nuevo/lote/", payload, format="json")

        self.assertEqual(response.status_code, 409, response.data)
        self.assertTrue(any(issue["code"] == "MANUAL_REMITO_INVALID" for issue in response.data["issues"]))
        self.assertIn("RDA R", response.data["issues"][-1]["message"])
        self.assertEqual(fake.ingresar_calls, 0)
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                  FROM ingresos t
                  JOIN devices d ON d.id = t.device_id
                 WHERE d.numero_serie = 'SN-LOTE-REG-RDA'
                """
            )
            self.assertEqual(cur.fetchone()[0], 0)

    def test_nuevo_ingreso_lote_registrar_asocia_remito_digital_existente(self):
        fake = FakeRisClient(
            sales_records=[
                {
                    "Comprobante_ID": "digital-26249",
                    "Comprobante_Tipo": "RIS",
                    "Comprobante_Letra": "R",
                    "Comprobante_PtoVenta": "00004",
                    "Comprobante_Numero": "00026249",
                }
            ],
            sales_details={
                "digital-26249": {
                    "Comprobante_Tipo": "RIS",
                    "Comprobante_Letra": "R",
                    "Comprobante_PtoVenta": "00004",
                    "Comprobante_Numero": "00026249",
                    "Comprobante_Items": [
                        {"Item_Tipo": "A", "Item_Partida": "SN-LOTE-REG-DUP-REMOTE"},
                    ],
                }
            },
        )
        payload = self._lote_payload("SN-LOTE-REG-DUP-REMOTE")
        payload.update({"ris_mode": "register", "manual_remito_number": "RIS R 00004-00026249"})

        with (
            _bejerman_settings(),
            _valid_ris_preflight(fake),
            patch("service.views.ingresos_views._map_motivo_to_db_label", return_value="otros"),
            patch(
                "service.views.ingresos_views._compute_factory_warranty_from_bejerman_sale",
                return_value={"garantia": False, "vence_el": None, "fecha_venta": None},
            ),
        ):
            response = self.client.post("/api/ingresos/nuevo/lote/", payload, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["document_mode"], "register")
        self.assertEqual(response.data["remito_number"], "RIS R 00004-00026249")
        self.assertEqual(fake.ingresar_calls, 0)
        self.assertEqual(fake.comprobantes, [])
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT r.document_mode, r.manual_remito_number, r.pdf_status, t.remito_ingreso
                  FROM ingresos t
                  JOIN bejerman_ingreso_remitos r ON r.ingreso_id = t.id
                  JOIN devices d ON d.id = t.device_id
                 WHERE d.numero_serie = 'SN-LOTE-REG-DUP-REMOTE'
                """
            )
            self.assertEqual(cur.fetchone(), ("register", "RIS R 00004-00026249", "not_applicable", "RIS R 00004-00026249"))

    def test_nuevo_ingreso_lote_registrar_asocia_remito_local_a_otro_equipo(self):
        existing_id = self._insert_ingreso(serial="SN-LOTE-REG-DUP-LOCAL-OLD")
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bejerman_ingreso_remitos (
                    ingreso_id,
                    status,
                    pdf_status,
                    document_mode,
                    manual_remito_number,
                    comprobante_tipo,
                    comprobante_letra,
                    comprobante_pto_venta,
                    comprobante_numero,
                    remito_number,
                    company_key
                )
                VALUES (%s, 'generated', 'not_applicable', 'register', %s, 'RIS', 'R', '00004', '00026249', %s, 'SEPID')
                """,
                [existing_id, "RIS R 00004-00026249", "RIS R 00004-00026249"],
            )
        fake = FakeRisClient()
        payload = self._lote_payload("SN-LOTE-REG-DUP-LOCAL")
        payload.update({"ris_mode": "register", "manual_remito_number": "RIS R 00004-00026249"})

        with (
            _bejerman_settings(),
            _valid_ris_preflight(fake),
            patch("service.views.ingresos_views._map_motivo_to_db_label", return_value="otros"),
            patch(
                "service.views.ingresos_views._compute_factory_warranty_from_bejerman_sale",
                return_value={"garantia": False, "vence_el": None, "fecha_venta": None},
            ),
        ):
            response = self.client.post("/api/ingresos/nuevo/lote/", payload, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["document_mode"], "register")
        self.assertEqual(response.data["remito_number"], "RIS R 00004-00026249")
        self.assertEqual(fake.ingresar_calls, 0)
        ingreso_ids = response.data["ingreso_ids"]
        self.assertEqual(len(ingreso_ids), 1)
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT d.numero_serie, r.document_mode, r.manual_remito_number, r.pdf_status, t.remito_ingreso
                  FROM ingresos t
                  JOIN devices d ON d.id = t.device_id
                 JOIN bejerman_ingreso_remitos r ON r.ingreso_id = t.id
                 WHERE t.id IN (%s, %s)
                 ORDER BY d.numero_serie
                """,
                [existing_id, ingreso_ids[0]],
            )
            rows = cur.fetchall()
        self.assertEqual(
            rows,
            [
                ("SN-LOTE-REG-DUP-LOCAL", "register", "RIS R 00004-00026249", "not_applicable", "RIS R 00004-00026249"),
                ("SN-LOTE-REG-DUP-LOCAL-OLD", "register", "RIS R 00004-00026249", "not_applicable", None),
            ],
        )

    def test_register_ris_batch_rechaza_cambiar_remito_de_ingreso_generado(self):
        ingreso_id = self._insert_ingreso(serial="SN-LOTE-REG-DIFF-LOCAL")
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bejerman_ingreso_remitos (
                    ingreso_id,
                    status,
                    pdf_status,
                    document_mode,
                    manual_remito_number,
                    comprobante_tipo,
                    comprobante_letra,
                    comprobante_pto_venta,
                    comprobante_numero,
                    remito_number,
                    company_key
                )
                VALUES (%s, 'generated', 'not_applicable', 'register', %s, 'RIS', 'R', '00004', '00026249', %s, 'SEPID')
                """,
                [ingreso_id, "RIS R 00004-00026249", "RIS R 00004-00026249"],
            )

        with (
            _bejerman_settings(),
            _valid_ris_preflight(FakeRisClient()),
            self.assertRaisesRegex(BejermanRisError, "ya tiene un RIS/remito generado"),
        ):
            register_ris_batch([ingreso_id], "RIS R 00004-00026250", user_id=self.admin.id)

        with connection.cursor() as cur:
            cur.execute(
                "SELECT remito_number, manual_remito_number FROM bejerman_ingreso_remitos WHERE ingreso_id = %s",
                [ingreso_id],
            )
            self.assertEqual(cur.fetchone(), ("RIS R 00004-00026249", "RIS R 00004-00026249"))

    def test_nuevo_ingreso_lote_duplicado_no_crea_parciales(self):
        existing_id = self._insert_ingreso(serial="SN-LOTE-DUP")
        payload = self._lote_payload("SN-LOTE-NEW", "SN-LOTE-DUP")

        with (
            _bejerman_settings(),
            _valid_ris_preflight(),
            patch("service.views.ingresos_views._map_motivo_to_db_label", return_value="otros"),
            patch(
                "service.views.ingresos_views._compute_factory_warranty_from_bejerman_sale",
                return_value={"garantia": False, "vence_el": None, "fecha_venta": None},
            ),
        ):
            response = self.client.post("/api/ingresos/nuevo/lote/", payload, format="json")

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["item_index"], 1)
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM ingresos t
                JOIN devices d ON d.id = t.device_id
                WHERE d.numero_serie IN ('SN-LOTE-NEW', 'SN-LOTE-DUP')
                """
            )
            self.assertEqual(cur.fetchone()[0], 1)
        self.assertEqual(existing_id, response.data["ingreso_id"])

    def test_nuevo_ingreso_lote_falla_ris_conserva_ingresos_con_estado_visible(self):
        payload = self._lote_payload("SN-LOTE-FAIL-1", "SN-LOTE-FAIL-2")

        with (
            _bejerman_settings(),
            patch("service.views.ingresos_views._map_motivo_to_db_label", return_value="otros"),
            patch(
                "service.views.ingresos_views._compute_factory_warranty_from_bejerman_sale",
                return_value={"garantia": False, "vence_el": None, "fecha_venta": None},
            ),
            _valid_ris_preflight(FakeRisClient()),
            patch(
                "service.bejerman_ris.resolve_customer_document_fields",
                side_effect=BejermanSdkResponseError("Cliente sin situación de IVA"),
            ),
        ):
            response = self.client.post("/api/ingresos/nuevo/lote/", payload, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertIn("No se pudo emitir RIS", response.data["detail"])
        ingreso_ids = response.data["ingreso_ids"]
        self.assertEqual(len(ingreso_ids), 2)
        with connection.cursor() as cur:
            cur.execute("SELECT status, last_error FROM bejerman_ingreso_remitos WHERE ingreso_id = ANY(%s)", [ingreso_ids])
            rows = cur.fetchall()
        self.assertEqual({row[0] for row in rows}, {"failed"})
        self.assertTrue(all("situación de IVA" in (row[1] or "") for row in rows))

    def test_ris_reintento_desde_ingreso_de_lote_reemite_lote_completo(self):
        payload = self._lote_payload("SN-LOTE-RETRY-1", "SN-LOTE-RETRY-2")

        with (
            _bejerman_settings(),
            patch("service.views.ingresos_views._map_motivo_to_db_label", return_value="otros"),
            patch(
                "service.views.ingresos_views._compute_factory_warranty_from_bejerman_sale",
                return_value={"garantia": False, "vence_el": None, "fecha_venta": None},
            ),
            _valid_ris_preflight(FakeRisClient()),
            patch(
                "service.bejerman_ris.resolve_customer_document_fields",
                side_effect=BejermanSdkResponseError("Cliente sin situación de IVA"),
            ),
        ):
            created = self.client.post("/api/ingresos/nuevo/lote/", payload, format="json")
        ingreso_ids = created.data["ingreso_ids"]
        fake = FakeRisClient(number="00004571")

        with (
            _bejerman_settings(),
            _valid_ris_preflight(fake),
            patch("service.bejerman_ris.resolve_customer_document_fields", return_value=_ris_customer_fields()),
        ):
            response = self.client.post(f"/api/ingresos/{ingreso_ids[1]}/ris/emitir/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(fake.ingresar_calls, 1)
        article_lines = [
            item for item in fake.comprobantes[0]["Comprobante_Items"] if item["Item_Tipo"] == "A"
        ]
        self.assertEqual(len(article_lines), 2)
        with connection.cursor() as cur:
            cur.execute("SELECT remito_ingreso FROM ingresos WHERE id = ANY(%s) ORDER BY id", [ingreso_ids])
            self.assertEqual([row[0] for row in cur.fetchall()], ["RIS R 00004-00004571", "RIS R 00004-00004571"])

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

    def test_baja_directa_encola_stock_to_desguace(self):
        ingreso_id = self._insert_ingreso(
            serial="SN-BAJA-ENDPOINT",
            numero_interno="MG 7788",
            customer_id=self.mg_customer_id,
            estado="ingresado",
        )

        response = self.client.post(f"/api/ingresos/{ingreso_id}/baja/", {}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data.get("estado"), "baja")
        self.assertEqual(response.data.get("bejerman_sync_job", {}).get("status"), "pending")
        job = self._job_row(ingreso_id, SYNC_TYPE_STOCK_TO_DESGUACE)
        self.assertIsNotNone(job)
        self.assertEqual(job["source_deposit"], "AUTO")
        self.assertEqual(job["target_deposit"], "DES")
        self.assertEqual(job["actor_user_id"], self.admin.id)
        self.assertIsNone(self._job_row(ingreso_id, SYNC_TYPE_STOCK_EXIT_RTS))

    def test_baja_mueve_partida_desde_stock_actual_hacia_des(self):
        for deposit in ("STR", "STL", "STC"):
            with self.subTest(deposit=deposit):
                serial = f"SN-BAJA-{deposit}"
                ingreso_id = self._insert_ingreso(
                    serial=serial,
                    numero_interno="MG 7790",
                    customer_id=self.mg_customer_id,
                )
                queued = enqueue_stock_baja_for_ingreso(ingreso_id, actor_user_id=self.admin.id)
                fake = FakeBejermanClient(
                    deposit_records={
                        "": [
                            {
                                "Comprobante_ArtPartida": serial,
                                "Comprobante_ArtDeposito": deposit,
                                "Comprobante_Art_CodGen": "ART-CPAP",
                                "Stock": 1,
                            }
                        ]
                    }
                )

                with _bejerman_settings():
                    stats = process_bejerman_jobs(job_id=queued["id"], client=fake)

                self.assertEqual(stats["succeeded"], 1)
                self.assertEqual(len(fake.movements), 2)
                sal = fake.movements[0][0]
                ent = fake.movements[1][0]
                self.assertEqual(sal["Comprobante_Tipo"], "SAL")
                self.assertEqual(sal["Comprobante_ArtDeposito"], deposit)
                self.assertEqual(sal["Comprobante_CantidadUM1"], -1)
                self.assertEqual(sal["Comprobante_CantidadUM2"], -1)
                self.assertEqual(sal["Comprobante_CodigoCausaEmision"], "DES")
                self.assertEqual(ent["Comprobante_Tipo"], "ENT")
                self.assertEqual(ent["Comprobante_ArtDeposito"], "DES")
                self.assertEqual(ent["Comprobante_CantidadUM1"], 1)
                self.assertEqual(ent["Comprobante_CantidadUM2"], 1)
                self.assertEqual(sal["Comprobante_ArtPartida"], serial)
                self.assertEqual(ent["Comprobante_ArtPartida"], serial)
                self.assertEqual(sal["Comprobante_Art_CodGen"], "ART-CPAP")
                self.assertEqual(ent["Comprobante_Art_CodGen"], "ART-CPAP")
                job = self._job_row(ingreso_id, SYNC_TYPE_STOCK_TO_DESGUACE)
                self.assertEqual(job["status"], "succeeded")
                request_payload = self._json_value(job["request_payload"])
                self.assertTrue(request_payload["sal_done"])
                self.assertTrue(request_payload["target_stock_entry_done"])
                self.assertEqual(request_payload["target_deposit"], "DES")
                self.assertEqual(request_payload["source_stock_deposit"], deposit)
                self.assertEqual(request_payload["causa_emision"], "DES")

    def test_baja_partida_ya_en_des_cierra_idempotente(self):
        ingreso_id = self._insert_ingreso(
            serial="SN-BAJA-DES",
            numero_interno="MG 7791",
            customer_id=self.mg_customer_id,
        )
        queued = enqueue_stock_baja_for_ingreso(ingreso_id, actor_user_id=self.admin.id)
        fake = FakeBejermanClient(
            deposit_records={
                "": [
                    {
                        "Comprobante_ArtPartida": "SN-BAJA-DES",
                        "Comprobante_ArtDeposito": "DES",
                        "Comprobante_Art_CodGen": "ART-DES",
                        "Stock": 1,
                    }
                ]
            }
        )

        with _bejerman_settings():
            stats = process_bejerman_jobs(job_id=queued["id"], client=fake)

        self.assertEqual(stats["succeeded"], 1)
        self.assertEqual(fake.movements, [])
        job = self._job_row(ingreso_id, SYNC_TYPE_STOCK_TO_DESGUACE)
        self.assertEqual(job["status"], "succeeded")
        request_payload = self._json_value(job["request_payload"])
        self.assertTrue(request_payload["idempotent"])
        self.assertEqual(request_payload["target_deposit"], "DES")

    def test_baja_sin_stock_bloquea_sin_emitir_movimientos(self):
        ingreso_id = self._insert_ingreso(
            serial="SN-BAJA-SIN-STOCK",
            numero_interno="MG 7792",
            customer_id=self.mg_customer_id,
        )
        queued = enqueue_stock_baja_for_ingreso(ingreso_id, actor_user_id=self.admin.id)
        fake = FakeBejermanClient(deposit_records={"": []})

        with _bejerman_settings():
            stats = process_bejerman_jobs(job_id=queued["id"], client=fake)

        self.assertEqual(stats["blocked"], 1)
        self.assertEqual(fake.movements, [])
        job = self._job_row(ingreso_id, SYNC_TYPE_STOCK_TO_DESGUACE)
        self.assertEqual(job["status"], "blocked")
        self.assertIn("No se encontró stock positivo", job["last_error"])

    def test_baja_con_stock_en_multiples_depositos_bloquea(self):
        ingreso_id = self._insert_ingreso(
            serial="SN-BAJA-MULTI",
            numero_interno="MG 7793",
            customer_id=self.mg_customer_id,
        )
        queued = enqueue_stock_baja_for_ingreso(ingreso_id, actor_user_id=self.admin.id)
        fake = FakeBejermanClient(
            deposit_records={
                "": [
                    {
                        "Comprobante_ArtPartida": "SN-BAJA-MULTI",
                        "Comprobante_ArtDeposito": "STR",
                        "Comprobante_Art_CodGen": "ART-CPAP",
                        "Stock": 1,
                    },
                    {
                        "Comprobante_ArtPartida": "SN-BAJA-MULTI",
                        "Comprobante_ArtDeposito": "STL",
                        "Comprobante_Art_CodGen": "ART-CPAP",
                        "Stock": 1,
                    },
                ]
            }
        )

        with _bejerman_settings():
            stats = process_bejerman_jobs(job_id=queued["id"], client=fake)

        self.assertEqual(stats["blocked"], 1)
        self.assertEqual(fake.movements, [])
        job = self._job_row(ingreso_id, SYNC_TYPE_STOCK_TO_DESGUACE)
        self.assertEqual(job["status"], "blocked")
        self.assertIn("múltiples depósitos", job["last_error"])

    def test_alta_directa_encola_stock_from_desguace(self):
        ingreso_id = self._insert_ingreso(
            serial="SN-ALTA-ENDPOINT",
            numero_interno="MG 7794",
            customer_id=self.mg_customer_id,
            estado="baja",
        )

        response = self.client.post(f"/api/ingresos/{ingreso_id}/alta/", {}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data.get("estado"), "ingresado")
        self.assertEqual(response.data.get("bejerman_sync_job", {}).get("status"), "pending")
        job = self._job_row(ingreso_id, SYNC_TYPE_STOCK_FROM_DESGUACE)
        self.assertIsNotNone(job)
        self.assertEqual(job["source_deposit"], "DES")
        self.assertEqual(job["target_deposit"], "STR")
        self.assertEqual(job["actor_user_id"], self.admin.id)

    def test_alta_mueve_partida_desde_des_hacia_str(self):
        ingreso_id = self._insert_ingreso(
            serial="SN-ALTA-DES",
            numero_interno="MG 7795",
            customer_id=self.mg_customer_id,
            estado="baja",
        )
        queued = enqueue_stock_alta_for_ingreso(ingreso_id, actor_user_id=self.admin.id)
        fake = FakeBejermanClient(
            deposit_records={
                "": [
                    {
                        "Comprobante_ArtPartida": "SN-ALTA-DES",
                        "Comprobante_ArtDeposito": "DES",
                        "Comprobante_Art_CodGen": "ART-CPAP",
                        "Stock": 1,
                    }
                ]
            }
        )

        with _bejerman_settings():
            stats = process_bejerman_jobs(job_id=queued["id"], client=fake)

        self.assertEqual(stats["succeeded"], 1)
        self.assertEqual(len(fake.movements), 2)
        sal = fake.movements[0][0]
        ent = fake.movements[1][0]
        self.assertEqual(sal["Comprobante_Tipo"], "SAL")
        self.assertEqual(sal["Comprobante_ArtDeposito"], "DES")
        self.assertEqual(sal["Comprobante_CantidadUM1"], -1)
        self.assertEqual(sal["Comprobante_CantidadUM2"], -1)
        self.assertEqual(sal["Comprobante_CodigoCausaEmision"], "ALQ")
        self.assertEqual(ent["Comprobante_Tipo"], "ENT")
        self.assertEqual(ent["Comprobante_ArtDeposito"], "STR")
        self.assertEqual(ent["Comprobante_CantidadUM1"], 1)
        self.assertEqual(ent["Comprobante_CantidadUM2"], 1)
        self.assertNotIn("Comprobante_CodigoCausaEmision", ent)
        self.assertEqual(sal["Comprobante_ArtPartida"], "SN-ALTA-DES")
        self.assertEqual(ent["Comprobante_ArtPartida"], "SN-ALTA-DES")
        self.assertEqual(sal["Comprobante_Art_CodGen"], "ART-CPAP")
        self.assertEqual(ent["Comprobante_Art_CodGen"], "ART-CPAP")
        job = self._job_row(ingreso_id, SYNC_TYPE_STOCK_FROM_DESGUACE)
        self.assertEqual(job["status"], "succeeded")
        request_payload = self._json_value(job["request_payload"])
        self.assertTrue(request_payload["sal_done"])
        self.assertTrue(request_payload["target_stock_entry_done"])
        self.assertEqual(request_payload["source_stock_deposit"], "DES")
        self.assertEqual(request_payload["target_deposit"], "STR")
        self.assertEqual(request_payload["causa_emision"], "ALQ")

    def test_alta_partida_ya_en_str_cierra_idempotente(self):
        ingreso_id = self._insert_ingreso(
            serial="SN-ALTA-STR",
            numero_interno="MG 7796",
            customer_id=self.mg_customer_id,
            estado="baja",
        )
        queued = enqueue_stock_alta_for_ingreso(ingreso_id, actor_user_id=self.admin.id)
        fake = FakeBejermanClient(
            deposit_records={
                "": [
                    {
                        "Comprobante_ArtPartida": "SN-ALTA-STR",
                        "Comprobante_ArtDeposito": "STR",
                        "Comprobante_Art_CodGen": "ART-STR",
                        "Stock": 1,
                    }
                ]
            }
        )

        with _bejerman_settings():
            stats = process_bejerman_jobs(job_id=queued["id"], client=fake)

        self.assertEqual(stats["succeeded"], 1)
        self.assertEqual(fake.movements, [])
        job = self._job_row(ingreso_id, SYNC_TYPE_STOCK_FROM_DESGUACE)
        self.assertEqual(job["status"], "succeeded")
        request_payload = self._json_value(job["request_payload"])
        self.assertTrue(request_payload["idempotent"])
        self.assertEqual(request_payload["target_deposit"], "STR")

    def test_alta_sin_stock_bloquea_sin_emitir_movimientos(self):
        ingreso_id = self._insert_ingreso(
            serial="SN-ALTA-SIN-STOCK",
            numero_interno="MG 7797",
            customer_id=self.mg_customer_id,
            estado="baja",
        )
        queued = enqueue_stock_alta_for_ingreso(ingreso_id, actor_user_id=self.admin.id)
        fake = FakeBejermanClient(deposit_records={"": []})

        with _bejerman_settings():
            stats = process_bejerman_jobs(job_id=queued["id"], client=fake)

        self.assertEqual(stats["blocked"], 1)
        self.assertEqual(fake.movements, [])
        job = self._job_row(ingreso_id, SYNC_TYPE_STOCK_FROM_DESGUACE)
        self.assertEqual(job["status"], "blocked")
        self.assertIn("No se encontró stock positivo", job["last_error"])

    def test_alta_con_stock_en_otro_deposito_bloquea(self):
        ingreso_id = self._insert_ingreso(
            serial="SN-ALTA-STL",
            numero_interno="MG 7798",
            customer_id=self.mg_customer_id,
            estado="baja",
        )
        queued = enqueue_stock_alta_for_ingreso(ingreso_id, actor_user_id=self.admin.id)
        fake = FakeBejermanClient(
            deposit_records={
                "": [
                    {
                        "Comprobante_ArtPartida": "SN-ALTA-STL",
                        "Comprobante_ArtDeposito": "STL",
                        "Comprobante_Art_CodGen": "ART-CPAP",
                        "Stock": 1,
                    }
                ]
            }
        )

        with _bejerman_settings():
            stats = process_bejerman_jobs(job_id=queued["id"], client=fake)

        self.assertEqual(stats["blocked"], 1)
        self.assertEqual(fake.movements, [])
        job = self._job_row(ingreso_id, SYNC_TYPE_STOCK_FROM_DESGUACE)
        self.assertEqual(job["status"], "blocked")
        self.assertIn("se esperaba DES", job["last_error"])

    def test_alta_con_stock_en_multiples_depositos_bloquea(self):
        ingreso_id = self._insert_ingreso(
            serial="SN-ALTA-MULTI",
            numero_interno="MG 7799",
            customer_id=self.mg_customer_id,
            estado="baja",
        )
        queued = enqueue_stock_alta_for_ingreso(ingreso_id, actor_user_id=self.admin.id)
        fake = FakeBejermanClient(
            deposit_records={
                "": [
                    {
                        "Comprobante_ArtPartida": "SN-ALTA-MULTI",
                        "Comprobante_ArtDeposito": "DES",
                        "Comprobante_Art_CodGen": "ART-CPAP",
                        "Stock": 1,
                    },
                    {
                        "Comprobante_ArtPartida": "SN-ALTA-MULTI",
                        "Comprobante_ArtDeposito": "STR",
                        "Comprobante_Art_CodGen": "ART-CPAP",
                        "Stock": 1,
                    },
                ]
            }
        )

        with _bejerman_settings():
            stats = process_bejerman_jobs(job_id=queued["id"], client=fake)

        self.assertEqual(stats["blocked"], 1)
        self.assertEqual(fake.movements, [])
        job = self._job_row(ingreso_id, SYNC_TYPE_STOCK_FROM_DESGUACE)
        self.assertEqual(job["status"], "blocked")
        self.assertIn("múltiples depósitos", job["last_error"])

    def test_alta_reintento_con_sal_done_emite_solo_ent_str(self):
        ingreso_id = self._insert_ingreso(
            serial="SN-ALTA-SAL-DONE",
            numero_interno="MG 7800",
            customer_id=self.mg_customer_id,
            estado="baja",
        )
        queued = enqueue_stock_alta_for_ingreso(ingreso_id, actor_user_id=self.admin.id)
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE bejerman_sync_jobs
                   SET request_payload=%s::jsonb,
                       response_payload=%s::jsonb,
                       article_code=%s
                 WHERE id=%s
                """,
                [
                    json.dumps(
                        {
                            "sal_done": True,
                            "alta_phase": "sal_done",
                            "article_code": "ART-ALTA",
                            "source_stock_deposit": "DES",
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps({"sal": {"Resultado": "OK"}}, ensure_ascii=False),
                    "ART-ALTA",
                    queued["id"],
                ],
            )
        fake = FakeBejermanClient(deposit_records={"": []})

        with _bejerman_settings():
            stats = process_bejerman_jobs(job_id=queued["id"], client=fake)

        self.assertEqual(stats["succeeded"], 1)
        self.assertEqual(len(fake.movements), 1)
        ent = fake.movements[0][0]
        self.assertEqual(ent["Comprobante_Tipo"], "ENT")
        self.assertEqual(ent["Comprobante_ArtDeposito"], "STR")
        self.assertEqual(ent["Comprobante_ArtPartida"], "SN-ALTA-SAL-DONE")
        self.assertEqual(ent["Comprobante_Art_CodGen"], "ART-ALTA")

    def test_job_stl_legacy_de_cliente_se_cierra_sin_emitir_movimientos(self):
        ingreso_id = self._insert_ingreso(serial="SN-LEGACY-CLIENT", numero_interno="")
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

        ingreso_id = self._enqueue_job(serial="SN-ARTICLE-SINGLE")
        fake = FakeBejermanClient(
            deposit_records={"STR": []},
            articles=[
                {"Art_CodGenerico": "ART-UNICO", "Art_DescripcionGeneral": "ResMed AirSense 10 CPAP"},
            ],
        )

        with _bejerman_settings():
            stats = process_bejerman_jobs(client=fake)

        self.assertEqual(stats["blocked"], 1)
        single_job = self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL)
        self.assertEqual(single_job["status"], "blocked")
        self.assertFalse(single_job["article_code"])
        self.assertIn("Confirmalo manualmente", single_job["last_error"])
        single_payload = self._json_value(single_job["response_payload"])
        self.assertEqual(len(single_payload["candidates"]), 1)
        self.assertEqual(fake.movements, [])
        with connection.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM bejerman_article_mappings WHERE article_code = %s",
                ["ART-UNICO"],
            )
            self.assertEqual(cur.fetchone()[0], 0)

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

    def test_controlado_sin_defecto_devolucion_demo_encola_str_a_val(self):
        ingreso_id = self._insert_ingreso(
            serial="SN-DEMO-CONTROL",
            estado="en_reparacion",
            ubicacion_id=self.taller_id,
            motivo="devolución demo",
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_token(self.jefe)}")

        response = self.client.post(f"/api/ingresos/{ingreso_id}/controlado-sin-defecto/", {}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL))
        self.assertIsNone(self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STC))
        job = self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_VAL)
        self.assertIsNotNone(job)
        self.assertEqual(job["source_deposit"], "STR")
        self.assertEqual(job["target_deposit"], "VAL")

    def test_controlado_sin_defecto_devolucion_demo_convertido_mg_prioriza_stl(self):
        ingreso_id = self._insert_ingreso(
            serial="SN-DEMO-MG-CONTROL",
            numero_interno="MG 0009",
            estado="en_reparacion",
            ubicacion_id=self.taller_id,
            customer_id=self.mg_customer_id,
            motivo="devolución demo",
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_token(self.jefe)}")

        response = self.client.post(f"/api/ingresos/{ingreso_id}/controlado-sin-defecto/", {}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        job = self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL)
        self.assertIsNotNone(job)
        self.assertEqual(job["target_deposit"], "STL")
        self.assertIsNone(self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_VAL))

    def test_codigo_mg_activo_de_cliente_encola_str_a_stl_al_quedar_listo(self):
        ingreso_id = self._insert_ingreso(
            serial="SN-CLIENT-MG-CODE",
            numero_interno="MG 0007",
            estado="en_reparacion",
            ubicacion_id=self.taller_id,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_token(self.jefe)}")

        response = self.client.post(f"/api/ingresos/{ingreso_id}/reparado/", {}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        job = self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL)
        self.assertIsNotNone(job)
        self.assertEqual(job["source_deposit"], "STR")
        self.assertEqual(job["target_deposit"], "STL")

    def test_ce_activo_reparado_encola_str_a_stl(self):
        ingreso_id = self._insert_ingreso(
            serial="SN-CE-READY",
            numero_interno="CE 0002",
            estado="en_reparacion",
            ubicacion_id=self.taller_id,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_token(self.jefe)}")

        response = self.client.post(f"/api/ingresos/{ingreso_id}/reparado/", {}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        job = self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL)
        self.assertIsNotNone(job)
        self.assertEqual(job["target_deposit"], "STL")

    def test_n_de_control_mg_activo_encola_str_a_stl(self):
        ingreso_id = self._insert_ingreso(
            serial="SN-CONTROL-MG",
            n_de_control="MG 2562",
            estado="en_reparacion",
            ubicacion_id=self.taller_id,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_token(self.jefe)}")

        response = self.client.post(f"/api/ingresos/{ingreso_id}/reparado/", {}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        job = self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL)
        self.assertIsNotNone(job)
        self.assertEqual(job["source_deposit"], "STR")
        self.assertEqual(job["target_deposit"], "STL")

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

    def test_worker_usa_empresa_del_job_mgbio(self):
        ingreso_id = self._insert_ingreso(
            serial="SN-MGBIO-WORKER",
            numero_interno="MG 3010",
            customer_id=self.mg_customer_id,
            empresa_bejerman="MGBIO",
        )
        event_id = self._insert_liberado_event(ingreso_id)
        enqueue_stock_transfer_for_ingreso(ingreso_id, event_id, actor_user_id=self.admin.id)
        job = self._job_row(ingreso_id, SYNC_TYPE_STOCK_STR_TO_STL)
        self.assertEqual(job["company_key"], "MGBIO")

        fake = FakeBejermanClient(
            source_records=[
                {
                    "Comprobante_ArtPartida": "SN-MGBIO-WORKER",
                    "Comprobante_ArtDeposito": "STR",
                    "Comprobante_Art_CodGen": "ART-MGBIO",
                    "Stock": 1,
                }
            ],
            target_records=[],
        )

        with _bejerman_settings(), patch("service.bejerman_sync.BejermanSDKClient", return_value=fake) as client_cls:
            stats = process_bejerman_jobs(limit=1)

        self.assertEqual(stats["succeeded"], 1)
        client_cls.assert_called_once_with(company_key="MGBIO", actor_user_id=self.admin.id)
        self.assertEqual(fake.movements[1][0]["Comprobante_ArtDeposito"], "STL")

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

    def test_stock_fix_dry_run_detecta_duplicado_mismo_deposito(self):
        self._insert_ingreso(serial="SN-STOCK-DUP", numero_interno="MG 6100")
        fake = FakeBejermanClient(
            deposit_records={
                "": [
                    {
                        "Art_Partida": "SN-STOCK-DUP",
                        "Art_CodDeposito": "STR",
                        "Art_CodGen": "ART-CPAP",
                        "Art_DispUM1": 2,
                    }
                ]
            }
        )

        with _bejerman_settings():
            stats = audit_duplicate_stock(client=fake, serials=["SN-STOCK-DUP"], run_id="RUN-DUP")

        self.assertEqual(stats["checked"], 1)
        self.assertEqual(stats["auto_candidates"], 1)
        self.assertEqual(stats["needs_approval"], 0)
        item = stats["items"][0]
        self.assertEqual(item["status"], "auto_candidate")
        self.assertEqual(item["problem"], "same_deposit_quantity")
        self.assertEqual(item["articulo"], "ART-CPAP")
        self.assertEqual(item["deposito_a_reducir"], "STR")
        self.assertEqual(item["cantidad"], "1")
        self.assertEqual(item["action"], "sal_err")

    def test_stock_fix_no_aplica_multi_deposito_sin_aprobacion(self):
        self._insert_ingreso(serial="SN-STOCK-MULTI", numero_interno="MG 6101")
        fake = FakeBejermanClient(
            deposit_records={
                "": [
                    {
                        "Art_Partida": "SN-STOCK-MULTI",
                        "Art_CodDeposito": "STL",
                        "Art_CodGen": "ART-CPAP",
                        "Art_DispUM1": 1,
                    },
                    {
                        "Art_Partida": "SN-STOCK-MULTI",
                        "Art_CodDeposito": "STR",
                        "Art_CodGen": "ART-CPAP",
                        "Art_DispUM1": 1,
                    },
                ]
            }
        )

        with _bejerman_settings():
            stats = apply_duplicate_stock_corrections(client=fake, serials=["SN-STOCK-MULTI"], run_id="RUN-MULTI")

        self.assertEqual(stats["issues"], 1)
        self.assertEqual(stats["needs_approval"], 1)
        self.assertEqual(stats["applied"], 0)
        self.assertEqual(fake.movements, [])

    def test_stock_fix_emite_sal_err_para_duplicado_mismo_deposito(self):
        self._insert_ingreso(serial="SN-STOCK-APPLY", numero_interno="MG 6102")
        fake = FakeBejermanClient(
            deposit_records={
                "": [
                    {
                        "Art_Partida": "SN-STOCK-APPLY",
                        "Art_CodDeposito": "STR",
                        "Art_CodGen": "ART-CPAP",
                        "Art_DispUM1": 2,
                    }
                ]
            }
        )

        with _bejerman_settings():
            stats = apply_duplicate_stock_corrections(client=fake, serials=["SN-STOCK-APPLY"], run_id="RUN-APPLY")

        self.assertEqual(stats["applied"], 1)
        self.assertEqual(len(fake.movements), 1)
        payload = fake.movements[0][0]
        self.assertEqual(payload["Comprobante_Tipo"], "SAL")
        self.assertEqual(payload["Comprobante_CodigoCausaEmision"], "ERR")
        self.assertEqual(payload["Comprobante_ArtDeposito"], "STR")
        self.assertEqual(payload["Comprobante_Art_CodGen"], "ART-CPAP")
        self.assertEqual(payload["Comprobante_ArtPartida"], "SN-STOCK-APPLY")
        self.assertEqual(payload["Comprobante_CantidadUM1"], -1)
        self.assertEqual(payload["Comprobante_CantidadUM2"], -1)
        self.assertTrue(payload["Comprobante_IdOrigen"].startswith("NEXORA-STOCKFIX-RUN-APPLY-"))

    def test_stock_fix_saltea_si_cambio_el_stock_antes_de_aplicar(self):
        self._insert_ingreso(serial="SN-STOCK-CHANGED", numero_interno="MG 6103")

        class ChangingStockClient:
            def __init__(self):
                self.calls = 0
                self.movements = []

            def stock_by_deposit_partida(self, deposit, serial):
                self.calls += 1
                quantity = 2 if self.calls == 1 else 1
                return {
                    "Resultado": "OK",
                    "DatosJSON": json.dumps(
                        [
                            {
                                "Art_Partida": serial,
                                "Art_CodDeposito": "STR",
                                "Art_CodGen": "ART-CPAP",
                                "Art_DispUM1": quantity,
                            }
                        ]
                    ),
                }

            def ingresar_lista_comprobantes_json(self, comprobantes):
                self.movements.append(comprobantes)
                return {"Resultado": "OK", "DatosJSON": "{}"}

        fake = ChangingStockClient()

        with _bejerman_settings():
            stats = apply_duplicate_stock_corrections(client=fake, serials=["SN-STOCK-CHANGED"], run_id="RUN-CHANGED")

        self.assertEqual(stats["applied"], 0)
        self.assertEqual(stats["blocked"], 1)
        self.assertEqual(fake.movements, [])
        self.assertEqual(stats["items"][0]["problem"], "stock_changed")

    def test_stock_fix_aplica_multi_deposito_con_csv_aprobado(self):
        self._insert_ingreso(serial="SN-STOCK-APPROVED", numero_interno="MG 6104")
        fake = FakeBejermanClient(
            deposit_records={
                "": [
                    {
                        "Art_Partida": "SN-STOCK-APPROVED",
                        "Art_CodDeposito": "STL",
                        "Art_CodGen": "ART-CPAP",
                        "Art_DispUM1": 1,
                    },
                    {
                        "Art_Partida": "SN-STOCK-APPROVED",
                        "Art_CodDeposito": "STR",
                        "Art_CodGen": "ART-CPAP",
                        "Art_DispUM1": 1,
                    },
                ]
            }
        )
        correction = ApprovedStockCorrection(
            numero_serie="SN-STOCK-APPROVED",
            articulo="ART-CPAP",
            deposito_a_reducir="STL",
            cantidad=1,
            motivo="Depósito correcto aprobado por Cobranzas",
            aprobado_por="Cobranzas",
        )

        with _bejerman_settings():
            stats = apply_approved_stock_corrections([correction], client=fake, run_id="RUN-APPROVED")

        self.assertEqual(stats["applied"], 1)
        self.assertEqual(len(fake.movements), 1)
        payload = fake.movements[0][0]
        self.assertEqual(payload["Comprobante_Tipo"], "SAL")
        self.assertEqual(payload["Comprobante_CodigoCausaEmision"], "ERR")
        self.assertEqual(payload["Comprobante_ArtDeposito"], "STL")
        self.assertEqual(payload["Comprobante_CantidadUM1"], -1)

    def test_restauracion_destino_bloquea_si_la_partida_ya_tiene_stock_en_otro_deposito(self):
        ingreso_id = self._enqueue_job(serial="SN-RESTORE-DUP-GUARD")
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
        fake = FakeBejermanClient(
            source_records=[
                {
                    "Comprobante_ArtPartida": "SN-RESTORE-DUP-GUARD",
                    "Comprobante_ArtDeposito": "STR",
                    "Comprobante_Art_CodGen": "ART-RESTORE",
                    "Stock": 1,
                }
            ],
            target_records=[],
        )

        with _bejerman_settings():
            stats = restore_target_stock_from_jobs(client=fake)

        self.assertEqual(stats["checked"], 1)
        self.assertEqual(stats["blocked"], 1)
        self.assertEqual(fake.movements, [])
        job = self._job_row(ingreso_id)
        self.assertEqual(job["status"], "blocked")
        self.assertIn("ya tiene stock positivo", job["last_error"])

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

    def test_endpoint_remitos_lista_registro_manual_fallido(self):
        ingreso_id = self._insert_ingreso(serial="SN-RDA-ENDPOINT", numero_interno="MG 1234")
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bejerman_ingreso_remitos(
                    ingreso_id, status, pdf_status, document_mode, manual_remito_number,
                    remito_number, comprobante_tipo, comprobante_letra, comprobante_pto_venta,
                    comprobante_numero, attempts, last_error, request_payload, company_key
                )
                VALUES (%s,'failed','not_applicable','register',%s,%s,'RDA','R','00001','00026285',1,%s,%s::jsonb,'SEPID')
                RETURNING id
                """,
                [
                    ingreso_id,
                    "RDA R 00001-00026285",
                    "RDA R 00001-00026285",
                    "Invalid object name 'expCuotasV'",
                    json.dumps({"ingresoIds": [ingreso_id]}, ensure_ascii=False),
                ],
            )
            remito_id = int(cur.fetchone()[0])

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_token(self.jefe)}")
        response = self.client.get("/api/bejerman/remitos/?status=failed&q=00026285")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["counters"]["failed"], 1)
        self.assertEqual(response.data["items"][0]["id"], remito_id)
        self.assertEqual(response.data["items"][0]["source"], "ingreso")
        self.assertEqual(response.data["items"][0]["operation_label"], "Registrar remito")
        self.assertTrue(response.data["items"][0]["retryable"])
        self.assertIn("expCuotasV", response.data["items"][0]["last_error"])

    def test_endpoint_remitos_muestra_bridge_error_de_orden_entrega(self):
        with connection.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS bejerman_remito_groups (
                  id                         TEXT PRIMARY KEY,
                  company_key                TEXT NULL,
                  comprobante_tipo           TEXT NOT NULL,
                  comprobante_letra          TEXT NOT NULL DEFAULT 'R',
                  comprobante_pto_venta      TEXT NULL,
                  comprobante_numero         TEXT NULL,
                  remito_number              TEXT NULL,
                  customer_code              TEXT NOT NULL,
                  customer_name              TEXT NOT NULL,
                  seller_code                TEXT NOT NULL,
                  payment_term_code          TEXT NOT NULL,
                  operation_code             TEXT NOT NULL,
                  deposit_code               TEXT NOT NULL,
                  status                     TEXT NOT NULL DEFAULT 'pending',
                  order_ids                  JSONB NOT NULL DEFAULT '[]'::jsonb,
                  response_summary           JSONB NOT NULL DEFAULT '{}'::jsonb,
                  created_at                 TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  generated_at               TIMESTAMPTZ NULL
                )
                """
            )
            cur.execute(
                """
                INSERT INTO bejerman_remito_groups(
                    id, company_key, comprobante_tipo, comprobante_letra, comprobante_pto_venta,
                    customer_code, customer_name, seller_code, payment_term_code,
                    operation_code, deposit_code, status, order_ids, response_summary
                )
                VALUES (
                    'brg-test-bridge-error', 'SEPID', 'RSS', 'R', '00004',
                    'C001', 'Cliente prueba', 'V001', 'CC',
                    'REP', 'STC', 'failed', '[]'::jsonb, %s::jsonb
                )
                """,
                [
                    json.dumps(
                        {
                            "message": "BEJERMAN_BRIDGE_RESPONSE_ERROR",
                            "bridgeError": "El comprobante no se importó por partida inexistente.",
                            "status": 502,
                        },
                        ensure_ascii=False,
                    )
                ],
            )

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_token(self.jefe)}")
        response = self.client.get("/api/bejerman/remitos/?status=failed&q=partida inexistente")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["items"][0]["id"], "brg-test-bridge-error")
        self.assertEqual(response.data["items"][0]["source"], "orden_entrega")
        self.assertIn("partida inexistente", response.data["items"][0]["last_error"])
        self.assertTrue(response.data["items"][0]["attempted_at"])
        self.assertEqual(response.data["items"][0]["updated_at"], response.data["items"][0]["attempted_at"])

    def test_endpoint_remitos_reintenta_registro_manual_fallido(self):
        ingreso_id = self._insert_ingreso(serial="SN-RDA-RETRY", numero_interno="MG 5678")
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bejerman_ingreso_remitos(
                    ingreso_id, status, pdf_status, document_mode, manual_remito_number,
                    remito_number, comprobante_tipo, comprobante_letra, comprobante_pto_venta,
                    comprobante_numero, attempts, last_error, request_payload, company_key
                )
                VALUES (%s,'failed','not_applicable','register',%s,%s,'RDA','R','00001','00026286',1,%s,%s::jsonb,'SEPID')
                RETURNING id
                """,
                [
                    ingreso_id,
                    "RDA R 00001-00026286",
                    "RDA R 00001-00026286",
                    "Invalid object name 'expCuotasV'",
                    json.dumps({"ingresoIds": [ingreso_id]}, ensure_ascii=False),
                ],
            )
            remito_id = int(cur.fetchone()[0])

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_token(self.jefe)}")
        with patch("service.views.bejerman_views.register_ris_batch", return_value={"status": "generated"}) as retry_mock:
            response = self.client.post(f"/api/bejerman/remitos/ingreso/{remito_id}/retry/", {}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["ok"])
        retry_mock.assert_called_once_with([ingreso_id], "RDA R 00001-00026286", user_id=self.jefe.id)

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

    def test_busqueda_y_mapeo_por_modelo_variante_usa_permiso_preflight(self):
        fake = FakeBejermanClient(
            articles=[
                {
                    "Art_CodGenerico": "ART-G2",
                    "Art_DescripcionGeneral": "ResMed AirSense 10 CPAP G2",
                    "Art_StockPorPartida": "S",
                    "Art_ParticipaCircuitoStock": "S",
                    "Art_Tipo": "1",
                }
            ]
        )

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_token(self.recepcion)}")
        with _bejerman_settings(), patch("service.views.bejerman_views.BejermanSDKClient", return_value=fake):
            response = self.client.get(f"/api/bejerman/articles/?model_id={self.model_id}&variante=G2&q=AirSense")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["context"]["model_id"], self.model_id)
        self.assertEqual(response.data["context"]["variante"], "G2")
        self.assertEqual(response.data["items"][0]["article_code"], "ART-G2")

        with _bejerman_settings(), patch("service.views.bejerman_views.BejermanSDKClient", return_value=fake):
            response = self.client.post(
                "/api/bejerman/article-mappings/",
                {
                    "model_id": self.model_id,
                    "variante": "G2",
                    "article_code": "ART-G2",
                    "article_description": "Artículo G2",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["mapping"]["article_code"], "ART-G2")
        self.assertEqual(response.data["mapping"]["variante"], "G2")

        response = self.client.get(f"/api/bejerman/article-mappings/?model_id={self.model_id}")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["items"][0]["article_code"], "ART-G2")

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

    def test_listado_bejerman_filtra_y_muestra_empresa(self):
        sepid_ingreso_id = self._insert_ingreso(
            serial="SN-COMPANY-SEPID-JOB",
            numero_interno="MG 1111",
            customer_id=self.mg_customer_id,
            empresa_bejerman="SEPID",
        )
        mgbio_ingreso_id = self._insert_ingreso(
            serial="SN-COMPANY-MGBIO-JOB",
            numero_interno="MG 2222",
            customer_id=self.mg_customer_id,
            empresa_bejerman="MGBIO",
        )
        enqueue_stock_transfer_for_ingreso(sepid_ingreso_id, self._insert_liberado_event(sepid_ingreso_id))
        enqueue_stock_transfer_for_ingreso(mgbio_ingreso_id, self._insert_liberado_event(mgbio_ingreso_id))

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_token(self.jefe)}")
        response = self.client.get("/api/bejerman/jobs/?company_key=MGBIO")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([item["ingreso_id"] for item in response.data["items"]], [mgbio_ingreso_id])
        self.assertEqual(response.data["items"][0]["company_key"], "MGBIO")
        self.assertEqual(response.data["items"][0]["company_label"], "MG BIO")

    def test_listado_bejerman_exige_permiso_de_pagina_especifico(self):
        response = self.client.get("/api/bejerman/jobs/")
        self.assertEqual(response.status_code, 403)

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_token(self.jefe)}")
        response = self.client.get("/api/bejerman/jobs/")
        self.assertEqual(response.status_code, 200, response.data)
