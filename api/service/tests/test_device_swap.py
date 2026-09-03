"""Cambio de equipo (permuta / parte de pago).

El caso que protegen estos tests: el cliente trae su equipo, se le entrega otro y el
suyo queda para SEPID. Bajo esa OS no sale nada para el cliente, así que NO se mueve el
stock a STC ni se emite el remito RSS. Antes se hacían las dos cosas, y encima con la
serie del equipo equivocado (el que se quedaba en el taller).

La segunda modalidad —`sin_entrega`— es la parte de pago sin permuta: tomamos el equipo
del cliente y no le entregamos ninguno. Mismo movimiento patrimonial y mismo corte de
stock, sin equipo de salida y por lo tanto sin serie que verificar al entregar.
"""

from unittest import skipUnless
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from service.auth import issue_token
from service.models import User
from service.permission_catalog import get_role_defaults


class DeviceSwapPermissionDefaultsTest(TestCase):
    """La matriz de roles, sin tocar la base."""

    def test_recepcion_registra_el_cambio_sin_poder_cerrar_la_reparacion(self):
        recepcion = get_role_defaults("recepcion")
        self.assertTrue(recepcion["action.ingreso.device_swap"])
        # El punto del permiso propio: recepción NO recibe la pestaña de diagnóstico.
        self.assertFalse(recepcion["action.ingreso.repair_transitions"])
        self.assertFalse(recepcion["action.ingreso.edit_diagnosis"])

    def test_administracion_y_jefatura_tambien_lo_tienen(self):
        for rol in ("admin", "jefe", "jefe_veedor", "supervisor"):
            with self.subTest(rol=rol):
                self.assertTrue(get_role_defaults(rol)["action.ingreso.device_swap"])

    def test_los_roles_que_no_atienden_el_mostrador_no_lo_tienen(self):
        for rol in ("tecnico", "cobranzas", "logistica"):
            with self.subTest(rol=rol):
                self.assertFalse(get_role_defaults(rol)["action.ingreso.device_swap"])


