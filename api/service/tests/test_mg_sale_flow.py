from unittest import skipUnless
from unittest.mock import patch

from django.core import mail
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from service.auth import issue_token
from service.models import User


@skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL")
class MgSaleFlowAPITest(TestCase):
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
                CREATE TABLE IF NOT EXISTS audit_log (
                    id BIGSERIAL PRIMARY KEY,
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
                CREATE TABLE IF NOT EXISTS user_permission_overrides (
                    user_id INTEGER NOT NULL,
                    permission_code TEXT NOT NULL,
                    effect TEXT NOT NULL,
                    PRIMARY KEY (user_id, permission_code)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS customers (
                    id BIGSERIAL PRIMARY KEY,
                    cod_empresa TEXT,
                    razon_social TEXT NOT NULL,
                    telefono TEXT
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
                    marca_id INTEGER REFERENCES marcas(id),
                    nombre TEXT NOT NULL,
                    tipo_equipo TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS locations (
                    id BIGSERIAL PRIMARY KEY,
                    nombre TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    id BIGSERIAL PRIMARY KEY,
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
                    id BIGSERIAL PRIMARY KEY,
                    device_id INTEGER NOT NULL REFERENCES devices(id),
                    estado TEXT,
                    motivo TEXT,
                    fecha_ingreso TIMESTAMPTZ NULL,
                    fecha_creacion TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    ubicacion_id INTEGER NULL REFERENCES locations(id),
                    informe_preliminar TEXT,
                    accesorios TEXT,
                    comentarios TEXT,
                    garantia_reparacion BOOLEAN,
                    garantia_fabrica BOOLEAN,
                    remito_ingreso TEXT
                )
                """
            )
            cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS fecha_entrega TIMESTAMPTZ NULL")
            cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS remito_salida TEXT")
            cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS factura_numero TEXT")
            cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS alquilado BOOLEAN NOT NULL DEFAULT FALSE")
            cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS alquiler_a TEXT")
            cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS alquiler_remito TEXT")
            cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS alquiler_fecha DATE")
            cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS recibido_por INTEGER NULL")
            cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS asignado_a INTEGER NULL")
            cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS equipo_variante TEXT")
            cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS propietario_nombre TEXT")
            cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS propietario_contacto TEXT")
            cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS propietario_doc TEXT")
            cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS garantia_fabrica BOOLEAN")
            cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS etiq_garantia_ok BOOLEAN")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ingreso_events (
                    id BIGSERIAL PRIMARY KEY,
                    ticket_id INTEGER,
                    ingreso_id INTEGER,
                    de_estado TEXT,
                    a_estado TEXT,
                    usuario_id INTEGER,
                    ts TIMESTAMPTZ,
                    comentario TEXT
                )
                """
            )
        call_command("apply_mg_sale_schema")
        call_command("apply_ticket_sale_states_schema")
        call_command("apply_historical_corrections_schema")
        call_command("apply_notifications_schema", verbosity=0)
        super().setUpClass()

    @classmethod
    def _last_id(cls, cur, table):
        cur.execute("SELECT currval(pg_get_serial_sequence(%s, 'id'))", [table])
        return int(cur.fetchone()[0])

    @classmethod
    def setUpTestData(cls):
        with connection.cursor() as cur:
            cur.execute("DELETE FROM device_mg_events")
            cur.execute("DELETE FROM ingreso_events")
            cur.execute("DELETE FROM user_permission_overrides")
            cur.execute("DELETE FROM ingresos")
            cur.execute("DELETE FROM devices")
            cur.execute("DELETE FROM models")
            cur.execute("DELETE FROM marcas")
            cur.execute("DELETE FROM locations")
            cur.execute("DELETE FROM customers")
        User.objects.all().delete()

        cls.user = User.objects.create(
            nombre="Admin MG",
            email="admin-mg@example.com",
            hash_pw="",
            rol="admin",
            activo=True,
        )
        cls.token = issue_token(cls.user)

        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO customers(cod_empresa, razon_social, telefono) VALUES (%s,%s,%s)",
                ["MGBIO", "MG BIO", ""],
            )
            cls.customer_mg_id = cls._last_id(cur, "customers")
            cur.execute(
                "INSERT INTO customers(cod_empresa, razon_social, telefono) VALUES (%s,%s,%s)",
                ["CLI001", "Clinica Demo", "123"],
            )
            cls.customer_cli_id = cls._last_id(cur, "customers")

            cur.execute("INSERT INTO marcas(nombre) VALUES (%s)", ["ResMed"])
            cls.marca_id = cls._last_id(cur, "marcas")
            cur.execute(
                "INSERT INTO models(marca_id, nombre, tipo_equipo) VALUES (%s,%s,%s)",
                [cls.marca_id, "AirSense 10", "CPAP"],
            )
            cls.model_id = cls._last_id(cur, "models")
            cur.execute("INSERT INTO locations(nombre) VALUES (%s)", ["Taller"])
            cls.taller_id = cls._last_id(cur, "locations")
            cur.execute("INSERT INTO locations(nombre) VALUES (%s)", ["-"])
            cls.dash_id = cls._last_id(cur, "locations")

            cur.execute(
                """
                INSERT INTO devices(
                    customer_id, marca_id, model_id, numero_serie, numero_interno, alquilado, mg_estado
                ) VALUES (%s,%s,%s,%s,%s,false,'activo')
                """,
                [cls.customer_cli_id, cls.marca_id, cls.model_id, "NS-MG-9001", "MG 9001"],
            )
            cls.device_id = cls._last_id(cur, "devices")

            cur.execute(
                """
                INSERT INTO devices(
                    customer_id, marca_id, model_id, numero_serie, numero_interno, alquilado, mg_estado
                ) VALUES (%s,%s,%s,%s,%s,false,'activo')
                """,
                [cls.customer_cli_id, cls.marca_id, cls.model_id, "NS-MG-9002", "MG 9002"],
            )
            cls.device_id_2 = cls._last_id(cur, "devices")

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.token}")

    def _url_venta(self, device_id):
        return f"/api/equipos/{device_id}/mg/venta/"

    def _url_reactivar(self, device_id):
        return f"/api/equipos/{device_id}/mg/reactivar/"

    def _url_nuevo_ingreso(self):
        return "/api/ingresos/nuevo/"

    def _url_entregar(self, ingreso_id):
        return f"/api/ingresos/{ingreso_id}/entregar/"

    def _create_ingreso(self, *, device_id, estado, alquilado=False):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ingresos(
                    device_id, estado, motivo, fecha_ingreso, ubicacion_id,
                    informe_preliminar, accesorios, comentarios,
                    garantia_reparacion, garantia_fabrica, remito_ingreso,
                    alquilado, alquiler_a, alquiler_remito, alquiler_fecha
                )
                VALUES (%s,%s,'reparación',now(),%s,'Test','','',false,false,'',
                        %s,%s,%s,%s)
                """,
                [
                    device_id,
                    estado,
                    self.taller_id,
                    alquilado,
                    "Clinica Demo" if alquilado else None,
                    "REM-ALQ" if alquilado else None,
                    "2026-03-01" if alquilado else None,
                ],
            )
            return self._last_id(cur, "ingresos")

    def _base_nuevo_payload(self, *, numero_serie, numero_interno):
        return {
            "cliente": {"id": self.customer_cli_id},
            "equipo": {
                "marca_id": self.marca_id,
                "modelo_id": self.model_id,
                "numero_serie": numero_serie,
                "numero_interno": numero_interno,
                "garantia": False,
            },
            "motivo": "otros",
            "informe_preliminar": "Test",
            "comentarios": "",
            "garantia_reparacion": False,
            "accesorios_items": [],
            "propietario": {"nombre": "", "contacto": "", "doc": ""},
            "fecha_ingreso": "2026-03-10",
            "remito_ingreso": "",
            "ubicacion_id": self.taller_id,
        }

    def test_mg_venta_acepta_solo_factura(self):
        resp = self.client.post(
            self._url_venta(self.device_id),
            {
                "factura_numero": "FAC-001",
                "venta_customer_id": self.customer_cli_id,
                "source": "equipos",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["device"]["mg_estado"], "inactivo_venta")
        self.assertTrue(resp.data["device"]["mg_inactivo_venta"])

    def test_mg_venta_acepta_solo_remito(self):
        resp = self.client.post(
            self._url_venta(self.device_id_2),
            {
                "remito_numero": "REM-001",
                "venta_customer_id": self.customer_cli_id,
                "source": "equipos",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["device"]["mg_estado"], "inactivo_venta")

    def test_mg_venta_rechaza_sin_comprador(self):
        resp = self.client.post(
            self._url_venta(self.device_id),
            {"factura_numero": "FAC-001", "source": "equipos"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("conflict_type"), "MG_VENTA_COMPRADOR_REQUERIDO")

    def test_mg_venta_rechaza_sin_comprobante(self):
        resp = self.client.post(
            self._url_venta(self.device_id),
            {"venta_customer_id": self.customer_cli_id, "source": "equipos"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("conflict_type"), "MG_VENTA_COMPROBANTE_REQUERIDO")

    def test_mg_venta_rechaza_doble_venta(self):
        first = self.client.post(
            self._url_venta(self.device_id),
            {
                "factura_numero": "FAC-111",
                "venta_customer_id": self.customer_cli_id,
                "source": "equipos",
            },
            format="json",
        )
        self.assertEqual(first.status_code, 200)
        second = self.client.post(
            self._url_venta(self.device_id),
            {
                "factura_numero": "FAC-112",
                "venta_customer_id": self.customer_cli_id,
                "source": "equipos",
            },
            format="json",
        )
        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.data.get("conflict_type"), "MG_YA_INACTIVO_VENTA")

    def test_mg_venta_guarda_comprador_y_numero_alternativo(self):
        resp = self.client.post(
            self._url_venta(self.device_id),
            {
                "factura_numero": "FAC-ALT-1",
                "venta_customer_id": self.customer_cli_id,
                "venta_numero_alternativo": "INT-COMP-123",
                "source": "equipos",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["device"].get("mg_venta_customer_id"), self.customer_cli_id)
        self.assertEqual(resp.data["device"].get("mg_venta_numero_alternativo"), "INT-COMP-123")

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT mg_venta_customer_id, mg_venta_numero_alternativo
                  FROM devices
                 WHERE id=%s
                """,
                [self.device_id],
            )
            drow = cur.fetchone()
            cur.execute(
                """
                SELECT venta_customer_id, venta_numero_alternativo
                  FROM device_mg_events
                 WHERE device_id=%s AND accion='venta'
                 ORDER BY id DESC
                 LIMIT 1
                """,
                [self.device_id],
            )
            erow = cur.fetchone()
        self.assertIsNotNone(drow)
        self.assertEqual(int(drow[0]), self.customer_cli_id)
        self.assertEqual(drow[1], "INT-COMP-123")
        self.assertIsNotNone(erow)
        self.assertEqual(int(erow[0]), self.customer_cli_id)
        self.assertEqual(erow[1], "INT-COMP-123")

    @override_settings(
        BAJA_NOTIFY_RECIPIENTS=["patrimonio@example.com"],
        PATRIMONIAL_NOTIFY_RECIPIENTS=[],
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    )
    def test_mg_venta_envia_aviso_patrimonial(self):
        mail.outbox = []
        resp = self.client.post(
            self._url_venta(self.device_id),
            {
                "factura_numero": "FAC-MAIL-1",
                "remito_numero": "REM-MAIL-1",
                "venta_customer_id": self.customer_cli_id,
                "observaciones": "Vendido con garantía",
                "source": "equipos",
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["patrimonio@example.com"])
        self.assertIn("venta de equipo MG", message.subject)
        self.assertIn("MG 9001", message.body)
        self.assertIn("FAC-MAIL-1", message.body)
        self.assertIn("REM-MAIL-1", message.body)
        self.assertIn("Clinica Demo", message.body)
        self.assertIn("Vendido con garantía", message.body)

        info = resp.data.get("patrimonial_email") or {}
        self.assertEqual(info.get("status"), "sent")
        self.assertEqual(info.get("event"), "venta_mg")
        self.assertEqual(info.get("recipients"), ["patrimonio@example.com"])

    @override_settings(
        BAJA_NOTIFY_RECIPIENTS=["patrimonio@example.com"],
        PATRIMONIAL_NOTIFY_RECIPIENTS=[],
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    )
    def test_mg_venta_desde_hoja_de_servicio_incluye_os(self):
        mail.outbox = []
        ingreso_id = self._create_ingreso(device_id=self.device_id, estado="ingresado")
        resp = self.client.post(
            self._url_venta(self.device_id),
            {
                "factura_numero": "FAC-MAIL-2",
                "venta_customer_id": self.customer_cli_id,
                "ingreso_id": ingreso_id,
                "source": "service_sheet",
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(f"- OS: {str(ingreso_id).zfill(5)}", mail.outbox[0].body)

    def test_mg_reactivacion_registra_evento(self):
        sold = self.client.post(
            self._url_venta(self.device_id),
            {
                "factura_numero": "FAC-200",
                "venta_customer_id": self.customer_cli_id,
                "source": "equipos",
            },
            format="json",
        )
        self.assertEqual(sold.status_code, 200)
        reactivated = self.client.post(
            self._url_reactivar(self.device_id),
            {"observaciones": "Corrección", "source": "equipos"},
            format="json",
        )
        self.assertEqual(reactivated.status_code, 200)
        self.assertEqual(reactivated.data["device"]["mg_estado"], "activo")

        with connection.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM device_mg_events WHERE device_id=%s AND accion='reactivacion'",
                [self.device_id],
            )
            count = int(cur.fetchone()[0] or 0)
        self.assertGreaterEqual(count, 1)

    def test_nuevo_ingreso_rechaza_mg_inactivo(self):
        sold = self.client.post(
            self._url_venta(self.device_id),
            {
                "factura_numero": "FAC-300",
                "venta_customer_id": self.customer_cli_id,
                "source": "equipos",
            },
            format="json",
        )
        self.assertEqual(sold.status_code, 200)

        payload = self._base_nuevo_payload(numero_serie="NS-MG-9001", numero_interno="MG 9001")
        with patch("service.views.ingresos_views.preflight_ris_for_request_payload", return_value={"can_emit": True}):
            resp = self.client.post(self._url_nuevo_ingreso(), payload, format="json")
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(resp.data.get("conflict_type"), "MG_INACTIVO_VENTA", resp.data)

    def test_nuevo_ingreso_por_ns_sigue_funcionando(self):
        sold = self.client.post(
            self._url_venta(self.device_id),
            {
                "remito_numero": "REM-301",
                "venta_customer_id": self.customer_cli_id,
                "source": "equipos",
            },
            format="json",
        )
        self.assertEqual(sold.status_code, 200)

        payload = self._base_nuevo_payload(numero_serie="NS-MG-9001", numero_interno="")
        with patch("service.views.ingresos_views.preflight_ris_for_request_payload", return_value={"can_emit": True}):
            resp = self.client.post(self._url_nuevo_ingreso(), payload, format="json")
        self.assertIn(resp.status_code, (200, 201), resp.data)
        self.assertTrue(bool(resp.data.get("ingreso_id")))

    def test_venta_mg_alquilado_desde_hoja_queda_vendido_entregado(self):
        ingreso_id = self._create_ingreso(device_id=self.device_id, estado="alquilado", alquilado=True)
        with connection.cursor() as cur:
            cur.execute("UPDATE devices SET alquilado=true WHERE id=%s", [self.device_id])

        resp = self.client.post(
            self._url_venta(self.device_id),
            {
                "factura_numero": "FAC-ALQ-1",
                "remito_numero": "REM-ALQ-1",
                "venta_customer_id": self.customer_cli_id,
                "source": "service_sheet",
                "ingreso_id": ingreso_id,
                "fecha_venta": "2026-03-25",
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 200)
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT estado, alquilado, ubicacion_id, factura_numero, remito_salida, fecha_entrega
                  FROM ingresos
                 WHERE id=%s
                """,
                [ingreso_id],
            )
            ingreso = cur.fetchone()
            cur.execute("SELECT alquilado, mg_estado FROM devices WHERE id=%s", [self.device_id])
            device = cur.fetchone()
            cur.execute(
                "SELECT COUNT(*) FROM ingreso_events WHERE ticket_id=%s AND a_estado='vendido_entregado'",
                [ingreso_id],
            )
            event_count = int(cur.fetchone()[0] or 0)

        self.assertEqual(ingreso[0], "vendido_entregado")
        self.assertFalse(bool(ingreso[1]))
        self.assertEqual(int(ingreso[2]), int(self.dash_id))
        self.assertEqual(ingreso[3], "FAC-ALQ-1")
        self.assertEqual(ingreso[4], "REM-ALQ-1")
        self.assertIsNotNone(ingreso[5])
        self.assertFalse(bool(device[0]))
        self.assertEqual(device[1], "inactivo_venta")
        self.assertGreaterEqual(event_count, 1)

    def test_venta_mg_no_alquilado_queda_pendiente_y_entrega_cierra_venta(self):
        ingreso_id = self._create_ingreso(device_id=self.device_id_2, estado="liberado", alquilado=False)

        sold = self.client.post(
            self._url_venta(self.device_id_2),
            {
                "factura_numero": "FAC-VTA-2",
                "venta_customer_id": self.customer_cli_id,
                "source": "service_sheet",
                "ingreso_id": ingreso_id,
                "fecha_venta": "2026-03-25",
            },
            format="json",
        )

        self.assertEqual(sold.status_code, 200)
        with connection.cursor() as cur:
            cur.execute(
                "SELECT estado, fecha_entrega, factura_numero, ubicacion_id FROM ingresos WHERE id=%s",
                [ingreso_id],
            )
            pending = cur.fetchone()
        self.assertEqual(pending[0], "vendido_pendiente_entrega")
        self.assertIsNone(pending[1])
        self.assertEqual(pending[2], "FAC-VTA-2")
        # El equipo todavía está acá esperando el RT: la OS tiene que quedar en el taller.
        # Mandarla a '-' -como sí corresponde cuando la venta ya se entregó- la sacaba de
        # todas las colas, que filtran por ubicación 'taller'.
        self.assertEqual(int(pending[3]), int(self.taller_id))

        delivered = self.client.post(
            self._url_entregar(ingreso_id),
            {"remito_salida": "REM-VTA-2", "fecha_entrega": "2026-03-26"},
            format="json",
        )

        self.assertEqual(delivered.status_code, 200)
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT estado, alquilado, remito_salida, factura_numero, fecha_entrega
                  FROM ingresos
                 WHERE id=%s
                """,
                [ingreso_id],
            )
            ingreso = cur.fetchone()
            cur.execute(
                "SELECT COUNT(*) FROM ingreso_events WHERE ticket_id=%s AND a_estado='vendido_entregado'",
                [ingreso_id],
            )
            event_count = int(cur.fetchone()[0] or 0)

        self.assertEqual(ingreso[0], "vendido_entregado")
        self.assertFalse(bool(ingreso[1]))
        self.assertEqual(ingreso[2], "REM-VTA-2")
        self.assertEqual(ingreso[3], "FAC-VTA-2")
        self.assertIsNotNone(ingreso[4])
        self.assertGreaterEqual(event_count, 1)

    def test_venta_sobre_os_ya_entregada_no_la_reabre(self):
        """Caso OS 29534: entregada el 04/09/2026, la venta registrada el 07/09 la mandaba
        de vuelta a 'vendido_pendiente_entrega' con el equipo ya en poder del cliente."""
        ingreso_id = self._create_ingreso(device_id=self.device_id_2, estado="entregado", alquilado=False)
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE ingresos
                   SET fecha_entrega = %s, remito_salida = %s
                 WHERE id = %s
                """,
                ["2026-09-04", "RSS R 00004-00009999", ingreso_id],
            )

        resp = self.client.post(
            self._url_venta(self.device_id_2),
            {
                "factura_numero": "FAC-VTA-3",
                "remito_numero": "REM-VTA-3",
                "venta_customer_id": self.customer_cli_id,
                "source": "service_sheet",
                "ingreso_id": ingreso_id,
                "fecha_venta": "2026-09-07",
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 200)
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT estado, ubicacion_id, remito_salida, fecha_entrega
                  FROM ingresos
                 WHERE id=%s
                """,
                [ingreso_id],
            )
            ingreso = cur.fetchone()

        self.assertEqual(ingreso[0], "vendido_entregado")
        self.assertEqual(int(ingreso[1]), int(self.dash_id))
        # La entrega real no se pisa con los datos de la venta.
        self.assertEqual(ingreso[2], "RSS R 00004-00009999")
        self.assertEqual(ingreso[3].date().isoformat(), "2026-09-04")

    def _vender_desde_equipos(self, device_id, **extra):
        payload = {
            "remito_numero": "RTN R 00004-00004033",
            "venta_customer_id": self.customer_cli_id,
            "source": "equipos",
            "fecha_venta": "2026-04-06",
            **extra,
        }
        return self.client.post(self._url_venta(device_id), payload, format="json")

    def test_venta_desde_equipos_saca_la_os_de_la_estanteria(self):
        """Caso OS 29131 (14/09/2026): la venta cargada desde Equipos -o por la planilla MADRE-
        no tocaba la OS, que quedaba "controlado sin defecto" en la estantería de alquiler con el
        equipo ya vendido, afuera de todas las colas."""
        ingreso_id = self._create_ingreso(device_id=self.device_id_2, estado="controlado_sin_defecto")
        with connection.cursor() as cur:
            cur.execute("INSERT INTO locations(nombre) VALUES (%s)", ["Estantería de Alquiler"])
            estanteria_id = self._last_id(cur, "locations")
            cur.execute("UPDATE ingresos SET ubicacion_id=%s WHERE id=%s", [estanteria_id, ingreso_id])

        resp = self._vender_desde_equipos(self.device_id_2)

        self.assertEqual(resp.status_code, 200, resp.data)
        with connection.cursor() as cur:
            cur.execute("SELECT estado, ubicacion_id FROM ingresos WHERE id=%s", [ingreso_id])
            estado, ubicacion_id = cur.fetchone()
            cur.execute(
                "SELECT ingreso_id FROM device_mg_events WHERE device_id=%s AND accion='venta' ORDER BY id DESC LIMIT 1",
                [self.device_id_2],
            )
            evento_ingreso_id = cur.fetchone()[0]
        # El equipo sigue acá hasta que salga con su RT o RTN: la OS va al Taller y aparece en
        # Liberados, no se queda en la estantería ofreciendo un equipo vendido.
        self.assertEqual(estado, "vendido_pendiente_entrega")
        self.assertEqual(int(ubicacion_id), int(self.taller_id))
        self.assertEqual(int(evento_ingreso_id), ingreso_id)

    def test_venta_desde_equipos_no_toca_una_reparacion_en_curso(self):
        ingreso_id = self._create_ingreso(device_id=self.device_id_2, estado="diagnosticado")

        resp = self._vender_desde_equipos(self.device_id_2)

        self.assertEqual(resp.status_code, 200, resp.data)
        with connection.cursor() as cur:
            cur.execute("SELECT estado, ubicacion_id FROM ingresos WHERE id=%s", [ingreso_id])
            estado, ubicacion_id = cur.fetchone()
        self.assertEqual(estado, "diagnosticado")
        self.assertEqual(int(ubicacion_id), int(self.taller_id))

    def test_salida_con_rtn_de_aporte_cierra_vendida_sin_mover_stock(self):
        """Los aportes a NOVAMED salen con RTN, que no se factura: marcado como venta cierra la OS
        y NEXORA no encola ningún pase de stock, porque el RTN ya lo movió en Bejerman."""
        ingreso_id = self._create_ingreso(device_id=self.device_id_2, estado="controlado_sin_defecto")

        with patch("service.views.ingresos_views.enqueue_stock_transfer_for_ingreso") as alquiler, patch(
            "service.views.ingresos_views.enqueue_client_ready_transfer_for_ingreso"
        ) as cliente, patch("service.views.ingresos_views.enqueue_demo_ready_transfer_for_ingreso") as demo:
            resp = self.client.post(
                self._url_entregar(ingreso_id),
                {"remito_salida": "RTN R 00004-00004033", "fecha_entrega": "2026-04-06", "venta": True},
                format="json",
            )

        self.assertEqual(resp.status_code, 200, resp.data)
        with connection.cursor() as cur:
            cur.execute("SELECT estado, remito_salida FROM ingresos WHERE id=%s", [ingreso_id])
            estado, remito_salida = cur.fetchone()
        self.assertEqual(estado, "vendido_entregado")
        self.assertEqual(remito_salida, "RTN R 00004-00004033")
        alquiler.assert_not_called()
        cliente.assert_not_called()
        demo.assert_not_called()

    def test_la_marca_de_venta_rechaza_un_remito_que_no_es_rt_ni_rtn(self):
        ingreso_id = self._create_ingreso(device_id=self.device_id_2, estado="controlado_sin_defecto")

        resp = self.client.post(
            self._url_entregar(ingreso_id),
            {"remito_salida": "RSS R 00004-00004833", "venta": True},
            format="json",
        )

        self.assertEqual(resp.status_code, 400)
        with connection.cursor() as cur:
            cur.execute("SELECT estado FROM ingresos WHERE id=%s", [ingreso_id])
            self.assertEqual(cur.fetchone()[0], "controlado_sin_defecto")
