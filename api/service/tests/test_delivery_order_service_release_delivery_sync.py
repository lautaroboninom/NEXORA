import json
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from service.bejerman_delivery import register_delivery_order_remitos
from service.delivery_orders import (
    DeliveryOrderError,
    _upsert_service_release_order_item,
    create_delivery_order,
    get_delivery_order,
    mark_delivered,
)
from service.models import User


class DeliveryOrderServiceReleaseSyncTests(TestCase):
    """Cubre la sincronizacion de mark_delivered() hacia `ingresos`.

    Reproduce el caso OS-29473: una orden de entrega service_release se marca
    entregada (con o sin remito cargado a mano) y eso debe reflejarse en el
    bloque "Entrega" de la hoja de servicio sin que el tecnico tenga que
    cargar el remito de nuevo.
    """

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
                CREATE TABLE IF NOT EXISTS customers (
                    id BIGSERIAL PRIMARY KEY,
                    cod_empresa TEXT NULL,
                    razon_social TEXT NOT NULL
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
                    numero_serie TEXT,
                    numero_interno TEXT,
                    alquilado BOOLEAN NOT NULL DEFAULT FALSE,
                    mg_estado TEXT NOT NULL DEFAULT 'activo'
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ingresos (
                    id BIGSERIAL PRIMARY KEY,
                    device_id INTEGER NOT NULL REFERENCES devices(id),
                    estado TEXT NOT NULL DEFAULT 'ingresado',
                    motivo TEXT NOT NULL DEFAULT 'reparación',
                    fecha_creacion TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    fecha_ingreso TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    ubicacion_id INTEGER NULL REFERENCES locations(id),
                    remito_salida TEXT NULL,
                    factura_numero TEXT NULL,
                    fecha_entrega TIMESTAMPTZ NULL,
                    alquilado BOOLEAN NOT NULL DEFAULT FALSE,
                    alquiler_a TEXT NULL,
                    alquiler_remito TEXT NULL,
                    alquiler_fecha TIMESTAMPTZ NULL,
                    resolucion TEXT NULL
                )
                """
            )
            # `resolve_ingreso_entrega_estado` lee `resolucion` para no marcar alquilado un
            # equipo que salio por cambio. Sin la columna la consulta aborta la transaccion
            # y, como el helper la traga en un `except`, lo que se ve es el INSERT siguiente
            # fallando con "current transaction is aborted": el CREATE de arriba no alcanza
            # si otro modulo ya creo la tabla.
            cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS resolucion TEXT NULL")
            cur.execute(
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS fecha_ingreso TIMESTAMPTZ "
                "NOT NULL DEFAULT CURRENT_TIMESTAMP"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ingreso_events (
                    id BIGSERIAL PRIMARY KEY,
                    ticket_id INTEGER,
                    ingreso_id INTEGER,
                    a_estado TEXT,
                    usuario_id INTEGER NULL,
                    comentario TEXT NULL,
                    ts TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

        call_command("apply_customer_bejerman_schema", verbosity=0)
        call_command("apply_delivery_orders_schema", verbosity=0)
        call_command("apply_user_permissions_schema", verbosity=0)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        User.objects.filter(email__endswith="@delivery-sync.test").delete()
        cls.jefe = User.objects.create(
            nombre="Jefatura de prueba",
            email="jefe@delivery-sync.test",
            hash_pw="",
            rol="jefe",
            activo=True,
        )
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO customers(cod_empresa, razon_social) VALUES (%s, %s) RETURNING id",
                ["CLI-SYNC", "Cliente Sync"],
            )
            cls.customer_id = int(cur.fetchone()[0])

    def _create_device(self, *, alquilado=False, mg_estado="activo", numero_serie="", numero_interno=""):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO devices(customer_id, numero_serie, numero_interno, alquilado, mg_estado)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                [self.customer_id, numero_serie, numero_interno, alquilado, mg_estado],
            )
            return int(cur.fetchone()[0])

    def _create_ingreso(
        self, *, estado, device_id=None, remito_salida=None, alquilado=False, fecha_ingreso=None
    ):
        device_id = device_id if device_id is not None else self._create_device()
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ingresos(device_id, estado, remito_salida, alquilado, fecha_ingreso)
                VALUES (%s, %s, %s, %s, COALESCE(%s::timestamptz, CURRENT_TIMESTAMP))
                RETURNING id
                """,
                [device_id, estado, remito_salida, alquilado, fecha_ingreso],
            )
            return int(cur.fetchone()[0])

    def _create_service_release_order(self, order_id, ingreso_id, *, status="armado_pendiente_entrega"):
        payload = {
            "id": order_id,
            "orderNumber": f"OE-{order_id}",
            "customerId": self.customer_id,
            "bejermanCustomerCode": "CLI-SYNC",
            "customerName": "Cliente Sync",
            "deliveryType": "service_release",
            "companyKey": "SEPID",
            "status": status,
            "sellerName": "Servicio Tecnico",
            "operationCompanyLabel": "SEPID",
            "ingresoId": ingreso_id,
            "items": [
                {
                    "articleCode": "SVC-001",
                    "description": "Servicio tecnico",
                    "quantity": 1,
                    "unitPrice": "0",
                    "priceCurrency": "ARS",
                    "discountPercent": "0",
                }
            ],
        }
        with (
            patch("service.delivery_orders.notify_delivery_order_created"),
            patch("service.delivery_orders.notify_delivery_order_remito_ready"),
        ):
            return create_delivery_order(payload, self.jefe.id)

    def _mark_delivered(self, order_id, remito):
        with patch("service.delivery_orders.notify_delivery_order_remito_ready"):
            return mark_delivered(order_id, self.jefe.id, remito)

    def _ingreso_row(self, ingreso_id):
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT estado, remito_salida, fecha_entrega, alquilado
                  FROM ingresos WHERE id = %s
                """,
                [ingreso_id],
            )
            row = cur.fetchone()
        return {"estado": row[0], "remito_salida": row[1], "fecha_entrega": row[2], "alquilado": row[3]}

    def _event_count(self, ingreso_id, a_estado):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM ingreso_events WHERE ticket_id = %s AND a_estado = %s",
                [ingreso_id, a_estado],
            )
            return int(cur.fetchone()[0] or 0)

    def _register_existing_remito(self, order_id, remito):
        """Carga en la orden un RSS que ya se emitio a mano en Bejerman."""
        with (
            patch("service.bejerman_delivery.BejermanSDKClient") as sdk_cls,
            patch("service.bejerman_delivery.notify_delivery_order_remito_ready"),
        ):
            result = register_delivery_order_remitos(
                order_id, self.jefe.id, {"remitos": [{"remitoNumber": remito}]}
            )
        # Un remito ya emitido en Bejerman se asocia, no se vuelve a emitir.
        sdk_cls.assert_not_called()
        return result

    def _registered_pv1_group(self, first_order_id, *, number="00026328"):
        group_id = f"brg-pv1-{number}"
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bejerman_remito_groups (
                  id, company_key, comprobante_tipo, comprobante_letra,
                  comprobante_pto_venta, comprobante_numero, remito_number,
                  customer_code, customer_name, seller_code, payment_term_code,
                  operation_code, deposit_code, status, order_ids,
                  response_summary, correlation_key, created_by_user_id, generated_at
                )
                VALUES (
                  %s, 'SEPID', 'RSS', 'R', '00001', %s, %s,
                  'CLI-SYNC', 'Cliente Sync', 'ADM', '30', 'REP', 'STC',
                  'generated', %s::jsonb, %s::jsonb, %s, %s, CURRENT_TIMESTAMP
                )
                """,
                [
                    group_id,
                    number,
                    f"RSS R 00001-{number}",
                    f'["{first_order_id}"]',
                    '{"documentMode":"register","stockMovementGenerated":true}',
                    f"NEXORA {group_id}",
                    self.jefe.id,
                ],
            )
        return group_id

    def _set_order_partida(self, order_id, *, article="1103002", partida="SERIE-SEGUNDA"):
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE delivery_order_items
                   SET article_code = %s,
                       partida = %s,
                       stock_deposit_code = 'STC'
                 WHERE order_id = %s
                """,
                [article, partida, order_id],
            )

    def test_mark_delivered_sincroniza_estado_remito_y_fecha(self):
        ingreso_id = self._create_ingreso(estado="liberado")
        order = self._create_service_release_order("sync-basico", ingreso_id)

        self._mark_delivered(order["id"], "RSS 0001-00026325")

        row = self._ingreso_row(ingreso_id)
        self.assertEqual(row["estado"], "entregado")
        self.assertEqual(row["remito_salida"], "RSS 0001-00026325")
        self.assertIsNotNone(row["fecha_entrega"])
        self.assertGreaterEqual(self._event_count(ingreso_id, "entregado"), 1)

    def test_mark_delivered_no_pisa_remito_ya_cargado(self):
        ingreso_id = self._create_ingreso(estado="liberado", remito_salida="MANUAL-999")
        order = self._create_service_release_order("sync-no-pisa", ingreso_id)

        self._mark_delivered(order["id"], "RSS 0001-00026325")

        row = self._ingreso_row(ingreso_id)
        self.assertEqual(row["estado"], "entregado")
        self.assertEqual(row["remito_salida"], "MANUAL-999")

    def test_mark_delivered_detecta_alquiler(self):
        device_id = self._create_device(alquilado=True)
        ingreso_id = self._create_ingreso(estado="liberado", device_id=device_id, alquilado=True)
        order = self._create_service_release_order("sync-alquiler", ingreso_id)

        self._mark_delivered(order["id"], "RSS 0001-00026400")

        row = self._ingreso_row(ingreso_id)
        self.assertEqual(row["estado"], "alquilado")
        self.assertTrue(row["alquilado"])

    def test_mark_delivered_detecta_venta_pendiente_a_vendido_entregado(self):
        ingreso_id = self._create_ingreso(estado="vendido_pendiente_entrega")
        order = self._create_service_release_order("sync-venta", ingreso_id)

        self._mark_delivered(order["id"], "RSS 0001-00026500")

        row = self._ingreso_row(ingreso_id)
        self.assertEqual(row["estado"], "vendido_entregado")
        self.assertFalse(row["alquilado"])

    def test_mark_delivered_no_toca_ingreso_ya_entregado(self):
        ingreso_id = self._create_ingreso(estado="entregado", remito_salida="YA-ENTREGADO")
        order = self._create_service_release_order("sync-ya-entregado", ingreso_id)

        self._mark_delivered(order["id"], "RSS 0001-00026600")

        row = self._ingreso_row(ingreso_id)
        self.assertEqual(row["estado"], "entregado")
        self.assertEqual(row["remito_salida"], "YA-ENTREGADO")

    def test_mark_delivered_ignora_ordenes_que_no_son_service_release(self):
        ingreso_id = self._create_ingreso(estado="liberado")
        order = self._create_service_release_order("sync-scope", ingreso_id)
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE delivery_orders SET delivery_type = 'sale' WHERE id = %s",
                [order["id"]],
            )

        self._mark_delivered(order["id"], "RT 0001-00026700")

        row = self._ingreso_row(ingreso_id)
        self.assertEqual(row["estado"], "liberado")
        self.assertIsNone(row["remito_salida"])

    def test_cargar_rss_emitido_en_bejerman_marca_la_os_entregada(self):
        """El caso que reporto Recepcion: el RSS se emite en Bejerman y se carga aca.

        Antes la carga solo dejaba el numero colgado de la orden de entrega y la hoja de
        servicio se quedaba en `liberado` para siempre, asi que el equipo entregado no
        figuraba como entregado en ningun lado.
        """
        ingreso_id = self._create_ingreso(estado="liberado")
        order = self._create_service_release_order("sync-rss-manual", ingreso_id)

        result = self._register_existing_remito(order["id"], "RSS R 00004-00009001")

        self.assertEqual(result["remitoSetStatus"], "complete")
        row = self._ingreso_row(ingreso_id)
        self.assertEqual(row["estado"], "entregado")
        self.assertEqual(row["remito_salida"], "RSS R 00004-00009001")
        self.assertIsNotNone(row["fecha_entrega"])
        self.assertGreaterEqual(self._event_count(ingreso_id, "entregado"), 1)

    def test_cargar_rss_deja_la_orden_abierta_como_la_emision_en_linea(self):
        """Cargar el remito no es entregar: el cierre administrativo sigue siendo de Recepcion.

        Espejo exacto de `test_rss_emitido_completa_entrega_del_ingreso`: emitir el RSS
        desde NEXORA cierra la OS y deja la orden abierta, y cargarlo a mano tiene que
        hacer lo mismo en vez de no hacer nada.
        """
        ingreso_id = self._create_ingreso(estado="liberado")
        order = self._create_service_release_order("sync-rss-abierta", ingreso_id)

        self._register_existing_remito(order["id"], "RSS R 00004-00009002")

        abierta = get_delivery_order(order["id"], include_events=False)
        self.assertEqual(abierta["status"], "armado_pendiente_entrega")
        self.assertIsNone(abierta.get("deliveredAt"))
        self.assertEqual(abierta.get("remitoNumber"), "RSS R 00004-00009002")
        self.assertEqual(self._ingreso_row(ingreso_id)["estado"], "entregado")

    def test_mismo_rss_00001_se_asocia_a_otra_orden_si_bejerman_contiene_sus_equipos(self):
        primer_ingreso = self._create_ingreso(estado="liberado")
        primera = self._create_service_release_order("pv1-primera", primer_ingreso)
        segundo_ingreso = self._create_ingreso(estado="liberado")
        segunda = self._create_service_release_order("pv1-segunda", segundo_ingreso)
        self._set_order_partida(segunda["id"])
        group_id = self._registered_pv1_group(primera["id"])
        remote_rows = [
            {
                "comprobante_id": "19041",
                "cliente_codigo": "CLI-SYNC",
                "operacion": "REP",
                "renglon": "6",
                "articulo": "1103002",
                "partida": "SERIE-SEGUNDA",
                "deposito": "STC",
                "cantidad": "1",
            }
        ]

        with (
            patch(
                "service.bejerman_delivery._registered_manual_document_rows",
                return_value=(remote_rows, {"status": "success"}),
            ),
            patch("service.bejerman_delivery.BejermanSDKClient") as sdk_cls,
            patch("service.bejerman_delivery.notify_delivery_order_remito_ready"),
        ):
            result = register_delivery_order_remitos(
                segunda["id"],
                self.jefe.id,
                {"remitos": [{"remitoNumber": "RSS R 00001-00026328"}]},
            )

        sdk_cls.assert_not_called()
        self.assertEqual(result["remitoSetStatus"], "complete")
        self.assertEqual(result["newRemitos"][0]["groupId"], group_id)
        self.assertTrue(result["newRemitos"][0]["generated"])
        actualizada = get_delivery_order(segunda["id"], include_events=False)
        self.assertEqual(actualizada["bejermanRemitoGroupId"], group_id)
        self.assertEqual(actualizada["remitoNumber"], "RSS R 00001-00026328")
        self.assertEqual(self._ingreso_row(segundo_ingreso)["estado"], "entregado")
        with connection.cursor() as cur:
            cur.execute("SELECT order_ids FROM bejerman_remito_groups WHERE id = %s", [group_id])
            linked_orders = cur.fetchone()[0]
        # El cursor crudo puede devolver el jsonb ya decodificado o todavia como texto; sin
        # esto el `set()` recorre el JSON caracter por caracter y la comparacion no dice nada.
        if isinstance(linked_orders, str):
            linked_orders = json.loads(linked_orders)
        self.assertEqual(set(linked_orders), {primera["id"], segunda["id"]})

    def test_mismo_rss_00001_no_se_asocia_si_falta_la_partida_en_bejerman(self):
        primer_ingreso = self._create_ingreso(estado="liberado")
        primera = self._create_service_release_order("pv1-falta-primera", primer_ingreso)
        segundo_ingreso = self._create_ingreso(estado="liberado")
        segunda = self._create_service_release_order("pv1-falta-segunda", segundo_ingreso)
        self._set_order_partida(segunda["id"])
        group_id = self._registered_pv1_group(primera["id"], number="00026329")

        with (
            patch(
                "service.bejerman_delivery._registered_manual_document_rows",
                return_value=(
                    [
                        {
                            "cliente_codigo": "CLI-SYNC",
                            "articulo": "1107006",
                            "partida": "OTRA-SERIE",
                            "deposito": "STC",
                            "cantidad": "1",
                        }
                    ],
                    {"status": "success"},
                ),
            ),
            patch("service.bejerman_delivery.BejermanSDKClient") as sdk_cls,
        ):
            with self.assertRaises(DeliveryOrderError) as ctx:
                register_delivery_order_remitos(
                    segunda["id"],
                    self.jefe.id,
                    {"remitos": [{"remitoNumber": "RSS R 00001-00026329"}]},
                )

        sdk_cls.assert_not_called()
        self.assertEqual(ctx.exception.code, "DELIVERY_REMITO_REUSE_ITEMS_MISMATCH")
        with connection.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM delivery_order_remito_links WHERE order_id = %s AND remito_group_id = %s",
                [segunda["id"], group_id],
            )
            self.assertEqual(cur.fetchone()[0], 0)

    def test_cargar_rss_cierra_una_venta_pendiente_de_entrega(self):
        ingreso_id = self._create_ingreso(estado="vendido_pendiente_entrega", alquilado=True)
        order = self._create_service_release_order("sync-rss-venta", ingreso_id)

        self._register_existing_remito(order["id"], "RSS R 00004-00009003")

        row = self._ingreso_row(ingreso_id)
        self.assertEqual(row["estado"], "vendido_entregado")
        self.assertFalse(row["alquilado"])
        self.assertIsNotNone(row["fecha_entrega"])

    def test_cargar_rss_no_reabre_una_os_ya_entregada(self):
        """Anota el numero real de Bejerman, pero no mueve el estado ni la fecha de entrega."""
        ingreso_id = self._create_ingreso(estado="entregado", remito_salida="YA-ENTREGADO")
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE ingresos SET fecha_entrega = %s WHERE id = %s",
                ["2026-01-05 10:00:00+00", ingreso_id],
            )
        order = self._create_service_release_order("sync-rss-ya-entregado", ingreso_id)

        self._register_existing_remito(order["id"], "RSS R 00004-00009004")

        row = self._ingreso_row(ingreso_id)
        self.assertEqual(row["estado"], "entregado")
        self.assertEqual(row["fecha_entrega"].date().isoformat(), "2026-01-05")
        # El comprobante real de Bejerman reemplaza al numero que se habia tipeado a mano,
        # igual que en la emision en linea.
        self.assertEqual(row["remito_salida"], "RSS R 00004-00009004")

    def test_cargar_rt_de_una_venta_no_toca_la_hoja_de_servicio(self):
        """La sincronizacion es solo de `service_release`: una venta cierra por otro lado."""
        ingreso_id = self._create_ingreso(estado="liberado")
        order = self._create_service_release_order("sync-rt-venta", ingreso_id)
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE delivery_orders SET delivery_type = 'sale' WHERE id = %s",
                [order["id"]],
            )

        self._register_existing_remito(order["id"], "RT R 00004-00009005")

        abierta = get_delivery_order(order["id"], include_events=False)
        self.assertEqual(abierta["status"], "armado_pendiente_entrega")
        row = self._ingreso_row(ingreso_id)
        self.assertEqual(row["estado"], "liberado")

    def _item_deposits(self, order_id):
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT stock_deposit_code
                  FROM delivery_order_items
                 WHERE order_id = %s
                 ORDER BY sort_order
                """,
                [order_id],
            )
            return [row[0] for row in cur.fetchall()]

    def test_el_sync_de_la_orden_no_borra_el_deposito_del_renglon(self):
        """Regresion del 02/09/2026: cada refresco anulaba `stock_deposit_code`.

        Con el deposito en NULL el modal de edicion caia en su default de venta (VAL) y
        buscaba ahi la serie del equipo reparado, que vive en STC: el operador veia
        "La partida ... no figura para este articulo en el deposito seleccionado".
        """
        ingreso_id = self._create_ingreso(estado="liberado")
        order = self._create_service_release_order("sync-deposito", ingreso_id)
        contexto = {
            "ingreso_id": ingreso_id,
            "device_id": None,
            "equipment_serial": "Y662124170133",
            "tipo_equipo": "Calentador humidificador",
            "marca": "Yamind",
            "modelo": "DM26",
        }

        with connection.cursor() as cur:
            # El renglon ya existe (lo creo la orden), asi que este upsert es el UPDATE.
            _upsert_service_release_order_item(cur, order["id"], contexto, {"article_code": "1111009"})
        self.assertEqual(self._item_deposits(order["id"]), ["STC"])

        # Y el alta de un renglon nuevo (otra OS en la misma orden) tambien nace en STC.
        otro_ingreso_id = self._create_ingreso(estado="liberado")
        with connection.cursor() as cur:
            _upsert_service_release_order_item(
                cur,
                order["id"],
                {**contexto, "ingreso_id": otro_ingreso_id, "equipment_serial": "Y662124170134"},
                {"article_code": "1111009"},
            )
        self.assertEqual(self._item_deposits(order["id"]), ["STC", "STC"])

    # ------------------------------------------------------------------ backfill
    def _correr_backfill(self, *, fase="os", apply_changes=False):
        salida = StringIO()
        call_command(
            "sync_delivered_service_release_ingresos",
            fase=fase,
            apply=apply_changes,
            stdout=salida,
            stderr=salida,
        )
        return salida.getvalue()

    def test_backfill_os_detecta_y_corrige_la_hoja_que_quedo_atras(self):
        """El caso que dejo el bug: la orden tiene el remito y la OS sigue en liberado.

        Se arma pasando por `mark_delivered` y despues volviendo la hoja a `liberado`,
        que es como quedaron las que se cargaron antes del arreglo del 02/09/2026.
        """
        ingreso_id = self._create_ingreso(estado="liberado")
        order = self._create_service_release_order("backfill-os", ingreso_id)
        self._mark_delivered(order["id"], "RSS R 00004-00004588")
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE ingresos SET estado='liberado', remito_salida=NULL, fecha_entrega=NULL WHERE id=%s",
                [ingreso_id],
            )

        salida = self._correr_backfill()
        self.assertIn(f"OS-{ingreso_id}", salida)
        self.assertIn("DRY-RUN", salida)
        # El dry-run no toca nada.
        self.assertEqual(self._ingreso_row(ingreso_id)["estado"], "liberado")

        self._correr_backfill(apply_changes=True)
        row = self._ingreso_row(ingreso_id)
        self.assertEqual(row["estado"], "entregado")
        self.assertEqual(row["remito_salida"], "RSS R 00004-00004588")
        self.assertIsNotNone(row["fecha_entrega"])

    def test_backfill_os_no_toca_una_hoja_ya_entregada(self):
        ingreso_id = self._create_ingreso(estado="liberado")
        order = self._create_service_release_order("backfill-ok", ingreso_id)
        self._mark_delivered(order["id"], "RSS R 00004-00004589")

        salida = self._correr_backfill(apply_changes=True)
        self.assertIn("Nada para sincronizar", salida)
        self.assertEqual(self._ingreso_row(ingreso_id)["remito_salida"], "RSS R 00004-00004589")


    def _sin_bejerman(self):
        """La fase no encuentra ningun RSS: aisla lo que decide NEXORA sola."""

        def _fake(sql, *, company_key=None, outcome=None, **kw):
            if outcome is not None:
                outcome.update({"status": "success"})
            return []

        return patch("service.bejerman_delivery._remote_bejerman_sql_csv", side_effect=_fake)

    def _con_bejerman(self, filas):
        def _fake(sql, *, company_key=None, outcome=None, **kw):
            if outcome is not None:
                outcome.update({"status": "success"})
            # Solo la primera empresa contesta: el resto devuelve vacio como en la realidad.
            return filas if company_key == "SEPID" else []

        return patch("service.bejerman_delivery._remote_bejerman_sql_csv", side_effect=_fake)

    def test_backfill_ordenes_no_toca_la_que_espera_el_boton_entregar(self):
        """Con el remito cargado, la orden abierta es el circuito normal, no un error."""
        ingreso_id = self._create_ingreso(estado="liberado")
        orden = self._create_service_release_order("backfill-con-remito", ingreso_id)
        self._register_existing_remito(orden["id"], "RSS R 00004-00004600")

        with self._sin_bejerman():
            salida = self._correr_backfill(fase="ordenes", apply_changes=True)

        self.assertIn("Con remito en la orden (1)", salida)
        self.assertIn("no se tocan", salida)
        self.assertEqual(get_delivery_order(orden["id"])["status"], "armado_pendiente_entrega")

    def test_backfill_ordenes_cierra_la_colgada_con_el_remito_de_la_hoja(self):
        ingreso_id = self._create_ingreso(estado="liberado")
        orden = self._create_service_release_order("backfill-hoja", ingreso_id)
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE ingresos
                   SET estado = 'entregado',
                       remito_salida = 'RSS R 00004-00004673',
                       fecha_entrega = TIMESTAMPTZ '2026-06-24 12:00:00+00'
                 WHERE id = %s
                """,
                [ingreso_id],
            )

        with self._sin_bejerman():
            salida = self._correr_backfill(fase="ordenes", apply_changes=True)

        self.assertIn("cerrar con RSS R 00004-00004673 (segun hoja de servicio)", salida)
        actualizada = get_delivery_order(orden["id"], include_events=False)
        self.assertEqual(actualizada["status"], "entregado_no_facturable")
        self.assertEqual(actualizada["remitoNumber"], "RSS R 00004-00004673")
        # La orden se entrego en junio, no hoy: sellar la fecha de hoy inventaria un dato.
        with connection.cursor() as cur:
            cur.execute("SELECT delivered_at FROM delivery_orders WHERE id = %s", [orden["id"]])
            self.assertEqual(cur.fetchone()[0].date().isoformat(), "2026-06-24")

    def test_backfill_ordenes_recupera_de_bejerman_el_numero_sin_punto_de_venta(self):
        """El operador escribio 4673 y el comprobante es 00004-00004673.

        Ese fragmento no alcanza para cerrar nada, pero sirve de control cruzado: si el RSS
        que Bejerman tiene para esa serie no termina en ese numero, no se toca la orden.
        """
        device_id = self._create_device(numero_serie="CTX16A7J153")
        ingreso_id = self._create_ingreso(
            estado="liberado", device_id=device_id, fecha_ingreso="2026-06-05"
        )
        orden = self._create_service_release_order("backfill-bejerman", ingreso_id)
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE ingresos
                   SET estado = 'entregado', remito_salida = '4673',
                       fecha_entrega = TIMESTAMPTZ '2026-06-24 12:00:00+00'
                 WHERE id = %s
                """,
                [ingreso_id],
            )
        filas = [
            {
                "letra": "R",
                "punto": "00004",
                "numero": "00004673",
                "fecha": "2026-06-24",
                "cliente_codigo": "CLI-SYNC",
                "cliente_nombre": "Cliente Sync",
                "deposito": "STC",
                "partida": "CTX16A7J153",
            }
        ]

        with self._con_bejerman(filas):
            salida = self._correr_backfill(fase="ordenes", apply_changes=True)

        self.assertIn("cerrar con RSS R 00004-00004673 (segun Bejerman)", salida)
        self.assertEqual(
            get_delivery_order(orden["id"], include_events=False)["remitoNumber"],
            "RSS R 00004-00004673",
        )

    def test_backfill_ordenes_deja_abierta_la_que_no_tiene_numero_recuperable(self):
        ingreso_id = self._create_ingreso(estado="liberado")
        orden = self._create_service_release_order("backfill-sin-numero", ingreso_id)
        with connection.cursor() as cur:
            # El campo con el numero de OS es el caso mas comun: no es un comprobante.
            cur.execute(
                "UPDATE ingresos SET estado='entregado', remito_salida=%s WHERE id=%s",
                [str(ingreso_id), ingreso_id],
            )

        with self._sin_bejerman():
            salida = self._correr_backfill(fase="ordenes", apply_changes=True)

        self.assertIn("sin numero de remito recuperable", salida)
        self.assertEqual(get_delivery_order(orden["id"])["status"], "armado_pendiente_entrega")

    def test_backfill_bejerman_cierra_la_hoja_con_la_fecha_del_comprobante(self):
        device_id = self._create_device(numero_serie="40966I0532")
        ingreso_id = self._create_ingreso(
            estado="liberado", device_id=device_id, fecha_ingreso="2026-04-30"
        )
        filas = [
            {
                "letra": "R",
                "punto": "00004",
                "numero": "00004519",
                "fecha": "2026-06-09",
                "cliente_codigo": "CLI-SYNC",
                "cliente_nombre": "Cliente Sync",
                "deposito": "STC",
                "partida": "40966I0532",
            }
        ]

        with self._con_bejerman(filas):
            salida = self._correr_backfill(fase="bejerman")
            self.assertIn("DRY-RUN", salida)
            self.assertEqual(self._ingreso_row(ingreso_id)["estado"], "liberado")

            self._correr_backfill(fase="bejerman", apply_changes=True)

        row = self._ingreso_row(ingreso_id)
        self.assertEqual(row["estado"], "entregado")
        self.assertEqual(row["remito_salida"], "RSS R 00004-00004519")
        # La entrega fue el 09/06; sellar hoy le daria 90 dias de garantia que no le tocan.
        # Se compara en la zona de la app: guardada a medianoche UTC, en hora argentina la
        # fecha se lee como el dia anterior y el backfill correria todo un dia para atras.
        self.assertEqual(
            timezone.localtime(row["fecha_entrega"]).date().isoformat(), "2026-06-09"
        )
        self.assertGreaterEqual(self._event_count(ingreso_id, "entregado"), 1)

    def test_backfill_bejerman_no_asocia_si_el_cliente_del_remito_no_es_el_del_equipo(self):
        device_id = self._create_device(numero_serie="40966I0533")
        ingreso_id = self._create_ingreso(
            estado="liberado", device_id=device_id, fecha_ingreso="2026-04-30"
        )
        filas = [
            {
                "letra": "R",
                "punto": "00004",
                "numero": "00004520",
                "fecha": "2026-06-09",
                "cliente_codigo": "OTRO",
                "cliente_nombre": "Otro Cliente",
                "deposito": "STC",
                "partida": "40966I0533",
            }
        ]

        with self._con_bejerman(filas):
            salida = self._correr_backfill(fase="bejerman", apply_changes=True)

        self.assertIn("el cliente del remito no coincide", salida)
        self.assertEqual(self._ingreso_row(ingreso_id)["estado"], "liberado")

    def test_backfill_bejerman_no_asocia_si_hay_mas_de_un_candidato(self):
        device_id = self._create_device(numero_serie="PRUEBA123")
        ingreso_id = self._create_ingreso(
            estado="liberado", device_id=device_id, fecha_ingreso="2026-06-16"
        )
        filas = [
            {
                "letra": "R",
                "punto": "00004",
                "numero": numero,
                "fecha": "2026-06-16",
                "cliente_codigo": "CLI-SYNC",
                "cliente_nombre": "Cliente Sync",
                "deposito": "STC",
                "partida": "PRUEBA123",
            }
            for numero in ("00004582", "00004588")
        ]

        with self._con_bejerman(filas):
            salida = self._correr_backfill(fase="bejerman", apply_changes=True)

        self.assertIn("varios candidatos", salida)
        self.assertEqual(self._ingreso_row(ingreso_id)["estado"], "liberado")