class DeviceSwapSetupMixin:
    """Esquema y datos compartidos por los dos casos de prueba."""

    @classmethod
    def setUpClass(cls):
        # Las tablas base las siembra `test_bejerman_sync` (los modelos son managed=False y
        # Django no crea nada). Acá sólo se agregan las columnas propias del cambio, igual
        # que hacen los demás módulos. Ver la receta de tests del proyecto.
        #
        # El outbox de notificaciones no lo crea ningún fixture y cerrar la OS dispara los
        # avisos: sin esta tabla, 11 tests revientan con un error de esquema que parece del
        # código. (`equipos_derivados` sigue faltando, pero su comando sólo crea el índice:
        # la tabla vive en `sql/schema.sql` y arrastra `proveedores_externos` y el enum.)
        call_command("apply_notifications_schema", verbosity=0)
        with connection.cursor() as cur:
            cur.execute(
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS device_cambio_id "
                "INTEGER NULL REFERENCES devices(id) ON DELETE SET NULL"
            )
            cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS cambio_destino_viejo TEXT NULL")
            cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS cambio_fecha TIMESTAMPTZ NULL")
            cur.execute(
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS cambio_registrado_por "
                "INTEGER NULL REFERENCES users(id) ON DELETE SET NULL"
            )
            cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS serial_cambio TEXT")
            cur.execute(
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS cambio_sin_entrega "
                "BOOLEAN NOT NULL DEFAULT FALSE"
            )
            # Quién era el dueño antes de que el equipo pasara a MG BIO.
            cur.execute(
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS cliente_original_id "
                "INTEGER NULL REFERENCES customers(id) ON DELETE SET NULL"
            )
            cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS resolucion TEXT")
            cur.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS tipo_equipo TEXT")
            cur.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS alquilado BOOLEAN NOT NULL DEFAULT FALSE")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS bejerman_article_mappings (
                    id BIGSERIAL PRIMARY KEY,
                    model_id INTEGER NOT NULL,
                    variante TEXT NOT NULL DEFAULT '',
                    article_code TEXT NOT NULL
                )
                """
            )
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        User.objects.all().delete()
        cls.recepcion = User.objects.create(
            nombre="Recepcion Mostrador",
            email="recepcion-swap@example.com",
            hash_pw="",
            rol="recepcion",
            activo=True,
        )
        cls.tecnico = User.objects.create(
            nombre="Tecnico Taller",
            email="tecnico-swap@example.com",
            hash_pw="",
            rol="tecnico",
            activo=True,
        )
        # Entregar exige `action.ingreso.edit_delivery`, que recepción NO tiene: los
        # tests de entrega usan administración. (Que recepción no pueda marcar entregado
        # es un hueco previo a este trabajo, ajeno al cambio de equipo.)
        cls.admin = User.objects.create(
            nombre="Admin Entrega",
            email="admin-swap@example.com",
            hash_pw="",
            rol="admin",
            activo=True,
        )
        cls.tokens = {
            "recepcion": issue_token(cls.recepcion),
            "tecnico": issue_token(cls.tecnico),
            "admin": issue_token(cls.admin),
        }

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        with connection.cursor() as cur:
            cur.execute("DELETE FROM ingreso_events")
            cur.execute("DELETE FROM ingresos")
            cur.execute("DELETE FROM devices")
            cur.execute("DELETE FROM customers")

            cur.execute("INSERT INTO customers (razon_social) VALUES ('Cliente Final') RETURNING id")
            self.customer_id = cur.fetchone()[0]

            # `_mg_owner_customer_id()` lo busca por razón social con LIKE '%mg%bio%'.
            cur.execute("INSERT INTO customers (razon_social) VALUES ('MG BIO SRL') RETURNING id")
            self.mg_owner_id = cur.fetchone()[0]

            # El equipo que trae el cliente.
            cur.execute(
                """
                INSERT INTO devices (customer_id, numero_serie, tipo_equipo)
                VALUES (%s, 'VIEJO-001', 'CPAP') RETURNING id
                """,
                [self.customer_id],
            )
            self.device_viejo = cur.fetchone()[0]

            # El que se le entrega: nuestro, libre, sin ingreso abierto.
            cur.execute(
                """
                INSERT INTO devices (customer_id, numero_serie, numero_interno, tipo_equipo)
                VALUES (%s, 'NUEVO-002', 'MG 0500', 'CPAP') RETURNING id
                """,
                [self.customer_id],
            )
            self.device_nuevo = cur.fetchone()[0]

            cur.execute(
                """
                INSERT INTO ingresos (device_id, estado, motivo, resolucion)
                VALUES (%s, 'reparado', 'reparación', NULL) RETURNING id
                """,
                [self.device_viejo],
            )
            self.ingreso_id = cur.fetchone()[0]

            # Artículo 110001 mapeado a un modelo: es el camino feliz del fichado.
            cur.execute("DELETE FROM bejerman_article_mappings")
            cur.execute("INSERT INTO marcas (nombre) VALUES ('Longfian') RETURNING id")
            marca_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO models (marca_id, nombre, tipo_equipo) VALUES (%s,'JAY-5','CPAP') RETURNING id",
                [marca_id],
            )
            self.model_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO bejerman_article_mappings (model_id, article_code) VALUES (%s,'110001')",
                [self.model_id],
            )

    def _auth(self, rol):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.tokens[rol]}")

    def _ingreso(self):
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT resolucion, serial_cambio, device_cambio_id, cambio_destino_viejo,
                       cambio_sin_entrega
                  FROM ingresos WHERE id=%s
                """,
                [self.ingreso_id],
            )
            cols = [c[0] for c in cur.description]
            return dict(zip(cols, cur.fetchone()))


@skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL")
class DeviceSwapApiTest(DeviceSwapSetupMixin, TestCase):
    # ---- permisos ----

    def test_recepcion_puede_registrar_el_cambio(self):
        self._auth("recepcion")
        resp = self.client.post(
            f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
            {"device_id": self.device_nuevo, "destino_viejo": "pendiente"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)

    def test_el_tecnico_no_puede_registrar_el_cambio(self):
        self._auth("tecnico")
        resp = self.client.post(
            f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
            {"device_id": self.device_nuevo},
            format="json",
        )
        self.assertEqual(resp.status_code, 403, resp.data)

    def test_recepcion_no_decide_el_destino_del_equipo_recibido(self):
        # 'mg' y 'desguace' pasan el equipo al patrimonio de MG BIO (y desguace mueve stock
        # real): la misma conversión, por su endpoint propio, exige `baja_alta` o
        # `devices_preventivos.manage`, que recepción no tiene. Sin este corte el cambio de
        # equipo era la puerta de atrás. Recepción registra y jefatura define el destino.
        for destino in ("mg", "desguace"):
            with self.subTest(destino=destino):
                self._auth("recepcion")
                resp = self.client.post(
                    f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
                    {"device_id": self.device_nuevo, "destino_viejo": destino},
                    format="json",
                )
                self.assertEqual(resp.status_code, 403, resp.data)
                # Y no dejó nada a medio registrar.
                self.assertIsNone(self._ingreso()["resolucion"])

    def test_con_el_permiso_patrimonial_el_destino_firme_se_registra(self):
        self._auth("admin")
        resp = self.client.post(
            f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
            {"device_id": self.device_nuevo, "destino_viejo": "mg"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(self._ingreso()["cambio_destino_viejo"], "mg")

    # ---- efectos ----

    def test_el_cambio_vincula_el_equipo_entregado_y_deja_la_resolucion(self):
        self._auth("admin")
        self.client.post(
            f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
            {"device_id": self.device_nuevo, "destino_viejo": "desguace"},
            format="json",
        )
        row = self._ingreso()
        self.assertEqual(row["resolucion"], "cambio")
        self.assertEqual(row["device_cambio_id"], self.device_nuevo)
        self.assertEqual(row["cambio_destino_viejo"], "desguace")
        # El snapshot de texto se mantiene sincronizado para el PDF y la verificación.
        self.assertIn("NUEVO-002", row["serial_cambio"])
        self.assertIn("MG 0500", row["serial_cambio"])

    def test_deja_traza_en_los_eventos_del_ingreso(self):
        self._auth("recepcion")
        self.client.post(
            f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
            {"device_id": self.device_nuevo},
            format="json",
        )
        with connection.cursor() as cur:
            cur.execute(
                "SELECT comentario FROM ingreso_events WHERE ticket_id=%s", [self.ingreso_id]
            )
            comentarios = [r[0] for r in cur.fetchall()]
        self.assertTrue(any("Cambio de equipo" in (c or "") for c in comentarios), comentarios)

    def test_deshacer_limpia_el_cambio_y_la_resolucion(self):
        self._auth("recepcion")
        self.client.post(
            f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
            {"device_id": self.device_nuevo},
            format="json",
        )
        resp = self.client.delete(f"/api/ingresos/{self.ingreso_id}/cambio-equipo/")
        self.assertEqual(resp.status_code, 200, resp.data)
        row = self._ingreso()
        self.assertIsNone(row["resolucion"])
        self.assertIsNone(row["device_cambio_id"])
        self.assertIsNone(row["serial_cambio"])

    # ---- el equipo tomado en parte de pago pasa a ser de MG BIO ----

    def _device_viejo(self):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT customer_id, COALESCE(numero_interno,'') AS ni FROM devices WHERE id=%s",
                [self.device_viejo],
            )
            cols = [c[0] for c in cur.description]
            return dict(zip(cols, cur.fetchone()))

    def test_destino_mg_sin_numero_interno_igual_lo_hace_propio(self):
        # Lo que define el patrimonio es el cliente, no el código MG.
        self._auth("admin")
        resp = self.client.post(
            f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
            {"device_id": self.device_nuevo, "destino_viejo": "mg"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        device = self._device_viejo()
        self.assertEqual(device["customer_id"], self.mg_owner_id)
        self.assertEqual(device["ni"], "")

    def test_el_pase_a_mg_guarda_quien_era_el_dueno(self):
        # Los papeles de la OS resuelven el cliente por `devices.customer_id`, que acá pasa a
        # MG BIO: sin este snapshot la orden de salida que firma el cliente salía a nombre de
        # MG BIO. Ver `test_pdf_cliente_original` para el lado de la lectura.
        self._auth("admin")
        resp = self.client.post(
            f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
            {"device_id": self.device_nuevo, "destino_viejo": "mg"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)

        with connection.cursor() as cur:
            cur.execute("SELECT cliente_original_id FROM ingresos WHERE id=%s", [self.ingreso_id])
            self.assertEqual(cur.fetchone()[0], self.customer_id)

    def test_con_destino_pendiente_no_hay_dueno_que_guardar(self):
        # 'pendiente' no cambia la propiedad, así que no hay snapshot que tomar.
        self._auth("recepcion")
        self.client.post(
            f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
            {"device_id": self.device_nuevo, "destino_viejo": "pendiente"},
            format="json",
        )
        with connection.cursor() as cur:
            cur.execute("SELECT cliente_original_id FROM ingresos WHERE id=%s", [self.ingreso_id])
            self.assertIsNone(cur.fetchone()[0])

    def test_destino_mg_con_numero_interno_lo_asigna(self):
        self._auth("admin")
        resp = self.client.post(
            f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
            {"device_id": self.device_nuevo, "destino_viejo": "mg", "numero_interno_mg": "MG 12"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        device = self._device_viejo()
        self.assertEqual(device["customer_id"], self.mg_owner_id)
        self.assertEqual(device["ni"], "MG 0012")

    def test_un_numero_interno_invalido_se_sigue_rechazando(self):
        self._auth("admin")
        resp = self.client.post(
            f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
            {"device_id": self.device_nuevo, "destino_viejo": "mg", "numero_interno_mg": "XX"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_destino_desguace_lo_hace_propio_y_lo_manda_a_des(self):
        self._auth("admin")
        with patch(
            "service.views.ingresos_views.enqueue_stock_baja_for_ingreso",
            return_value={"status": "pending"},
        ) as mock_baja:
            resp = self.client.post(
                f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
                {"device_id": self.device_nuevo, "destino_viejo": "desguace"},
                format="json",
            )
        self.assertEqual(resp.status_code, 200, resp.data)
        mock_baja.assert_called_once()
        self.assertEqual(mock_baja.call_args.args[0], self.ingreso_id)
        # Sin pasar a MG BIO, el circuito de baja rechazaría el equipo por no ser propio.
        self.assertEqual(self._device_viejo()["customer_id"], self.mg_owner_id)
        self.assertEqual(resp.data["bejerman_sync_job"], {"status": "pending"})

    def test_desguace_no_cierra_la_orden(self):
        # La OS tiene que poder liberarse igual: el cliente se lleva el equipo del cambio y
        # necesita su orden de salida.
        self._auth("admin")
        with patch(
            "service.views.ingresos_views.enqueue_stock_baja_for_ingreso",
            return_value={"status": "pending"},
        ):
            self.client.post(
                f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
                {"device_id": self.device_nuevo, "destino_viejo": "desguace"},
                format="json",
            )
        with connection.cursor() as cur:
            cur.execute("SELECT estado::text FROM ingresos WHERE id=%s", [self.ingreso_id])
            self.assertEqual(cur.fetchone()[0], "reparado")

    # ---- "va para venta": propio de SEPID y al depósito de ventas ----

    def test_destino_venta_lo_hace_propio_y_lo_manda_a_val(self):
        self._auth("admin")
        with patch(
            "service.views.ingresos_views.enqueue_stock_venta_for_ingreso",
            return_value={"status": "pending"},
        ) as mock_venta:
            resp = self.client.post(
                f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
                {"device_id": self.device_nuevo, "destino_viejo": "venta"},
                format="json",
            )
        self.assertEqual(resp.status_code, 200, resp.data)
        mock_venta.assert_called_once()
        self.assertEqual(mock_venta.call_args.args[0], self.ingreso_id)
        self.assertEqual(self._ingreso()["cambio_destino_viejo"], "venta")
        # El movimiento a VAL sólo tiene sentido si el equipo ya es nuestro: si no,
        # estaríamos poniendo a la venta la unidad de un cliente.
        self.assertEqual(self._device_viejo()["customer_id"], self.mg_owner_id)
        self.assertEqual(resp.data["bejerman_sync_job"], {"status": "pending"})
        self.assertIn("VAL", resp.data["bejerman_sync_label"])

    def test_venta_no_manda_el_equipo_a_desguace(self):
        self._auth("admin")
        with (
            patch(
                "service.views.ingresos_views.enqueue_stock_venta_for_ingreso",
                return_value={"status": "pending"},
            ),
            patch("service.views.ingresos_views.enqueue_stock_baja_for_ingreso") as mock_baja,
        ):
            self.client.post(
                f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
                {"device_id": self.device_nuevo, "destino_viejo": "venta"},
                format="json",
            )
        mock_baja.assert_not_called()

    def test_venta_no_cierra_la_orden(self):
        self._auth("admin")
        with patch(
            "service.views.ingresos_views.enqueue_stock_venta_for_ingreso",
            return_value={"status": "pending"},
        ):
            self.client.post(
                f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
                {"device_id": self.device_nuevo, "destino_viejo": "venta"},
                format="json",
            )
        with connection.cursor() as cur:
            cur.execute("SELECT estado::text FROM ingresos WHERE id=%s", [self.ingreso_id])
            self.assertEqual(cur.fetchone()[0], "reparado")

    def test_recepcion_no_puede_poner_el_equipo_a_la_venta(self):
        # Mismo gate que 'mg' y 'desguace': pasar el equipo a nuestro nombre lo decide
        # jefatura, no el mostrador.
        self._auth("recepcion")
        with patch(
            "service.views.ingresos_views.enqueue_stock_venta_for_ingreso"
        ) as mock_venta:
            resp = self.client.post(
                f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
                {"device_id": self.device_nuevo, "destino_viejo": "venta"},
                format="json",
            )
        self.assertEqual(resp.status_code, 403, resp.data)
        mock_venta.assert_not_called()
        self.assertEqual(self._device_viejo()["customer_id"], self.customer_id)

    def test_parte_de_pago_sin_entrega_puede_ir_para_venta(self):
        self._auth("admin")
        with patch(
            "service.views.ingresos_views.enqueue_stock_venta_for_ingreso",
            return_value={"status": "pending"},
        ) as mock_venta:
            resp = self.client.post(
                f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
                {"sin_entrega": True, "destino_viejo": "venta"},
                format="json",
            )
        self.assertEqual(resp.status_code, 200, resp.data)
        mock_venta.assert_called_once()
        row = self._ingreso()
        self.assertTrue(row["cambio_sin_entrega"])
        self.assertEqual(row["cambio_destino_viejo"], "venta")
        self.assertEqual(self._device_viejo()["customer_id"], self.mg_owner_id)

    def test_destino_pendiente_no_cambia_el_dueno_ni_mueve_stock(self):
        self._auth("recepcion")
        with patch(
            "service.views.ingresos_views.enqueue_stock_baja_for_ingreso"
        ) as mock_baja:
            self.client.post(
                f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
                {"device_id": self.device_nuevo, "destino_viejo": "pendiente"},
                format="json",
            )
        mock_baja.assert_not_called()
        self.assertEqual(self._device_viejo()["customer_id"], self.customer_id)

    # ---- parte de pago sin entregar otro equipo ----
    #
    # Pasa seguido: tomamos el equipo del cliente a cuenta y no sale ninguno a cambio.
    # Es el mismo movimiento patrimonial del cambio, sin equipo de salida.

    def test_parte_de_pago_sin_entrega_lo_hace_propio_sin_vincular_ningun_equipo(self):
        self._auth("admin")
        resp = self.client.post(
            f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
            {"sin_entrega": True, "destino_viejo": "mg"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        row = self._ingreso()
        self.assertEqual(row["resolucion"], "cambio")
        self.assertTrue(row["cambio_sin_entrega"])
        # Nada sale: ni vínculo ni snapshot de texto, que es lo que la entrega verifica.
        self.assertIsNone(row["device_cambio_id"])
        self.assertIsNone(row["serial_cambio"])
        self.assertEqual(self._device_viejo()["customer_id"], self.mg_owner_id)

    def test_la_parte_de_pago_admite_numero_mg(self):
        self._auth("admin")
        resp = self.client.post(
            f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
            {"sin_entrega": True, "destino_viejo": "mg", "numero_interno_mg": "MG 77"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(self._device_viejo()["ni"], "MG 0077")

    def test_la_parte_de_pago_tambien_puede_ir_a_desguace(self):
        self._auth("admin")
        with patch(
            "service.views.ingresos_views.enqueue_stock_baja_for_ingreso",
            return_value={"status": "pending"},
        ) as mock_baja:
            resp = self.client.post(
                f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
                {"sin_entrega": True, "destino_viejo": "desguace"},
                format="json",
            )
        self.assertEqual(resp.status_code, 200, resp.data)
        mock_baja.assert_called_once()
        self.assertEqual(self._device_viejo()["customer_id"], self.mg_owner_id)

    def test_no_se_puede_pedir_sin_entrega_y_mandar_un_equipo(self):
        # Las dos cosas juntas son una contradicción: o se entrega algo o no.
        self._auth("admin")
        resp = self.client.post(
            f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
            {"sin_entrega": True, "device_id": self.device_nuevo, "destino_viejo": "mg"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIsNone(self._ingreso()["resolucion"])

    def test_sin_la_marca_explicita_se_sigue_exigiendo_el_equipo(self):
        # Que la pantalla se olvide de mandar el equipo no puede registrar una parte de pago.
        self._auth("admin")
        resp = self.client.post(
            f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
            {"destino_viejo": "mg"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("Elegí el equipo", resp.data["detail"])

    def test_la_traza_de_la_parte_de_pago_no_habla_de_ninguna_entrega(self):
        self._auth("admin")
        self.client.post(
            f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
            {"sin_entrega": True, "destino_viejo": "mg"},
            format="json",
        )
        with connection.cursor() as cur:
            cur.execute(
                "SELECT comentario FROM ingreso_events WHERE ticket_id=%s", [self.ingreso_id]
            )
            comentarios = [r[0] or "" for r in cur.fetchall()]
        self.assertTrue(any("Parte de pago" in c for c in comentarios), comentarios)
        self.assertFalse(any("se entrega" in c.lower() for c in comentarios), comentarios)

    def test_deshacer_tambien_limpia_la_marca_de_sin_entrega(self):
        self._auth("admin")
        self.client.post(
            f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
            {"sin_entrega": True, "destino_viejo": "mg"},
            format="json",
        )
        resp = self.client.delete(f"/api/ingresos/{self.ingreso_id}/cambio-equipo/")
        self.assertEqual(resp.status_code, 200, resp.data)
        row = self._ingreso()
        self.assertIsNone(row["resolucion"])
        self.assertFalse(row["cambio_sin_entrega"])

    def test_el_get_devuelve_la_parte_de_pago_como_registrada(self):
        # La pantalla la reconoce por esta marca: sin ella parecería una OS sin cambio.
        self._auth("admin")
        self.client.post(
            f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
            {"sin_entrega": True, "destino_viejo": "mg"},
            format="json",
        )
        resp = self.client.get(f"/api/ingresos/{self.ingreso_id}/cambio-equipo/")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(resp.data["sin_entrega"])
        self.assertEqual(resp.data["equipo_entregado"], "")
        self.assertEqual(resp.data["destino_viejo"], "mg")

    def test_deshacer_no_toca_una_orden_resuelta_de_otra_forma(self):
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE ingresos SET resolucion='reparado' WHERE id=%s", [self.ingreso_id]
            )
        self._auth("recepcion")
        resp = self.client.delete(f"/api/ingresos/{self.ingreso_id}/cambio-equipo/")
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(self._ingreso()["resolucion"], "reparado")

    # ---- validaciones ----

    def test_no_se_puede_entregar_el_mismo_equipo_que_ingreso(self):
        self._auth("recepcion")
        resp = self.client.post(
            f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
            {"device_id": self.device_viejo},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("distinto", str(resp.data.get("detail", "")).lower())

    def test_no_se_puede_entregar_un_equipo_alquilado(self):
        with connection.cursor() as cur:
            cur.execute("UPDATE devices SET alquilado=TRUE WHERE id=%s", [self.device_nuevo])
        self._auth("recepcion")
        resp = self.client.post(
            f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
            {"device_id": self.device_nuevo},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("alquilado", str(resp.data.get("detail", "")).lower())

    def test_no_se_puede_entregar_un_equipo_con_un_ingreso_abierto(self):
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO ingresos (device_id, estado, motivo) VALUES (%s, 'reparar', 'reparación')",
                [self.device_nuevo],
            )
        self._auth("recepcion")
        resp = self.client.post(
            f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
            {"device_id": self.device_nuevo},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("reparar", str(resp.data.get("detail", "")).lower())

    def test_una_orden_ya_entregada_no_admite_registrar_el_cambio(self):
        with connection.cursor() as cur:
            cur.execute("UPDATE ingresos SET estado='entregado' WHERE id=%s", [self.ingreso_id])
        self._auth("recepcion")
        resp = self.client.post(
            f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
            {"device_id": self.device_nuevo},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_destino_invalido_se_rechaza(self):
        self._auth("recepcion")
        resp = self.client.post(
            f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
            {"device_id": self.device_nuevo, "destino_viejo": "al_tacho"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)

    # ---- candidatos: salen del stock real de Bejerman ----

    def _stock(self, *filas):
        """Parchea el volcado de stock cacheado del depósito."""
        return patch(
            "service.views.ingresos_views.cached_stock_rows",
            return_value=list(filas),
        )

    @staticmethod
    def _fila_stock(article_code, partida, deposito="VAL", disponible=1):
        return {
            "articleCode": article_code,
            "depositCode": deposito,
            "partida": partida,
            "availableQuantity": disponible,
            "realQuantity": disponible,
        }

    def test_los_candidatos_solo_traen_articulos_de_equipo(self):
        # En Bejerman los equipos arrancan con 11; el resto son repuestos e insumos.
        self._auth("recepcion")
        with self._stock(
            self._fila_stock("110001", "SERIE-EQUIPO"),
            self._fila_stock("220002", "SERIE-REPUESTO"),
        ):
            resp = self.client.get(f"/api/ingresos/{self.ingreso_id}/cambio-equipo/candidatos/")
        self.assertEqual(resp.status_code, 200, resp.data)
        partidas = [i["partida"] for i in resp.data["items"]]
        self.assertEqual(partidas, ["SERIE-EQUIPO"])

    def test_los_candidatos_descartan_lo_que_no_tiene_stock(self):
        self._auth("recepcion")
        with self._stock(
            self._fila_stock("110001", "CON-STOCK", disponible=2),
            self._fila_stock("110002", "SIN-STOCK", disponible=0),
        ):
            resp = self.client.get(f"/api/ingresos/{self.ingreso_id}/cambio-equipo/candidatos/")
        self.assertEqual([i["partida"] for i in resp.data["items"]], ["CON-STOCK"])

    def test_los_candidatos_reusan_la_ficha_existente_si_la_serie_ya_esta_cargada(self):
        self._auth("recepcion")
        with self._stock(self._fila_stock("110001", "NUEVO-002")):
            resp = self.client.get(f"/api/ingresos/{self.ingreso_id}/cambio-equipo/candidatos/")
        fila = resp.data["items"][0]
        self.assertEqual(fila["device_id"], self.device_nuevo)

    def test_el_candidato_con_una_os_abierta_en_nexora_se_marca_no_disponible(self):
        # Bejerman puede tener stock de algo que acá está dentro de otra reparación.
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO ingresos (device_id, estado, motivo) VALUES (%s,'reparar','reparación')",
                [self.device_nuevo],
            )
        self._auth("recepcion")
        with self._stock(self._fila_stock("110001", "NUEVO-002")):
            resp = self.client.get(f"/api/ingresos/{self.ingreso_id}/cambio-equipo/candidatos/")
        fila = resp.data["items"][0]
        self.assertFalse(fila["disponible"])
        self.assertIn("reparar", fila["motivo_no_disponible"].lower())

    def test_el_candidato_sin_mapeo_se_ofrece_igual_pero_avisado(self):
        self._auth("recepcion")
        with self._stock(self._fila_stock("119999", "SIN-MAPEO")):
            resp = self.client.get(f"/api/ingresos/{self.ingreso_id}/cambio-equipo/candidatos/")
        fila = resp.data["items"][0]
        self.assertFalse(fila["mapeado"])
        self.assertEqual(fila["marca"], "")

    def test_registrar_con_una_partida_nueva_ficha_el_equipo(self):
        self._auth("recepcion")
        resp = self.client.post(
            f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
            {"partida": "PARTIDA-NUEVA", "article_code": "110001"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        with connection.cursor() as cur:
            cur.execute("SELECT id FROM devices WHERE numero_serie='PARTIDA-NUEVA'")
            creado = cur.fetchone()
        self.assertIsNotNone(creado)
        self.assertEqual(self._ingreso()["device_cambio_id"], creado[0])

    def test_registrar_con_una_partida_ya_fichada_no_duplica_el_equipo(self):
        self._auth("recepcion")
        resp = self.client.post(
            f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
            {"partida": "NUEVO-002", "article_code": "110001"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(self._ingreso()["device_cambio_id"], self.device_nuevo)
        with connection.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM devices WHERE numero_serie='NUEVO-002'")
            self.assertEqual(cur.fetchone()[0], 1)


@skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL")
class DeviceSwapCortaStockYRemitoTest(DeviceSwapSetupMixin, TestCase):
    """Lo que realmente estaba roto: la OS con cambio no debe mover stock ni emitir RSS."""

    def _registrar_cambio(self):
        self._auth("recepcion")
        resp = self.client.post(
            f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
            {"device_id": self.device_nuevo},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)

    def test_ingreso_tiene_cambio_de_equipo_detecta_la_resolucion(self):
        from service.views.ingresos_views import ingreso_tiene_cambio_de_equipo

        self.assertFalse(ingreso_tiene_cambio_de_equipo(self.ingreso_id))
        self._registrar_cambio()
        self.assertTrue(ingreso_tiene_cambio_de_equipo(self.ingreso_id))

    def test_la_entrega_no_encola_el_traslado_al_cliente(self):
        self._registrar_cambio()
        self._auth("admin")
        with patch(
            "service.views.ingresos_views.enqueue_client_ready_transfer_for_ingreso"
        ) as mock_transfer:
            self.client.post(
                f"/api/ingresos/{self.ingreso_id}/entregar/",
                {"remito_salida": "0004-00001234", "serial_confirm": "NUEVO-002"},
                format="json",
            )
        mock_transfer.assert_not_called()

    def test_la_entrega_rechaza_una_serie_que_no_es_la_del_cambio(self):
        self._registrar_cambio()
        self._auth("admin")
        resp = self.client.post(
            f"/api/ingresos/{self.ingreso_id}/entregar/",
            {"remito_salida": "0004-00001234", "serial_confirm": "OTRA-SERIE"},
            format="json",
        )
        # Antes esto vivía dentro de un `except: pass` y la entrega pasaba igual.
        self.assertEqual(resp.status_code, 409, resp.data)

    def test_la_entrega_pide_confirmar_la_serie(self):
        self._registrar_cambio()
        self._auth("admin")
        resp = self.client.post(
            f"/api/ingresos/{self.ingreso_id}/entregar/",
            {"remito_salida": "0004-00001234"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_la_entrega_acepta_cualquiera_de_las_dos_partes_del_identificador(self):
        self._registrar_cambio()
        self._auth("admin")
        resp = self.client.post(
            f"/api/ingresos/{self.ingreso_id}/entregar/",
            {"remito_salida": "0004-00001234", "serial_confirm": "MG 0500"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)

    def _marcar_reparado(self):
        """El técnico repara el equipo tomado: es lo esperable, ya es nuestro.

        Se mockea el pase a STL porque un equipo que acaba de pasar a MG BIO dispara el
        movimiento a la estantería de alquiler, ajeno a lo que se prueba acá.
        """
        self._auth("admin")
        with patch("service.views.ingresos_views.enqueue_stock_transfer_for_ingreso"):
            resp = self.client.post(f"/api/ingresos/{self.ingreso_id}/reparado/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)

    def test_marcar_reparado_no_borra_la_parte_de_pago(self):
        # La secuencia real: recepción toma el equipo, el técnico lo repara. Antes, marcar
        # reparado pisaba la resolución y limpiaba las columnas del cambio, dejando sueltos
        # el pase a MG BIO y el movimiento a Desguace, que no se revierten.
        self._auth("admin")
        self.client.post(
            f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
            {"sin_entrega": True, "destino_viejo": "mg"},
            format="json",
        )
        self._marcar_reparado()
        row = self._ingreso()
        self.assertEqual(row["resolucion"], "reparado")
        self.assertTrue(row["cambio_sin_entrega"])
        self.assertEqual(row["cambio_destino_viejo"], "mg")

    def test_el_corte_de_stock_sobrevive_a_marcar_reparado(self):
        from service.views.ingresos_views import ingreso_tiene_cambio_de_equipo

        self._auth("admin")
        self.client.post(
            f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
            {"sin_entrega": True, "destino_viejo": "mg"},
            format="json",
        )
        self._marcar_reparado()
        # Sin esto la OS emitiría un RSS y mandaría a STC un equipo que ya es nuestro.
        self.assertTrue(ingreso_tiene_cambio_de_equipo(self.ingreso_id))

    def test_marcar_reparado_sigue_limpiando_un_cambio_con_equipo(self):
        # Ahí la permuta sí es el resultado de la OS: si se resuelve como reparado, el
        # cambio deja de aplicar y no puede quedar la serie colgada.
        self._auth("recepcion")
        self.client.post(
            f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
            {"device_id": self.device_nuevo, "destino_viejo": "pendiente"},
            format="json",
        )
        self._marcar_reparado()
        row = self._ingreso()
        self.assertEqual(row["resolucion"], "reparado")
        self.assertIsNone(row["device_cambio_id"])
        self.assertIsNone(row["serial_cambio"])
        self.assertFalse(row["cambio_sin_entrega"])

    def test_deshacer_la_parte_de_pago_no_borra_la_resolucion_de_taller(self):
        self._auth("admin")
        self.client.post(
            f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
            {"sin_entrega": True, "destino_viejo": "mg"},
            format="json",
        )
        self._marcar_reparado()
        self._auth("recepcion")
        resp = self.client.delete(f"/api/ingresos/{self.ingreso_id}/cambio-equipo/")
        self.assertEqual(resp.status_code, 200, resp.data)
        row = self._ingreso()
        # Se deshace la toma, no la reparación.
        self.assertEqual(row["resolucion"], "reparado")
        self.assertFalse(row["cambio_sin_entrega"])

    def test_la_parte_de_pago_tampoco_mueve_stock_al_cliente(self):
        # El corte es el mismo que el del cambio, y acá encima no sale ningún equipo.
        self._auth("admin")
        self.client.post(
            f"/api/ingresos/{self.ingreso_id}/cambio-equipo/",
            {"sin_entrega": True, "destino_viejo": "mg"},
            format="json",
        )
        self._auth("admin")
        with patch(
            "service.views.ingresos_views.enqueue_client_ready_transfer_for_ingreso"
        ) as mock_transfer:
            resp = self.client.post(
                f"/api/ingresos/{self.ingreso_id}/entregar/",
                {"remito_salida": "0004-00001234"},
                format="json",
            )
        # Sin equipo que verificar, la entrega no puede exigir una serie: si la exigiera,
        # la OS quedaría trabada sin forma de cerrarse.
        self.assertEqual(resp.status_code, 200, resp.data)
        mock_transfer.assert_not_called()
